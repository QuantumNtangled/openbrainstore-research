"""One tau2 episode: one task, one trial, one arm. Builds the Environment, the Claude Code
agent and user simulator, runs tau2's own Orchestrator and grades with tau2's evaluator."""

import json
import re
import time
import traceback
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from tau2.data_model.simulation import TerminationReason
from tau2.data_model.tasks import RewardType
from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation
from tau2.orchestrator.orchestrator import Orchestrator
from tau2.runner.build import build_environment

from .claude_cli import BudgetExceeded, ClaudeConfig, InvocationBudget
from .common import OBS_ALL_TOOLS, safe_name, write_json
from .obs import obs_http_server, obs_mcp_server
from .participants import ClaudeCodeAgent, ClaudeCodeUser, EngineSpec
from .prompts import agent_append_prompt
from .relay import get_relay

MAX_STEPS = 200      # tau2 DEFAULT_MAX_STEPS
MAX_ERRORS = 10      # tau2 DEFAULT_MAX_ERRORS


@dataclass
class ArmSetup:
    """How the agent is configured. Arms A-D (and the learning loop's train agent) are
    instances of this."""
    name: str
    obs_dir: Path | None = None        # OBS data dir; None = no OBS tools
    obs_tools: tuple = ("agent_startup", "get_skill")
    skills_md: str | None = None       # arm D: promoted skill text in the prompt
    agent_model: str | None = None     # this arm's agent model, when it differs from --agent-model
    obs_url: str | None = None         # hosted OBS instead of a local data dir
    obs_key_env: str = "OBS_AGENT_KEY"  # env var holding the hosted agent key

    @property
    def obs(self) -> bool:
        return self.obs_dir is not None or self.obs_url is not None


@dataclass
class EpisodeContext:
    domain: str
    task: object                       # tau2 Task
    trial: int
    arm: ArmSetup
    agent_cfg: ClaudeConfig
    user_cfg: ClaudeConfig
    out_path: Path
    log_dir: Path
    workdir: Path
    budget: InvocationBudget
    seed: int = 0
    env_hook: object = None            # mock mode: (role, ctx) -> extra env for the claude process
    extra: dict = field(default_factory=dict)


def scoring_task(task):
    """tau2 v1.0.1 puts NL_ASSERTION in the reward basis of 112/114 retail tasks; 40 of those
    have assertions, which tau2 grades with an OpenAI judge (gpt-4.1). This study grades
    without a judge model: the NL component is dropped and the rest of the basis (DB state,
    env assertions, actions, communicate substrings) is scored exactly as tau2 does."""
    t = deepcopy(task)
    ec = t.evaluation_criteria
    if ec is not None and ec.reward_basis and RewardType.NL_ASSERTION in ec.reward_basis:
        ec.reward_basis = [b for b in ec.reward_basis if b != RewardType.NL_ASSERTION] or [RewardType.DB]
    return t


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def foreign_emails(sim_json: dict, task) -> list[str]:
    """Email addresses the customer or agent wrote that appear in neither the task data nor any
    tool result: the signature of Claude Code's account context leaking into the conversation.
    Returned masked (the operator's address should not spread further)."""
    known = json.dumps(task.model_dump(mode="json")).lower()
    said = []
    for m in sim_json.get("messages") or []:
        if m.get("role") == "tool":
            known += " " + (m.get("content") or "").lower()
        elif m.get("content"):
            said += _EMAIL.findall(m["content"])
        for tc in m.get("tool_calls") or []:
            said += _EMAIL.findall(json.dumps(tc.get("arguments") or {}))
    out = sorted({e for e in said if e.lower() not in known})
    return [e[0] + "***@" + e.split("@", 1)[1] for e in out]


