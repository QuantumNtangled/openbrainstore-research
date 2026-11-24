"""pass^k (tau-bench), task-level bootstrap CIs, paired arm differences, cost and OBS usage."""

import random
from collections import defaultdict
from math import comb
from statistics import mean, pstdev

ROLES = ("agent", "user")


def valid(episodes: list[dict]) -> list[dict]:
    """Infra errors are excluded from the metrics (as tau2 does), and reported separately."""
    return [e for e in episodes if e.get("status") in ("ok", "budget_exceeded")]


def by_task(episodes: list[dict]) -> dict[str, list[bool]]:
    out = defaultdict(list)
    for e in valid(episodes):
        out[str(e["task_id"])].append(bool(e.get("success")))
    return dict(out)


def pass_hat_k_task(results: list[bool], k: int) -> float | None:
    """C(c, k) / C(n, k): the chance that k trials drawn without replacement all succeed."""
    n, c = len(results), sum(results)
    if n < k:
        return None
    return comb(c, k) / comb(n, k)


def pass_hat_k(tasks: dict[str, list[bool]], k: int) -> float | None:
    vals = [v for v in (pass_hat_k_task(r, k) for r in tasks.values()) if v is not None]
    return mean(vals) if vals else None


def bootstrap(tasks: dict[str, list[bool]], k: int, reps: int = 2000, seed: int = 7) -> tuple:
    ids = [t for t, r in tasks.items() if len(r) >= k]
    if not ids:
        return (None, None)
    rng = random.Random(seed)
    stats = []
    for _ in range(reps):
        sample = [ids[rng.randrange(len(ids))] for _ in ids]
        stats.append(mean(pass_hat_k_task(tasks[t], k) for t in sample))
    stats.sort()
    return (stats[int(0.025 * reps)], stats[int(0.975 * reps) - 1])


def paired_diff(a: dict[str, list[bool]], b: dict[str, list[bool]], k: int, reps: int = 2000,
                seed: int = 11) -> dict:
    """Arm a minus arm b, bootstrapping over the tasks both arms have >= k trials for."""
    ids = sorted(t for t in a if t in b and len(a[t]) >= k and len(b[t]) >= k)
    if not ids:
        return {"k": k, "n_tasks": 0}
    d = {t: pass_hat_k_task(a[t], k) - pass_hat_k_task(b[t], k) for t in ids}
    rng = random.Random(seed)
    stats = sorted(mean(d[ids[rng.randrange(len(ids))]] for _ in ids) for _ in range(reps))
    lo, hi = stats[int(0.025 * reps)], stats[int(0.975 * reps) - 1]
    return {"k": k, "n_tasks": len(ids), "diff": mean(d.values()), "ci95": [lo, hi],
            "share_bootstrap_above_0": sum(s > 0 for s in stats) / reps}


def trial_sd(episodes: list[dict]) -> tuple[float, int]:
    """SD across trials of the per-trial mean success rate (the gate's noise estimate)."""
    per_trial = defaultdict(list)
    for e in valid(episodes):
        per_trial[e["trial"]].append(bool(e.get("success")))
    means = [mean(v) for v in per_trial.values() if v]
    if len(means) < 2:
        return 0.0, len(means)
    # sample SD
    m = mean(means)
    return (sum((x - m) ** 2 for x in means) / (len(means) - 1)) ** 0.5, len(means)


def cost_block(episodes: list[dict]) -> dict:
    out = {"episodes": len(episodes)}
    for role in ROLES:
        tot = defaultdict(float)
        for e in episodes:
            u = (e.get(role) or {}).get("usage") or {}
            for k in ("api_calls", "input_tokens", "cache_creation_input_tokens",
                      "cache_read_input_tokens", "output_tokens", "cost_usd_est"):
                tot[k] += float(u.get(k) or 0)
            tot["cli_total_cost_usd"] += float(u.get("cli_total_cost_usd_max") or 0)
            tot["claude_processes"] += 1 if (e.get(role) or {}).get("started") else 0
        out[role] = {k: round(v, 6) for k, v in tot.items()}
    out["cost_usd_est_total"] = round(sum(out[r]["cost_usd_est"] for r in ROLES), 6)
    out["cli_total_cost_usd_total"] = round(sum(out[r]["cli_total_cost_usd"] for r in ROLES), 6)
    n = max(1, len(episodes))
    out["cost_usd_est_per_episode"] = round(out["cost_usd_est_total"] / n, 6)
    firsts = [((e.get("agent") or {}).get("usage") or {}).get("first_call_prompt_tokens") for e in episodes]
    firsts = [f for f in firsts if f]
    out["agent_first_call_prompt_tokens_mean"] = round(mean(firsts), 1) if firsts else None
    return out


def obs_block(episodes: list[dict]) -> dict:
    v = valid(episodes)
    if not v:
        return {}
    return {
        "startup_rate": mean(1.0 if (e.get("obs") or {}).get("startup_calls") else 0.0 for e in v),
        "get_skill_rate": mean(1.0 if (e.get("obs") or {}).get("get_skill_calls") else 0.0 for e in v),
        "mean_get_skill_calls": mean((e.get("obs") or {}).get("get_skill_calls") or 0 for e in v),
    }


def summarize_arm(episodes: list[dict], k_max: int) -> dict:
    tasks = by_task(episodes)
    v = valid(episodes)
    out = {"n_tasks": len(tasks), "n_episodes": len(v),
           "infra_errors": sum(1 for e in episodes if e.get("status") == "infra_error"),
           "budget_exceeded": sum(1 for e in episodes if e.get("status") == "budget_exceeded"),
           "empty_agent_replies": sum(((e.get("agent") or {}).get("empty_replies") or 0) for e in v),
           "episodes_with_foreign_emails": sum(1 for e in v if e.get("foreign_emails")),
           "terminations": _count(e.get("termination") for e in v),
           "pass_hat_k": {}}
    for k in range(1, k_max + 1):
        p = pass_hat_k(tasks, k)
        lo, hi = bootstrap(tasks, k)
        out["pass_hat_k"][str(k)] = {"value": p, "ci95": [lo, hi],
                                     "n_tasks": sum(1 for r in tasks.values() if len(r) >= k)}
    out["cost"] = cost_block(episodes)
    out["obs"] = obs_block(episodes)
    return out


def _count(xs) -> dict:
    out = defaultdict(int)
    for x in xs:
        out[str(x)] += 1
    return dict(out)


def mean_success(episodes: list[dict]) -> float | None:
    v = valid(episodes)
    return mean(1.0 if e.get("success") else 0.0 for e in v) if v else None


def per_trial_means(episodes: list[dict]) -> list[float]:
    per = defaultdict(list)
    for e in valid(episodes):
        per[e["trial"]].append(1.0 if e.get("success") else 0.0)
    return [mean(v) for _, v in sorted(per.items())]


__all__ = ["by_task", "pass_hat_k", "bootstrap", "paired_diff", "summarize_arm", "trial_sd",
           "mean_success", "per_trial_means", "pstdev"]
