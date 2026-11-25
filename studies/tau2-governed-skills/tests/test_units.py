"""Fast checks of the metric, pricing, pacing and leakage pieces (no tau2 runs)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tau2_skills.common import cost_of_model_usage  # noqa: E402
from tau2_skills.leakage import build_denylist, screen  # noqa: E402
from tau2_skills.metrics import paired_diff, pass_hat_k, pass_hat_k_task  # noqa: E402
from tau2_skills.runner import Pacer, PlanLimitStop, utilization  # noqa: E402
from tau2_skills.claude_cli import RateLimited, window_reset  # noqa: E402


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


def test_overload_is_not_the_weekly_reset():
    # a 529 "overloaded" while the plan windows are still allowed: back off, don't wait for
    # the weekly reset (this stopped the tel-or run for 2.5 days at 15% utilization)
    allowed = {"status": "allowed", "resetsAt": 1795842000, "rateLimitType": "seven_day",
               "unifiedWindows": {"seven_day": {"utilization": 0.15, "resetsAt": 1795842000,
                                                "status": "allowed"}}}
    assert window_reset(allowed) is None and window_reset(None) is None
    hit = {"status": "rejected", "resetsAt": 1795000000, "rateLimitType": "five_hour",
           "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": 1795000000,
                                            "status": "rejected"},
                              "seven_day": {"utilization": 0.15, "resetsAt": 1795842000,
                                            "status": "allowed"}}}
    assert window_reset(hit) == 1795000000.0
    p = Pacer(max_pause=3 * 3600)
    assert p.trip(RateLimited("API Error: 529 Overloaded", window_reset(allowed))) < 120


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


def test_hosted_obs_never_holds_the_key():
    """--obs-url: the agent's MCP config carries a ${VAR} placeholder that Claude Code expands
    from its environment, so a key never lands in a run's files; each setup has its own slot."""
    from tau2_skills.hosted import _fingerprint, slot_key_env, slot_profile, slot_slug
    from tau2_skills.obs import HOSTED_URL, obs_http_server

    assert obs_http_server(HOSTED_URL, slot_key_env(2)) == {
        "type": "http", "url": "https://openbrainstore.com/mcp",
        "headers": {"Authorization": "Bearer ${OBS_KEY_2}"}}
    assert (slot_profile(3), slot_slug(3, "data-limit-refuel-check")) == (
        "obs-study-slot-3", "s3-data-limit-refuel-check")
    a = {"kind": "guardrail", "title": "T", "when_to_use": "W", "body": "B"}
    assert _fingerprint(a) == _fingerprint(dict(a)) != _fingerprint({**a, "body": "B2"})
