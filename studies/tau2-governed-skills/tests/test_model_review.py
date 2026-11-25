"""Offline test of a model in the reviewer's seat (learn --reviewer-model), with the fake CLI.

Seeded from a gate-only run like the human-reviewed arm. The fake reviewer edits the
proposal; the edit is screened, measured by the same gate and promoted as
"model:<M>+study:gate". A second run whose reviewer slips a denylisted string into its edit
gets that edit turned into a reject, so nothing reaches the gate."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_review import _env  # noqa: E402
from test_smoke import load, run  # noqa: E402


def test_model_reviewer(tmp_path):
    env = _env(tmp_path)
    runs = tmp_path / "runs"
    base = ["--runs-dir", str(runs), "--domain", "retail", "--fake-claude", "--concurrency", "2",
            "--max-invocations", "80", "--task-ids", "0", "1", "--trials", "2", "--rounds", "1",
            "--max-edits", "2"]
    run(["learn", "--run", "gate", *base], env)

    args = ["learn", "--run", "rev-m", *base, "--seed-from", "gate", "--reviewer-model", "fake-opus"]
    out = run(args, env)
    assert "awaiting review" not in out, out
    ldir = runs / "rev-m" / "learn"
    state = load(ldir / "state.json")
    assert state["r1"]["verdict"] == "promoted", state["r1"]
    dec = load(ldir / "rounds" / "r1" / "review_decisions.json")
    assert dec["reviewer"] == "model:fake-opus" and dec["model"] == "fake-opus"
    assert list(dec["decisions"]) == ["finish-confirmed-writes"]   # the screened-out leak isn't shown
    assert dec["decisions"]["finish-confirmed-writes"]["decision"] == "edit"
    prompt = (ldir / "rounds" / "r1" / "reviewer_prompt.md").read_text(encoding="utf-8")
    assert "### Proposal: finish-confirmed-writes" in prompt and "lookup-example" not in prompt
    assert "## Tools the agent can call" in prompt
    st = load(ldir / "obs" / "blobs" / "tenants" / "study" / "catalog" / "finish-confirmed-writes"
              / "state.json")
    assert any(h["action"] == "promote" and h["by"] == "model:fake-opus+study:gate"
               for h in st["history"]), st
    final = (ldir / "catalog_final.md").read_text(encoding="utf-8")
    assert "Reviewed: finish confirmed writes" in final
    print("ok  model reviewer: edit -> screened -> gate -> promoted by model:fake-opus+study:gate")

    # an edit carrying a denylisted string is rejected before the gate
    env2 = {**env, "FAKE_REVIEW_LEAK": "Quote reference 4815162342 to the customer."}
    run(["learn", "--run", "rev-leak", *base, "--seed-from", "gate", "--reviewer-model", "fake-opus"], env2)
    ldir = runs / "rev-leak" / "learn"
    state = load(ldir / "state.json")
    assert state["r1"]["verdict"] == "no-candidate" and state["r1"]["reviewed"], state["r1"]
    d = load(ldir / "rounds" / "r1" / "review_decisions.json")["decisions"]["finish-confirmed-writes"]
    assert d["decision"] == "reject" and d["leakage_hits"], d
    assert not (ldir / "rounds" / "r1" / "episodes").exists()
    rows = [json.loads(x) for x in (ldir / "edit_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {r["slug"]: r["verdict"] for r in rows}["finish-confirmed-writes"] == "rejected-review"
    print("ok  model reviewer: a leaking edit is rejected by the screen, nothing reaches the gate")