def run_episode(ctx: EpisodeContext) -> dict:
    t0 = time.time()
    key = safe_name(f"{uuid.uuid4().hex[:10]}", 12)
    env = build_environment(ctx.domain)
    task = ctx.task
    relay = get_relay()
    try:
        user_tools = env.get_user_tools(include=task.user_tools) or []
    except Exception:
        user_tools = []

    arm = ctx.arm
    hook = ctx.env_hook or (lambda role, c: {})
    agent_spec = EngineSpec(
        role="agent", cfg=ctx.agent_cfg, workdir=ctx.workdir / "agent", log_dir=ctx.log_dir,
        budget=ctx.budget, label=f"{arm.name}/{task.id}/t{ctx.trial}", relay=relay,
        relay_key=f"{key}-a", tau2_tools=env.get_tools(),
        obs_server=(obs_http_server(arm.obs_url, arm.obs_key_env) if arm.obs_url
                    else obs_mcp_server(arm.obs_dir) if arm.obs else None),
        obs_tools=tuple(arm.obs_tools) if arm.obs else (),
        obs_disallowed=tuple(t for t in OBS_ALL_TOOLS if t not in arm.obs_tools) if arm.obs else (),
        append_prompt=agent_append_prompt(env.get_policy(), obs=arm.obs,
                                          recall="recall" in arm.obs_tools, skills_md=arm.skills_md),
        env=hook("agent", ctx))
    user_spec = EngineSpec(
        role="user", cfg=ctx.user_cfg, workdir=ctx.workdir / "user", log_dir=ctx.log_dir,
        budget=ctx.budget, label=f"{arm.name}/{task.id}/t{ctx.trial}", relay=relay,
        relay_key=f"{key}-u", env=hook("user", ctx))
    agent = ClaudeCodeAgent(tools=env.get_tools(), domain_policy=env.get_policy(), spec=agent_spec)
    user = ClaudeCodeUser(instructions=str(task.user_scenario), tools=user_tools, spec=user_spec,
                          label=f"claude-code:{ctx.user_cfg.model}")
    orch = Orchestrator(domain=ctx.domain, agent=agent, user=user, environment=env, task=task,
                        max_steps=MAX_STEPS, max_errors=MAX_ERRORS, seed=ctx.seed,
                        simulation_id=key, validate_communication=False)
    result = {"domain": ctx.domain, "task_id": task.id, "trial": ctx.trial, "arm": arm.name,
              "status": "ok", "started": t0, **ctx.extra}
    sim = None
    try:
        try:
            sim = orch.run()
        except BudgetExceeded as e:
            # the per-process cost cap stopped a runaway agent: graded as a failure
            result.update(status="budget_exceeded", error=str(e))
        if sim is not None:
            graded = evaluate_simulation(simulation=sim, task=scoring_task(task),
                                         evaluation_type=EvaluationType.ALL, solo_mode=False,
                                         domain=ctx.domain)
            sim.reward_info = graded
            result.update(
                reward=float(graded.reward),
                success=graded.reward >= 1.0 - 1e-6,
                termination=sim.termination_reason.value if hasattr(sim.termination_reason, "value")
                else str(sim.termination_reason),
                reward_breakdown={str(k.value if hasattr(k, "value") else k): v
                                  for k, v in (graded.reward_breakdown or {}).items()},
                graded_basis=[str(b.value) for b in scoring_task(task).evaluation_criteria.reward_basis]
                if task.evaluation_criteria else None,
                simulation=sim.model_dump(mode="json"),
            )
            result["foreign_emails"] = foreign_emails(result["simulation"], task)
        else:
            result.update(reward=0.0, success=False, termination="budget_exceeded")
    finally:
        try:
            orch._cleanup()
        except Exception:
            pass
    result["agent"] = agent.engine.report()
    result["user"] = user.engine.report()
    obs_calls = [u for u in result["agent"]["tool_uses"] if str(u.get("name", "")).startswith("mcp__obs__")]
    result["obs"] = {
        "startup_calls": sum(1 for u in obs_calls if u["name"].endswith("agent_startup")),
        "get_skill_calls": sum(1 for u in obs_calls if u["name"].endswith("get_skill")),
        "refs_fetched": sorted({r for u in obs_calls if u["name"].endswith("get_skill")
                                for r in ((u.get("input") or {}).get("refs") or [])}),
    }
    result["wall_s"] = round(time.time() - t0, 1)
    write_json(ctx.out_path, result)
    return result


def infra_failure(ctx: EpisodeContext, err: BaseException, attempt: int) -> dict:
    return {"domain": ctx.domain, "task_id": ctx.task.id, "trial": ctx.trial, "arm": ctx.arm.name,
            "status": "infra_error", "attempt": attempt, "error": f"{type(err).__name__}: {err}",
            "traceback": traceback.format_exc()[-4000:], **ctx.extra}


def is_terminal_ok(res: dict | None) -> bool:
    return bool(res) and res.get("status") in ("ok", "budget_exceeded")


def termination_is_infra(res: dict) -> bool:
    return res.get("termination") in (TerminationReason.INFRASTRUCTURE_ERROR.value,)
