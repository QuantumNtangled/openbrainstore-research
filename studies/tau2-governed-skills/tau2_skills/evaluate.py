"""Arms A-D on the TEST split, k trials per task, then summary.json + summary.md.

  A  Claude Code + tau2 tools
  B  A + OBS (agent_startup, get_skill) with an EMPTY catalog, + the OBS prompt snippet
  C  A + OBS with the promoted catalog from the learning run, + the OBS prompt snippet
  D  A + the promoted skill text in the appended system prompt, no OBS

Several learned catalogs can be compared in one interleaved run: `--catalog rev=DIR
--catalog opus=DIR --arms A,B,C:rev,C:opus` runs arm C once per catalog (results under
eval/C-rev and eval/C-opus). Plain C and D use the unnamed --catalog, as before."""

import hashlib
import json
import os
import time
from pathlib import Path

from .common import RESULTS_DIR, TAU2_COMMIT, TAU2_TAG, read_json, write_json
from .episode import ArmSetup
from .harness import build_harness
from .metrics import by_task, paired_diff, summarize_arm
from .obs import ObsStore
from .splits import load_split_tasks

def arm_key(spec: str) -> str:
    """'C:rev' -> 'C-rev' (the arm's label and its eval/ directory); 'a' -> 'A'."""
    letter, _, name = spec.strip().partition(":")
    letter, name = letter.upper(), name.strip()
    if len(letter) != 1 or letter not in "ABCD" or (name and letter in "AB"):
        raise SystemExit(f"unknown arm {spec!r} (A, B, C, D, or C:NAME / D:NAME)")
    return f"{letter}-{name}" if name else letter


def parse_arms(text: str) -> list[str]:
    keys = [arm_key(a) for a in text.split(",") if a.strip()]
    if len(set(keys)) != len(keys):
        raise SystemExit(f"--arms lists an arm twice: {text}")
    return keys


def pairs_for(arms: list[str]) -> list[tuple[str, str]]:
    """Paired differences to report: each C against B, A and its own D; each D against A;
    B against A; and every two C arms against each other (e.g. two catalogs)."""
    have, out = set(arms), []
    cs = [a for a in arms if a[0] == "C"]
    for c in cs:
        d = "D" + c[1:]
        out += [(c, "B"), (c, "A"), (d, "A"), (c, d)]
    out += [(d, "A") for d in arms if d[0] == "D"]
    out.append(("B", "A"))
    out += [(x, y) for i, x in enumerate(cs) for y in cs[i + 1:]]
    seen, res = set(), []
    for pair in out:
        if pair[0] in have and pair[1] in have and pair not in seen:
            seen.add(pair)
            res.append(pair)
    return res


def parse_catalogs(values, default_dir: Path) -> dict[str, Path]:
    """--catalog values -> {name: dir}; the unnamed one is ''. With none given, '' is the
    run's own learned catalog."""
    out: dict[str, Path] = {}
    for v in values or []:
        name, sep, d = v.partition("=")
        name, d = (name.strip(), d) if sep else ("", v)
        if name in out:
            raise SystemExit(f"--catalog {name or '(unnamed)'} given twice")
        out[name] = Path(d)
    if not out:
        out[""] = default_dir
    return out


def prepare_arm(key: str, edir: Path, catalog: "ObsStore | None", recall: bool) -> ArmSetup:
    tools = ("agent_startup", "get_skill") + (("recall",) if recall else ())
    letter = key[0]
    if letter == "A":
        return ArmSetup("A")
    if letter == "B":
        store = ObsStore(edir / "obs_B")
        if store.items():
            raise SystemExit(f"{store.data_dir} is not empty; arm B needs an empty catalog")
        return ArmSetup("B", obs_dir=store.data_dir, obs_tools=tools)
    if catalog is None:
        raise SystemExit(f"arm {key} has no catalog")
    if letter == "C":
        store = ObsStore(edir / f"obs_{key}")
        ready = edir / f"obs_{key}_ready"
        if not ready.exists():
            catalog.copy_catalog_to(store)
            ready.write_text("1")
        return ArmSetup(key, obs_dir=store.data_dir, obs_tools=tools)
    path = edir / f"arm_{key}_skills.md"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(catalog.promoted_markdown(), encoding="utf-8")
    return ArmSetup(key, skills_md=path.read_text(encoding="utf-8"))


def parse_arm_models(values, arms: list[str]) -> dict[str, str]:
    """--arm-model ARM=MODEL values -> {arm key: model}."""
    out = {}
    for v in values or []:
        arm, sep, model = v.partition("=")
        key = arm_key(arm.strip()) if sep else ""
        if not sep or not model.strip() or key not in arms:
            raise SystemExit(f"--arm-model {v!r}: give ARM=MODEL for an arm listed in --arms")
        out[key] = model.strip()
    return out


def hosted_state_path(args) -> Path:
    from urllib.parse import urlparse
    return Path(args.runs_dir) / f"hosted_slots.{urlparse(args.obs_url).netloc.replace(':', '_')}.json"


