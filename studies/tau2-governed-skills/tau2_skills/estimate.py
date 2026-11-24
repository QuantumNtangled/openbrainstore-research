"""Back-of-envelope token, invocation and API-equivalent cost estimates.

Prompt sizes come from the pinned tau2 data (policy, tool schemas, user prompts; chars/4
tokens). The per-episode shape (API calls, output and growth per call) is calibrated on the
one real smoke episode (retail train task 0, arm C, Haiku 5.5): 10 agent API calls, first
prompt 12,095 tokens, ~800 new tokens cached per call, ~300 output tokens per call (thinking
included); 4 user calls, ~220 output tokens each. Longer domains are scaled up by assumption.
Re-run with the pilot's numbers before committing to the full run."""

import json

from .common import CACHE_WRITE_1H_X, PRICES

ASSUMED = {   # per episode; telecom routes many device steps through the user simulator
    "retail": {"agent_calls": 14, "user_calls": 6, "out_agent": 300, "out_user": 220},
    "airline": {"agent_calls": 12, "user_calls": 6, "out_agent": 300, "out_user": 220},
    "telecom": {"agent_calls": 30, "user_calls": 16, "out_agent": 300, "out_user": 220},
}
GROWTH_AGENT = 800      # new tokens per agent call (measured: 7,195 over 9 calls)
GROWTH_USER = 800       # the agent's replies are long Markdown (measured: 2,458 over 3 calls)
USER_CC_OVERHEAD = 950  # measured: 1,557-token first user-sim prompt vs ~600 of tau2 prompt
ARM_EXTRA = {"A": 0, "B": 700, "C": 1500, "D": 1000}   # OBS tool schemas / fetched or pasted skills
SPLITS = {"retail": (74, 40), "airline": (30, 20), "telecom": (74, 40)}


def domain_sizes(domain: str) -> dict:
    from tau2.runner import build_environment
    from tau2.user.user_simulator import get_global_user_sim_guidelines
    env = build_environment(domain)
    tools = json.dumps([t.openai_schema for t in env.get_tools()])
    try:
        utools = env.get_user_tools() or []
    except Exception:
        utools = []
    return {"policy_tokens": len(env.get_policy()) // 4, "tool_tokens": len(tools) // 4,
            "user_prompt_tokens": len(get_global_user_sim_guidelines(bool(utools))) // 4 + 300,
            "user_tool_tokens": len(json.dumps([t.openai_schema for t in utools])) // 4}


def process_cost(model: str, prefix: int, calls: int, growth: int, out: int) -> tuple[float, int]:
    """One persistent claude process. Claude Code caches with the 1-hour TTL on a subscription
    (writes at 2x input): call 1 writes the prefix; each later call reads the cached prefix +
    history and writes the new growth."""
    p = PRICES[model]
    rin, rout = p["in"], p["out"]
    tokens = prefix
    cost = prefix * rin * CACHE_WRITE_1H_X
    for i in range(2, calls + 1):
        read = prefix + (i - 2) * growth
        cost += read * rin * p["cache_read_x"] + growth * rin * CACHE_WRITE_1H_X
        tokens += read + growth
    cost += calls * out * rout
    return cost / 1e6, tokens + calls * out


def episode(domain: str, arm: str, sizes: dict, cc_overhead: int, agent_model: str, user_model: str):
    s = ASSUMED[domain]
    prefix = cc_overhead + sizes["policy_tokens"] + sizes["tool_tokens"] + 200 + ARM_EXTRA[arm]
    ac, at = process_cost(agent_model, prefix, s["agent_calls"], GROWTH_AGENT, s["out_agent"])
    uprefix = USER_CC_OVERHEAD + sizes["user_prompt_tokens"] + sizes["user_tool_tokens"]
    uc, ut = process_cost(user_model, uprefix, s["user_calls"], GROWTH_USER, s["out_user"])
    return ac + uc, at + ut, s["agent_calls"] + s["user_calls"]


def table(cc_overhead: int, train_trials: int, rounds: int, k: int, agent_model: str,
          improver_model: str) -> str:
    p = PRICES[improver_model]
    improver_cost = (25_000 * p["in"] + 4_000 * p["out"]) / 1e6   # ~25K prompt, ~4K out incl. thinking
    lines = [f"Assumptions: Claude Code's own prompt {cc_overhead:,} tokens per agent call; train T="
             f"{train_trials} trials per measurement, {rounds} learning rounds, test k={k} x 4 arms; "
             f"agent {agent_model}, improver {improver_model}; tokens = chars/4 for tau2 text.", "",
             "| user sim | domain | train/test | $ / episode | tokens / episode | API calls / episode | "
             "train baseline $ | per learning round $ | test, 4 arms $ | domain total $ | episodes | "
             "claude invocations |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    totals = {}
    for user_model in ("claude-haiku-5-5", "claude-sonnet-5-5"):
        tot_cost = tot_eps = tot_inv = tot_tok = 0
        for domain in ("retail", "airline", "telecom"):
            sizes = domain_sizes(domain)
            n_train, n_test = SPLITS[domain]
            c_cost, c_tok, c_calls = episode(domain, "C", sizes, cc_overhead, agent_model, user_model)
            base_eps = n_train * train_trials
            base = base_eps * c_cost
            per_round = base + improver_cost
            test_cost = test_tok = 0.0
            for arm in "ABCD":
                ec, et, _ = episode(domain, arm, sizes, cc_overhead, agent_model, user_model)
                test_cost += n_test * k * ec
                test_tok += n_test * k * et
            eps = base_eps * (1 + rounds) + 4 * n_test * k
            inv = 2 * eps + rounds
            total = base + rounds * per_round + test_cost
            tot_cost += total
            tot_eps += eps
            tot_inv += inv
            tot_tok += base_eps * (1 + rounds) * c_tok + test_tok
            lines.append(f"| {user_model} | {domain} | {n_train}/{n_test} | {c_cost:.4f} | {c_tok:,} | {c_calls} | "
                         f"{base:.2f} | {per_round:.2f} | {test_cost:.2f} | {total:.2f} | {eps:,} | {inv:,} |")
        totals[user_model] = (tot_cost, tot_eps, tot_inv, tot_tok)
    lines.append("")
    for um, (c, e, i, t) in totals.items():
        lines.append(f"- All three domains, user simulator {um}: ${c:.2f} API-equivalent, {e:,} episodes, "
                     f"{i:,} claude invocations, ~{t / 1e6:.0f}M tokens processed (mostly cache reads).")
    return "\n".join(lines)


def cmd_estimate(args) -> None:
    print(table(args.cc_overhead, args.trials, args.rounds, args.k, args.agent_model, args.improver_model))
