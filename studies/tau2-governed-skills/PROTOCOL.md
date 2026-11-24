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

The runs use the reviewer's Claude subscription, so dollars below are **API-equivalent** (what the same
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
  `--stop-at-utilization` (default 0.9; use 0.7–0.8 to leave room for the reviewer's own work).
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

- Prediction (direction and size of C − B at pass^1, per domain):
- Kill criterion (what result means the catalog does not help, e.g. the 95% CI of C − B
  includes 0 on every domain, or C − D < 0):
- Domains, k, and user-simulator model actually used:
- Date and commit of this entry:

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
