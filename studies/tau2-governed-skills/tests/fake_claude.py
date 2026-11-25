"""A scripted stand-in for the `claude` CLI, for offline tests. It speaks the same
stream-json protocol the harness uses (init / assistant / user / result events), connects to
the real MCP servers named in --mcp-config (the tau2 relay over HTTP, `obs serve` over stdio)
and really calls their tools. No model is involved: the "policy" is a script.

Roles (FAKE_CLAUDE_ROLE):
  agent     first turn: agent_startup + get_skill when OBS tools are offered, then the task's
            gold actions from FAKE_CLAUDE_SCRIPT. Without the mock skill (MARKER, from the
            prompt or from get_skill) it skips the last write action, so the task fails.
  user      first turn asks for help; any later turn ends the conversation (###STOP###).
  improver  prints one clean proposal (containing MARKER) and one that leaks a user id.

FAKE_CLAUDE_RATE_LIMIT_ONCE=<path>: the first agent process that sees no file at <path>
creates it and fails its first turn with a rate-limit error."""

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

MARKER = "write-through rule"


def parse_args(argv):
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("-p", "--print", action="store_true")
    p.add_argument("--model", default="claude-haiku-5-5")
    p.add_argument("--input-format", default="text")
    p.add_argument("--output-format", default="text")
    p.add_argument("--mcp-config", nargs="*", default=[])
    p.add_argument("--allowedTools", "--allowed-tools", default="")
    p.add_argument("--disallowedTools", "--disallowed-tools", default="")
    p.add_argument("--tools", nargs="*", default=None)
    p.add_argument("--system-prompt", default=None)
    p.add_argument("--system-prompt-file", default=None)
    p.add_argument("--append-system-prompt", default=None)
    p.add_argument("--append-system-prompt-file", default=None)
    p.add_argument("--permission-mode", default="default")
    args, _ = p.parse_known_args(argv)
    return args


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def split_list(s: str) -> list[str]:
    return [x for x in re.split(r"[,\s]+", s or "") if x]


