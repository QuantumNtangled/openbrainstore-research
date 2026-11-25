"""`episode`: run one arm on a few explicit tasks. Used for the real-model smoke test and for
debugging a single conversation. Arm C here serves a catalog imported from a directory
(INDEX.md + item files, the `obs catalog import` format) instead of a learning run."""

import json
from pathlib import Path

from .evaluate import hosted_arms, prepare_arm
from .harness import build_harness
from .metrics import summarize_arm
from .obs import ObsStore
from .splits import load_split_tasks


def cmd_episode(args) -> None:
    h = build_harness(args)
    if not args.task_ids and args.limit is None:
        raise SystemExit("give --task-ids or --limit")
    tasks = load_split_tasks(args.domain, args.split, limit=args.limit, seed=args.sample_seed,
                             only=args.task_ids)
    edir = h.run_dir / "episode"
    if getattr(args, "obs_url", None):
        catalog = None
        if args.catalog:
            catalog = ObsStore(Path(args.catalog))
        elif args.catalog_import:
            catalog = ObsStore(edir / "catalog_src")
            if not catalog.items():
                catalog.import_dir(Path(args.catalog_import))
        a = args.arm.upper()
        setups, _ = hosted_arms([a], args, {"": catalog} if catalog else {}, edir, own=catalog is None)
        return _report(h.batch(tasks, args.trials, setups[a], edir / a, extra={"split": args.split}),
                       args, h)
    if args.catalog:
        catalog = ObsStore(Path(args.catalog))
    else:
        catalog = ObsStore(edir / "catalog_src")
        if args.catalog_import and not catalog.items():
            catalog.import_dir(Path(args.catalog_import))
    arm = prepare_arm(args.arm.upper(), edir, catalog, recall=False)
    _report(h.batch(tasks, args.trials, arm, edir / arm.name, extra={"split": args.split}), args, h)


def _report(eps, args, h) -> None:
    summary = summarize_arm(eps, args.trials)
    for e in eps:
        a = e.get("agent") or {}
        print(json.dumps({
            "task": e["task_id"], "trial": e["trial"], "status": e.get("status"), "reward": e.get("reward"),
            "termination": e.get("termination"), "obs": e.get("obs"), "wall_s": e.get("wall_s"),
            "agent_usage": a.get("usage"), "user_usage": (e.get("user") or {}).get("usage"),
            "agent_init": a.get("init"), "error": e.get("error")}, indent=1))
    print(json.dumps({"pass^1": summary["pass_hat_k"]["1"], "cost": summary["cost"],
                      "claude_invocations_this_run": h.budget.n}, indent=1))
