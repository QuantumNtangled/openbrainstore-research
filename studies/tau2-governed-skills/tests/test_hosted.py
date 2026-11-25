"""Hosted mode (--obs-url), end to end and offline. A local OpenBrainStore server in OAuth mode
with agent keys stands in for openbrainstore.com, and tests/fake_claude.py for Claude Code.

    learn     retail TRAIN tasks 0 and 1, every measurement served from slot 1
    evaluate  retail TEST task 5, arms B (slot 1, empty) and C (slot 2, the learned catalog)

The server needs OpenBrainStore with agent keys and the catalog:approve permission, which only
the hosted service's code has, so this test runs only when OBS_HOSTED_PYTHON names a Python
that can import it (add OBS_HOSTED_PYTHONPATH if it needs one). Every key is a throwaway value
made on that local server."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_review import _env  # noqa: E402
from test_smoke import load, run  # noqa: E402

HOSTED_PY = os.environ.get("OBS_HOSTED_PYTHON")
pytestmark = pytest.mark.skipif(not HOSTED_PY, reason="needs OBS_HOSTED_PYTHON (the hosted service's code)")
OWNER = "study-tester"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def server(tmp_path):
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("OBS_")}
    if os.environ.get("OBS_HOSTED_PYTHONPATH"):
        env["PYTHONPATH"] = os.environ["OBS_HOSTED_PYTHONPATH"]
    env.update(OBS_DATA_DIR=str(tmp_path / "server"), OBS_BACKEND="sqlite", OBS_CATALOG="all",
               OBS_AUTH="oauth", OBS_ISSUER_URL=f"http://localhost:{port}", OBS_GH_CLIENT_ID="x",
               OBS_GH_CLIENT_SECRET="y", OBS_ALLOWED_HOSTS=f"localhost:{port},localhost")
    proc = subprocess.Popen([HOSTED_PY, "-m", "openbrainstore.cli", "serve", "--http", "--port", str(port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 30
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("local OpenBrainStore server did not start")
            time.sleep(0.3)

    def make_key(name: str, scopes: list[str], agent: str = "") -> str:
        code = ("import sys; from openbrainstore.auth import agent_keys as k; print(k.get_key_store()"
                ".create(sys.argv[1], sys.argv[2], agent=sys.argv[3], scopes=sys.argv[4].split(','))[1])")
        out = subprocess.run([HOSTED_PY, "-c", code, OWNER, name, agent, ",".join(scopes)], env=env,
                             capture_output=True, text=True, check=True)
        return out.stdout.strip().splitlines()[-1]

    try:
        yield f"http://localhost:{port}/mcp", make_key
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_hosted_end_to_end(tmp_path, server, monkeypatch):
    url, make_key = server
    env = _env(tmp_path)
    env["OBS_ADMIN_KEY"] = make_key("study admin", ["catalog:read", "catalog:propose", "catalog:approve"])
    runs = tmp_path / "runs"
    run(["hosted-setup", "--obs-url", url, "--slots", "2", "--runs-dir", str(runs)], env)
    env["OBS_KEY_1"] = make_key("slot 1", ["catalog:read"], agent="obs-study-slot-1")
    env["OBS_KEY_2"] = make_key("slot 2", ["catalog:read"], agent="obs-study-slot-2")
    common = ["--run", "hs", "--runs-dir", str(runs), "--domain", "retail", "--fake-claude",
              "--concurrency", "2", "--max-invocations", "80", "--obs-url", url]

    run(["learn", *common, "--task-ids", "0", "1", "--trials", "2", "--rounds", "1", "--max-edits", "2"], env)
    ldir = runs / "hs" / "learn"
    state = load(ldir / "state.json")
    assert state["r0"]["score"] == 0.0 and state["r1"]["verdict"] == "promoted", state
    assert load(ldir / "rounds" / "r0" / "hosted_served.json")["refs"] == []
    r1 = [load(p) for p in (ldir / "rounds" / "r1" / "episodes").glob("*.json")]
    assert r1 and all(e["obs"]["refs_fetched"] == ["procedure:s1-finish-confirmed-writes@v1"] for e in r1)
    print("ok  learn: every measurement served from slot 1 of the account; candidate promoted")

    run(["evaluate", *common, "--task-ids", "5", "--k", "1", "--arms", "B,C"], env)
    summary = load(tmp_path / "results" / "hs" / "summary.json")
    p1 = {a: m["pass_hat_k"]["1"]["value"] for a, m in summary["arms"].items()}
    assert p1 == {"B": 0.0, "C": 1.0}, p1
    served = summary["hosted"]["arms"]
    assert served["B"]["slot"] == 1 and served["B"]["refs"] == []
    assert served["C"]["slot"] == 2 and served["C"]["refs"] == ["procedure:s2-finish-confirmed-writes@v1"]
    print("ok  evaluate: B from an empty slot 1, C from slot 2; C passes, B fails")

    # an agent outside the slots (the owner's own agents) never sees the study's skills
    from tau2_skills.hosted import HostedCatalog
    monkeypatch.setenv("OBS_OUTSIDE_KEY", make_key("someone's own agent", ["catalog:read"]))
    import json
    start = json.loads(HostedCatalog(url, tmp_path / "x.json").call("OBS_OUTSIDE_KEY", [
        ("agent_startup", {"task": "anything", "format": "json"})])[0])
    assert not start.get("guardrails") and not start.get("index"), start
    print("ok  the study's skills are visible only to their slots")
