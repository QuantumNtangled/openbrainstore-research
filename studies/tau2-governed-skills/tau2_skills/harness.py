"""Shared run setup: claude configs (real or fake), the invocation cap, the pacer, and the
per-episode context factory used by both the learning loop and the evaluation."""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .claude_cli import ClaudeConfig, InvocationBudget, find_claude
from .common import DEFAULT_MODEL, FAKE_CLAUDE, RUNS_DIR, assert_clean_workdir, default_workdir, safe_name, write_json
from .episode import ArmSetup, EpisodeContext
from .runner import BatchSpec, Pacer, run_batch


def add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--run", required=True, help="run name (short: Windows MAX_PATH)")
    p.add_argument("--domain", default="retail", choices=["retail", "airline", "telecom"])
    p.add_argument("--agent-model", default=DEFAULT_MODEL)
    p.add_argument("--agent-effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--user-model", default=DEFAULT_MODEL)
    p.add_argument("--user-effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--concurrency", type=int, default=2, help="episodes in flight (2 claude processes each)")
    p.add_argument("--max-invocations", type=int, default=None,
                   help="hard cap on claude processes started by this run (across restarts)")
    p.add_argument("--max-budget-usd", type=float, default=1.0,
                   help="per-process API-equivalent cap passed to claude (a runaway-loop guard)")
    p.add_argument("--turn-timeout", type=float, default=900.0)
    p.add_argument("--backoff-base", type=float, default=60.0, help="first pause after a rate limit (s)")
    p.add_argument("--backoff-cap", type=float, default=3600.0)
    p.add_argument("--max-pause", type=float, default=3 * 3600.0,
                   help="stop (resumable) instead of sleeping longer than this after a rate limit (s)")
    p.add_argument("--stop-at-utilization", type=float, default=0.9,
                   help="stop starting episodes once the plan's reported window utilization reaches "
                        "this (0-1); finished episodes are kept, rerun to resume. Negative disables.")
    p.add_argument("--max-attempts", type=int, default=3, help="attempts per episode on infra errors")
    p.add_argument("--prompt-transport", default="auto", choices=["auto", "file", "inline"])
    p.add_argument("--setting-sources", default="project",
                   help="passed to --setting-sources; 'project' skips the user's own settings")
    p.add_argument("--max-run-usd", type=float, default=None,
                   help="stop (resumable) once this run's API-equivalent spend, counted across "
                        "restarts from the episode files, reaches this")
    p.add_argument("--allow-api-key", action="store_true",
                   help="keep going when claude reports it is billing an API key, not the subscription")
    p.add_argument("--fake-claude", action="store_true",
                   help="use the scripted fake CLI (tests/fake_claude.py): offline, no model calls")
    p.add_argument("--runs-dir", default=str(RUNS_DIR))


@dataclass
class Harness:
    args: argparse.Namespace
    run_dir: Path
    agent_cfg: ClaudeConfig
    user_cfg: ClaudeConfig
    improver_cfg: ClaudeConfig
    budget: InvocationBudget
    pacer: Pacer
    workdir: Path

    def make_ctx_factory(self, arm: ArmSetup, extra: dict | None = None, seed_base: int = 0):
        def make(task, trial, out_path, log_dir):
            return EpisodeContext(
                domain=self.args.domain, task=task, trial=trial, arm=arm, agent_cfg=self.agent_cfg,
                user_cfg=self.user_cfg, out_path=out_path, log_dir=log_dir,
                workdir=self.workdir / safe_name(f"{out_path.parent.name}-{out_path.stem}", 48),
                budget=self.budget, seed=seed_base + trial, extra=dict(extra or {}),
                env_hook=fake_env_hook if self.args.fake_claude else None)
        return make

    def batch(self, tasks, trials: int, arm: ArmSetup, out_dir: Path, extra: dict | None = None):
        return run_batch(BatchSpec(domain=self.args.domain, tasks=tasks, trials=trials, arm=arm,
                                   out_dir=out_dir, make_ctx=self.make_ctx_factory(arm, extra),
                                   concurrency=self.args.concurrency, max_attempts=self.args.max_attempts,
                                   pacer=self.pacer))


def build_harness(args: argparse.Namespace, improver_model: str | None = None,
                  improver_effort: str = "medium") -> Harness:
    run_dir = Path(args.runs_dir) / args.run
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(FAKE_CLAUDE)] if args.fake_claude else find_claude()

    def cfg(model, effort):
        return ClaudeConfig(cmd=cmd, model=model, effort=effort, setting_sources=args.setting_sources,
                            turn_timeout=args.turn_timeout, max_budget_usd=args.max_budget_usd,
                            prompt_transport=args.prompt_transport)

    workdir = default_workdir() / safe_name(args.run, 24)
    workdir.mkdir(parents=True, exist_ok=True)
    assert_clean_workdir(workdir)
    h = Harness(args=args, run_dir=run_dir, agent_cfg=cfg(args.agent_model, args.agent_effort),
                user_cfg=cfg(args.user_model, args.user_effort),
                improver_cfg=cfg(improver_model or args.agent_model, improver_effort),
                budget=InvocationBudget(args.max_invocations, run_dir / "claude_invocations.jsonl"),
                pacer=Pacer(args.backoff_base, args.backoff_cap, run_dir / "pacing.jsonl",
                            max_pause=args.max_pause,
                            stop_at_utilization=args.stop_at_utilization if args.stop_at_utilization >= 0 else None),
                workdir=workdir)
    h.pacer.require_subscription = not args.allow_api_key
    h.pacer.max_run_usd = args.max_run_usd
    h.pacer.spent_usd = prior_spend(run_dir)
    write_json(run_dir / f"config.{args.command}.json", {
        "argv": sys.argv, "args": vars(args), "claude_cmd": cmd,
        "agent": vars(h.agent_cfg), "user": vars(h.user_cfg), "improver": vars(h.improver_cfg)})
    return h


def prior_spend(run_dir: Path) -> float:
    """API-equivalent spend already recorded in this run's episode checkpoints and improver
    outputs, so --max-run-usd holds across restarts."""
    total = 0.0
    for p in run_dir.rglob("*.json"):
        if p.name.startswith((".tmp-", "config.", "state", "summary")) or p.parent.name == "obs":
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        if "agent" in d and "task_id" in d:
            total += sum(float((((d.get(r) or {}).get("usage") or {}).get("cost_usd_est")) or 0.0)
                         for r in ("agent", "user"))
        elif p.name == "improver_output.json":
            total += float(d.get("total_cost_usd") or 0.0)
    return total


# ---- mock mode: the fake CLI gets each episode's gold actions as its "policy"

def fake_env_hook(role: str, ctx: EpisodeContext) -> dict:
    from tau2.environment.toolkit import ToolType, get_tool_types
    from tau2.runner.build import build_environment
    env = {"FAKE_CLAUDE_ROLE": role}
    if role == "agent":
        task = ctx.task
        actions = [a for a in (task.evaluation_criteria.actions or []) if a.requestor == "assistant"]
        types = get_tool_types(build_environment(ctx.domain).tools)
        writes = [i for i, a in enumerate(actions) if types.get(a.name) == ToolType.WRITE]
        script = {"actions": [{"name": a.name, "arguments": a.arguments} for a in actions],
                  "drop_index": writes[-1] if writes else None,
                  "communicate": list(task.evaluation_criteria.communicate_info or [])}
        path = ctx.log_dir / "fake_script.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(script), encoding="utf-8")
        env["FAKE_CLAUDE_SCRIPT"] = str(path)
    return env
