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

**DRAFT, awaiting the reviewer's agreement. No TEST episode has been run.**

The learning rounds were run on telecom only (`tel-h`, `tel-hr`, `tel-or`, `tel-o`; results in §14).

**Arms.** All arms run in one interleaved `evaluate` run: arm by arm within each task, so drift falls on every arm equally.

| arm | catalog | how it got there |
|---|---|---|
| A | no OBS | plain Haiku with the tau2 prompt |
| B | OBS, empty | also stands for **gate-only Haiku**, whose final catalog is empty (§14) |
| C:auto | Haiku's round-1 batch as written: `use-device-tools-not-manual-steps@v1` + `data-limit-refuel-check@v1` | **auto-approval**: the agent's own proposals go live with no gate and no review. Only the leakage screen applies, because it protects the test set. |
| C:rev | `tel-hr` final: the reviewer's edit `use-device-tools-not-manual-steps@v2` + `data-limit-refuel-check@v1` | **human review + gate** |
| C:orev | `tel-or` final: `use-device-tools-not-manual-steps@v2` + `data-limit-refuel-check@v2`, both edited by Opus (train 0.964) | **model review + gate**: Opus 5.5 in the reviewer's seat for Haiku's proposals, with the same inputs and the same gate |
| C:opus | `tel-o` final: `always-check-data-limit-on-data-issues@v1` + `customer-runs-device-checks-no-early-transfer@v1` | **stronger skill writer + gate** |

- **Arm D is not run.** Every promoted item that matters here is a guardrail, and guardrails are already inlined in `agent_startup`. So C − D would mostly measure tool overhead. It's left for a follow-up.
- **The `C:auto` catalog** is built from `runs/tel-h/learn/rounds/r1/improver_output.json`, exactly as written. Its items are promoted with `by=study:auto-approve` into a fresh data dir.

**Setup.**
- Telecom TEST: 40 tasks, k = 4 trials per task per arm, so 6 arms × 160 = 960 episodes.
- Agent: `claude-haiku-5-5`, effort medium. User simulator: `claude-sonnet-5-5`, effort medium.
  - The simulator is the same as in training. It differs from §6's default (Haiku), which was changed before the learning runs.
- Concurrency 4, so each task's 4 trials run in one wave. `--stop-at-utilization 0.7`.

**Primary comparisons** (pass^1, paired over tasks, 95% bootstrap CI as in §9):
1. **C:rev − B**: what human review adds over gate-only, for Haiku-written skills.
2. **C:auto − B**: what auto-approval does.
3. **C:orev − B**: what a model reviewer adds over gate-only, for the same Haiku proposals.
4. **C:opus − B**: what a stronger skill writer does, with gate-only approval.

Secondary:
- C:rev − C:auto: review against auto-approval of the same round-1 proposals.
- C:rev − C:orev: human (AI-assisted) review against AI review alone.
- C:opus − C:rev.
- B − A.
- pass^4 for every arm.

**Predictions,** from the train results (no-skills re-measures 0.676–0.712; C:auto set 0.360; C:rev set 0.896 and 0.932; C:opus set 0.977 and 0.973). The test tasks are new, so some shrinkage is expected.

| comparison | expected pass^1 difference |
|---|---|
| C:rev − B | about +0.15 (somewhere in +0.05 to +0.25); CI above 0 |
| C:auto − B | about −0.25 (somewhere in −0.40 to −0.10); CI below 0 |
| C:orev − B | about +0.15 (somewhere in +0.05 to +0.25); CI above 0, if Opus rejects or fixes the device-tools premise as the human review did |
| C:opus − B | about +0.20 (somewhere in +0.10 to +0.30); CI above 0 |
| C:opus − C:rev | positive but small; CI may include 0 |
| B − A | within ±0.05 |

**Kill criteria** (decided now, reported whatever happens):
- **Skills don't transfer:** if the 95% CIs of C:rev − B, C:orev − B and C:opus − B all include 0 or lie below it, the learned catalogs did not carry over to held-out tasks. The report says so.
- **Auto-approval isn't harmful on held-out tasks:** if the CI of C:auto − B includes 0 or lies above it, the train-split harm did not generalize. The governance claim is then limited to the train split.
- **Review adds nothing:** if C:rev − C:auto has a CI that includes 0, review made no difference on held-out tasks.

Every catalog item passed the leakage screen when it was proposed. The reviewer's edits were screened by hand with the same screen.

- Date and commit of this entry: (filled in when agreed)

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
- **Caveat on the human-reviewed arm.** The reviewer's reviews in `tel-hr` were assisted by Claude Opus 5.5, the coding assistant running this study.
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
