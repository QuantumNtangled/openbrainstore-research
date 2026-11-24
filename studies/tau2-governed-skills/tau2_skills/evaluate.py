"""Arms A-D on the TEST split, k trials per task, then summary.json + summary.md.

  A  Claude Code + tau2 tools
  B  A + OBS (agent_startup, get_skill) with an EMPTY catalog, + the OBS prompt snippet
  C  A + OBS with the promoted catalog from the learning run, + the OBS prompt snippet
  D  A + the promoted skill text in the appended system prompt, no OBS"""

import hashlib
import json
import time
from pathlib import Path

from .common import RESULTS_DIR, TAU2_COMMIT, TAU2_TAG, read_json, write_json
from .episode import ArmSetup
from .harness import build_harness
from .metrics import by_task, paired_diff, summarize_arm
from .obs import ObsStore
from .splits import load_split_tasks

PAIRS = (("C", "B"), ("C", "A"), ("D", "A"), ("C", "D"), ("B", "A"))


def prepare_arm(name: str, edir: Path, catalog: ObsStore, recall: bool) -> ArmSetup:
    tools = ("agent_startup", "get_skill") + (("recall",) if recall else ())
    if name == "A":
        return ArmSetup("A")
    if name == "B":
        store = ObsStore(edir / "obs_B")
        if store.items():
            raise SystemExit(f"{store.data_dir} is not empty; arm B needs an empty catalog")
        return ArmSetup("B", obs_dir=store.data_dir, obs_tools=tools)
    if name == "C":
        store = ObsStore(edir / "obs_C")
        if not (edir / "obs_C_ready").exists():
            catalog.copy_catalog_to(store)
            (edir / "obs_C_ready").write_text("1")
        return ArmSetup("C", obs_dir=store.data_dir, obs_tools=tools)
    if name == "D":
        path = edir / "arm_D_skills.md"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(catalog.promoted_markdown(), encoding="utf-8")
        return ArmSetup("D", skills_md=path.read_text(encoding="utf-8"))
    raise SystemExit(f"unknown arm {name}")


def catalog_snapshot(catalog: ObsStore) -> dict:
    items = [i for i in catalog.items(status="promoted") if i["kind"] != "agent"]
    refs = sorted(i["ref"] for i in items)
    return {"refs": refs, "hash": hashlib.sha256("\n".join(refs).encode()).hexdigest()[:12],
            "chars": sum(len(i["body"]) for i in items)}


def cmd_evaluate(args) -> None:
    h = build_harness(args)
    edir = h.run_dir / "eval"
    catalog = ObsStore(Path(args.catalog) if args.catalog else h.run_dir / "learn" / "obs")
    snap = catalog_snapshot(catalog)
    frozen = read_json(edir / "catalog_snapshot.json")
    if frozen and frozen["hash"] != snap["hash"]:
        raise SystemExit("the learned catalog changed since this evaluation started; use a new --run")
    write_json(edir / "catalog_snapshot.json", snap)
    if not snap["refs"]:
        print("warning: the catalog has no promoted items, so C equals B and D equals A", flush=True)
    test = load_split_tasks(args.domain, "test", limit=args.test_limit, seed=args.sample_seed,
                            only=args.task_ids)
    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    if args.arm_order == "interleave":
        # one arm after another per task keeps time-of-day and rate-limit effects balanced
        setups = {a: prepare_arm(a, edir, catalog, args.recall) for a in arms}
        for task in test:
            for a in arms:
                h.batch([task], args.k, setups[a], edir / a, extra={"split": "test"})
    else:
        for a in arms:
            h.batch(test, args.k, prepare_arm(a, edir, catalog, args.recall), edir / a,
                    extra={"split": "test"})
    write_summary(args.run, h.run_dir, arms, args.k, extra={"catalog": snap,
                                                            "claude_invocations": h.budget.n})


def load_arm(run_dir: Path, arm: str) -> list[dict]:
    return [read_json(p) for p in sorted((run_dir / "eval" / arm).glob("*.json"))]


