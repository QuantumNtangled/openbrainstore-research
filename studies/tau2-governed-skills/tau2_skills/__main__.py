"""python -m tau2_skills <command> ...   (see ../README.md)"""

import argparse

from .common import DEFAULT_MODEL, ensure_utf8_mode, quiet_tau2_logs


def main() -> None:
    ensure_utf8_mode()
    quiet_tau2_logs()          # before tau2 is imported: it logs its registry at import time
    p = argparse.ArgumentParser(prog="tau2_skills")
    sub = p.add_subparsers(dest="command", required=True)
    from .harness import add_common_args

    def hosted_args(sp):
        from .obs import HOSTED_URL
        sp.add_argument("--obs-url", nargs="?", const=HOSTED_URL, default=None, metavar="URL",
                        help=f"agents use your hosted OpenBrainStore account instead of a local "
                             f"server (bare flag: {HOSTED_URL}); see `hosted-setup` for the keys")
        sp.add_argument("--obs-key-env", default="OBS_AGENT_KEY", metavar="NAME",
                        help="with --obs-url and no catalog to publish, arm C uses your account's own "
                             "catalog through the agent key in this environment variable")

    def task_args(sp):
        sp.add_argument("--task-ids", nargs="*", default=None, help="explicit task ids (must be in the split)")
        sp.add_argument("--sample-seed", type=int, default=0, help="seed for --*-limit sampling")

    sp = sub.add_parser("learn", help="learning loop on the TRAIN split")
    add_common_args(sp)
    hosted_args(sp)
    task_args(sp)
    sp.add_argument("--train-limit", type=int, default=None, help="use a seeded sample of N train tasks")
    sp.add_argument("--trials", type=int, default=3, help="trials per train task per measurement (T)")
    sp.add_argument("--rounds", type=int, default=3)
    sp.add_argument("--max-edits", type=int, default=3, help="proposals considered per round")
    sp.add_argument("--max-failures", type=int, default=8, help="failed episodes shown to the improver")
    sp.add_argument("--gate-z", type=float, default=1.645)
    sp.add_argument("--min-delta", type=float, default=0.02)
    sp.add_argument("--remeasure-current", action="store_true",
                    help="re-run the current catalog each round instead of reusing its last score")
    sp.add_argument("--improver-model", default=DEFAULT_MODEL)
    sp.add_argument("--improver-effort", default="medium")
    sp.add_argument("--review", action="store_true",
                    help="reviewed arm: pause each round for a human approve/edit/reject decision "
                         "per proposal (rounds/<r>/review_decisions.json) before the gate")
    sp.add_argument("--reviewer", default=None,
                    help="recorded as who reviewed (default human:reviewer, or model:<M> with --reviewer-model)")
    sp.add_argument("--reviewer-model", default=None,
                    help="a model takes the reviewer's seat (implies --review): it decides approve, "
                         "edit or reject per proposal instead of pausing for a person")
    sp.add_argument("--reviewer-effort", default="medium")
    sp.add_argument("--seed-from", default=None,
                    help="another learn run with the same config: reuse its round-0 baseline and "
                         "its round-1 improver proposals, so this run starts from the same point")

    sp = sub.add_parser("evaluate", help="arms A-D on the TEST split")
    add_common_args(sp)
    task_args(sp)
    sp.add_argument("--arms", default="A,B,C,D")
    sp.add_argument("--k", type=int, default=4, help="trials per test task")
    sp.add_argument("--test-limit", type=int, default=None)
    sp.add_argument("--catalog", action="append", default=None, metavar="[NAME=]DIR",
                    help="OBS data dir of a learned catalog (default: runs/<run>/learn/obs). "
                         "Repeat with NAME=DIR to compare catalogs in one interleaved run: arms "
                         "C:NAME and D:NAME use catalog NAME, and plain C/D use the unnamed one")
    sp.add_argument("--recall", action="store_true", help="also offer OBS recall in arms B and C")
    sp.add_argument("--arm-order", default="interleave", choices=["interleave", "sequential"])
    hosted_args(sp)

    sp = sub.add_parser("summarize", help="rebuild results/<run>/summary.* from episode files")
    sp.add_argument("--run", required=True)
    sp.add_argument("--arms", default="A,B,C,D", help="as in evaluate, e.g. A,B,C:rev,D:rev")
    sp.add_argument("--k", type=int, default=4)
    from .common import RUNS_DIR
    sp.add_argument("--runs-dir", default=str(RUNS_DIR))

    sp = sub.add_parser("episode", help="one arm on explicit tasks (smoke tests, debugging)")
    add_common_args(sp)
    sp.add_argument("--split", default="train", choices=["train", "test"])
    sp.add_argument("--task-ids", nargs="+", default=None)
    sp.add_argument("--limit", type=int, default=None,
                    help="seeded sample of N tasks (same sample as learn --train-limit N)")
    sp.add_argument("--sample-seed", type=int, default=0)
    sp.add_argument("--arm", default="C", choices=["A", "B", "C", "D", "a", "b", "c", "d"])
    sp.add_argument("--trials", type=int, default=1)
    sp.add_argument("--catalog", default=None, help="OBS data dir to serve (e.g. runs/<run>/learn/obs)")
    sp.add_argument("--catalog-import", default=None, help="directory with INDEX.md + item files")
    hosted_args(sp)

    sp = sub.add_parser("hosted-setup", help="create the agent profiles a hosted run uses, and list its keys")
    from .common import RUNS_DIR as _RD
    from .obs import HOSTED_URL as _HU
    sp.add_argument("--obs-url", nargs="?", const=_HU, default=_HU, metavar="URL")
    sp.add_argument("--slots", type=int, default=4)
    sp.add_argument("--runs-dir", default=str(_RD))
    sp.add_argument("--keys-only", action="store_true", help="only print which keys to create")

    sp = sub.add_parser("estimate", help="token / invocation / API-equivalent cost estimate")
    sp.add_argument("--cc-overhead", type=int, default=6_000,
                    help="Claude Code's own prompt tokens per agent call (measure in the pilot)")
    sp.add_argument("--trials", type=int, default=3)
    sp.add_argument("--rounds", type=int, default=3)
    sp.add_argument("--k", type=int, default=4)
    sp.add_argument("--agent-model", default=DEFAULT_MODEL)
    sp.add_argument("--improver-model", default=DEFAULT_MODEL)

    sp = sub.add_parser("splits", help="print the official split counts")

    args = p.parse_args()
    from .runner import PlanLimitStop
    try:
        dispatch(args)
    except PlanLimitStop as e:
        print(f"\nSTOPPED: {e}\nEvery finished episode is checkpointed. Rerun the same command to resume.")
        raise SystemExit(3)


