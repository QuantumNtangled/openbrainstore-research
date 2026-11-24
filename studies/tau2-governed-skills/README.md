# tau2-skills: an OBS skill catalog on τ²-bench

Does an OpenBrainStore skill catalog, learned from τ²-bench's TRAIN split and promoted only
through a train-split gate, make Claude Haiku 5.5 (running as Claude Code) better on the
held-out TEST split? The design, splits, arms, gate, leakage rules, metrics and cost estimate
are in [PROTOCOL.md](PROTOCOL.md). This file is how to run it.

Every model call (the agent, the user simulator and the improver) goes through the **Claude
Code CLI in headless mode on your subscription login**. No API key is used or needed.

## What you need

- Claude Code on `PATH` and signed in with your Claude subscription (`claude`, then `/login`).
  Tested with Claude Code 2.1.316.
- **Nothing else to set.** Do not set `ANTHROPIC_API_KEY` in the shell you run this from:
  Claude Code would bill that key instead of the subscription. The harness checks each
  session's `apiKeySource` and stops if it is anything but `none` (`--allow-api-key` overrides).
- Python 3.12 or 3.13 and git. No Docker.

## Setup (once)

```powershell
powershell -ExecutionPolicy Bypass -File studies\tau2-skills\setup.ps1
```

This clones τ²-bench into `vendor/tau2-bench`, pins it to v1.0.1
(`fc0055dc4e0a316c3f83133267fbd6faaa770992`, refusing anything else) and installs τ², this
repo's `openbrainstore` package and test tools into a virtualenv at
`%USERPROFILE%\.venvs\tau2-skills`. The venv is outside the study folder on purpose: τ²'s
dependency litellm ships files whose paths exceed Windows' 260-character limit under this
worktree's path. `vendor/`, `runs/` and `results/` are gitignored.

All commands below run from `studies\tau2-skills` with that venv's Python:

```powershell
cd studies\tau2-skills
$py = "$env:USERPROFILE\.venvs\tau2-skills\Scripts\python.exe"
```

## Offline check (no model calls)

```powershell
& $py -X utf8 -m pytest tests -q -s
```

`tests/fake_claude.py` stands in for the CLI: same stream-json protocol, real MCP calls to the
τ² relay and to `obs serve`, a scripted policy instead of a model. The smoke test runs one full
learning round on retail TRAIN tasks 0 and 1 and all four arms on TEST task 5, with an injected
rate-limit error. About 45 seconds.

## Pilot (train only; run this first)

Measures real per-episode cost and plan usage without touching TEST, so the pre-registration
can still come before any test run. About 60 episodes, ~120 claude invocations,
~$0.65 API-equivalent, ~30 minutes.

```powershell
# learning loop on a seeded sample of 10 retail train tasks, 2 trials, 1 round
& $py -m tau2_skills learn --run pilot --domain retail --train-limit 10 --trials 2 --rounds 1 --max-edits 2 --max-invocations 100 --max-run-usd 2

# price arms A and D on the same 10 train tasks (1 trial each), using the pilot's catalog
& $py -m tau2_skills episode --run pilot-a --domain retail --split train --limit 10 --arm A --max-invocations 25 --max-run-usd 1
& $py -m tau2_skills episode --run pilot-d --domain retail --split train --limit 10 --arm D --catalog runs\pilot\learn\obs --max-invocations 25 --max-run-usd 1

# recalibrate the estimate with what the pilot measured (see runs\pilot\learn\rounds\r0\episodes\*.json)
& $py -m tau2_skills estimate --cc-overhead 6000
```

Read `runs\pilot\learn\state.json` (scores, noise floor), `runs\pilot\learn\edit_log.jsonl`
(every proposal and verdict), `runs\pilot\learn\rounds\r1\improver_prompt.md` (exactly what
the improver saw) and `runs\pilot\pacing.jsonl` (plan utilization after each episode).

## Full run, per domain

