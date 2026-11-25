"""Offline test of named catalogs in evaluate (`--catalog NAME=DIR`, arms C:NAME / D:NAME),
with the fake claude CLI.

One learning run promotes the mock skill. Evaluate then compares it ("good") against an
empty catalog ("empty") in one interleaved run: C:good passes, C:empty behaves like B, and
the summary reports C-good against C-empty."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_review import _env  # noqa: E402
from test_smoke import load, run  # noqa: E402


def test_named_catalogs(tmp_path):
    from tau2_skills.evaluate import arm_key, pairs_for, parse_arms

    assert parse_arms("a,b,C:rev,d:rev") == ["A", "B", "C-rev", "D-rev"]
    assert arm_key("C") == "C"
    for bad in ("B:x", "E", "CC"):
        try:
            arm_key(bad)
        except SystemExit:
            pass
        else:
            raise AssertionError(bad)
    assert pairs_for(["A", "B", "C-rev", "C-opus", "D-rev"]) == [
        ("C-rev", "B"), ("C-rev", "A"), ("D-rev", "A"), ("C-rev", "D-rev"),
        ("C-opus", "B"), ("C-opus", "A"), ("B", "A"), ("C-rev", "C-opus")]
    print("ok  arm names and paired differences")

    env = _env(tmp_path)
    runs = tmp_path / "runs"
    base = ["--runs-dir", str(runs), "--domain", "retail", "--fake-claude", "--concurrency", "2",
            "--max-invocations", "80"]
    run(["learn", "--run", "src", *base, "--task-ids", "0", "1", "--trials", "2", "--rounds", "1",
         "--max-edits", "2"], env)
    assert load(runs / "src" / "learn" / "state.json")["r1"]["verdict"] == "promoted"
    empty = tmp_path / "empty_obs"

    run(["evaluate", "--run", "ev", *base, "--task-ids", "5", "--k", "2",
         "--catalog", f"good={runs / 'src' / 'learn' / 'obs'}", "--catalog", f"empty={empty}",
         "--arms", "B,C:good,C:empty,D:good"], env)
    summary = load(tmp_path / "results" / "ev" / "summary.json")
    p1 = {a: m["pass_hat_k"]["1"]["value"] for a, m in summary["arms"].items()}
    assert p1 == {"B": 0.0, "C-good": 1.0, "C-empty": 0.0, "D-good": 1.0}, p1
    assert summary["catalog"]["good"]["refs"] == ["procedure:finish-confirmed-writes@v1"]
    assert summary["catalog"]["empty"]["refs"] == []
    assert summary["paired"]["C-good-C-empty"]["1"]["diff"] == 1.0
    assert summary["paired"]["C-good-B"]["1"]["diff"] == 1.0
    eps = [load(p) for p in (runs / "ev" / "eval" / "C-good").glob("*.json")]
    assert eps and all(e["arm"] == "C-good" and e["obs"]["get_skill_calls"] == 1 for e in eps)
    print("ok  two catalogs in one run: C-good 1.0, C-empty 0.0 (= B), D-good 1.0")

    # the catalogs are frozen: a resumed run with a different catalog under the same name aborts
    import subprocess
    cmd = [sys.executable, "-X", "utf8", "-m", "tau2_skills", "evaluate", "--run", "ev", *base,
           "--task-ids", "5", "--k", "2", "--catalog", f"good={empty}", "--arms", "C:good"]
    cp = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True,
                        text=True, encoding="utf-8", timeout=600)
    assert cp.returncode != 0 and "catalog changed" in (cp.stderr + cp.stdout)
    print("ok  a changed catalog under the same name aborts the resumed run")
