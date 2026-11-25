"""tau2 participants backed by a persistent Claude Code process.

ClaudeCodeAgent is a tau2 HalfDuplexAgent; ClaudeCodeUser subclasses tau2's UserSimulator
(its system prompt, persona and stop tokens are tau2's). Each owns one `claude -p` process in
stream-json mode. A tau2 turn becomes: send the incoming text as a user turn, then wait for
either a tool call from the relay (returned to the Orchestrator as a one-call message, which
it executes on the Environment) or the end of the CLI turn (returned as the text message)."""

import itertools
import json
import queue
import time
from dataclasses import dataclass, field
from pathlib import Path

from tau2.agent.base_agent import HalfDuplexAgent
from tau2.data_model.message import (AssistantMessage, MultiToolMessage, ToolCall, ToolMessage,
                                     UserMessage)
from tau2.user.user_simulator import UserSimulator

from .claude_cli import (CLIError, ClaudeConfig, ClaudeSession, InvocationBudget, RateLimited,
                         window_reset,
                         check_result, prompt_args)
from .relay import Relay


@dataclass
class EngineSpec:
    role: str                         # "agent" | "user"
    cfg: ClaudeConfig
    workdir: Path                     # empty directory the process runs in
    log_dir: Path
    budget: InvocationBudget
    label: str
    relay: Relay | None = None
    relay_key: str = ""
    tau2_tools: list = field(default_factory=list)
    obs_server: dict | None = None    # --mcp-config entry for `obs serve`
    obs_tools: tuple = ()             # OBS tools offered to the model
    obs_disallowed: tuple = ()
    system_prompt: str | None = None  # replaces Claude Code's prompt (user simulator)
    append_prompt: str | None = None  # appended to Claude Code's prompt (agent)
    env: dict = field(default_factory=dict)


class Engine:
    def __init__(self, spec: EngineSpec):
        self.spec = spec
        self.events: queue.Queue = queue.Queue()
        self.pending: dict[str, object] = {}
        self._ids = itertools.count(1)
        self.session: ClaudeSession | None = None
        self.turn_texts: list[str] = []
        self.tool_uses: list[dict] = []   # every tool_use block the model emitted (tau2 + OBS)
        self.turns = 0
        self.empty_replies = 0
        self.relay_url = None
        if spec.relay is not None and spec.tau2_tools:
            self.relay_url = spec.relay.register(spec.relay_key, spec.tau2_tools, self.events)

    def _start(self) -> None:
        s = self.spec
        servers = {}
        allowed = []
        if self.relay_url:
            servers["tau2"] = {"type": "http", "url": self.relay_url}
            # explicit names: headless runs must never stop at a permission prompt
            allowed += [f"mcp__tau2__{t.name}" for t in s.tau2_tools]
        if s.obs_server is not None:
            servers["obs"] = s.obs_server
            allowed += [f"mcp__obs__{t}" for t in s.obs_tools]
        args = s.cfg.common_args(streaming=True)
        if servers:
            path = s.log_dir / f"{s.role}.mcp.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"mcpServers": servers}, indent=1), encoding="utf-8")
            args += ["--mcp-config", str(path)]
        if allowed:
            args += ["--allowedTools", ",".join(allowed)]
        if s.obs_disallowed:
            args += ["--disallowedTools", ",".join(f"mcp__obs__{t}" for t in s.obs_disallowed)]
        if s.system_prompt is not None:
            args += prompt_args("--system-prompt", s.system_prompt, s.cfg.prompt_transport,
                                s.log_dir / f"{s.role}.system_prompt.md")
        if s.append_prompt is not None:
            args += prompt_args("--append-system-prompt", s.append_prompt, s.cfg.prompt_transport,
                                s.log_dir / f"{s.role}.append_prompt.md")
        self.session = ClaudeSession(s.cfg, args, s.workdir, self.events, s.log_dir, s.role,
                                     s.budget, s.label, env=s.env)

    # ---- one tau2 turn

    def send(self, text: str):
        if self.session is None:
            self._start()
        self.turns += 1
        self.session.send_user(text)
        return self._wait()

    def deliver(self, msgs: list[ToolMessage]):
        for m in msgs:
            fut = self.pending.pop(m.id, None)
            if fut is not None and not fut.done():
                fut.set_result((m.content or "", bool(m.error)))
        return self._wait()

    def _wait(self):
        deadline = time.monotonic() + self.spec.cfg.turn_timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise CLIError(f"{self.spec.role}: no response within {self.spec.cfg.turn_timeout}s")
            try:
                kind, payload = self.events.get(timeout=left)
            except queue.Empty:
                continue
            if kind == "call":
                name, arguments, fut = payload
                call_id = f"{self.spec.role[0]}{next(self._ids)}"
                self.pending[call_id] = fut
                return "call", ToolCall(id=call_id, name=name, arguments=arguments,
                                        requestor="assistant" if self.spec.role == "agent" else "user")
            if kind == "exit":
                tail = self.session.stderr_tail() if self.session else ""
                raise CLIError(f"claude ({self.spec.role}) exited with {payload}: {tail}")
            ev = payload
            if ev.get("type") == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text", "").strip():
                        self.turn_texts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        # the reply to the customer is the text after the last tool call
                        self.turn_texts = []
                        self.tool_uses.append({"turn": self.turns, "name": block.get("name"),
                                               "input": block.get("input")})
            elif ev.get("type") == "result":
                try:
                    check_result(ev)
                except RateLimited as e:
                    if not e.reset_at:
                        e.reset_at = window_reset(self.session.usage.rate_limit_info)
                    raise
                text = "\n\n".join(self.turn_texts).strip() or (ev.get("result") or "").strip()
                self.turn_texts = []
                if not text:
                    self.empty_replies += 1
                    text = "..."   # tau2 rejects an empty message; logged as empty_replies
                return "final", text

    def close(self) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_result(("Error: the conversation has ended.", True))
        self.pending.clear()
        if self.spec.relay is not None and self.relay_url:
            self.spec.relay.unregister(self.spec.relay_key)
        if self.session is not None:
            self.session.close()

    def report(self) -> dict:
        s = self.session
        return {
            "started": s is not None,
            "turns": self.turns,
            "empty_replies": self.empty_replies,
            "tool_uses": self.tool_uses,
            "usage": s.usage.summary() if s else None,
            "init": _init_digest(s.init_event) if s and s.init_event else None,
            "wall_s": round(time.time() - s.started, 1) if s else 0.0,
        }


