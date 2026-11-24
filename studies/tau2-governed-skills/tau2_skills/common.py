"""Paths, pins, prices and small IO helpers shared by every module."""

import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path

STUDY_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = STUDY_DIR.parents[1]
VENDOR_DIR = STUDY_DIR / "vendor" / "tau2-bench"
RUNS_DIR = STUDY_DIR / "runs"
RESULTS_DIR = Path(os.environ.get("TAU2_STUDY_RESULTS") or (STUDY_DIR / "results"))
FAKE_CLAUDE = STUDY_DIR / "tests" / "fake_claude.py"

# tau2-bench pin. v1.0.1 is the newest 1.x release; 2.0.0 (2026-10-19) changed scoring and
# defaults to OpenAI-judged communication checks, so it would add a judge model.
TAU2_TAG = "v1.0.1"
TAU2_COMMIT = "fc0055dc4e0a316c3f83133267fbd6faaa770992"

DOMAINS = ("retail", "airline", "telecom")
DEFAULT_MODEL = "claude-haiku-5-5"
OBS_USER = "study"

# Every OBS tool the server registers with OBS_CATALOG set (and OBS_VIEWS unset).
# The agent is offered only the ones in ObsTools; the rest go in --disallowedTools.
OBS_ALL_TOOLS = ("remember", "update", "recall", "list_memories", "graph", "get_memory_schema",
                 "link", "forget", "export", "agent_startup", "get_skill", "log_episode",
                 "propose_skill")

# API-equivalent prices, USD per million tokens. The runs go through a Claude subscription,
# so these are what the same tokens would cost on the API, not money spent.
# Haiku 5.5 has two rate cards, picked by prompt length (input + cache read + cache write).
# Cache writes bill at 1.25x input (5-minute TTL) or 2x (1-hour TTL, which Claude Code uses on
# a subscription); cache reads at the per-model multiplier. These reproduce the CLI's own
# modelUsage.costUSD exactly in the real smoke test. Verify against the live pricing page
# before quoting a budget.
PRICES = {
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50, "in_long": 0.50, "out_long": 2.50,
                         "long_over": 100_000, "cache_read_x": 0.10},
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00, "cache_read_x": 0.05},
    "claude-opus-5-5": {"in": 4.00, "out": 20.00, "cache_read_x": 0.05},
}
CACHE_WRITE_X = 1.25      # 5-minute TTL
CACHE_WRITE_1H_X = 2.0    # 1-hour TTL


def price_for(model: str) -> dict | None:
    for name, p in PRICES.items():
        if (model or "").startswith(name):
            return p
    return None


def cost_of(model: str, inp: int, cw_5m: int, cw_1h: int, cr: int, out: int,
            long_prompt: bool = False) -> float | None:
    """API-equivalent USD for a token bundle. long_prompt selects Haiku 5.5's >100K card."""
    p = price_for(model)
    if p is None:
        return None
    long_ = long_prompt and "long_over" in p
    rin = p["in_long"] if long_ else p["in"]
    rout = p["out_long"] if long_ else p["out"]
    return (inp * rin + cw_5m * rin * CACHE_WRITE_X + cw_1h * rin * CACHE_WRITE_1H_X
            + cr * rin * p["cache_read_x"] + out * rout) / 1e6


def cost_of_model_usage(model: str, mu: dict) -> float | None:
    """Cost of one entry of a result event's cumulative `modelUsage`."""
    cw = int(mu.get("cacheCreationInputTokens") or 0)
    cw_1h = int(mu.get("cacheCreationEphemeral1hInputTokens") or 0)
    cw_5m = int(mu.get("cacheCreationEphemeral5mInputTokens") or (cw - cw_1h))
    return cost_of(model, int(mu.get("inputTokens") or 0), cw_5m, cw_1h,
                   int(mu.get("cacheReadInputTokens") or 0), int(mu.get("outputTokens") or 0))


def ensure_utf8_mode() -> None:
    """tau2 opens its data files with the locale encoding; on Windows that is cp1252 and the
    telecom manuals fail to decode. Re-exec this process in UTF-8 mode when needed."""
    if os.name == "nt" and not sys.flags.utf8_mode:
        import subprocess
        argv = [sys.executable, "-X", "utf8"] + (
            ["-m", "tau2_skills"] + sys.argv[1:] if sys.argv[0].endswith("__main__.py") else sys.argv)
        raise SystemExit(subprocess.call(argv))


def quiet_tau2_logs(level: str = "ERROR") -> None:
    """tau2 logs through loguru (and warns that our messages carry no per-message cost: cost
    is accounted per claude process instead); the MCP SDK logs every request at INFO."""
    import logging

    from loguru import logger
    logger.remove()
    logger.add(sys.stderr, level=level)
    for name in ("mcp", "httpx", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)


_io_lock = threading.Lock()


def write_json(path: Path, data) -> None:
    """Atomic write: a checkpoint is either complete or absent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _io_lock, open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    try:
        return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    except FileNotFoundError:
        return []


def safe_name(s: str, limit: int = 40) -> str:
    """Filesystem-safe, short (Windows MAX_PATH is 260 and telecom task ids are ~200 chars)."""
    import hashlib
    clean = re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_")
    if len(clean) <= limit:
        return clean or "x"
    return clean[: limit - 9] + "_" + hashlib.sha1(s.encode()).hexdigest()[:8]


def default_workdir() -> Path:
    """Where the claude processes run: an empty directory outside the repo, so no CLAUDE.md,
    AGENTS.md or project settings are discovered."""
    return Path(os.environ.get("TAU2_STUDY_WORKDIR") or (Path(tempfile.gettempdir()) / "tau2s"))


def assert_clean_workdir(path: Path) -> None:
    """Claude Code reads CLAUDE.md from the working directory and its parents."""
    for d in [path, *path.parents]:
        for name in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md"):
            if (d / name).exists():
                raise SystemExit(f"{d / name} would be loaded into every claude session; "
                                 "set TAU2_STUDY_WORKDIR to a directory without one above it")
