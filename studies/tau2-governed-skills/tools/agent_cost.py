"""Agent-only token cost per conversation for each arm of an evaluation run, as measured, and the
same token counts re-priced at another model's rates.

    python tools/agent_cost.py tel-test [--runs-dir runs] [--reprice claude-opus-5-5]

The measured figure is the Claude Code CLI's own cost for the agent's process. The re-priced
figure answers "what would these conversations cost if that model used the same tokens", which
is an estimate: a different model takes a different number of turns and writes different
amounts. The simulated customer is left out; in a deployment the customer is a person.

Prices: Anthropic's list API prices on 2026-11-25, USD per million tokens. Check the live
pricing page (platform.claude.com/docs/en/about-claude/pricing) before relying on them.
"""

import argparse
import glob
import json
import statistics as st
from pathlib import Path

PRICES = {   # input, 5-minute cache write, 1-hour cache write, cache read, output
    "claude-haiku-5-5": dict(i=0.10, w5=0.125, w1=0.20, r=0.01, o=0.50),     # prompts up to 100K
    "claude-sonnet-5-5": dict(i=2.0, w5=2.50, w1=4.0, r=0.10, o=10.0),
    "claude-opus-5-5": dict(i=4.0, w5=5.0, w1=8.0, r=0.20, o=20.0),
}


def priced(u: dict, p: dict) -> float:
    cw = u.get("cache_creation_input_tokens", 0)
    c1 = u.get("cache_creation_1h_input_tokens", cw)
    return (u.get("input_tokens", 0) * p["i"] + (cw - c1) * p["w5"] + c1 * p["w1"]
            + u.get("cache_read_input_tokens", 0) * p["r"] + u.get("output_tokens", 0) * p["o"]) / 1e6


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run")
    ap.add_argument("--runs-dir", type=Path, default=Path(__file__).resolve().parents[1] / "runs")
    ap.add_argument("--reprice", default="claude-opus-5-5", choices=sorted(PRICES))
    args = ap.parse_args()
    edir = args.runs_dir / args.run / "eval"
    arms = sorted(d.name for d in edir.iterdir() if d.is_dir() and any(d.glob("*.json")))
    print(f"{'arm':10s} {'n':>4s} {'pass^1':>7s} {'measured $/conv':>16s} {args.reprice + ' $/conv':>26s} {'ratio':>6s}")
    for arm in arms:
        eps = [json.load(open(f, encoding="utf-8")) for f in glob.glob(str(edir / arm / "*.json"))]
        eps = [e for e in eps if e.get("status") != "infra_error"]
        us = [(e.get("agent") or {}).get("usage") or {} for e in eps]
        measured = st.mean(u.get("cost_usd_est", 0) or 0 for u in us)
        other = st.mean(priced(u, PRICES[args.reprice]) for u in us)
        p1 = st.mean(1.0 if (e.get("reward") or 0) >= 0.999 else 0.0 for e in eps)
        print(f"{arm:10s} {len(eps):4d} {p1:7.3f} {measured:16.5f} {other:26.4f} {other / measured:5.1f}x")


if __name__ == "__main__":
    main()
