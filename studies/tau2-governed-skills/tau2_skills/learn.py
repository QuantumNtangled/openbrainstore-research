"""Learning loop, TRAIN split only.

  round 0   run the agent (OBS connected, empty catalog) on the train tasks, T trials each;
            the spread of the per-trial pass rates sets the gate's noise floor
  round r   collect failures -> log them as OBS episodes -> the improver proposes items citing
            those episode ids -> leakage screen -> clean items become drafts through OBS's
            propose path -> a sandbox copy of the catalog with the drafts promoted runs the
            same train tasks, T trials -> promote (by="study:gate") only if it beats the
            current catalog by more than the noise floor, else reject the drafts
  every decision is appended to edit_log.jsonl, which the improver reads next round

TEST tasks are loaded only to build the leakage denylist and the contamination check; no
test task is ever run, shown to the improver or used in a decision here."""

import hashlib
import json
from math import sqrt
from pathlib import Path

from .claude_cli import CLIError, RateLimited
from .common import append_jsonl, read_json, read_jsonl, write_json
from .episode import ArmSetup
from .harness import build_harness
from .improver import KINDS, build_prompt, call as call_improver
from .leakage import build_denylist, screen, test_contamination
from .metrics import mean_success, per_trial_means, trial_sd, valid
from .obs import ObsStore
from .splits import load_split_tasks


def _version(ref: str) -> int:
    return int(ref.rsplit("@v", 1)[1])


def _body_key(p: dict) -> str:
    return hashlib.sha256(" ".join((p.get("body") or "").lower().split()).encode()).hexdigest()[:16]


def _pick_failures(episodes: list[dict], cap: int) -> list[dict]:
    """Failed train episodes, one per task first (deterministic order), then the rest."""
    fails = sorted((e for e in valid(episodes) if not e.get("success")),
                   key=lambda e: (str(e["task_id"]), e["trial"]))
    seen, first, rest = set(), [], []
    for e in fails:
        (rest if e["task_id"] in seen else first).append(e)
        seen.add(e["task_id"])
    return (first + rest)[:cap]


