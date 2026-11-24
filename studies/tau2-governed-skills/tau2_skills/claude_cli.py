"""Driving the Claude Code CLI headless: persistent stream-json sessions (agent, user
simulator), one-shot print calls (improver), usage accounting, rate-limit detection and a
hard cap on how many claude processes a run may start."""

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .common import append_jsonl, cost_of_model_usage


class CLIError(RuntimeError):
    """The claude process failed in a way that is not the model's fault (retryable)."""


class RateLimited(CLIError):
    def __init__(self, message: str, reset_at: float | None = None):
        super().__init__(message)
        self.reset_at = reset_at


class BudgetExceeded(RuntimeError):
    """The per-process --max-budget-usd cap was hit (counted as an agent failure)."""


class InvocationCapReached(RuntimeError):
    pass


_RATE_RE = re.compile(r"usage limit|rate.?limit|\b429\b|overloaded|too many requests|limit reached|"
                      r"hit your limit|exceeded your", re.I)
_RESET_EPOCH_RE = re.compile(r"\|(\d{10})\b")


def find_claude() -> list[str]:
    """The real CLI. On Windows the npm shim is a .cmd that would route every argument through
    cmd.exe quoting, so call the native binary it points at."""
    env = os.environ.get("TAU2_STUDY_CLAUDE")
    if env:
        return [env]
    exe = shutil.which("claude")
    if exe is None:
        raise SystemExit("claude is not on PATH (set TAU2_STUDY_CLAUDE to its path)")
    p = Path(exe)
    if p.suffix.lower() in (".cmd", ".bat", ".ps1", "") and os.name == "nt":
        native = p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if native.exists():
            return [str(native)]
    return [str(p)]


@dataclass
class ClaudeConfig:
    cmd: list[str]
    model: str
    effort: str | None = "medium"
    setting_sources: str | None = "project"
    permission_mode: str = "dontAsk"
    turn_timeout: float = 900.0
    max_budget_usd: float | None = None
    prompt_transport: str = "auto"      # auto | file | inline (for --append-system-prompt)
    extra_args: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)

    def common_args(self, streaming: bool) -> list[str]:
        a = ["-p", "--model", self.model, "--no-session-persistence", "--strict-mcp-config",
             "--tools", "", "--disable-slash-commands", "--permission-mode", self.permission_mode]
        if self.setting_sources is not None:
            a += ["--setting-sources", self.setting_sources]
        if self.effort:
            a += ["--effort", self.effort]
        if streaming:
            a += ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
        else:
            a += ["--output-format", "json"]
        if self.max_budget_usd:
            a += ["--max-budget-usd", f"{self.max_budget_usd:g}"]
        return a + list(self.extra_args)


def prompt_args(flag: str, text: str, transport: str, path: Path) -> list[str]:
    """--append-system-prompt / --system-prompt, inline or as a file. Windows caps a command
    line at 32,767 characters, and the telecom policy alone is ~24K."""
    if transport == "file" or (transport == "auto" and len(text) > 20_000):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return [flag + "-file", str(path)]
    return [flag, text]


class InvocationBudget:
    """Counts every claude process this harness starts; refuses to start one past the cap."""

    def __init__(self, cap: int | None, log_path: Path | None):
        self.cap, self.log_path, self.n = cap, log_path, 0
        self._lock = threading.Lock()
        if log_path is not None:
            from .common import read_jsonl
            self.n = len(read_jsonl(log_path))   # resumable: the cap spans restarts

    def take(self, role: str, label: str) -> int:
        with self._lock:
            if self.cap is not None and self.n >= self.cap:
                raise InvocationCapReached(f"claude invocation cap {self.cap} reached")
            self.n += 1
            if self.log_path is not None:
                append_jsonl(self.log_path, {"n": self.n, "role": role, "label": label, "ts": time.time()})
            return self.n


def _prompt_tokens(u: dict) -> int:
    return sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                            "cache_read_input_tokens"))


