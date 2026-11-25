# Who should approve an AI agent's new skills? (τ²-bench telecom)

Claude Haiku 5.5 works a simulated phone-company help desk, writes itself new skills from
its failures, and those skills go live under one of four approval paths: an automatic score
check only, a human reviewer, an AI reviewer (Claude Opus 5.5), or a stronger model writing
the skills. The design, the pre-registered predictions and every result are in
[PROTOCOL.md](PROTOCOL.md). This file is how to run it.

Every model in the study, the agent, the simulated customer, the skill writer and the AI
reviewer, is **Claude Code running headless** (`claude -p`). The harness starts one Claude
Code process per role and connects it to the benchmark's tools and to OpenBrainStore over
MCP. No API key is used: the runs use a Claude subscription login.

## What you need

- Claude Code on `PATH`, signed in with a Claude subscription (run `claude`, then `/login`).
  Tested with Claude Code 2.1.316 and later.
- **No `ANTHROPIC_API_KEY`** in the shell you run from: Claude Code would bill that key instead
  of the subscription. The harness checks every session's `apiKeySource` and stops if it is
  anything but `none` (`--allow-api-key` overrides).
- Python 3.12 or 3.13, and git. Windows, macOS or Linux. No Docker.

## Setup (once)

From this folder:

```
python bootstrap.py
```

