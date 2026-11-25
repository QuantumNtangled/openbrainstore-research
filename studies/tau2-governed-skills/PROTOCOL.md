# Protocol: does a learned OBS skill catalog improve Claude Haiku 5.5 on τ²-bench?

Status: **staged, not run.** Everything below is fixed before the paid run, except the
pre-registration section at the end, which is filled in after the train baseline and before
any test episode.

## 1. Question

Does OpenBrainStore's skill catalog, with items learned from τ²-bench's TRAIN split and promoted
only through a train-split gate, measurably improve Claude Haiku 5.5 (running as Claude Code)
on the held-out TEST split?

Secondary: is any gain due to retrieval by reference (`agent_startup` / `get_skill`), or would
the same text pasted into the prompt do as well? Does merely connecting OBS (extra tools and
prompt) change the score?

## 2. Benchmark and version

- **τ²-bench v1.0.1**, commit `fc0055dc4e0a316c3f83133267fbd6faaa770992` (tagged 2026-07-16),
  checked out in `vendor/tau2-bench` by `setup.ps1`, which refuses any other commit.
- Why not v2.0.0 (2026-10-19): its CHANGELOG says 2.0 scores are not comparable with 1.x, and
  it grades communication with an OpenAI judge (`gpt-5.5`) and NL assertions with
  `gpt-5.4-mini` by default. 1.0.1 grades the three domains used here by database end state,
  environment assertions, actions and substring communicate checks.
- **Grading deviation (all arms alike).** In v1.0.1, 112 of 114 retail tasks list
  `NL_ASSERTION` in their reward basis, and 40 of those carry assertions that tau2 grades with
  `gpt-4.1` (an OpenAI call). This study uses no judge model: `NL_ASSERTION` is removed from the
  basis and everything else is scored exactly as tau2 does (`tau2_skills/episode.py:scoring_task`).
  Affected tasks: retail train 29, retail test 11. Airline (DB + COMMUNICATE) and telecom
  (ENV_ASSERTION, plus ACTION on 20 tasks) are graded as published. Consequence: retail numbers
  are not comparable to the public leaderboard; comparisons between arms are unaffected.
- tau2 defaults kept: `max_steps` 200, `max_errors` 10, communication-protocol validation off,
  the orchestrator's fixed opening line "Hi! How can I help you today?".

## 3. Splits

Read from each domain's `data/tau2/domains/<domain>/split_tasks.json` at the pinned commit.
Train and test are disjoint and their union is the `base` split in every domain.

| domain | train | test | base | note |
|---|---|---|---|---|
| retail | 74 | 40 | 114 | matches the "74 train / 40 test" figure |
| airline | 30 | 20 | 50 | |
| telecom | 74 | 40 | 114 | `tasks.json` holds 2,285 generated tasks (`full`); the leaderboard's `base` is these 114 |

All three domains have an official split, so the seeded fallback in `splits.py` (sorted ids,
`random.Random(20261124)` shuffle, 35% test) is not used. `--train-limit` / `--test-limit`
samples are seeded by SHA-256 of `seed:task_id` and are the same for every arm and round.

## 4. How an episode runs

Everything model-side goes through the **Claude Code CLI in headless mode** on the operator's
subscription login (no API key). Verified with Claude Code 2.1.316.

- **Agent under test:** one persistent `claude -p --input-format stream-json --output-format
  stream-json` process per episode, `--model claude-haiku-5-5 --effort medium`. Claude Code's
  default system prompt is kept in every arm; tau2's own agent prompt (`AGENT_INSTRUCTION` +
  the domain policy, verbatim) goes in `--append-system-prompt[-file]`.
- **Isolation:** `--strict-mcp-config`, `--tools ""` (no built-in tools), `--disable-slash-commands`,
  `--setting-sources project` from an empty working directory outside the repo (the harness
  refuses to start if a CLAUDE.md or AGENTS.md is above it), `--no-session-persistence`,
  `--permission-mode dontAsk` with every offered MCP tool named in `--allowedTools`, and the
  OBS tools not offered listed in `--disallowedTools`. The real smoke run's `init` event showed
  exactly the 16 retail tools plus `agent_startup` and `get_skill`, no skills, no slash
  commands, and `apiKeySource: none` (subscription login).
- **τ² tools:** served by an in-process MCP server (Streamable HTTP on 127.0.0.1). It executes
  nothing: each call is handed to tau2's own `Orchestrator`, which executes it on the task's
  `Environment` as it does for tau2's agents. The trajectory, final DB state and grading are
  tau2's.
- **User simulator:** tau2's `UserSimulator` (its guidelines, persona and scenario) with Claude
  Code as the model: a second persistent `claude -p` process, `--model claude-haiku-5-5
  --effort medium`, tau2's user prompt as `--system-prompt`, no tools (telecom: tau2's device
  tools through the same relay, executed by the Orchestrator). Identical in every arm.
- **Per-process cap:** `--max-budget-usd 1` (API-equivalent) guards against runaway loops; an
  episode stopped by it is graded as a failure.

Deviations from tau2's stock agent loop, identical in every arm:

1. A short `<harness>` note says the customer was already greeted (Claude Code cannot be given
   a prior assistant turn) and that the text at the end of each turn goes to the customer.
2. **Account context.** Claude Code attaches the signed-in account's email address to the first
   user turn of every session (seen in a diagnostic call; in the 2.1.316 binary only
   `ANTHROPIC_UNIX_SOCKET` turns it off). In the first real episode it confused the user
   simulator's opening line. Both prompts now say this context belongs to the operator and must
   not be used; the second real episode opened normally. Every episode records any email
   address the customer or agent wrote that appears in neither the task data nor a tool result
   (`foreign_emails`, masked) and the summary counts them.
3. The reply sent to the customer is the text after the turn's last tool call (tau2 likewise
   never shows the user text that shares a message with a tool call).
4. An empty final reply is sent as "..." (tau2 rejects empty messages) and counted.

## 5. Arms (TEST split only)

| arm | OBS tools offered | appended to Claude Code's prompt | catalog |
|---|---|---|---|
| **A** | none | tau2 prompt + harness note | none |
| **B** | `agent_startup`, `get_skill` (local `obs serve`, own data dir) | A + the OBS snippet from `docs/specs/skill-catalog.md` | **empty** |
| **C** | same as B | same as B | the promoted items from the learning run, copied into C's own data dir |
| **D** | none | A + the same promoted items, rendered exactly as `get_skill` returns them | none |

B controls for the extra tools and prompt; C − B is the catalog effect; C − D isolates
retrieval by reference from "more prompt". `--recall` additionally offers OBS `recall` in B and
C (off by default; the memory store is empty). Arms are interleaved task by task by default, so
time-of-day and rate-limit effects fall evenly on all arms. The catalog snapshot (sorted refs
and hash) is frozen at the first evaluate call; a changed catalog aborts the run.