def write_summary(run: str, run_dir: Path, arms: list[str], k: int, extra: dict | None = None) -> dict:
    data = {a: load_arm(run_dir, a) for a in arms}
    learn_state = read_json(run_dir / "learn" / "state.json", {}) or {}
    cfg = read_json(run_dir / "config.evaluate.json", {}) or {}
    summary = {
        "run": run, "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "benchmark": {"name": "tau2-bench", "tag": TAU2_TAG, "commit": TAU2_COMMIT,
                      "grading": "tau2 v1.0.1 reward basis minus NL_ASSERTION (no judge model)"},
        "domain": cfg.get("args", {}).get("domain"), "k": k,
        "models": {"agent": cfg.get("agent", {}).get("model"), "agent_effort": cfg.get("agent", {}).get("effort"),
                   "user": cfg.get("user", {}).get("model"), "user_effort": cfg.get("user", {}).get("effort")},
        "learning": {key: learn_state.get(key) for key in ("train_ids", "trials", "r0", "current", "final")},
        "arms": {a: summarize_arm(eps, k) for a, eps in data.items()},
        "paired": {},
        **(extra or {}),
    }
    tasks = {a: by_task(eps) for a, eps in data.items()}
    for x, y in PAIRS:
        if x in tasks and y in tasks:
            summary["paired"][f"{x}-{y}"] = {str(kk): paired_diff(tasks[x], tasks[y], kk)
                                             for kk in sorted({1, k})}
    out = RESULTS_DIR / run
    write_json(out / "summary.json", summary)
    (out / "summary.md").write_text(render_md(summary), encoding="utf-8")
    print(render_md(summary))
    return summary


def _fmt(v, ci=None) -> str:
    if v is None:
        return "n/a"
    s = f"{v:.3f}"
    if ci and ci[0] is not None:
        s += f" [{ci[0]:.3f}, {ci[1]:.3f}]"
    return s


def render_md(s: dict) -> str:
    k = s["k"]
    lines = [f"# {s['run']}: tau2-bench {s['benchmark']['tag']} {s.get('domain') or ''} TEST, k={k}", "",
             f"Agent {s['models']['agent']} (effort {s['models']['agent_effort']}), user simulator "
             f"{s['models']['user']} (effort {s['models']['user_effort']}). Grading: {s['benchmark']['grading']}.",
             "", f"| arm | tasks | episodes | pass^1 [95% CI] | pass^{k} [95% CI] | infra err | "
                 "startup rate | get_skill rate | API-eq $ total | $ / episode |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for a, m in s["arms"].items():
        p1, pk = m["pass_hat_k"].get("1", {}), m["pass_hat_k"].get(str(k), {})
        o = m.get("obs") or {}
        lines.append(f"| {a} | {m['n_tasks']} | {m['n_episodes']} | {_fmt(p1.get('value'), p1.get('ci95'))} | "
                     f"{_fmt(pk.get('value'), pk.get('ci95'))} | {m['infra_errors']} | "
                     f"{_fmt(o.get('startup_rate'))} | {_fmt(o.get('get_skill_rate'))} | "
                     f"{m['cost']['cost_usd_est_total']:.4f} | {m['cost']['cost_usd_est_per_episode']:.4f} |")
    if s.get("paired"):
        lines += ["", "| difference | k | tasks | mean diff [95% CI] | bootstrap share > 0 |", "|---|---|---|---|---|"]
        for name, byk in s["paired"].items():
            for kk, d in byk.items():
                if d.get("n_tasks"):
                    lines.append(f"| {name} | {kk} | {d['n_tasks']} | {_fmt(d['diff'], d['ci95'])} | "
                                 f"{d['share_bootstrap_above_0']:.3f} |")
    lines += ["", "Costs are API-equivalent (the runs use a Claude subscription). Claude Code's own "
              "system prompt is part of every agent call; the mean first-call prompt size per arm is "
              "in summary.json (`cost.agent_first_call_prompt_tokens_mean`)."]
    return "\n".join(lines) + "\n"


def cmd_summarize(args) -> None:
    run_dir = Path(args.runs_dir) / args.run
    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    write_summary(args.run, run_dir, arms, args.k)