It clones τ²-bench into `vendor/tau2-bench`, pins it to v1.0.1
(`fc0055dc4e0a316c3f83133267fbd6faaa770992`, refusing anything else), creates a virtualenv and
installs τ², OpenBrainStore (the public
[openbrainstore-core](https://github.com/QuantumNtangled/openbrainstore-core), pinned to one
commit in `requirements.txt`) and the test tools. Then activate the virtualenv:

| OS | virtualenv | activate |
|---|---|---|
| macOS, Linux | `.venv` in this folder | `source .venv/bin/activate` |
| Windows (PowerShell) | `%USERPROFILE%\.venvs\tau2-governed-skills` | `& "$HOME\.venvs\tau2-governed-skills\Scripts\Activate.ps1"` |

On Windows the virtualenv lives outside the project folder because τ²'s dependency litellm
ships files whose paths can pass the 260-character limit. Pass `--venv PATH` to choose another
place. `vendor/`, `runs/`, `results/` and `.venv/` are gitignored.

Every command below runs from this folder with the virtualenv active.

## Offline check (no model calls)

```
python -m pytest tests -q -s
```

`tests/fake_claude.py` stands in for Claude Code: the same stream-json protocol, real MCP calls
to the τ² relay and to OpenBrainStore, and a scripted policy instead of a model. It runs a full
learning round, a human-review round, a model-review round and every arm on a test task, with
an injected rate-limit error. A few minutes.

## Run it against your own OpenBrainStore

The quickest way to try the study's skills, or your own, is against your OpenBrainStore
account at `https://openbrainstore.com/mcp`. The agent connects there exactly as it would in
your own work.

1. Sign in at [openbrainstore.com/app](https://openbrainstore.com/app) with Google or GitHub,
   and switch the catalog on in the **Catalog** tab.
2. Put the skills you want to test in your catalog, and approve them there. For example, ask
   any connected agent to propose the files from one of this study's final catalogs with
   `propose_skill`, then approve each one in the Catalog tab.
3. In **Agents**, create a key with `catalog:read`, and put it in your shell's environment,
   never in a file:

   ```
   export OBS_AGENT_KEY=...            # macOS, Linux
   $env:OBS_AGENT_KEY = "..."          # Windows PowerShell
   ```

   The harness passes Claude Code the placeholder `${OBS_AGENT_KEY}`, which Claude Code
   fills in from the environment when it connects, so the key never appears in the run's
   files or logs.
4. Run one conversation, then a comparison on the held-out tasks:

   ```
   python -m tau2_skills episode --run try1 --domain telecom --split test --task-ids <id> --obs-url
   python -m tau2_skills evaluate --run mine --domain telecom --obs-url --k 3
   ```

With `--obs-url` the comparison runs two arms, interleaved task by task: **A** (no
OpenBrainStore) and **C** (your catalog). `--obs-url URL` points at another server, and
`--obs-key-env NAME` reads the key from a different variable.

**What hosted mode can't do.** The learning loop and an exact reproduction of the published
numbers use a local OpenBrainStore, which the harness starts for you. Each setup needs its own
isolated catalog (including an empty one for the no-skills baseline), and the score check
measures candidate skills before anyone approves them. The hosted service never gives an agent
a skill its owner hasn't approved, by design. A hosted catalog also can't be frozen for the
length of a run, so its summary records the URL instead of a catalog hash.

## Reproduce the study

These are the exact commands behind the published results (telecom, 74 practice tasks and
40 held-out tasks). Each learning run takes several hours and a large share of a week's plan
usage; see PROTOCOL.md §11 for measured usage, and §12 for how runs pace themselves.

```
# Learning runs on the practice (train) split
python -m tau2_skills learn --run tel-h  --domain telecom --trials 3 --rounds 3 --max-edits 3 --remeasure-current --user-model claude-sonnet-5-5 --improver-model claude-haiku-5-5 --concurrency 3 --stop-at-utilization 0.7 --max-invocations 3600 --max-run-usd 80
python -m tau2_skills learn --run tel-hr --seed-from tel-h --review --reviewer human:reviewer --domain telecom --trials 3 --rounds 3 --max-edits 3 --remeasure-current --user-model claude-sonnet-5-5 --improver-model claude-haiku-5-5 --concurrency 3 --stop-at-utilization 0.7 --max-invocations 3600 --max-run-usd 80
python -m tau2_skills learn --run tel-or --seed-from tel-h --reviewer-model claude-opus-5-5 --domain telecom --trials 3 --rounds 3 --max-edits 3 --remeasure-current --user-model claude-sonnet-5-5 --improver-model claude-haiku-5-5 --concurrency 4 --stop-at-utilization 0.7 --max-invocations 3600 --max-run-usd 80
python -m tau2_skills learn --run tel-o  --domain telecom --trials 3 --rounds 3 --max-edits 3 --remeasure-current --user-model claude-sonnet-5-5 --improver-model claude-opus-5-5 --concurrency 3 --stop-at-utilization 0.7 --max-invocations 3600 --max-run-usd 80

# Held-out (test) split: no skills against the three approved catalogs, interleaved
python -m tau2_skills evaluate --run tel-test --domain telecom --catalog rev=runs/tel-hr/learn/obs --catalog orev=runs/tel-or/learn/obs --catalog opus=runs/tel-o/learn/obs --arms B,C:rev,C:orev,C:opus --k 3 --user-model claude-sonnet-5-5 --concurrency 3 --stop-at-utilization 0.7 --max-invocations 1600 --max-run-usd 80

# Rebuild a summary from the episode files at any time
python -m tau2_skills summarize --run tel-test --arms B,C:rev,C:orev,C:opus --k 3
```

`--review` pauses each round after the automatic screens and writes `rounds/<r>/review.md`.
Record approve, edit or reject for each proposal in `rounds/<r>/review_decisions.json`, then
rerun the same command. `--reviewer-model` puts a model in that seat instead.

Outputs:
- `results/<run>/summary.json` and `summary.md`: pass^1 … pass^k with bootstrap 95% CIs and
  paired differences between arms.
- `runs/<run>/learn/state.json`: each round's scores and verdicts.
- `runs/<run>/learn/edit_log.jsonl`: every proposal and what happened to it.
- `runs/<run>/learn/rounds/<r>/`: exactly what the skill writer and the reviewer saw.
- `runs/<run>/eval/<arm>/<task>__t<trial>.json`: one file per conversation, with the full τ²
  trajectory, the reward breakdown, usage and OpenBrainStore calls. The raw Claude Code event
  stream of every process sits next to it.

## Stopping and resuming

Every conversation is checkpointed. Rerun the same command to resume; finished conversations
are skipped and infrastructure errors retried. A run stops by itself (exit code 3) when:

- a rate-limit pause would last longer than `--max-pause` (default 3 h),
- the plan's reported utilization reaches `--stop-at-utilization` (default 0.9; lower it to
  keep headroom for your own use),
- `--max-invocations` or `--max-run-usd` is reached (both count across restarts).

Shorter rate limits pause the whole run and back off automatically. A model that is briefly
overloaded is retried, not mistaken for an exhausted plan.

## Useful options

| option | default | |
|---|---|---|
| `--agent-model`, `--user-model`, `--improver-model` | `claude-haiku-5-5` | the simulated customer is identical across arms |
| `--agent-effort`, `--user-effort`, `--improver-effort` | `medium` | |
| `--reviewer-model`, `--reviewer-effort` | off, `medium` | a model in the reviewer's seat |
| `--concurrency` | 2 | conversations in flight; each runs two Claude Code processes |
| `--train-limit`, `--test-limit`, `--task-ids` | all | seeded samples or explicit ids (must be in the split) |
| `--recall` | off | also offer OpenBrainStore `recall` in arms B and C |
| `--remeasure-current` | off | re-run the current catalog every round, so drift can't pass as a gain |
| `--gate-z`, `--min-delta` | 1.645, 0.02 | noise floor = max(min_delta, z · sd_trial · √(2/T)) |
| `--obs-url [URL]`, `--obs-key-env` | off, `OBS_AGENT_KEY` | serve arm C from a hosted OpenBrainStore account |
| `--fake-claude` | off | the offline scripted CLI |

## Layout

| path | |
|---|---|
| `tau2_skills/claude_cli.py` | Claude Code processes (stream-json sessions, one-shot calls), usage accounting, rate-limit detection, invocation cap |
| `tau2_skills/relay.py` | τ² tools as an in-process MCP server; calls are executed by τ²'s orchestrator |
| `tau2_skills/participants.py` | the τ² agent and customer, each backed by a Claude Code process |
| `tau2_skills/episode.py` | one conversation: environment, participants, orchestrator, grading (natural-language judge removed) |
| `tau2_skills/runner.py` | batches, checkpoints, retries, pacing |
| `tau2_skills/obs.py` | OpenBrainStore: local data dirs for the harness's own catalog work, and the agent's MCP connection (local or hosted) |
| `tau2_skills/learn.py`, `improver.py`, `reviewer.py`, `leakage.py` | the learning loop, the skill writer, the model reviewer and the leak filter |
| `tau2_skills/evaluate.py`, `metrics.py` | arms on the test split, pass^k, CIs, summaries |
| `tau2_skills/estimate.py` | usage and cost estimate |
| `smoke_catalog/` | a hand-written one-item catalog for the real smoke test |
| `tests/` | the fake CLI, the offline end-to-end tests, unit tests |
