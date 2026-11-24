"""Offline test of the reviewed arm (learn --review --seed-from), with the fake claude CLI.

A gate-only run makes the baseline and the round-1 proposals. Two reviewed runs are seeded
from it, so they share its baseline episodes and its exact proposals (no new improver call).
Each one pauses for decisions, then:
  approve -> the gate measures the draft and promotes it, recorded as "<reviewer>+study:gate"
  reject  -> nothing reaches the gate; the round ends with no candidate."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_smoke import load, run  # noqa: E402


def _env(tmp_path):
    env = dict(os.environ)
    env.update(PYTHONUTF8="1", TAU2_STUDY_RESULTS=str(tmp_path / "results"),
               TAU2_STUDY_WORKDIR=str(tmp_path / "work"))
    for k in list(env):
        if k.startswith("OBS_"):
            env.pop(k)
    return env


def _decide(ldir: Path, decision: str, reviewer: str = "human:test") -> None:
    tmpl = load(ldir / "rounds" / "r1" / "review_template.json")
    decisions = {slug: {**d, "decision": decision, "note": f"test {decision}"}
                 for slug, d in tmpl["decisions"].items()}
    (ldir / "rounds" / "r1" / "review_decisions.json").write_text(
        json.dumps({"reviewer": reviewer, "decisions": decisions}), encoding="utf-8")


def test_reviewed_arm(tmp_path):
    env = _env(tmp_path)
    runs = tmp_path / "runs"
    base = ["--runs-dir", str(runs), "--domain", "retail", "--fake-claude", "--concurrency", "2",
            "--max-invocations", "80", "--task-ids", "0", "1", "--trials", "2", "--rounds", "1",
            "--max-edits", "2"]
    run(["learn", "--run", "gate", *base], env)
    gate_state = load(runs / "gate" / "learn" / "state.json")
    assert gate_state["r1"]["verdict"] == "promoted"

    for name, decision in (("rev-ok", "approve"), ("rev-no", "reject")):
        args = ["learn", "--run", name, *base, "--seed-from", "gate", "--review",
                "--reviewer", "human:test"]
        out = run(args, env)
        ldir = runs / name / "learn"
        assert "awaiting review" in out, out
        state = load(ldir / "state.json")
        assert state["seeded_from"] == "gate" and state["r0"] == gate_state["r0"]
        sheet = (ldir / "rounds" / "r1" / "review.md").read_text(encoding="utf-8")
        assert "finish-confirmed-writes" in sheet and "rejected-leakage" in sheet
        tmpl = load(ldir / "rounds" / "r1" / "review_template.json")
        assert list(tmpl["decisions"]) == ["finish-confirmed-writes"]   # the leak never reaches review
        # no improver call in the seeded run: its proposals are the gate run's, byte for byte
        assert (ldir / "rounds" / "r1" / "improver_output.json").read_bytes() == \
            (runs / "gate" / "learn" / "rounds" / "r1" / "improver_output.json").read_bytes()

        _decide(ldir, decision)
        out = run(args, env)
        state = load(ldir / "state.json")
        log = {r["slug"]: r for r in map(json.loads, (ldir / "edit_log.jsonl").read_text(
            encoding="utf-8").splitlines())}
        row = log["finish-confirmed-writes"]
        assert row["review"] == {"decision": decision, "note": f"test {decision}", "by": "human:test"}
        st = load(ldir / "obs" / "blobs" / "tenants" / "study" / "catalog" / "finish-confirmed-writes"
                  / "state.json")
        if decision == "approve":
            assert state["r1"]["verdict"] == "promoted", state["r1"]
            assert any(h["action"] == "promote" and h["by"] == "human:test+study:gate"
                       for h in st["history"]), st
            print("ok  reviewed arm: approve -> gate measured the draft -> promoted by human:test+study:gate")
        else:
            assert state["r1"]["verdict"] == "no-candidate" and state["r1"]["reviewed"], state["r1"]
            assert row["verdict"] == "rejected-review"
            assert not (ldir / "rounds" / "r1" / "episodes").exists()   # nothing was measured
            print("ok  reviewed arm: reject -> no gate measurement, round ends with no candidate")