## 6. Models and settings

| role | model | effort | other |
|---|---|---|---|
| agent | `claude-haiku-5-5` | `medium` (configurable) | adaptive thinking (the model's default); Claude Code sends no temperature/top_p/top_k |
| user simulator | `claude-haiku-5-5` (configurable, identical across arms) | `medium` | tau2 prompt via `--system-prompt` |
| improver | `claude-haiku-5-5` (configurable) | `medium` | one `claude -p --output-format json` call per round, custom system prompt, no tools |

## 7. Learning loop and promotion gate (TRAIN only)

1. **Round 0.** Run the OBS-connected agent (arm-B configuration, empty catalog) on the train
   tasks, **T = 3** trials each. Score = mean success over all train episodes.
2. **Noise floor**, from round 0: `floor = max(min_delta, z · sd_trial · sqrt(2/T))`, where
   `sd_trial` is the sample SD of the per-trial mean pass rates, `z = 1.645` (one-sided 95%) and
   `min_delta = 0.02`. With T < 2 the floor is `min_delta` and the state file says so.
3. **Each round r (at most 3).** Take up to 8 failed episodes of the current catalog (one per
   task first, deterministic order). Log each as an OBS episode (an `optimize` session plus
   `log_episode`) so it has a real `epi_…` id.
4. **Improver.** Its prompt holds the domain policy, the current promoted catalog, the full edit
   log, and the failed conversations with the grader's findings (DB match, expected actions,
   communicate checks). It returns at most **3** proposals as JSON: slug, kind, title,
   when_to_use, body, hypothesis, evidence (episode ids; ids it was not shown are dropped).