def dispatch(args) -> None:
    if args.command == "learn":
        from .learn import cmd_learn
        cmd_learn(args)
    elif args.command == "evaluate":
        from .evaluate import cmd_evaluate
        cmd_evaluate(args)
    elif args.command == "summarize":
        from .evaluate import cmd_summarize
        cmd_summarize(args)
    elif args.command == "episode":
        from .single import cmd_episode
        cmd_episode(args)
    elif args.command == "hosted-setup":
        from .evaluate import hosted_state_path
        from .hosted import HostedCatalog, setup_help
        if not args.keys_only:
            made = HostedCatalog(args.obs_url, hosted_state_path(args)).setup(args.slots)
            print(f"profiles created: {', '.join(made) or 'none (they already exist)'}", end="\n\n")
        print(setup_help(args.obs_url, args.slots))
    elif args.command == "estimate":
        from .estimate import cmd_estimate
        cmd_estimate(args)
    elif args.command == "splits":
        from .common import TAU2_COMMIT, TAU2_TAG
        from .splits import official_split
        print(f"tau2-bench {TAU2_TAG} ({TAU2_COMMIT})")
        for d in ("retail", "airline", "telecom"):
            sp_ = official_split(d)
            print(d, {k: len(v) for k, v in sp_.items()} if sp_ else "no official split")


if __name__ == "__main__":
    main()
