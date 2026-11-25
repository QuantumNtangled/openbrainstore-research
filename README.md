# OpenBrainStore Research

Studies of long-term memory and skills for AI agents, with the code, protocols and records
behind each result. The write-ups live at
[openbrainstore.com/research](https://openbrainstore.com/research).

Each study is self-contained in its own folder under `studies/`: its harness, its protocol
(with predictions written down before the results), its tests and its results. Agents in
these studies are real coding agents, not API loops: the harness runs **Claude Code headless**
(`claude -p`) for every role and connects it to the benchmark and to
[OpenBrainStore](https://openbrainstore.com) over MCP, the same way you'd connect your own.

## Studies

| study | question | status |
|---|---|---|
| [tau2-governed-skills](studies/tau2-governed-skills) | When an agent writes its own skills, who should approve them: a score check, a person, or another AI? Measured on τ²-bench telecom with Claude Haiku 5.5. | practice results final; held-out test in progress |

## Running a study

Every study runs on Windows, macOS and Linux with Python 3.12 or 3.13 and a signed-in Claude
Code. Each folder has a `bootstrap.py` for setup and a README with the exact commands. The
offline tests use a scripted stand-in for Claude Code, so they need no login and make no model
calls; they run on all three systems in CI.

To try a study's agent against your own memory, sign in at
[openbrainstore.com/app](https://openbrainstore.com/app), create an agent key and point the
harness at `https://openbrainstore.com/mcp`. Each study's README shows how.

## License

[Apache License 2.0](LICENSE); see [NOTICE](NOTICE). Use the code, protocols and records in
your own research, commercial or not.

The studies install other software under its own license: OpenBrainStore itself
([openbrainstore-core](https://github.com/QuantumNtangled/openbrainstore-core), PolyForm Small
Business 1.0.0) for local runs, and each benchmark from its own source at setup.