def _init_digest(ev: dict) -> dict:
    """What the CLI says it loaded: the isolation check reads this."""
    keep = ("model", "tools", "mcp_servers", "permissionMode", "slash_commands", "agents", "skills",
            "plugins", "claude_code_version", "output_style", "apiKeySource", "cwd")
    return {k: ev.get(k) for k in keep if k in ev}


def _tool_messages(message) -> list[ToolMessage]:
    if isinstance(message, MultiToolMessage):
        return list(message.tool_messages)
    return [message]


@dataclass
class _State:
    messages: list = field(default_factory=list)


class ClaudeCodeAgent(HalfDuplexAgent):
    def __init__(self, tools, domain_policy: str, spec: EngineSpec):
        super().__init__(tools=tools, domain_policy=domain_policy)
        self.engine = Engine(spec)

    def get_init_state(self, message_history=None):
        return _State(messages=list(message_history or []))

    def set_seed(self, seed: int):
        self.seed = seed   # Claude Code exposes no sampling seed

    def generate_next_message(self, message, state):
        if isinstance(message, UserMessage):
            kind, out = self.engine.send(message.content or "")
        else:
            kind, out = self.engine.deliver(_tool_messages(message))
        if kind == "call":
            msg = AssistantMessage(role="assistant", content=None, tool_calls=[out])
        else:
            msg = AssistantMessage(role="assistant", content=out)
        state.messages.append(msg)
        return msg, state

    def stop(self, message=None, state=None):
        self.engine.close()


class ClaudeCodeUser(UserSimulator):
    """tau2's user simulator, with Claude Code as the model. Its system prompt (guidelines,
    persona, scenario) comes from tau2; the CLI gets it via --system-prompt."""

    def __init__(self, instructions: str, tools, spec: EngineSpec, label: str):
        super().__init__(llm=label, instructions=instructions, tools=tools or None, llm_args={})
        from .prompts import USER_SIM_NOTE
        spec.system_prompt = self.system_prompt + USER_SIM_NOTE
        spec.tau2_tools = list(tools or [])
        self.engine = Engine(spec)

    def set_seed(self, seed: int):
        self.seed = seed

    def generate_next_message(self, message, state):
        if isinstance(message, AssistantMessage):
            kind, out = self.engine.send(message.content or "")
        else:
            kind, out = self.engine.deliver(_tool_messages(message))
        if kind == "call":
            msg = UserMessage(role="user", content=None, tool_calls=[out])
        else:
            msg = UserMessage(role="user", content=out)
        state.messages.append(msg)
        return msg, state

    def stop(self, message=None, state=None):
        self.engine.close()