class UsageLog:
    """Token and cost accounting for one claude process.

    Verified against Claude Code 2.1.316 in the real smoke test: assistant events carry the
    usage of the request as it started (prompt sizes are right, output_tokens is not final),
    and each result event carries the session's cumulative `modelUsage` and `total_cost_usd`.
    So prompt sizes and the API-call count come from assistant events (de-duplicated by message
    id: one event per content block), token totals and cost from the last result."""

    def __init__(self):
        self.calls: dict[str, dict] = {}
        self.order: list[str] = []
        self.results: list[dict] = []
        self.rate_limit_info: dict | None = None

    def on_event(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "assistant":
            msg = ev.get("message") or {}
            mid = msg.get("id") or f"anon{len(self.order)}"
            if mid not in self.calls:
                self.calls[mid] = {"model": msg.get("model", ""), "usage": dict(msg.get("usage") or {})}
                self.order.append(mid)
        elif t == "result":
            self.results.append({k: ev.get(k) for k in (
                "subtype", "is_error", "duration_ms", "duration_api_ms", "num_turns",
                "total_cost_usd", "usage", "modelUsage")})
        elif t == "rate_limit_event":
            self.rate_limit_info = ev.get("rate_limit_info") or {}

    def summary(self) -> dict:
        prompts = [_prompt_tokens(self.calls[m]["usage"]) for m in self.order]
        tot = {"api_calls": len(self.order), "input_tokens": 0, "cache_creation_input_tokens": 0,
               "cache_creation_1h_input_tokens": 0, "cache_read_input_tokens": 0,
               "output_tokens": 0, "thinking_tokens": 0, "cost_usd_est": 0.0,
               "first_call_prompt_tokens": prompts[0] if prompts else 0,
               "max_call_prompt_tokens": max(prompts) if prompts else 0}
        last = next((r for r in reversed(self.results) if r.get("modelUsage")), None)
        if last:
            for model, mu in last["modelUsage"].items():
                tot["input_tokens"] += int(mu.get("inputTokens") or 0)
                tot["cache_creation_input_tokens"] += int(mu.get("cacheCreationInputTokens") or 0)
                tot["cache_creation_1h_input_tokens"] += int(mu.get("cacheCreationEphemeral1hInputTokens") or 0)
                tot["cache_read_input_tokens"] += int(mu.get("cacheReadInputTokens") or 0)
                tot["output_tokens"] += int(mu.get("outputTokens") or 0)
                tot["thinking_tokens"] += int(mu.get("thinkingTokens") or 0)
                c = cost_of_model_usage(model, mu)
                tot["cost_usd_est"] += c or 0.0
                if c is None:
                    tot["cost_usd_est_note"] = f"no price entry for {model}"
            tot["source"] = "result.modelUsage"
        else:   # the process died before any turn finished: prompt-side lower bound only
            for m in self.order:
                u = self.calls[m]["usage"]
                tot["input_tokens"] += int(u.get("input_tokens") or 0)
                tot["cache_read_input_tokens"] += int(u.get("cache_read_input_tokens") or 0)
                tot["cache_creation_input_tokens"] += int(u.get("cache_creation_input_tokens") or 0)
            tot["source"] = "assistant events (no result; output tokens unknown)"
        if any(p > 100_000 for p in prompts):
            tot["cost_usd_est_note"] = "a call exceeded 100K prompt tokens: Haiku's long-context card applies"
        costs = [r["total_cost_usd"] for r in self.results if r.get("total_cost_usd") is not None]
        tot["cli_total_cost_usd_last"] = costs[-1] if costs else None
        tot["cli_total_cost_usd_max"] = max(costs) if costs else None
        tot["results"] = len(self.results)
        tot["rate_limit_info"] = self.rate_limit_info
        return tot


def check_result(ev: dict) -> None:
    """Raise for a result event that ended the turn with an error."""
    subtype = ev.get("subtype") or ""
    if not ev.get("is_error") and subtype in ("", "success"):
        return
    text = " ".join(str(ev.get(k) or "") for k in ("result", "error", "subtype"))
    if "budget" in subtype:
        raise BudgetExceeded(text[:500])
    if _RATE_RE.search(text):
        m = _RESET_EPOCH_RE.search(text)
        raise RateLimited(text[:500], float(m.group(1)) if m else None)
    raise CLIError(f"claude turn failed: {text[:500]}")


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc.kill()


class ClaudeSession:
    """One persistent `claude -p --input-format stream-json` process. Parsed stdout events go
    into `events` as ("cli", event); process exit as ("exit", returncode). The caller merges
    these with tool calls arriving from the relay on the same queue."""

    def __init__(self, cfg: ClaudeConfig, args: list[str], cwd: Path, events: queue.Queue,
                 log_dir: Path, role: str, budget: InvocationBudget, label: str, env: dict | None = None):
        budget.take(role, label)
        cwd.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        self.role, self.events = role, events
        self.usage = UsageLog()
        self.init_event: dict | None = None
        self.session_id = ""
        self._events_log = open(log_dir / f"{role}.events.jsonl", "a", encoding="utf-8")
        self._stderr_tail: list[str] = []
        child_env = dict(os.environ)
        child_env.update(cfg.env)
        child_env.update(env or {})
        (log_dir / f"{role}.argv.json").write_text(json.dumps(cfg.cmd + args, indent=1), encoding="utf-8")
        self.started = time.time()
        self.proc = subprocess.Popen(cfg.cmd + args, cwd=str(cwd), env=child_env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            self._events_log.write(line + "\n")
            self._events_log.flush()
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.usage.on_event(ev)
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                self.init_event = ev
                self.session_id = ev.get("session_id") or ""
            self.events.put(("cli", ev))
        self.events.put(("exit", self.proc.wait()))

    def _read_stderr(self) -> None:
        for raw in self.proc.stderr:
            self._stderr_tail = (self._stderr_tail + [raw.decode("utf-8", errors="replace")])[-40:]

    def stderr_tail(self) -> str:
        return "".join(self._stderr_tail)[-2000:]

    def send_user(self, text: str) -> None:
        msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]},
               "parent_tool_use_id": None, "session_id": self.session_id}
        try:
            self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except OSError as e:
            raise CLIError(f"claude ({self.role}) stdin closed: {e}; stderr: {self.stderr_tail()}") from e

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            _kill_tree(self.proc)
        _kill_tree(self.proc)
        try:
            self._events_log.close()
        except OSError:
            pass


