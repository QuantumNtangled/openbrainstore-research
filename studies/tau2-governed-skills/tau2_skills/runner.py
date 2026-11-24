"""Run a batch of episodes (tasks x trials for one arm) with a checkpoint per episode,
bounded concurrency, retries, and a run-wide pause when the plan's usage limit is hit."""

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .claude_cli import CLIError, InvocationCapReached, RateLimited
from .common import append_jsonl, read_json, safe_name, write_json
from .episode import ArmSetup, EpisodeContext, infra_failure, is_terminal_ok, run_episode


class PlanLimitStop(Exception):
    """Stop the run cleanly; every finished episode is checkpointed, rerun to resume."""


def utilization(info: dict | None) -> tuple[float, float | None]:
    """Highest utilization across the plan windows a rate_limit_event reports, and its reset."""
    best, reset = 0.0, None
    for w in ((info or {}).get("unifiedWindows") or {}).values():
        u = float(w.get("utilization") or 0.0)
        if u >= best:
            best, reset = u, w.get("resetsAt")
    return best, reset


class Pacer:
    """Shared by every worker. A rate-limit error pauses the whole run: until the reset time
    the CLI reported (plus a margin), else exponential backoff from `base` up to `cap`. A pause
    longer than `max_pause` (a weekly window, say) stops the run instead of sleeping for days."""

    def __init__(self, base: float = 60.0, cap: float = 3600.0, log_path: Path | None = None,
                 max_pause: float = 3 * 3600.0, stop_at_utilization: float | None = 0.9):
        self.base, self.cap, self.log_path = base, cap, log_path
        self.max_pause, self.stop_at_utilization = max_pause, stop_at_utilization
        self._until = 0.0
        self._strikes = 0
        self._lock = threading.Lock()
        self.stop_reason: str | None = None
        self.last_utilization: float | None = None

    require_subscription = True
    max_run_usd: float | None = None
    spent_usd: float = 0.0

    def observe(self, res: dict) -> None:
        """After each episode: stop starting new ones once the plan is nearly used up, the
        run's API-equivalent budget is spent, or Claude Code is billing an API key instead of
        the subscription login."""
        with self._lock:
            self.spent_usd += sum(float((((res.get(r) or {}).get("usage") or {}).get("cost_usd_est")) or 0.0)
                                  for r in ("agent", "user"))
            if self.max_run_usd is not None and self.spent_usd >= self.max_run_usd:
                self.stop_reason = (f"API-equivalent spend ${self.spent_usd:.2f} reached --max-run-usd "
                                    f"${self.max_run_usd:.2f}")
        for role in ("agent", "user"):
            src = ((res.get(role) or {}).get("init") or {}).get("apiKeySource")
            if self.require_subscription and src not in (None, "none"):
                self.stop_reason = (f"claude reported apiKeySource={src!r}: it is billing an API key, "
                                    "not the subscription login (unset it, or pass --allow-api-key)")
            info = (((res.get(role) or {}).get("usage") or {}).get("rate_limit_info"))
            if not info:
                continue
            u, reset = utilization(info)
            self.last_utilization = u
            if self.log_path:
                append_jsonl(self.log_path, {"ts": time.time(), "event": "utilization", "utilization": u,
                                             "status": info.get("status"), "resets_at": reset})
            if self.stop_at_utilization is not None and u >= self.stop_at_utilization:
                self.stop_reason = (f"plan utilization {u:.0%} >= {self.stop_at_utilization:.0%}; "
                                    f"window resets at {time.ctime(reset) if reset else 'unknown'}")

    def wait(self) -> None:
        while True:
            with self._lock:
                left = self._until - time.time()
            if left <= 0:
                return
            time.sleep(min(left, 30.0))

    def trip(self, err: RateLimited) -> float:
        with self._lock:
            self._strikes += 1
            if err.reset_at and err.reset_at > time.time():
                delay = err.reset_at - time.time() + 60.0
            else:
                delay = min(self.cap, self.base * (2 ** (self._strikes - 1))) * (1 + 0.1 * random.random())
            if self.log_path:
                append_jsonl(self.log_path, {"ts": time.time(), "event": "rate_limited",
                                             "pause_s": round(delay, 1), "error": str(err)[:300]})
            if delay > self.max_pause:
                self.stop_reason = (f"rate limited until {time.ctime(time.time() + delay)}, longer than "
                                    f"--max-pause {self.max_pause:.0f}s")
                raise PlanLimitStop(self.stop_reason)
            self._until = max(self._until, time.time() + delay)
            return delay

    def ok(self) -> None:
        with self._lock:
            self._strikes = 0


