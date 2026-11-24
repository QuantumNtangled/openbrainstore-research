"""The improver: reads TRAIN failures and proposes catalog items as JSON. The harness (not the
model) screens them and submits the clean ones through OBS's propose path as drafts."""

import json
import re
from pathlib import Path

from .claude_cli import ClaudeConfig, InvocationBudget, prompt_args, run_print

KINDS = ("skill", "procedure", "guardrail", "checklist")

SYSTEM = """You improve the skill catalog of a customer-service AI agent. The agent follows a \
written domain policy and uses tools. A catalog item is short, reusable guidance the agent \
loads at the start of a conversation: a skill, procedure, guardrail or checklist.

You will see the policy, the current catalog, earlier proposals and their verdicts, and \
transcripts of conversations the agent got wrong, with what the grader found.

Rules for every item you propose:
- Teach procedure that transfers to new customers and new requests. Never include anything \
specific to one conversation: no names, user ids, order, reservation, item or payment ids, \
emails, phone numbers, addresses, zip codes, dates, amounts or flight numbers. Items \
containing such strings are rejected automatically.
- Do not restate the policy wholesale. Target the specific mistakes the transcripts show.
- Do not re-propose an idea that the edit log shows was rejected.
- Each item: slug (lowercase letters, digits, hyphens), kind (skill | procedure | guardrail | \
checklist), title (<= 120 chars), when_to_use (one sentence, <= 40 words), body (Markdown, \
<= 1,200 words), hypothesis (which failure it should fix and why), evidence (episode ids \
from the transcripts it is based on).

Reply with only a JSON object in a ```json fenced block:
{"proposals": [{"slug": "...", "kind": "...", "title": "...", "when_to_use": "...", \
"body": "...", "hypothesis": "...", "evidence": ["epi_..."]}]}"""


def render_episode(ep: dict, max_chars: int = 7000) -> str:
    """A failed train episode as the improver sees it: the conversation and the grader's view."""
    sim = ep.get("simulation") or {}
    lines = []
    for m in sim.get("messages") or []:
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                lines.append(f"agent -> {tc['name']}({json.dumps(tc.get('arguments'), ensure_ascii=False)})")
            if m.get("content"):
                lines.append(f"agent: {m['content']}")
        elif role == "user":
            for tc in m.get("tool_calls") or []:
                lines.append(f"customer -> {tc['name']}({json.dumps(tc.get('arguments'), ensure_ascii=False)})")
            if m.get("content"):
                lines.append(f"customer: {m['content']}")
        elif role == "tool":
            c = (m.get("content") or "")
            lines.append(f"  tool result{' (error)' if m.get('error') else ''}: "
                         f"{c[:400] + ('...' if len(c) > 400 else '')}")
    convo = "\n".join(lines)
    if len(convo) > max_chars:
        convo = convo[: max_chars // 2] + "\n[... middle of conversation omitted ...]\n" + convo[-max_chars // 2:]
    ri = sim.get("reward_info") or {}
    grader = [f"termination: {ep.get('termination')}", f"reward: {ep.get('reward')}",
              f"graded on: {', '.join(ep.get('graded_basis') or [])}"]
    db = ri.get("db_check")
    if db:
        grader.append(f"final database matches the expected one: {db.get('db_match')}")
    for a in ri.get("action_checks") or []:
        act = a.get("action") or {}
        grader.append(f"expected action {act.get('name')}({json.dumps(act.get('arguments'), ensure_ascii=False)}) "
                      f"-> {'done' if a.get('action_match') else 'NOT done as expected'}")
    for c in ri.get("communicate_checks") or []:
        grader.append(f"should have told the customer: {c.get('info')!r} -> {'yes' if c.get('met') else 'NO'}")
    for c in ri.get("env_assertions") or []:
        grader.append(f"env assertion {c.get('env_assertion', {}).get('func_name', '')}: "
                      f"{'met' if c.get('met') else 'NOT met'}")
    return (f"### Episode {ep['episode_id']}\n\nConversation:\n{convo}\n\nGrader:\n- "
            + "\n- ".join(grader))


def build_prompt(domain: str, policy: str, catalog_md: str, edit_log: list[dict],
                 failures: list[dict], max_edits: int) -> str:
    log_lines = [f"- round {e['round']}: {e.get('slug')} ({e.get('kind')}) - {e.get('verdict')}; "
                 f"hypothesis: {e.get('hypothesis', '')[:300]}" for e in edit_log]
    return "\n\n".join([
        f"# Domain: {domain}",
        "## Policy the agent follows\n\n" + policy,
        "## Current catalog (promoted items)\n\n" + (catalog_md or "(empty)"),
        "## Edit log (earlier proposals and verdicts)\n\n" + ("\n".join(log_lines) or "(none yet)"),
        f"## Failed conversations ({len(failures)})\n\n" + "\n\n".join(render_episode(f) for f in failures),
        f"## Your task\n\nPropose at most {max_edits} new catalog items (or new versions of "
        "existing slugs) that would have prevented these failures without hurting other "
        "conversations. Fewer, sharper items beat many vague ones. Reply with the JSON block only.",
    ])


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse(text: str) -> list[dict]:
    m = _FENCE.search(text or "")
    raw = m.group(1) if m else (text or "")[(text or "").find("{"):]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    props = data.get("proposals") if isinstance(data, dict) else None
    return [p for p in props or [] if isinstance(p, dict)]


def call(cfg: ClaudeConfig, prompt: str, workdir: Path, log_dir: Path, budget: InvocationBudget,
         label: str) -> tuple[list[dict], dict]:
    args = cfg.common_args(streaming=False) + prompt_args(
        "--system-prompt", SYSTEM, cfg.prompt_transport, log_dir / "improver.system_prompt.md")
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "improver_prompt.md").write_text(prompt, encoding="utf-8")
    ev = run_print(cfg, args, prompt, workdir, log_dir, "improver", budget, label)
    return parse(ev.get("result") or ""), ev