class Fake:
    def __init__(self, args):
        self.args = args
        self.role = os.environ.get("FAKE_CLAUDE_ROLE", "agent")
        self.sid = str(uuid.uuid4())
        self.sessions = {}
        self.tools: dict[str, tuple[str, str]] = {}   # full name -> (server, tool)
        self.calls = 0
        self.turn = 0
        self.cost = 0.0
        self.model_usage = {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0,
                            "cacheCreationInputTokens": 0, "cacheCreationEphemeral1hInputTokens": 0,
                            "cacheCreationEphemeral5mInputTokens": 0, "thinkingTokens": 0, "costUSD": 0.0}
        prompt = ""
        for flag in ("system_prompt", "append_system_prompt"):
            if getattr(args, flag):
                prompt += getattr(args, flag)
            f = getattr(args, flag + "_file")
            if f:
                prompt += Path(f).read_text(encoding="utf-8")
        self.prompt = prompt

    def usage(self) -> dict:
        """Per-request usage as the real CLI reports it (1-hour cache writes), accumulated into
        the session's modelUsage, which result events carry (verified format, CLI 2.1.316)."""
        first = self.calls == 0
        self.calls += 1
        u = {"input_tokens": 2, "cache_creation_input_tokens": 3000 if first else 120,
             "cache_read_input_tokens": 0 if first else 3000 + 120 * self.calls, "output_tokens": 40}
        mu = self.model_usage
        mu["inputTokens"] += u["input_tokens"]
        mu["cacheCreationInputTokens"] += u["cache_creation_input_tokens"]
        mu["cacheCreationEphemeral1hInputTokens"] += u["cache_creation_input_tokens"]
        mu["cacheReadInputTokens"] += u["cache_read_input_tokens"]
        mu["outputTokens"] += u["output_tokens"]
        self.cost += (u["input_tokens"] * 0.1 + u["cache_creation_input_tokens"] * 0.2
                      + u["cache_read_input_tokens"] * 0.01 + u["output_tokens"] * 0.5) / 1e6
        mu["costUSD"] = self.cost
        return u

    def assistant(self, content: list) -> None:
        emit({"type": "assistant", "session_id": self.sid, "parent_tool_use_id": None,
              "message": {"id": f"msg_{uuid.uuid4().hex[:20]}", "type": "message", "role": "assistant",
                          "model": self.args.model, "content": content, "usage": self.usage()}})

    async def connect(self, stack: contextlib.AsyncExitStack) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamablehttp_client
        servers = {}
        for cfg in self.args.mcp_config:
            text = Path(cfg).read_text(encoding="utf-8") if Path(cfg).exists() else cfg
            servers.update(json.loads(text).get("mcpServers", {}))
        denied = split_list(self.args.disallowedTools)
        for name, s in servers.items():
            if s.get("type") == "http":
                read, write, _ = await stack.enter_async_context(streamablehttp_client(s["url"]))
            else:
                env = dict(os.environ)
                env.update(s.get("env") or {})
                read, write = await stack.enter_async_context(stdio_client(StdioServerParameters(
                    command=s["command"], args=s.get("args", []), env=env)))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.sessions[name] = session
            for t in (await session.list_tools()).tools:
                full = f"mcp__{name}__{t.name}"
                if full in denied or f"mcp__{name}" in denied:
                    continue
                self.tools[full] = (name, t.name)
        emit({"type": "system", "subtype": "init", "session_id": self.sid, "model": self.args.model,
              "tools": sorted(self.tools), "permissionMode": self.args.permission_mode,
              "mcp_servers": [{"name": n, "status": "connected"} for n in self.sessions],
              "slash_commands": [], "agents": [], "skills": [], "plugins": [],
              "claude_code_version": "fake", "cwd": os.getcwd()})

    async def call(self, full: str, arguments: dict) -> str:
        server, tool = self.tools[full]
        tid = f"toolu_{uuid.uuid4().hex[:20]}"
        self.assistant([{"type": "tool_use", "id": tid, "name": full, "input": arguments}])
        res = await self.sessions[server].call_tool(tool, arguments)
        text = "\n".join(getattr(c, "text", "") for c in res.content)
        emit({"type": "user", "session_id": self.sid, "parent_tool_use_id": None,
              "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                       "content": text, "is_error": bool(res.isError)}]}})
        return text

    def finish(self, text: str) -> None:
        self.assistant([{"type": "text", "text": text}])
        emit({"type": "rate_limit_event", "session_id": self.sid,
              "rate_limit_info": {"status": "allowed", "resetsAt": int(time.time()) + 86400,
                                  "rateLimitType": "seven_day",
                                  "unifiedWindows": {"seven_day": {"utilization": 0.01, "status": "allowed",
                                                                   "resetsAt": int(time.time()) + 86400}}}})
        emit({"type": "result", "subtype": "success", "is_error": False, "result": text,
              "session_id": self.sid, "num_turns": self.turn, "total_cost_usd": round(self.cost, 8),
              "usage": {"input_tokens": 2, "output_tokens": 40},
              "modelUsage": {self.args.model: dict(self.model_usage)}, "duration_ms": 5})

    async def agent_turn(self, text: str) -> None:
        if self.turn > 1:
            return self.finish("You're welcome. Is there anything else I can help you with?")
        flag = os.environ.get("FAKE_CLAUDE_RATE_LIMIT_ONCE")
        if flag and not Path(flag).exists():
            Path(flag).write_text("tripped")
            emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "result": "API Error: 429 rate_limit_error (fake)", "session_id": self.sid})
            raise SystemExit(1)
        skills = ""
        if "mcp__obs__agent_startup" in self.tools:
            out = await self.call("mcp__obs__agent_startup", {"task": text[:200], "harness": "claude-code"})
            refs = re.findall(r"`((?:skill|procedure|guardrail|checklist|script):[a-z0-9-]+@v\d+)`", out)
            ses = re.search(r"ses_[0-9A-Z]+", out)
            if refs and "mcp__obs__get_skill" in self.tools:
                skills = await self.call("mcp__obs__get_skill",
                                         {"refs": refs, "session_id": ses.group(0) if ses else ""})
        script = json.loads(Path(os.environ["FAKE_CLAUDE_SCRIPT"]).read_text(encoding="utf-8"))
        has_skill = MARKER in self.prompt or MARKER in skills
        for i, a in enumerate(script["actions"]):
            if i == script.get("drop_index") and not has_skill:
                continue
            await self.call(f"mcp__tau2__{a['name']}", a["arguments"])
        self.finish(" ".join(["All done, everything you asked for is taken care of."]
                             + list(script.get("communicate") or [])))

    async def user_turn(self, text: str) -> None:
        if self.turn == 1:
            return self.finish("Hi, I need some help with a recent order, please.")
        self.finish("Thank you, that's everything. ###STOP###")

    async def stream(self) -> None:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def reader():
            for line in sys.stdin.buffer:
                loop.call_soon_threadsafe(q.put_nowait, line.decode("utf-8"))
            loop.call_soon_threadsafe(q.put_nowait, None)

        threading.Thread(target=reader, daemon=True).start()
        async with contextlib.AsyncExitStack() as stack:
            await self.connect(stack)
            while True:
                line = await q.get()
                if line is None:
                    break
                if not line.strip():
                    continue
                msg = json.loads(line)
                content = msg["message"]["content"]
                text = content if isinstance(content, str) else " ".join(
                    b.get("text", "") for b in content if b.get("type") == "text")
                self.turn += 1
                if self.role == "user":
                    await self.user_turn(text)
                else:
                    await self.agent_turn(text)

    def improver(self) -> None:
        prompt = sys.stdin.buffer.read().decode("utf-8")
        epis = sorted(set(re.findall(r"epi_[0-9A-Z]{26}", prompt)))
        uids = re.findall(r"\b[a-z]+_[a-z]+_\d{4}\b", prompt)
        proposals = [
            {"slug": "finish-confirmed-writes", "kind": "procedure",
             "title": "Finish every write the customer confirmed",
             "when_to_use": "Whenever a customer has confirmed an exchange, return, modification or cancellation.",
             "body": (f"Apply the {MARKER}: once the customer confirms an action, call the write "
                      "tool before replying, then tell the customer it is done. Never end the turn "
                      "with a confirmed action still pending."),
             "hypothesis": "Failed episodes ended without the final write tool call.",
             "evidence": epis[:2]},
            {"slug": "lookup-example", "kind": "skill", "title": "Look up the customer",
             "when_to_use": "When identifying a customer.",
             "body": f"For example, look up {uids[0] if uids else 'mei_kovacs_8020'} by name and zip first.",
             "hypothesis": "Agents look customers up inconsistently.", "evidence": epis[:1]},
        ]
        text = "```json\n" + json.dumps({"proposals": proposals}, indent=1) + "\n```"
        self.turn = 1
        emit({"type": "result", "subtype": "success", "is_error": False, "result": text,
              "session_id": self.sid, "num_turns": 1, "total_cost_usd": 0.0012,
              "usage": {"input_tokens": len(prompt) // 4, "output_tokens": 600}, "duration_ms": 5})


def reviewer(fake: "Fake") -> None:
    """Model reviewer: edits every proposal (keeping MARKER, so the agent still passes), adding
    the string in FAKE_REVIEW_LEAK when set, to exercise the leakage screen on edits."""
    prompt = sys.stdin.buffer.read().decode("utf-8")
    leak = os.environ.get("FAKE_REVIEW_LEAK", "")
    decisions = {slug: {"decision": "edit", "note": "fake reviewer tightened the wording",
                        "title": "Reviewed: finish confirmed writes",
                        "when_to_use": "Whenever a customer has confirmed a write action.",
                        "body": f"Apply the {MARKER}: call the write tool before replying. {leak}".strip()}
                 for slug in re.findall(r"^### Proposal: (\S+)", prompt, re.M)}
    text = "```json\n" + json.dumps({"decisions": decisions}, indent=1) + "\n```"
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text,
          "session_id": fake.sid, "num_turns": 1, "total_cost_usd": 0.001,
          "usage": {"input_tokens": len(prompt) // 4, "output_tokens": 300}, "duration_ms": 5})


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(sys.argv[1:])
    fake = Fake(args)
    if "You review proposed catalog items" in fake.prompt:
        reviewer(fake)
        return
    if fake.role == "improver" or args.input_format != "stream-json":
        fake.improver()
        return
    asyncio.run(fake.stream())


if __name__ == "__main__":
    main()
