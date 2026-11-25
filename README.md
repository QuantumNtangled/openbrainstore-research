# OpenBrainStore Research

Open research on agentic AI: how agents learn from experience, remember, use tools and work
together, and how to keep that safe and affordable. Each result comes with the code, protocol
and records behind it, so you can check it or run it yourself.

## How the studies work

- **Self-contained.** Each study lives in its own folder under `studies/`, with its harness,
  protocol, tests and setup.
- **Real agents.** Studies run agents the way people deploy them, through real agent
  harnesses and tools, not hand-rolled API loops. Each study's README says which harness,
  models and tools it uses.
- **Written down first.** Each protocol records the design and how results will be judged
  before the results come in. Anything decided afterwards is labelled as such.
- **Paired and repeated.** Setups being compared run side by side on the same tasks, with
  several tries per task, and results are reported with confidence intervals.

## Studies

| study | question | status |
|---|---|---|
| [tau2-governed-skills](studies/tau2-governed-skills) | How to govern self-learning agents: the skills an agent writes from its own failures, approved by a score check, a human reviewer, an AI reviewer or a stronger writer. On τ²-bench telecom, a small model with the right skills came within 2.5 points of the strongest model at about 1/24 of its cost. | final |

## Running a study

Each study's README lists what it needs and the exact commands, and each has offline tests
that need no login and make no model calls. CI runs them on Windows, macOS and Linux.

## License

[Apache License 2.0](LICENSE); see [NOTICE](NOTICE). Use the code, protocols and records in
your own research, commercial or not. A study may install other software, such as a benchmark
or a memory server, under that software's own license; its README says which.