5. **Screens**, in order: leakage (section 8) → `rejected-leakage`; same body as an earlier
   rejected proposal → `rejected-duplicate`; bad kind or OBS validation error → `rejected-invalid`.
   Clean proposals become drafts through `server.propose_skill` (the MCP tool's own function).
6. **Gate.** Copy the catalog's canonical blobs into a sandbox data dir, promote the round's
   drafts there (`by="study:candidate-sandbox"`, sandbox only), and run the same train tasks,
   T trials. If `candidate − current > floor`, promote the drafts in the study catalog with
   `catalog.promote_version(..., by="study:gate")` (the CLI's human-equivalent path); otherwise
   reject them with `catalog.reject(..., by="study:gate")`. One verdict per round covers all of
   its drafts. `current` is the last accepted score (`--remeasure-current` re-runs it each round
   instead, at twice the cost, to counter winner's-curse bias).
7. **Edit log** (`runs/<run>/learn/edit_log.jsonl`): round, slug, ref, kind, title, when_to_use,
   hypothesis, evidence, body hash, train score before/after, delta, noise floor, verdict,
   leakage hits. The improver reads it every round and is told not to re-propose rejected ideas.
8. Stop after 3 rounds or when no train failures remain. Every step checkpoints; rerunning the
   command resumes.

**TEST isolation.** `learn` never runs a test task. Test tasks are loaded only to build the
leakage denylist and a contamination check: before each improver call, its prompt is scanned for
strings that occur only in test tasks (not in train tasks or the policy), and the call is refused
if any are found. Explicit `--task-ids` must belong to the requested split.

This loop follows the steps given in the study brief (run, collect failures, propose with
evidence, screen, gate against a noise floor, promote, log, cap). I have not read the RRSI paper
(arXiv 2609.24972) itself, so nothing here should be taken as a claim about its exact method.

## 8. Leakage rules

A proposal is rejected if its slug, title, when_to_use or body contains:

- any string specific to a train **or** test task, from a denylist built from the task files:
  non-numeric task ids, "task N" references, user ids, full names and distinctive name tokens
  (first/last name arguments, "You are …" phrases), order / reservation / item / product /
  payment ids, emails, phone numbers, zip codes, flight numbers, and every ID-like argument
  value in the gold actions;
- any ID-shaped token at all: `#W…` order ids, `name_name_1234` user ids, `credit_card_…` /
  `paypal_…` / `gift_card_…` ids, emails, phone numbers, numbers of 6+ digits.

The screen is deliberately conservative: a false positive costs one proposal, which the edit log
records with the matched strings (as they appear in the proposal, so nothing about test tasks
is revealed to the improver).

## 9. Metrics

- **pass^k** (τ-bench): for a task with n trials and c successes, `C(c,k)/C(n,k)`, averaged over
  tasks with at least k valid trials. Reported for k = 1…K.
- **95% CIs:** percentile bootstrap over tasks, 2,000 resamples, fixed seed.
- **Paired differences** C−B, C−A, D−A, C−D, B−A at k = 1 and k = K: per-task differences
  bootstrapped over the tasks both arms have, with the share of resamples above 0.
- **Primary comparison: C − B at pass^1** on each domain's test split. Secondary: C − A, C − D,
  pass^K.
- **Exclusions:** infrastructure errors (CLI crashes, timeouts after 3 attempts) are excluded and
  counted, as tau2 does. Budget-capped episodes count as failures.
- **Also reported per arm:** OBS bootstrap compliance (share of episodes calling `agent_startup`,
  `get_skill`), empty replies, episodes with foreign emails, terminations, and cost (below).

## 10. Trials

- Test: **k = 4** trials per task per arm (4 arms × 40 retail test tasks × 4 = 640 episodes).
- Train: **T = 3** trials per task per measurement.
- Claude Code exposes no sampling seed; trials differ by the model's own sampling. tau2's
  orchestrator seed (trial index) still fixes its own randomness.

## 11. Usage and API-equivalent cost

The runs use a Claude subscription, so dollars below are **API-equivalent** (what the same
tokens would cost on the API at Haiku 5.5's $0.10 / $0.50 per million input/output tokens for
prompts ≤100K tokens, cache reads 0.1×, Claude Code's 1-hour cache writes 2×; Sonnet 5.5
$2 / $10, cache reads 0.05×). The harness computes this from each session's cumulative
`modelUsage`; on the smoke runs it matched the CLI's own `total_cost_usd` to the last digit.

**Measured (2 real smoke episodes, retail train task 0, arm C):** agent 10–12 API calls,
first prompt 12.1K tokens (Claude Code's own prompt ≈ 6K of it, estimated as the first prompt
minus our appended prompt, tool schemas and the first message at 4 chars/token), ~170–210K
tokens processed per episode (agent + user), nearly all cache reads; user simulator 4–5 calls,
1.6K-token first prompt; **$0.008 API-equivalent and ~50 s per episode**, 2 claude invocations
per episode.

**Estimate for the whole study** (`python -m tau2_skills estimate`; tau2 text at chars/4, the
per-episode shape calibrated on the smoke episode and scaled up for longer domains, so treat it
as ±50%):

| user sim | domain | train/test | $ / episode | tokens / episode | API calls / episode | train baseline $ | per learning round $ | test, 4 arms $ | domain total $ | episodes | claude invocations |
|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-haiku-5-5 | retail | 74/40 | 0.0109 | 274,306 | 20 | 2.41 | 2.41 | 6.80 | 16.45 | 1,528 | 3,059 |
| claude-haiku-5-5 | airline | 30/20 | 0.0098 | 228,348 | 18 | 0.88 | 0.89 | 3.06 | 6.60 | 680 | 1,363 |
| claude-haiku-5-5 | telecom | 74/40 | 0.0265 | 987,916 | 46 | 5.88 | 5.88 | 16.73 | 40.25 | 1,528 | 3,059 |
| claude-sonnet-5-5 | retail | 74/40 | 0.0462 | 274,306 | 20 | 10.25 | 10.25 | 29.40 | 70.41 | 1,528 | 3,059 |
| claude-sonnet-5-5 | airline | 30/20 | 0.0451 | 228,348 | 18 | 4.06 | 4.06 | 14.36 | 30.61 | 680 | 1,363 |
| claude-sonnet-5-5 | telecom | 74/40 | 0.1370 | 987,916 | 46 | 30.41 | 30.41 | 87.45 | 209.10 | 1,528 | 3,059 |

(T = 3, 3 learning rounds, k = 4, 4 arms; each round re-runs all train tasks once.)

- All three domains, Haiku 5.5 user simulator: **≈ $63 API-equivalent**, 3,736 episodes,
  ≈ 7,500 claude invocations, ≈ 2 billion tokens processed (mostly cache reads).
- Same with a Sonnet 5.5 user simulator: **≈ $310**.
- Retail alone with Haiku: ≈ $16; airline ≈ $7; telecom ≈ $40.

**Recommended caps.** Pilot: `--max-run-usd 2` and `--max-invocations 150`. Full retail run:
`--max-run-usd 30` per command; all three domains: $100 total. These are API-equivalent
ceilings; on the subscription the binding limit is the plan's usage window (section 12).

**Pilot (train only, so the pre-registration can still come before any test run):** 10 retail
train tasks × 2 trials, 1 learning round (40 episodes + 1 improver call ≈ 81 invocations,
≈ $0.45), plus arms A and D on the same 10 train tasks × 1 trial (20 episodes) to price every
arm without touching TEST. Total ≈ 60 episodes, ≈ 120 invocations, ≈ $0.65 API-equivalent,
≈ 30 minutes at concurrency 2. Use it to replace the assumed calls and tokens per episode and
to measure how much one episode moves the plan's utilization.

## 12. Pacing across plan limits

- **Checkpoint per episode.** Each (arm, task, trial) is one JSON file written atomically;
  rerunning the same command skips finished episodes and retries infrastructure errors.
  Learning-loop state (scores, noise floor, OBS episode ids, improver output, pending drafts,
  verdicts) checkpoints the same way.
- **Backoff.** A rate-limit error pauses the whole run: until the reset time Claude Code reports
  (`rate_limit_event.resetsAt`, or an epoch in the error text) plus 60 s, else exponential
  backoff from 60 s to 1 h. A pause longer than `--max-pause` (3 h) stops the run cleanly
  instead (exit code 3, "rerun to resume").
- **Utilization stop.** Claude Code emits `rate_limit_event`s with each window's utilization
  (the smoke runs saw the 7-day window at 5%). The harness logs them to
  `runs/<run>/pacing.jsonl` and stops starting new episodes once any window reaches
  `--stop-at-utilization` (default 0.9; use 0.7–0.8 to leave room for your own work).
- **Hard caps.** `--max-invocations` counts every claude process across restarts
  (`claude_invocations.jsonl`); `--max-run-usd` stops on API-equivalent spend across restarts.
- **Billing guard.** If a session's `init` event reports an `apiKeySource` other than `none`
  (an API key would be billed instead of the subscription), the run stops unless
  `--allow-api-key` is given.
- **Suggested schedule.** Concurrency 2 (four claude processes). Retail first (learn, then the
  pre-registration, then evaluate), then airline, then telecom; one command per window,
  resumed as limits allow. At ~50 s per episode and concurrency 2, retail is ≈ 11 hours of
  wall time; with telecom episodes assumed twice as long, the three domains are ≈ 35 hours.

## 13. Pre-registered prediction and kill criterion

*To be filled in after the train baseline (round 0) and the learning rounds, and before any
TEST episode. Commit this section before running `evaluate`.*

**Agreed by the reviewer on 2026-11-25, as a three-arm run.** A six-arm draft (A, B, C:auto, C:rev, C:orev, C:opus; 960 episodes) was offered first. The reviewer chose to run only the three governed catalogs on the held-out tasks. No TEST episode had been run before this entry was committed.

**Amendment, same day, before any result existed:** the reviewer changed the trials from 4 to 3 per task per arm. The first launch (k = 4) was stopped about a minute in, while its first four episodes were still running; none had finished, so no score was seen. Their partial logs are kept in `runs/tel-test/aborted-k4/` and are not used. Everything below is written for k = 3.

**Second amendment, same day:** the reviewer added the no-skills baseline, arm B (OBS connected, empty catalog: the configuration of every train re-measure). The run was stopped and restarted with B interleaved. At the restart 6 episodes had finished (the first task, arms C:rev and C:orev); their scores were not looked at and they are kept. For that first task B runs a few minutes after those two arms; every later task is fully interleaved. The baseline predictions and the transfer rule below are the ones from the six-arm draft. The shrinkage rule now compares gains over B rather than raw scores, since a baseline is available.

The learning rounds were run on telecom only (`tel-h`, `tel-hr`, `tel-or`, `tel-o`; results in §14).

**Arms.** All arms run in one interleaved `evaluate` run (`--run tel-test`): arm by arm within each task, so drift falls on every arm equally.

| arm | catalog (frozen refs) | how it got there |
|---|---|---|
| B | OBS connected, **empty** | the no-skills baseline; also where gate-only Haiku (`tel-h`) ended up |
| C:rev | `tel-hr` final: `use-device-tools-not-manual-steps@v2` + `data-limit-refuel-check@v1` (train 0.896) | **human review + gate**: Haiku proposed, the reviewer (with AI help) approved or edited, the gate measured |
| C:orev | `tel-or` final: `use-device-tools-not-manual-steps@v2` + `data-limit-refuel-check@v2` (train 0.964) | **model review + gate**: Haiku proposed, Opus 5.5 in the reviewer's seat with the same inputs, the same gate |
| C:opus | `tel-o` final: `always-check-data-limit-on-data-issues@v1` + `customer-runs-device-checks-no-early-transfer@v1` (train 0.977) | **stronger skill writer + gate**: Opus proposed, gate only |

**Not run, and what that means for the report.**
- **No arm A** (plain Haiku without OBS). B − A, the cost of the extra tools and prompt alone, is not measured.
- **No C:auto arm.** The harm of auto-approval rests on the train measurement (Haiku's round-1 batch scored 0.360 against a 0.676 re-measure, §14).
- **No arm D**, as in the draft: every promoted item here is a guardrail, already inlined by `agent_startup`.

**Setup.**
- Telecom TEST: 40 tasks, k = 3 trials per task per arm, so 4 arms × 120 = 480 episodes.
- Agent: `claude-haiku-5-5`, effort medium. User simulator: `claude-sonnet-5-5`, effort medium (the same as in training; §6's default is Haiku).
- Concurrency 3, so each task's 3 trials run in one wave. `--stop-at-utilization 0.7`.

**Primary comparisons** (pass^1, paired over tasks, 95% bootstrap CI as in §9):
1. **C:rev − B**: what Haiku-written skills with human (AI-assisted) review add over no skills.
2. **C:orev − B**: the same, with Opus as the reviewer.
3. **C:opus − B**: what Opus-written skills with gate-only approval add over no skills.
4. **C:rev − C:orev**: human (AI-assisted) review against model review, for the same Haiku proposals.
5. **C:opus − C:orev**: a stronger skill writer with gate-only approval against a weaker writer whose proposals a strong model reviewed.

Secondary:
- C:opus − C:rev.
- pass^1 of each arm with its 95% CI, and pass^3 of each arm.
- The tasks where the arms disagree most, read by hand for the report.

**Predictions,** from the train results (C:rev set 0.896 and 0.932; C:orev 0.964 and 0.955; C:opus 0.977 and 0.973). The test tasks are new, so some shrinkage is expected.

| quantity | expected |
|---|---|
| pass^1 of each C arm | 0.75 to 0.95 |
| C:rev − B | about +0.15 (somewhere in +0.05 to +0.25); CI above 0 |
| C:orev − B | about +0.15 (somewhere in +0.05 to +0.25); CI above 0 |
| C:opus − B | about +0.20 (somewhere in +0.10 to +0.30); CI above 0 |
| C:rev − C:orev | within ±0.07; CI includes 0 |
| C:opus − C:orev | within ±0.07; CI includes 0 |
| C:opus − C:rev | small and positive, under +0.10; CI may include 0 |

**Decision rules** (decided now, reported whatever happens):
- **Skills don't transfer:** if the 95% CIs of C:rev − B, C:orev − B and C:opus − B all include 0 or lie below it, the learned catalogs did not carry over to held-out tasks. The report says so.
- **Review path:** if the CI of C:rev − C:orev includes 0, human and model review produced catalogs that are indistinguishable on held-out tasks. If it excludes 0, the report names which reviewer did better and reads the disagreeing tasks to say why.
- **Writer against reviewer:** if the CI of C:opus − C:orev includes 0, reviewing a weaker model's proposals matched letting a stronger model write them. If C:opus is clearly ahead, the report says review did not close the gap to a stronger writer.
- **Shrinkage:** if any C arm's TEST gain over B is less than half its train gain over the train re-measure, the report flags that the catalog fit the train tasks more than it generalized.

Every catalog item passed the leakage screen when it was proposed. The reviewers' edits (human and Opus) were screened with the same screen.

- Date and commit of this entry: 2026-11-25, the commit that adds this text (see `git log -- PROTOCOL.md`).

### Results (TEST), recorded 2026-11-25

Run `tel-test`: 4 arms × 40 tasks × 3 trials = 480 episodes, finished 2026-11-25 at about 11:35 ET. 0 infrastructure errors, 974 Claude Code invocations, about $23.45 API-equivalent.

| arm | pass^1 [95% CI] | pass^3 [95% CI] | $ / episode |
|---|---|---|---|
| B (no skills) | 0.775 [0.675, 0.867] | 0.550 [0.400, 0.700] | 0.0429 |
| C:rev (human review + gate) | 0.925 [0.875, 0.967] | 0.800 [0.675, 0.925] | 0.0524 |
| C:orev (Opus review + gate) | 0.917 [0.867, 0.967] | 0.775 [0.650, 0.900] | 0.0516 |
| C:opus (Opus writes + gate) | 0.983 [0.958, 1.000] | 0.950 [0.875, 1.000] | 0.0485 |

**Predictions against outcomes** (pass^1, paired over the 40 tasks):

| quantity | predicted | observed | verdict |
|---|---|---|---|
| C:rev − B | +0.15 (+0.05 to +0.25); CI above 0 | +0.150 [+0.067, +0.242] | as predicted |
| C:orev − B | +0.15 (+0.05 to +0.25); CI above 0 | +0.142 [+0.067, +0.225] | as predicted |
| C:opus − B | +0.20 (+0.10 to +0.30); CI above 0 | +0.208 [+0.125, +0.300] | as predicted |
| C:rev − C:orev | within ±0.07; CI includes 0 | +0.008 [−0.042, +0.058] | as predicted |
| C:opus − C:orev | within ±0.07; CI includes 0 | +0.067 [+0.025, +0.108] | size as predicted, but the CI excludes 0: the prediction that it would be too close to call was wrong |
| C:opus − C:rev | small and positive, under +0.10; CI may include 0 | +0.058 [+0.008, +0.108] | as predicted |
| pass^1 of each C arm | 0.75 to 0.95 | 0.925, 0.917, 0.983 | C:opus is above the range |
| B (rough estimate, not registered) | 0.55 to 0.75 | 0.775 | slightly above |

**Decision rules:**
- **Skills don't transfer:** not met. All three CIs against B lie above 0, so every governed catalog carried over to held-out tasks.
- **Review path:** the CI of C:rev − C:orev includes 0. Human (AI-assisted) review and Opus review produced catalogs that are indistinguishable on held-out tasks.
- **Writer against reviewer:** C:opus is clearly ahead of C:orev (+0.067, CI +0.025 to +0.108). On this benchmark, reviewing a weaker model's proposals did not close the gap to having a stronger model write the skills.
- **Shrinkage:** the train gains (against the same day's no-skills re-measure) were C:rev +0.220, C:orev +0.297 and C:opus +0.301. The TEST gains over B kept 68%, 48% and 69% of them. C:orev kept less than half, so the rule flags it: its catalog fit the practice tasks more than it generalized. Context for the report: C:orev missed the half mark by 0.006, and its train gain was measured against the lowest no-skills re-measure of the study (0.667).

**Exploratory (not pre-registered):**
- By issue type (pass^1, B → C:rev / C:orev / C:opus):
  - MMS, 16 tasks: 0.62 → 0.88 / 0.81 / 0.96
  - mobile data, 9 tasks: 0.78 → 0.93 / 0.96 / 1.00
  - service, 15 tasks: 0.93 → 0.98 / 1.00 / 1.00
  - The gains concentrate where the agent without skills was weakest.
- No task got clearly worse with any catalog: no task has two or more fewer passes (of 3) than B.
- Both of Opus's skills are guardrails, inlined in `agent_startup`, so its agents almost never call `get_skill` (rate 0.008). Each reviewed Haiku catalog includes a procedure, fetched in 64% (C:rev) and 61% (C:orev) of conversations. Format, always in context against fetched on demand, may explain part of the gap. This study doesn't test it.
- Skills raised the API-equivalent cost per conversation by 13% (C:opus) to 22% (C:rev).

### Cost: small model with skills against a large model (added 2026-11-25 after the results; not pre-registered)

**Correction to the exploratory cost line above.** Its +13% to +22% per conversation included the simulated customer (Sonnet 5.5). That's 85–90% of each conversation's bill, and a deployment doesn't pay it. Counting the agent alone, the skills added 11% (C:opus) to 18% (C:rev, C:orev).

**Prices.** Anthropic's list API prices on 2026-11-25 (platform.claude.com pricing page), USD per million tokens:

| model | input | output | cache read | 1-hour cache write |
|---|---|---|---|---|
| Haiku 5.5 (prompts up to 100K tokens) | 0.10 | 0.50 | 0.01 | 0.20 |
| Sonnet 5.5 | 2 | 10 | 0.10 | 4 |
| Opus 5.5 | 4 | 20 | 0.20 | 8 |

- Claude Code writes the prompt cache with a 1-hour TTL.
- No agent call in `tel-test` passed Haiku's 100K long-prompt threshold: the longest agent prompt averaged about 22.6K tokens.

**Agent-only cost per conversation on TEST.**
- Haiku is measured; these figures equal the CLI's own `modelUsage.costUSD`.
- "Opus, same tokens" prices each episode's measured agent tokens at Opus rates. It's an estimate: Opus wasn't run as the agent.

| arm | pass^1 | Haiku, measured | Opus, same tokens (estimate) | ratio |
|---|---|---|---|---|
| B | 0.775 | $0.00677 | $0.231 | 34× |
| C:rev | 0.925 | $0.00798 | $0.265 | 33× |
| C:orev | 0.917 | $0.00796 | $0.259 | 33× |
| C:opus | 0.983 | $0.00750 | $0.250 | 33× |

- **Per solved conversation,** Haiku costs $0.0087 without skills and $0.0076 with Opus's skills.
- **Where the tokens go.** Per C:opus conversation, on average:
  - 13.1 agent calls
  - 249K cache-read tokens, most of them Claude Code's own system prompt, re-read on every call
  - 17.7K cache-write tokens
  - 2.9K output tokens
- **Why the ratio is near-fixed.** With this mix, the ratio is set by the list-price ratios: 20× for cache reads, 40× for input, cache writes and output. It barely depends on the counts. If Opus used fewer turns, its cost would fall roughly in proportion.
- **One-time cost of learning Opus's skills** (`tel-o`, all three rounds): $57.21 API-equivalent.
  - Haiku agent: $8.52
  - simulated customers: $47.80
  - Opus as improver: $0.89
  - Against an Opus agent without skills ($0.231 per conversation), Haiku with the skills saves about $0.224 per conversation, so the learning run pays back after about 255 conversations.
- **For comparison, the other learning runs:**
  - `tel-hr`: about $59.3, with Haiku as improver at $0.03
  - `tel-or`: $58.97, including $0.30 of Opus review
- **Limits.**
  - Opus's success rate as the agent isn't known.
  - The comparison claims only that Haiku with Opus-written skills reached 0.983 on TEST, at about 1/33 of the agent-token cost Opus would have on the same conversations.
  - A measured Opus arm would need a new, pre-registered run.


## 14. Governance observations (for the report)

The study's gate promotes automatically (`by=study:gate`). The product puts a person
in that seat: in OpenBrainStore's Catalog tab, a human approves or rejects each
proposal, with its evidence in front of them. This log records cases where an
automatic gate and a human reviewer could reasonably decide differently. Entries
are written when a proposal is made, before the gate's result is known.

- **2026-11-24, telecom round 1: the two improvers read the same failures in opposite directions.**
  - Haiku proposed the guardrail `use-device-tools-not-manual-steps`: "Do not tell the customer to perform device checks that your tools can perform."
  - Opus proposed the guardrail `customer-runs-device-checks-no-early-transfer`: "The customer can run every device check and fix; never transfer because you 'cannot see the phone'."
  - In τ² telecom the device tools belong to the user (dual control). The agent can only direct the customer. Haiku's premise therefore misreads the environment, and Opus's matches it.
  - The gate scores a proposal batch as a whole, so a batch can pass while one of its rules is wrong. A reviewer who knows the domain would reject or rewrite that rule.
  - Record each batch's gate verdict here once known, along with whether this guardrail was part of a promoted batch.

Possible follow-up arm: **C-reviewed**. Same as C, but every proposal also passes a human review step (approve, reject or edit) before promotion. Compare it with gate-only C on TEST to measure what governance adds beyond automatic selection.

### Reviewed arm (C-reviewed), set up 2026-11-24

`learn --review --seed-from tel-h` starts from the gate-only Haiku run's round-0 baseline and
uses its exact round-1 proposals, with no new improver call, so the only difference is the
human seat.
- **Each round:** the run writes `rounds/<r>/review.md` and stops. The reviewer records approve, reject or edit for each proposal that passed the automatic screens, in `review_decisions.json`.
- **Gate:** the reviewed batch then goes through the same gate. Promotions are recorded as `human:reviewer+study:gate`.
- **What the reviewer sees:** the proposal, the improver's hypothesis, and the domain's own documentation, as a reviewer would in OpenBrainStore's Catalog tab.
- **Automatic screens stay binding:** the reviewer can't override the leakage screen, because it protects the test set.

- **2026-11-24, telecom round 1: the automatic leakage screen rejected Haiku's `mms-failure-triage-order` checklist** for containing `4g_5g_preferred`.
  - That string is a value of the user's `set_network_mode_preference` tool, which also appears in a test task's expected actions. It's domain vocabulary, not a leak, so this is a false positive of a blunt automatic filter.
  - It stays rejected in every arm, so the arms remain comparable.
- **Fact for the guardrail review:** in τ² telecom every device tool belongs to the customer. That's 30 tools, including `check_status_bar`, `toggle_airplane_mode`, `reset_apn_settings` and `can_send_mms`.
  - The agent has only account tools: customer lookup, line suspend/resume, roaming enable/disable, `get_data_usage`, `refuel_data`, bills and payment requests, and transfer.
  - Haiku's `use-device-tools-not-manual-steps` guardrail tells the agent these device tools are its own.

### Gate results (train), recorded 2026-11-24

- **Haiku gate-only (tel-h), round 1: rejected.**
  - The candidate scored 0.360. The fresh re-measure of the current (empty) catalog scored 0.703. Delta −0.342; floor 0.058.
  - The wrong guardrail was never promoted. Rejecting the batch also discarded `data-limit-refuel-check`, which the reviewed arm kept.
- **Haiku gate-only (tel-h), round 2: the improver repeated the misreading.**
  - The two new procedures tell the agent to call `reboot_device`, `check_apn_settings`, `grant_app_permission` and `can_send_mms` itself. All four are customer tools.
  - The round-1 rejection was in the edit log the improver read, and it still repeated the error.
  - First 75 candidate episodes: 0.03, against a baseline of 0.44 on the same conversations.
  - A gate catches such a batch only after measuring it for a full round. A reviewer catches it on reading.
- **Correction to an earlier note: Haiku does call `get_skill`.**
  - An earlier tally read a field that doesn't exist (`get_skill` instead of `get_skill_calls`). That made it look like no episode fetched a skill.
  - Real counts:

    | Run, round | Episodes that fetched a skill |
    |---|---|
    | tel-h r1 | 184/222 |
    | tel-h r2 | 73/75 |
    | tel-hr r1 | 182/204 |
    | tel-o r1 | 7/222 |

  - Haiku fetches procedures and checklists named in the `agent_startup` index. It rarely fetches guardrails, because their text is already inlined.
  - Opus's round-1 items were all guardrails, so its run barely needs `get_skill`.
  - Consequence: a wrong procedure is fetched and followed. That's how tel-h r2 falls to 0.03.
- **Run-to-run drift is larger than the round-0 noise floor.**
  - The same empty catalog re-measured hours later scored 0.703 against 0.626 in tel-h, and 0.66 against 0.70 in tel-o.
  - The floor (0.058) comes from within-run trial SD, which understates the difference between sessions.
  - The gate's comparison against a fresh re-measure (`--remeasure-current`) is what keeps this drift from passing as a gain.
  - TEST arms should run interleaved, not one after another.

### Later rounds (train), recorded 2026-11-24 evening

- **Gate only, Haiku (`tel-h`), round 2: rejected.** The candidate scored 0.450, against a re-measure of 0.694. Round 3 is running.
- **Opus teacher (`tel-o`): finished.**
  - Round 2 was rejected at the ceiling: 0.995 against 0.973, a gain of 0.023, under the 0.069 floor. Its rejected items were a procedure making roaming two-sided (line and phone) and a guardrail on reading device checks.
  - In round 3 the only proposal, a pre-transfer checklist, was screened out for `4g_5g_preferred`.
  - Final promoted set: the two round-1 guardrails. Train score 0.977.
- **Reviewed (`tel-hr`), round 1: PROMOTED.**
  - The candidate scored 0.896, against a re-measure of 0.676 (gain +0.221, floor 0.058).
  - It was recorded as `human:reviewer+study:gate`.
- **`tel-hr` round 2: no clean proposals.**
  - The only proposal, a pre-transfer MMS checklist, was screened out for `4g_5g_preferred` and also for `transfer_to_human_agents`.
  - `transfer_to_human_agents` is the agent's own transfer tool, so this was a second false positive.
- **`tel-hr` round 3: edited by the reviewer.**
  - The proposal was Haiku's `mms-check-order-and-gate` checklist.
  - Checked against `tech_support_manual.md`, it was correct, apart from two gaps:
    - It checked roaming only on the line. The manual also requires the phone's Data Roaming switch to be ON when abroad (manual §"Addressing Data Roaming Problems").
    - It didn't say that the phone steps are the customer's to run.
  - The reviewer chose to edit both. The edited text passed the same leakage screen before it was saved.
- **A string screen can be worded around.**
  - The round-3 proposal wrote "4G/5G preferred mode" instead of the literal enum value, and passed.
  - Here the value is harmless domain vocabulary, but this shows the screen catches exact strings, not meaning.
  - A reviewer, or a semantic check, covers what a string match misses.
- **Gate only, Haiku (`tel-h`): finished with nothing promoted.**
  - Round 3 was rejected: the candidate scored 0.532, against a re-measure of 0.712.
  - The round-3 batch again contained the device-tools misreading, now as the guardrail `device-checks-are-tool-calls` ("Run device diagnostics and fixes with tools, not manual steps for the customer"). That makes three rounds out of three.
  - The run used 3,111 Claude invocations. Its final catalog is empty, so gate-only C on TEST is the same configuration as arm B.
- **Reviewed (`tel-hr`), round 3: rejected at the ceiling.**
  - The reviewer-edited checklist scored 0.950, against a re-measure of 0.932 for the round-1 set. That's a gain of 0.018, under the 0.058 floor.
  - The round-1 set itself scored 0.896 when it was promoted and 0.932 on this re-measure, with nothing changed: another sign of drift.
  - Final promoted set: `guardrail:use-device-tools-not-manual-steps@v2` (the reviewer's edit) and `procedure:data-limit-refuel-check@v1` (approved as written). Train score at promotion was 0.896.
  - The run used 1,778 Claude invocations. Rounds 0 and 1 were shared with `tel-h` through `--seed-from`, so they aren't counted again.
- **Opus round 3, re-checked:** its screened proposal `pre-transfer-settings-audit` matched only `4g_5g_preferred`, the same false positive as `tel-hr` round 1.

### Training phase: summary (2026-11-24)

| run | improver | review | promoted | train score of final set |
|---|---|---|---|---|
| `tel-h` | Haiku | gate only | nothing; rounds 1–3 rejected (0.360, 0.450, 0.532 against re-measures of 0.703, 0.694, 0.712) | n/a (empty, = arm B) |
| `tel-hr` | Haiku | human + gate | round 1: 1 edited guardrail + 1 approved procedure | 0.896 (re-measured 0.932) |
| `tel-o` | Opus | gate only | round 1: 2 guardrails | 0.977 (re-measured 0.973) |
| `tel-or` | Haiku | Opus 5.5 review + gate | round 1: 1 edited guardrail + 1 edited procedure | 0.964 (re-measured 0.955) |

- Both runs that promoted anything did so in round 1 and then hit the ceiling. Their later candidates gained 1.8 to 2.3 points, under floors of 5.8 to 6.9.
- Gate-only Haiku wrote the same wrong premise (the agent operates the customer's phone) in all three rounds. The gate kept it out, but nothing useful got in either.

### Model-reviewed arm (`tel-or`), set up 2026-11-24 late evening

- **Design:** `learn --seed-from tel-h --reviewer-model claude-opus-5-5`. It is the same as `tel-hr`, except that Opus 5.5 takes the reviewer's seat (`tau2_skills/reviewer.py`).
  - Each round it sees the proposals that passed the screens, the proposer's hypotheses, the policy (main policy plus tech-support manual), the agent's 13 tools and the customer's 30 tools, the current catalog and the edit log.
  - It doesn't see transcripts or any TEST data.
  - Edits are leakage-screened automatically. An edit that trips the screen becomes a reject.
  - Promotions are recorded as `model:claude-opus-5-5+study:gate`.
- **Caveat on the human-reviewed arm.** The human reviewer works at OpenBrainStore and has no background in telecom support. Their decisions in `tel-hr` were assisted by Claude Opus 5.5, the coding assistant running this study.
  - In round 1 the assistant pointed out that the device tools belong to the customer and drafted the replacement guardrail text.
  - In round 3 it checked the checklist against the manual and drafted the two fixes.
  - The reviewer chose each decision from options the assistant laid out. `tel-hr` is therefore "human + AI-assisted review", not unaided human review.
  - `tel-or` measures the AI reviewer alone. The difference between the two catalogs on TEST shows what the human decision added.
- **`tel-or` round 1: the model reviewer edited both proposals.** These decisions were recorded before the gate's result was known.
  - **`use-device-tools-not-manual-steps`, edited:** "The body wrongly says the device tools belong to the agent. They are customer-side tools."
    - This is the same flaw the human-assisted review caught. It was rewritten as "guide the customer through their device tools" and lists the customer's check and fix tools by name.
  - **`data-limit-refuel-check`, edited.** The human-assisted review had approved this one as written. Opus found two problems:
    - Its last line has the agent "use `can_send_mms` to confirm", but that's a customer tool.
    - It offers a plan change, but the agent's 13 tools can't apply one.
    - The rewrite drops the plan-change option, states the 2GB refuel cap, and has the customer re-check on their own device.
  - **Note for the report:** the reviewer was given which tools belong to the agent and which to the customer, which any product reviewer would have from the agent's tool manifest. That list is what makes the device-tools misreading detectable. The improver saw only the policy and the transcripts.
- **Interruption and fix (2026-11-25, 00:08).** The run stopped after the full candidate measurement (222/222) and 21 of the 222 current-set re-measures.
  - Cause: one 529 "overloaded" reply to the user simulator matched the rate-limit pattern. The harness then took the seven-day window's reset (Nov 28) as the pause, and the pacer stopped the run. Plan utilization was 15%.
  - Fix (commit 6e2ba98): a window's reset is used only when that window is actually exhausted; otherwise the run backs off and retries. A unit test covers it.
  - The run resumed from its checkpoint with the same command. No episode was re-run or dropped, and nothing about the arms changed.
- **`tel-or` round-1 candidate, measured before the gate's comparison: train pass^1 = 0.964** (222 episodes; it loaded a skill in 82% of them). For reference, on the same 74 tasks × 3:
  - baseline 0.626
  - `tel-hr` round-1 candidate (human + AI-assisted review) 0.896
  - `tel-o` final (Opus teacher) 0.977
  - The 6.8-point gap to `tel-hr` is about the size of the run-to-run drift recorded above, so it isn't evidence by itself. The interleaved TEST evaluation (§13) is the comparison that counts.

### `tel-or` results (train), recorded 2026-11-25

- **Round 1: promoted.** Candidate 0.964 against a re-measure of 0.667. The gain is +29.7 points, against a floor of 5.8.
- **Round 2: no candidate.** Haiku's only proposal (an MMS triage order) was screened out on `4g_5g_preferred`, the same false positive as before, so there was nothing to review.
- **Round 3: rejected by the gate.** Candidate 0.973 against a re-measure of 0.955. The gain is +1.8 points, under the floor: the same ceiling as `tel-hr` and `tel-o`.
  - The MMS triage proposal was screened out again.
  - Opus edited the other two before they were measured. Both corrections were checked against the domain files:
    - **`speed-test-poor-only-vpn-rule`.** Haiku's guardrail would have told the agent that a Good speed is fine, to disconnect a VPN only on a Poor *speed test*, and to offer a transfer when the customer wants more. The tech-support manual says "Any speed below 'Excellent' is considered slow". It ties `disconnect_vpn` to the VPN's own performance level being Poor in `check_vpn_status`. Opus rewrote the guardrail to match.
    - **`roaming-enable-when-traveling`.** Haiku's procedure enabled roaming on the line and told the agent *not* to ask for the phone's `toggle_roaming` in its place. The manual requires both: Data Roaming ON on the phone, and roaming enabled on the line. Opus added the device step.
- **Final set:** `data-limit-refuel-check@v2` and `use-device-tools-not-manual-steps@v2`, both edited by Opus and promoted as `model:claude-opus-5-5+study:gate`. Train score 0.964.
- **Cost:** 1,787 Claude Code invocations for the run. The two review calls cost about $0.15 each at API-equivalent prices, so about $0.30 of model review for the whole run.
- **What it shows, on train only:**
  - The model reviewer caught the fatal device-tools premise in round 1, as the AI-assisted human review did.
  - It also caught three further errors that would otherwise have reached the gate: two in round 1 and the VPN contradiction in round 3. Under gate-only promotion, a proposal like the VPN guardrail could pass the gate whenever it doesn't cost enough train points. It would then sit in the catalog contradicting policy.
  - Whether these catalogs differ on held-out tasks is for the TEST evaluation (§13).

## 15. Follow-up: Opus as the agent (TEST), pre-registration

**Agreed on 2026-11-25, as drafted. No episode of this run had been started when this entry was committed.**

**Why.** The post-hoc cost analysis (§13) estimated that Opus as the agent would cost about 33 times Haiku with Opus-written skills. That estimate prices Haiku's token counts at Opus rates. This run measures Opus's success rate and token cost directly, and puts Haiku with Opus's skills next to it in the same interleaved run, so the comparison doesn't cross runs or days.

**Arms.** All three run in one interleaved `evaluate` run (`--run tel-test-opus`), arm by arm within each task.

| arm | agent | catalog | stands for |
|---|---|---|---|
| B | `claude-opus-5-5` | OBS connected, empty | Opus alone |
| C:opus | `claude-opus-5-5` | `tel-o` final: `always-check-data-limit-on-data-issues@v1` + `customer-runs-device-checks-no-early-transfer@v1` | Opus with the skills it wrote |
| C:small | `claude-haiku-5-5` (via `--arm-model`) | the same `tel-o` catalog | Haiku with Opus's skills, re-measured alongside the Opus arms |

- These are the skills `tel-o` learned with Haiku as the agent. Opus uses them as they are; nothing is re-learned for Opus.
- B here is Opus with an empty catalog, not the Haiku baseline of `tel-test`.

**Setup.**
- Telecom TEST: 40 tasks, k = 3, so 3 arms × 120 = 360 episodes.
- Agent effort medium for both models. User simulator `claude-sonnet-5-5`, effort medium, as in every other run. Concurrency 3, `--stop-at-utilization 0.7`.
- Command:

  ```
  python -m tau2_skills evaluate --run tel-test-opus --domain telecom --agent-model claude-opus-5-5 --catalog opus=runs/tel-o/learn/obs --catalog small=runs/tel-o/learn/obs --arms B,C:opus,C:small --arm-model C:small=claude-haiku-5-5 --k 3 --user-model claude-sonnet-5-5 --concurrency 3 --stop-at-utilization 0.7 --max-invocations 1400 --max-run-usd 120
  ```

- Expected usage, API-equivalent: about $60 for the Opus agent (240 episodes at about $0.25), under $1 for Haiku and about $14 for the simulated customer, so about $75.

**Primary comparisons** (pass^1, paired over the 40 tasks, 95% bootstrap CI as in §9):
1. **C:small − B**: Haiku with Opus's skills against Opus alone. This is the cost question.
2. **C:opus − B**: what the skills add for the model that wrote them.

Secondary:
- C:opus − C:small.
- pass^3 for each arm.
- The agent's measured token cost per conversation and per solved conversation, for each arm. This uses `tools/agent_cost.py`, which counts the agent only, at the §13 prices.
- C:small against `tel-test`'s C:opus (0.983): a cross-run replication.

**Predictions:**

| quantity | expected |
|---|---|
| pass^1 of B (Opus alone) | about 0.90 (0.80 to 0.97) |
| C:opus − B | about +0.05 (−0.02 to +0.12); CI may include 0 |
| C:small − B | about +0.05 (−0.05 to +0.12) |
| pass^1 of C:small | 0.93 to 1.00 |
| measured agent cost, Opus arms ÷ C:small | about 30× (20× to 40×) |

**Decision rules** (decided now, reported whatever happens):
- **Parity.**
  - If the lower end of the 95% CI of C:small − B is above −0.05, Haiku with Opus's skills does at least as well as Opus alone, within 5 points, at the measured cost ratio. The report then states the cost saving as measured.
  - If the lower end is at or below −0.05, the report gives the measured gap and compares cost per solved conversation instead.
- **Skills help the writer too:** if the CI of C:opus − B lies above 0, the skills help even the model that wrote them.
- **Replication:** if C:small lands more than 0.07 below `tel-test`'s 0.983, the report flags run-to-run drift and gives both numbers.
- **Cost:** the measured ratio replaces the §13 estimate in the report, whichever way it falls.

- Date and commit of this entry: 2026-11-25, the commit that adds this text (see `git log -- PROTOCOL.md`).

### Results (TEST), recorded 2026-11-25

Run `tel-test-opus`: 3 arms × 40 tasks × 3 trials = 360 episodes, finished 2026-11-25 at about 15:08 ET. 0 infrastructure errors. $58.72 API-equivalent in all, including the simulated customer.

Agent cost below is the agent alone (`tools/agent_cost.py`, §13 prices). It equals the CLI's own cost for each agent process.

| arm | agent | pass^1 [95% CI] | pass^3 [95% CI] | agent $ / conversation | agent $ / solved conversation |
|---|---|---|---|---|---|
| B (empty catalog) | Opus | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.1770 | 0.1770 |
| C:opus (`tel-o` skills) | Opus | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.1912 | 0.1912 |
| C:small (`tel-o` skills) | Haiku | 0.975 [0.933, 1.000] | 0.950 [0.875, 1.000] | 0.0074 | 0.0076 |

**Predictions against outcomes** (pass^1, paired over the 40 tasks):

| quantity | predicted | observed | verdict |
|---|---|---|---|
| pass^1 of B (Opus alone) | about 0.90 (0.80 to 0.97) | 1.000 | wrong: above the range. Opus solved all 120 conversations |
| C:opus − B | about +0.05 (−0.02 to +0.12); CI may include 0 | 0.000 [0.000, 0.000] | inside the range, below the point estimate. With B at 1.000 there was no room to gain |
| C:small − B | about +0.05 (−0.05 to +0.12) | −0.025 [−0.067, 0.000] | inside the range, but the sign is wrong: Haiku with Opus's skills was slightly behind Opus alone |
| pass^1 of C:small | 0.93 to 1.00 | 0.975 | as predicted |
| agent cost, Opus arms ÷ C:small | about 30× (20× to 40×) | 23.9× (B), 25.8× (C:opus) | inside the range, below the point estimate |

**Decision rules:**
- **Parity: not met.** The lower end of the CI of C:small − B is −0.067, below −0.05. As the rule says, the report gives the measured gap and compares cost per solved conversation:
  - Gap: −2.5 points pass^1 [−6.7, 0.0]; −5.0 points pass^3 [−12.5, 0.0].
  - Cost per solved conversation: $0.0076 (Haiku with Opus's skills) against $0.1770 (Opus alone), 23× less.
- **Skills help the writer too: not met.** The CI of C:opus − B is [0.000, 0.000]. Both arms solved everything, so this test can't show a gain for Opus.
- **Replication: no drift.** C:small reached 0.975 here against 0.983 for the same configuration in `tel-test`, a difference of 0.008.
- **Cost:** the measured ratio, about 24×, replaces the §13 estimate of 33×.

**Exploratory (not pre-registered):**
- **Haiku's misses.** All three are MMS tasks with the Hard customer persona: two tasks, one of them missed twice. On every other task Haiku with Opus's skills matched Opus.
  - `[mms_issue]airplane_mode_on|bad_network_preference|bad_wifi_calling|break_apn_mms_setting|break_app_both_permissions|unseat_sim_card|user_abroad_roaming_enabled_off[PERSONA:Hard]`, trials 0 and 1
  - `[mms_issue]airplane_mode_on|bad_network_preference|break_app_both_permissions|data_usage_exceeded|unseat_sim_card|user_abroad_roaming_disabled_on[PERSONA:Hard]`, trial 1
- **Why the measured ratio is below the estimate.** Opus used fewer tokens per conversation than Haiku did.

  | per conversation | Opus (B) | Haiku (C:small) |
  |---|---|---|
  | API calls | 10.4 | 12.8 |
  | cache-read tokens | 168K | 239K |
  | output tokens | 1.5K | 2.8K |

  Priced at Opus rates, Haiku's token counts in this run would cost $0.248 per conversation, which reproduces the §13 estimate. So the estimate's error came from the token counts, not the prices.
- **Skills on Opus** added 8% to its agent cost (C:opus against B), with no measurable gain on this test.
- **Cross-run comparison (not paired).** `tel-test`'s Haiku without skills scored 0.775. With Opus's skills Haiku reached 0.975 here and 0.983 there. That closes about 89–92% of the gap to Opus's 1.000, at about 1/24 of Opus's agent cost.
- **Payback, against the measured Opus cost.** Haiku with the skills saves about $0.170 per conversation. The $57.21 learning run (`tel-o`) pays back after about 340 conversations.
- **Limits.**
  - Opus is at the ceiling on these 40 tasks. The run can't say whether skills would help Opus on harder tasks, or how large the Opus–Haiku gap would be there.
  - One domain (telecom), one benchmark.