```powershell
# 1. learning loop on all train tasks: T=3 trials, up to 3 rounds of up to 3 edits
& $py -m tau2_skills learn --run retail1 --domain retail --trials 3 --rounds 3 --max-edits 3 --max-run-usd 30

# 2. fill in PROTOCOL.md section 13 (prediction and kill criterion) and commit it

# 3. arms A-D on all test tasks, k=4, arms interleaved task by task
& $py -m tau2_skills evaluate --run retail1 --domain retail --k 4 --max-run-usd 30

# rebuild the summary from the episode files at any time
& $py -m tau2_skills summarize --run retail1 --k 4
```

Then the same with `--domain airline` and `--domain telecom` (new `--run` names).

Outputs: `results\<run>\summary.json` and `summary.md` (pass^1 … pass^k with bootstrap 95%
CIs, paired differences C−B, C−A, D−A, C−D, B−A, OBS call rates, API-equivalent cost per arm);
`runs\<run>\eval\<arm>\<task>__t<trial>.json` (one file per episode: full τ² trajectory,
reward breakdown, per-process usage, the CLI's `init` event, OBS calls); the raw stream-json
of every claude process next to it.

## Stopping and resuming

Every episode is checkpointed. Rerun the same command to resume; finished episodes are
skipped and infrastructure errors retried. A run stops by itself (exit code 3) when:

- a rate-limit pause would exceed `--max-pause` (default 3 h),
- the plan's reported utilization reaches `--stop-at-utilization` (default 0.9; lower it to
  keep headroom for your own use),
- `--max-invocations` or `--max-run-usd` is reached (both count across restarts).

Shorter rate limits pause the whole run and back off automatically.

## Useful options

| option | default | |
|---|---|---|
| `--agent-model`, `--user-model`, `--improver-model` | `claude-haiku-5-5` | user simulator is identical across arms |
| `--agent-effort`, `--user-effort`, `--improver-effort` | `medium` | |
| `--concurrency` | 2 | episodes in flight; each runs two claude processes |
| `--train-limit`, `--test-limit`, `--task-ids` | all | seeded samples / explicit ids (must be in the split) |
| `--recall` | off | also offer OBS `recall` in arms B and C |
| `--remeasure-current` | off | re-run the current catalog every round (costs 2×) |
| `--gate-z`, `--min-delta` | 1.645, 0.02 | noise floor = max(min_delta, z · sd_trial · √(2/T)) |
| `--fake-claude` | off | the offline scripted CLI |

## Layout

| path | |
|---|---|
| `tau2_skills/claude_cli.py` | claude processes (stream-json sessions, one-shot calls), usage accounting, rate-limit detection, invocation cap |
| `tau2_skills/relay.py` | τ² tools as an in-process MCP server; calls are executed by τ²'s Orchestrator |
| `tau2_skills/participants.py` | τ² agent and user simulator backed by Claude Code |
| `tau2_skills/episode.py` | one episode: environment, participants, orchestrator, grading (NL judge removed) |
| `tau2_skills/runner.py` | batches, checkpoints, retries, pacing |
| `tau2_skills/obs.py` | in-process OBS: propose, promote/reject, episodes, catalog copies |
| `tau2_skills/learn.py`, `improver.py`, `leakage.py` | the TRAIN learning loop |
| `tau2_skills/evaluate.py`, `metrics.py` | arms A-D on TEST, pass^k, CIs, summary |
| `tau2_skills/estimate.py` | usage and cost estimate |
| `smoke_catalog/` | a hand-written one-item catalog for the real smoke test only |
| `tests/` | the fake CLI, the offline smoke test, unit tests |

## Real smoke test (done during staging)

Retail train task 0, one trial, arm C (the `smoke_catalog` item served over MCP),
`claude-haiku-5-5`:

```powershell
& $py -m tau2_skills episode --run real2 --domain retail --split train --task-ids 0 --arm C --catalog-import smoke_catalog --concurrency 1 --max-attempts 1 --max-invocations 2 --prompt-transport file
```

Run twice (before and after the account-context fix in PROTOCOL.md section 4): reward 1.0
both times, `agent_startup` and `get_skill` called once each, 47 s and 55 s, 2 claude
invocations each, $0.0080 and $0.0079 API-equivalent.
