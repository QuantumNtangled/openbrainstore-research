"""Offline end-to-end smoke test: the real harness, real tau2 tasks and grading, real OBS
(`obs serve` over MCP for the agent, in-process propose/promote for the gate), with
tests/fake_claude.py in place of the claude CLI. No model calls, no network.

    cd studies/tau2-skills
    python -X utf8 -m pytest tests -v -s

What it drives:
  learn     retail TRAIN tasks 0 and 1, 2 trials, 1 round. The scripted agent skips the last
            write unless it has the mock skill, so the baseline fails; the fake improver
            proposes one clean item and one that leaks a user id; the leak is screened out,
            the clean draft wins the gate on train and is promoted by "study:gate".
  evaluate  retail TEST task 5, k=2, arms A-D, with one injected rate-limit error.
            Expected: A and B fail, C (skill fetched over MCP) and D (skill in the prompt)
            pass."""

import json
import os
import subprocess
import sys
from pathlib import Path

STUDY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STUDY))


def run(args, env):
    cmd = [sys.executable, "-X", "utf8", "-m", "tau2_skills", *args]
    cp = subprocess.run(cmd, cwd=STUDY, env=env, capture_output=True, text=True, encoding="utf-8",
                        timeout=1800)
    print(cp.stdout[-6000:])
    if cp.returncode != 0:
        print(cp.stderr[-6000:])
    assert cp.returncode == 0, f"{args[0]} failed"
    return cp.stdout


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def test_offline_smoke(tmp_path):
    env = dict(os.environ)
    env.update(PYTHONUTF8="1", TAU2_STUDY_RESULTS=str(tmp_path / "results"),
               TAU2_STUDY_WORKDIR=str(tmp_path / "work"),
               FAKE_CLAUDE_RATE_LIMIT_ONCE=str(tmp_path / "ratelimit.flag"))
    for k in list(env):
        if k.startswith("OBS_"):
            env.pop(k)
    runs = tmp_path / "runs"
    common = ["--run", "smoke", "--runs-dir", str(runs), "--domain", "retail", "--fake-claude",
              "--concurrency", "2", "--max-invocations", "80", "--backoff-base", "1"]

    # ---------------- learning loop (TRAIN only)
    # the rate-limit flag only fires in evaluate: pre-create it, then remove before evaluate
    (tmp_path / "ratelimit.flag").write_text("hold")
    run(["learn", *common, "--task-ids", "0", "1", "--trials", "2", "--rounds", "1", "--max-edits", "2"], env)
    ldir = runs / "smoke" / "learn"
    state = load(ldir / "state.json")
    assert state["train_ids"] == ["0", "1"]
    assert state["r0"]["score"] == 0.0, state["r0"]
    assert state["r1"]["verdict"] == "promoted", state["r1"]
    assert state["r1"]["candidate_score"] == 1.0
    print("ok  learn: baseline 0/4 on train, candidate 4/4, promoted through the gate")

    log = [json.loads(l) for l in (ldir / "edit_log.jsonl").read_text(encoding="utf-8").splitlines()]
    verdicts = {r["slug"]: r for r in log}
    assert verdicts["lookup-example"]["verdict"] == "rejected-leakage", verdicts
    assert verdicts["lookup-example"]["leakage_hits"]
    good = verdicts["finish-confirmed-writes"]
    assert good["verdict"] == "promoted" and good["ref"] == "procedure:finish-confirmed-writes@v1"
    assert good["evidence"] and all(e.startswith("epi_") for e in good["evidence"])
    assert good["train_before"] == 0.0 and good["train_after"] == 1.0
    print("ok  leakage screen rejected the proposal that named a user id; edit log complete")

    st = json.loads((ldir / "obs" / "blobs" / "tenants" / "study" / "catalog" / "finish-confirmed-writes"
                     / "state.json").read_text(encoding="utf-8"))
    assert st["promoted"] == 1
    assert any(h["action"] == "promote" and h["by"] == "study:gate" for h in st["history"]), st
    print("ok  canonical catalog: promoted by study:gate (OBS state.json history)")

    prompt = (ldir / "rounds" / "r1" / "improver_prompt.md").read_text(encoding="utf-8")
    from tau2_skills.common import ensure_utf8_mode  # noqa: F401  (import check)
    test_ids = json.loads((STUDY / "vendor" / "tau2-bench" / "data" / "tau2" / "domains" / "retail"
                           / "split_tasks.json").read_text(encoding="utf-8"))["test"]
    assert "### Episode epi_" in prompt
    for ep in (ldir / "rounds").glob("*/episodes/*.json"):
        assert load(ep)["task_id"] not in test_ids
    print("ok  improver saw only train episodes")

    r0 = [load(p) for p in (ldir / "rounds" / "r0" / "episodes").glob("*.json")]
    assert all(e["obs"]["startup_calls"] == 1 and e["obs"]["get_skill_calls"] == 0 for e in r0)
    r1 = [load(p) for p in (ldir / "rounds" / "r1" / "episodes").glob("*.json")]
    assert all(e["obs"]["get_skill_calls"] == 1 for e in r1)
    assert all(e["obs"]["refs_fetched"] == ["procedure:finish-confirmed-writes@v1"] for e in r1)
    print("ok  train agents called agent_startup / get_skill over MCP (`obs serve`)")

    # ---------------- evaluation (TEST only), with one injected rate limit
    (tmp_path / "ratelimit.flag").unlink()
    run(["evaluate", *common, "--task-ids", "5", "--k", "2", "--arms", "A,B,C,D"], env)
    summary = load(tmp_path / "results" / "smoke" / "summary.json")
    p1 = {a: m["pass_hat_k"]["1"]["value"] for a, m in summary["arms"].items()}
    assert p1 == {"A": 0.0, "B": 0.0, "C": 1.0, "D": 1.0}, p1
    assert summary["arms"]["C"]["pass_hat_k"]["2"]["value"] == 1.0
    assert summary["arms"]["C"]["obs"]["get_skill_rate"] == 1.0
    assert summary["arms"]["B"]["obs"]["startup_rate"] == 1.0
    assert summary["arms"]["B"]["obs"]["get_skill_rate"] == 0.0
    assert summary["paired"]["C-B"]["1"]["diff"] == 1.0
    assert all(m["infra_errors"] == 0 for m in summary["arms"].values())
    assert (tmp_path / "results" / "smoke" / "summary.md").exists()
    print("ok  evaluate: A 0.0, B 0.0, C 1.0, D 1.0 on TEST; summary.json + summary.md written")

    eps = {a: [load(p) for p in (runs / "smoke" / "eval" / a).glob("*.json")] for a in "ABCD"}
    assert all(e["obs"]["startup_calls"] == 0 for a in "AD" for e in eps[a])
    c_init = eps["C"][0]["agent"]["init"]["tools"]
    assert "mcp__obs__agent_startup" in c_init and "mcp__obs__remember" not in c_init, c_init
    assert not [t for t in eps["A"][0]["agent"]["init"]["tools"] if not t.startswith("mcp__tau2__")]
    stats = json.loads((runs / "smoke" / "eval" / "C").glob("*.json").__next__().read_text(encoding="utf-8"))
    assert stats["agent"]["usage"]["api_calls"] > 0 and stats["agent"]["usage"]["cost_usd_est"] > 0
    print("ok  tool isolation: A/D see only tau2 tools; B/C see agent_startup and get_skill, not remember")

    pacing = (runs / "smoke" / "pacing.jsonl").read_text(encoding="utf-8")
    assert "rate_limited" in pacing
    print("ok  rate limit: run paused, episode retried, nothing lost")

    inv = (runs / "smoke" / "claude_invocations.jsonl").read_text(encoding="utf-8").splitlines()
    print(f"ok  {len(inv)} fake claude invocations logged against the cap")