@dataclass
class BatchSpec:
    domain: str
    tasks: list                         # tau2 Task objects
    trials: int
    arm: ArmSetup
    out_dir: Path                       # <out_dir>/<task>__t<trial>.json
    make_ctx: object                    # (task, trial, out_path, log_dir) -> EpisodeContext
    concurrency: int = 2
    max_attempts: int = 3
    pacer: Pacer = field(default_factory=Pacer)
    progress: bool = True


def episode_path(out_dir: Path, task_id: str, trial: int) -> Path:
    return out_dir / f"{safe_name(task_id)}__t{trial}.json"


def run_batch(spec: BatchSpec) -> list[dict]:
    todo, done = [], []
    for task in spec.tasks:
        for trial in range(spec.trials):
            p = episode_path(spec.out_dir, task.id, trial)
            prev = read_json(p)
            if is_terminal_ok(prev):
                done.append(prev)
            else:
                todo.append((task, trial, p, 0))   # a resumed run retries earlier infra errors
    total = len(todo) + len(done)
    if spec.progress:
        print(f"[{spec.arm.name}] {len(done)}/{total} episodes already checkpointed, {len(todo)} to run",
              flush=True)
    lock = threading.Lock()
    counter = {"n": len(done)}

    def work(item):
        task, trial, path, attempts = item
        log_dir = path.with_suffix("")
        last = None
        while attempts < spec.max_attempts:
            spec.pacer.wait()
            if spec.pacer.stop_reason:
                return None            # not started; the next run picks it up
            attempts += 1
            ctx = spec.make_ctx(task, trial, path, log_dir)
            try:
                res = run_episode(ctx)
                spec.pacer.ok()
                spec.pacer.observe(res)
                return res
            except (InvocationCapReached, PlanLimitStop):
                raise
            except RateLimited as e:
                delay = spec.pacer.trip(e)
                print(f"  rate limited on {task.id} t{trial}; pausing the run {delay:.0f}s", flush=True)
                attempts -= 1          # a plan limit is not the episode's fault
                last = e
            except CLIError as e:
                last = e
                res = infra_failure(ctx, e, attempts)
                write_json(path, res)
            except Exception as e:     # a harness bug: record and keep the batch going
                last = e
                res = infra_failure(ctx, e, attempts)
                write_json(path, res)
        res = infra_failure(spec.make_ctx(task, trial, path, log_dir), last or RuntimeError("?"), attempts)
        write_json(path, res)
        return res

    results = list(done)
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, spec.concurrency)) as ex:
            futs = [ex.submit(work, it) for it in todo]
            for f in as_completed(futs):
                try:
                    res = f.result()
                except (InvocationCapReached, PlanLimitStop) as e:
                    spec.pacer.stop_reason = spec.pacer.stop_reason or str(e)
                    continue
                if res is None:
                    continue
                results.append(res)
                with lock:
                    counter["n"] += 1
                    if spec.progress:
                        tag = ("ok " if res.get("success") else "FAIL") if res.get("status") != "infra_error" else "ERR "
                        print(f"  [{spec.arm.name}] {counter['n']}/{total} {tag} task={str(res['task_id'])[:50]} "
                              f"t{res['trial']} reward={res.get('reward')} "
                              f"obs(startup={res.get('obs', {}).get('startup_calls')}, "
                              f"get_skill={res.get('obs', {}).get('get_skill_calls')}) "
                              f"{res.get('wall_s', '')}s", flush=True)
    if spec.pacer.stop_reason:
        raise PlanLimitStop(spec.pacer.stop_reason)
    return results