def cmd_learn(args) -> None:
    h = build_harness(args, improver_model=args.improver_model, improver_effort=args.improver_effort)
    from tau2.runner import build_environment
    policy = build_environment(args.domain).get_policy()
    ldir = h.run_dir / "learn"
    state_path = ldir / "state.json"
    state = read_json(state_path, {}) or {}
    train = load_split_tasks(args.domain, "train", limit=args.train_limit, seed=args.sample_seed,
                             only=args.task_ids)
    train_ids = [t.id for t in train]
    if state.get("train_ids") and state["train_ids"] != train_ids:
        raise SystemExit("this run already used a different train sample; start a new --run")
    all_train = load_split_tasks(args.domain, "train")
    all_test = load_split_tasks(args.domain, "test")
    denylist = build_denylist(all_train + all_test)
    T = args.trials
    state.update(domain=args.domain, train_ids=train_ids, trials=T, z=args.gate_z,
                 min_delta=args.min_delta, max_rounds=args.rounds, max_edits=args.max_edits)
    write_json(state_path, state)
    canon = ObsStore(ldir / "obs")
    log_path = ldir / "edit_log.jsonl"

    def measure(tag: str, source: ObsStore | None, sandbox_drafts=()) -> list[dict]:
        rdir = ldir / "rounds" / tag
        snap = ObsStore(rdir / "obs")
        if not (rdir / "obs_ready").exists():
            (source or canon).copy_catalog_to(snap)
            for slug, version in sandbox_drafts:
                snap.promote(slug, version, by="study:candidate-sandbox")
            (rdir / "obs_ready").write_text("1")
        eps = h.batch(train, T, ArmSetup(f"train-{tag}", obs_dir=snap.data_dir), rdir / "episodes",
                      extra={"split": "train", "round": tag})
        return eps

    # ---- round 0: baseline and noise floor
    eps0 = measure("r0", canon)
    if "r0" not in state:
        sd, n_trials = trial_sd(eps0)
        floor = max(args.min_delta, args.gate_z * sd * sqrt(2.0 / max(1, T)))
        state["r0"] = {"score": mean_success(eps0), "per_trial": per_trial_means(eps0), "trial_sd": sd,
                       "noise_floor": floor, "valid_episodes": len(valid(eps0)),
                       "noise_note": ("noise floor = max(min_delta, z * sd_trial * sqrt(2/T)); "
                                      "sd_trial is the SD of the per-trial mean pass rate in round 0")}
        if n_trials < 2:
            state["r0"]["warning"] = "fewer than 2 trials: the noise floor is just min_delta"
        state["current"] = {"round": "r0", "score": state["r0"]["score"]}
        write_json(state_path, state)
    floor = state["r0"]["noise_floor"]
    print(f"round 0: train pass^1 = {state['r0']['score']:.3f}  noise floor = {floor:.3f}", flush=True)
    cur_eps, cur_score = eps0, state["current"]["score"]
    epi_ids = state.setdefault("episode_ids", {})   # episode file -> OBS epi id

    for r in range(1, args.rounds + 1):
        tag = f"r{r}"
        rdir = ldir / "rounds" / tag
        if tag in state and state[tag].get("verdict"):
            if state[tag]["verdict"] == "promoted":
                cur_eps = [read_json(p) for p in sorted((rdir / "episodes").glob("*.json"))]
                cur_score = state[tag]["candidate_score"]
            continue
        failures = _pick_failures(cur_eps, args.max_failures)
        if not failures:
            print(f"round {r}: no train failures left; stopping", flush=True)
            break
        for f in failures:
            key = f"{f.get('round')}/{f['task_id']}/t{f['trial']}"
            if key not in epi_ids:
                epi_ids[key] = canon.log_failure(
                    task_summary=f"tau2 {args.domain} train conversation",
                    summary=f"tau2 {args.domain} train task {f['task_id']} trial {f['trial']}: "
                            f"reward {f.get('reward')}, termination {f.get('termination')}",
                    score=float(f.get("reward") or 0.0))
            f["episode_id"] = epi_ids[key]
        write_json(state_path, state)
        edit_log = read_jsonl(log_path)
        prompt = build_prompt(args.domain, policy, canon.promoted_markdown(), edit_log, failures,
                              args.max_edits)
        leaked = test_contamination(prompt, all_test, all_train, policy)
        if leaked:
            raise SystemExit(f"refusing to call the improver: its prompt contains TEST-only strings {leaked[:5]}")
        out_path = rdir / "improver_output.json"
        cached = read_json(out_path)
        if cached is None:
            try:
                proposals, ev = call_improver(h.improver_cfg, prompt, h.workdir / f"improver-{tag}",
                                              rdir, h.budget, f"improver/{tag}")
            except RateLimited as e:
                h.pacer.trip(e)
                raise SystemExit(f"improver rate limited ({e}); rerun the same command to resume")
            except CLIError as e:
                raise SystemExit(f"improver failed: {e}")
            cached = {"proposals": proposals, "result": ev.get("result"), "usage": ev.get("usage"),
                      "total_cost_usd": ev.get("total_cost_usd"), "model": h.improver_cfg.model}
            write_json(out_path, cached)
        proposals = cached["proposals"][: args.max_edits]
        shown = {f["episode_id"] for f in failures}
        rejected_bodies = {e.get("body_key") for e in edit_log if str(e.get("verdict", "")).startswith("rejected")}
        pending = (state.get(tag) or {}).get("pending")
        drafts, rows = ([tuple(d) for d in pending["drafts"]], pending["rows"]) if pending else ([], [])
        for p in ([] if pending else proposals):
            row = {"round": r, "slug": p.get("slug"), "kind": p.get("kind"), "title": p.get("title"),
                   "when_to_use": p.get("when_to_use"), "hypothesis": p.get("hypothesis", ""),
                   "evidence": [x for x in (p.get("evidence") or []) if x in shown],
                   "body_key": _body_key(p), "train_before": cur_score, "noise_floor": floor}
            text = " ".join(str(p.get(k) or "") for k in ("slug", "title", "when_to_use", "body"))
            hits = screen(text, denylist)
            if hits:
                row.update(verdict="rejected-leakage", leakage_hits=hits[:20])
            elif row["body_key"] in rejected_bodies:
                row.update(verdict="rejected-duplicate")
            elif p.get("kind") not in KINDS:
                row.update(verdict="rejected-invalid", error=f"kind must be one of {KINDS}")
            else:
                try:
                    res = canon.propose(slug=p["slug"], title=p["title"], when_to_use=p["when_to_use"],
                                        body=p["body"], kind=p["kind"], evidence=row["evidence"])
                    row["ref"] = res["ref"]
                    drafts.append((p["slug"], _version(res["ref"])))
                except (KeyError, ValueError, TypeError) as e:
                    row.update(verdict="rejected-invalid", error=str(e)[:300])
            rows.append(row)
        if not drafts:
            for row in rows:
                append_jsonl(log_path, row)
            state[tag] = {"verdict": "no-candidate", "proposals": len(proposals)}
            write_json(state_path, state)
            print(f"round {r}: no clean proposals ({len(proposals)} screened out)", flush=True)
            continue
        # drafts exist now: checkpoint them so a resumed run measures these, not new copies
        state[tag] = {"pending": {"drafts": [list(d) for d in drafts], "rows": rows}}
        write_json(state_path, state)
        eps = measure(tag, canon, sandbox_drafts=drafts)
        cand = mean_success(eps)
        before = cur_score
        if args.remeasure_current:
            before = mean_success(measure(f"{tag}-current", canon))
        delta = (cand or 0.0) - (before or 0.0)
        passed = delta > floor
        for slug, version in drafts:
            if passed:
                canon.promote(slug, version, by="study:gate")
            else:
                canon.reject(slug, version, by="study:gate")
        for row in rows:
            if "verdict" not in row:      # went through the gate (screened-out rows keep theirs)
                row.update(verdict="promoted" if passed else "rejected-gate",
                           train_before=before, train_after=cand, delta=delta)
            append_jsonl(log_path, row)
        state[tag] = {"verdict": "promoted" if passed else "rejected", "candidate_score": cand,
                      "before": before, "delta": delta, "noise_floor": floor,
                      "drafts": [f"{s}@v{v}" for s, v in drafts]}
        if passed:
            cur_eps, cur_score = eps, cand
            state["current"] = {"round": tag, "score": cand}
        write_json(state_path, state)
        print(f"round {r}: candidate {cand:.3f} vs current {before:.3f} (delta {delta:+.3f}, "
              f"floor {floor:.3f}) -> {'PROMOTED' if passed else 'rejected'}", flush=True)

    final_md = canon.promoted_markdown()
    (ldir / "catalog_final.md").write_text(final_md or "(empty catalog)\n", encoding="utf-8")
    state["final"] = {"promoted": [i["ref"] for i in canon.items(status="promoted")],
                      "train_score": cur_score,
                      "claude_invocations": h.budget.n}
    write_json(state_path, state)
    print(json.dumps(state["final"], indent=1))