def hosted_arms(arms: list[str], args, catalogs: dict, edir: Path, own: bool) -> tuple[dict, dict]:
    """Arm setups for a hosted run (--obs-url). A has no OpenBrainStore; D inlines a local
    catalog in the prompt, as in a local run; B and every C get a slot of their own, in
    --arms order, published and checked before any conversation starts. With `own`, plain C
    is your account's own catalog as served to the key in --obs-key-env, published by nobody."""
    from .hosted import HostedCatalog, slot_key_env
    if getattr(args, "recall", False):
        raise SystemExit("--recall isn't available with --obs-url: slot keys only load skills")
    hc = HostedCatalog(args.obs_url, hosted_state_path(args))
    tools = ("agent_startup", "get_skill")
    setups, served, slot = {}, {}, 0
    for a in arms:
        if a == "A" or a[0] == "D":
            setups[a] = prepare_arm(a, edir, catalogs.get(a[2:]) if a[0] == "D" else None, False)
            continue
        if a == "C" and own:
            if not os.environ.get(args.obs_key_env):
                raise SystemExit(f"set {args.obs_key_env} to an OpenBrainStore agent key with Load skills; "
                                 "it is read from the environment only")
            setups[a] = ArmSetup("C", obs_url=args.obs_url, obs_key_env=args.obs_key_env, obs_tools=tools)
            served[a] = {"key_env": args.obs_key_env, "own_catalog": True}
            continue
        slot += 1
        items = [] if a == "B" else catalogs[a[2:]].items(status="promoted")
        slugs = hc.publish(slot, items)
        served[a] = {"slot": slot, **hc.verify(slot, slugs)}
        setups[a] = ArmSetup(a, obs_url=args.obs_url, obs_key_env=slot_key_env(slot), obs_tools=tools)
        print(f"[{a}] slot {slot}: {len(slugs)} item(s) served", flush=True)
    return setups, served


def catalog_snapshot(catalog: ObsStore) -> dict:
    items = [i for i in catalog.items(status="promoted") if i["kind"] != "agent"]
    refs = sorted(i["ref"] for i in items)
    return {"refs": refs, "hash": hashlib.sha256("\n".join(refs).encode()).hexdigest()[:12],
            "chars": sum(len(i["body"]) for i in items)}


def cmd_evaluate(args) -> None:
    arms = parse_arms(args.arms)
    hosted = bool(getattr(args, "obs_url", None))
    h = build_harness(args)
    edir = h.run_dir / "eval"
    # hosted, with no catalog to publish: plain C is the account's own catalog
    own = hosted and not args.catalog and not (h.run_dir / "learn" / "obs").exists()
    if own and args.arms == "A,B,C,D":
        arms = ["A", "C"]
    dirs = parse_catalogs(args.catalog, h.run_dir / "learn" / "obs")
    used = {a[2:] for a in arms if a[0] in "CD" and not (own and a == "C")}
    if used - set(dirs):
        raise SystemExit(f"arms name catalogs with no --catalog: {sorted(used - set(dirs))}")
    catalogs = {n: ObsStore(dirs[n]) for n in sorted(used)}
    snaps = {n: catalog_snapshot(c) for n, c in catalogs.items()}
    frozen = read_json(edir / "catalog_snapshot.json")
    if frozen and "hash" in frozen:            # written by a single-catalog run
        frozen = {"": frozen}
    if frozen and any(n in frozen and frozen[n]["hash"] != sn["hash"] for n, sn in snaps.items()):
        raise SystemExit("a learned catalog changed since this evaluation started; use a new --run")
    merged = {**(frozen or {}), **snaps}
    write_json(edir / "catalog_snapshot.json", merged[""] if set(merged) == {""} else merged)
    for n, sn in snaps.items():
        if not sn["refs"]:
            print(f"warning: catalog {n or '(unnamed)'} has no promoted items, so its C equals B "
                  f"and its D equals A", flush=True)
    test = load_split_tasks(args.domain, "test", limit=args.test_limit, seed=args.sample_seed,
                            only=args.task_ids)

    arm_models = parse_arm_models(getattr(args, "arm_model", None), arms)
    served = {}
    if hosted:
        setups, served = hosted_arms(arms, args, catalogs, edir, own)
        write_json(edir / "hosted_served.json", {"url": args.obs_url, "arms": served})
    else:
        setups = {a: prepare_arm(a, edir, catalogs.get(a[2:]) if a[0] in "CD" else None, args.recall)
                  for a in arms}

    for a, model in arm_models.items():
        setups[a].agent_model = model

    if args.arm_order == "interleave":
        # one arm after another per task keeps time-of-day and rate-limit effects balanced
        for task in test:
            for a in arms:
                h.batch([task], args.k, setups[a], edir / a, extra={"split": "test"})
    else:
        for a in arms:
            h.batch(test, args.k, setups[a], edir / a, extra={"split": "test"})
    extra = {"catalog": snaps[""] if set(snaps) == {""} else snaps, "claude_invocations": h.budget.n}
    if hosted:
        extra["hosted"] = {"url": args.obs_url, "arms": served}
    if arm_models:
        extra["arm_models"] = arm_models
    write_summary(args.run, h.run_dir, arms, args.k, extra=extra)


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
    for x, y in pairs_for(arms):
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
             f"{s['models']['user']} (effort {s['models']['user_effort']}). Grading: {s['benchmark']['grading']}."
             + ("" if not s.get("arm_models") else " Per-arm agent models: "
                + ", ".join(f"{a} = {m}" for a, m in s["arm_models"].items()) + "."),
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
    write_summary(args.run, run_dir, parse_arms(args.arms), args.k)