def run_print(cfg: ClaudeConfig, args: list[str], prompt: str, cwd: Path, log_dir: Path, role: str,
              budget: InvocationBudget, label: str, timeout: float = 1800.0) -> dict:
    """One-shot `claude -p --output-format json`, prompt on stdin. Returns the result event."""
    budget.take(role, label)
    cwd.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{role}.argv.json").write_text(json.dumps(cfg.cmd + args, indent=1), encoding="utf-8")
    env = dict(os.environ)
    env.update(cfg.env)
    try:
        cp = subprocess.run(cfg.cmd + args, cwd=str(cwd), env=env, input=prompt.encode("utf-8"),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise CLIError(f"claude ({role}) timed out after {timeout}s") from e
    out = cp.stdout.decode("utf-8", errors="replace")
    (log_dir / f"{role}.stdout.json").write_text(out, encoding="utf-8")
    try:
        ev = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    except json.JSONDecodeError:
        ev = {}
    if ev.get("type") != "result":
        err = cp.stderr.decode("utf-8", errors="replace")[-1500:]
        if _RATE_RE.search(out + err):
            raise RateLimited(f"claude ({role}) rate limited: {(out + err)[-500:]}")
        raise CLIError(f"claude ({role}) exited {cp.returncode} without a result: {err}")
    check_result(ev)
    return ev
