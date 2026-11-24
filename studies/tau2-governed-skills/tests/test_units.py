"""Fast checks of the metric, pricing, pacing and leakage pieces (no tau2 runs)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tau2_skills.common import cost_of_model_usage  # noqa: E402
from tau2_skills.leakage import build_denylist, screen  # noqa: E402
from tau2_skills.metrics import paired_diff, pass_hat_k, pass_hat_k_task  # noqa: E402
from tau2_skills.runner import Pacer, PlanLimitStop, utilization  # noqa: E402
from tau2_skills.claude_cli import RateLimited  # noqa: E402


def test_pass_hat_k():
    assert pass_hat_k_task([True, True, False, False], 1) == 0.5
    assert pass_hat_k_task([True, True, True, False], 2) == pytest.approx(3 / 6)
    assert pass_hat_k_task([True] * 4, 4) == 1.0
    assert pass_hat_k_task([True, False], 4) is None            # too few trials for k
    assert pass_hat_k({"a": [True, True], "b": [False, True]}, 2) == 0.5
    d = paired_diff({"a": [True, True], "b": [True, False]}, {"a": [False, False], "b": [True, False]}, 1)
    assert d["diff"] == pytest.approx(0.5) and d["n_tasks"] == 2


def test_cost_matches_cli():
    # modelUsage from the real smoke episode (Claude Code 2.1.316): the CLI reported 0.00667242
    mu = {"inputTokens": 20, "outputTokens": 2947, "cacheReadInputTokens": 133932,
          "cacheCreationInputTokens": 19288, "cacheCreationEphemeral1hInputTokens": 19288,
          "cacheCreationEphemeral5mInputTokens": 0}
    assert cost_of_model_usage("claude-haiku-5-5", mu) == pytest.approx(0.00667242, rel=1e-6)


def test_pacer_stops_on_utilization_and_long_pause(tmp_path):
    info = {"status": "allowed", "unifiedWindows": {"seven_day": {"utilization": 0.93, "resetsAt": 1}}}
    assert utilization(info) == (0.93, 1)
    p = Pacer(stop_at_utilization=0.9, log_path=tmp_path / "p.jsonl")
    p.observe({"agent": {"usage": {"rate_limit_info": info}}})
    assert p.stop_reason and "93%" in p.stop_reason
    p2 = Pacer(max_pause=60)
    import time
    with pytest.raises(PlanLimitStop):
        p2.trip(RateLimited("usage limit reached", reset_at=time.time() + 7200))


def test_leakage_screen():
    task = {"id": "7", "user_scenario": {"instructions": {"known_info": "You are Mei Kovacs, mei_kovacs_8020, zip 28236."}},
            "evaluation_criteria": {"actions": [{"name": "get_order_details", "arguments": {"order_id": "#W6390527"}}]}}
    dl = build_denylist([task])
    assert screen("Always confirm before a write.", dl) == []
    hits = screen("Look up Kovacs (mei_kovacs_8020) and order #W6390527 in 28236; see task 7.", dl)
    for s in ("Kovacs", "mei_kovacs_8020", "#W6390527", "28236", "task 7"):
        assert any(s.lower() in h.lower() for h in hits), (s, hits)


def test_redact_masks_test_only_strings_and_screen_comes_back_clean():
    from tau2_skills.leakage import Denylist, redact, screen
    dl = Denylist()
    dl.literals = {"3821016478", "#w1234567"}
    dl.names = {"Mia", "Garcia"}  # single name tokens, as task_denylist builds them
    text = "Looked up product 3821016478 for Mia Garcia; order #W1234567. Keep 38210164780."
    out, n = redact(text, dl)
    assert n == 4
    assert "3821016478 " not in out and "Mia" not in out and "Garcia" not in out and "#W1234567" not in out
    assert "38210164780" in out          # boundary: a longer number is not touched
    assert screen(out, dl, generic=False) == []
