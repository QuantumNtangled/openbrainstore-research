"""A model in the reviewer's seat (`learn --review --reviewer-model M`).

It sees what the human reviewer saw: the proposals that passed the automatic screens with
the proposer's hypothesis, the domain policy, which tools belong to the agent and which to
the customer, the current catalog and the earlier verdicts. It does not see transcripts or
any TEST data. For each proposal it answers approve, edit (with the full corrected text) or
reject, in the same shape as a human's review_decisions.json. The harness screens every
edit for leakage before it is saved, and the same gate decides afterwards."""

import json
import re
import time
from pathlib import Path

from .claude_cli import ClaudeConfig, InvocationBudget, prompt_args, run_print

SYSTEM = """You review proposed catalog items for a customer-service AI agent, before they \
are tested and promoted. You are the approver: an item you let through will be loaded by \
the agent in every future conversation, so a wrong item does real harm.

You will see the domain policy the agent follows, the tools the agent can call, the tools \
the customer can use (if the domain gives the customer any), the current catalog, earlier \
proposals and their verdicts, and the new proposals with the proposer's hypothesis.

For each proposal decide one of:
- approve: correct and useful as written.
- edit: the idea is useful but the text is wrong, incomplete or unclear. Give the full \
corrected title, when_to_use and body.
- reject: wrong, harmful, redundant with the catalog, or not useful.

Check every claim in an item against the policy and the tool lists. An item must not tell \
the agent to do anything the policy forbids, or to call a tool it does not have. Nothing \
specific to one conversation: no names, ids, emails, phone numbers, addresses, dates or \
amounts. Edits containing such strings are rejected automatically.

Reply with only a JSON object in a ```json fenced block:
{"decisions": {"<slug>": {"decision": "approve | edit | reject", "note": "why, in one or \
two sentences", "title": "(edit only)", "when_to_use": "(edit only)", "body": "(edit only)"}}}"""


def build_prompt(domain: str, policy: str, agent_tools: str | None, user_tools: str | None,
                 catalog_md: str, edit_log: list[dict], items: list[dict]) -> str:
    log_lines = [f"- round {e['round']}: {e.get('slug')} ({e.get('kind')}) - {e.get('verdict')}"
                 for e in edit_log]
    blocks = []
    for it in items:
        blocks.append("\n".join([
            f"### Proposal: {it['slug']}", "",
            f"- kind: {it.get('kind')}", f"- title: {it.get('title')}",
            f"- when_to_use: {it.get('when_to_use')}",
            f"- proposer's hypothesis: {it.get('hypothesis', '')}", "", "Body:", "",
            "```markdown", (it.get("body") or "").strip(), "```"]))
    return "\n\n".join([
        f"# Domain: {domain}",
        "## Policy the agent follows\n\n" + policy,
        "## Tools the agent can call\n\n" + (agent_tools or "(none)"),
        "## Tools the customer can use\n\n" + (user_tools or "(the customer has no tools in this domain)"),
        "## Current catalog (promoted items)\n\n" + (catalog_md or "(empty)"),
        "## Edit log (earlier proposals and verdicts)\n\n" + ("\n".join(log_lines) or "(none yet)"),
        f"## Proposals to review ({len(items)})\n\n" + "\n\n".join(blocks),
        "## Your task\n\nDecide approve, edit or reject for every proposal above, keyed by its "
        "slug. Reply with the JSON block only.",
    ])


_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)


def parse(text: str) -> dict:
    m = _FENCE.search(text or "")
    raw = m.group(1) if m else (text or "")[(text or "").find("{"):]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    dec = data.get("decisions") if isinstance(data, dict) else None
    return {k: v for k, v in (dec or {}).items() if isinstance(v, dict)}


def call(cfg: ClaudeConfig, prompt: str, workdir: Path, log_dir: Path, budget: InvocationBudget,
         label: str) -> tuple[dict, dict]:
    args = cfg.common_args(streaming=False) + prompt_args(
        "--system-prompt", SYSTEM, cfg.prompt_transport, log_dir / "reviewer.system_prompt.md")
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "reviewer_prompt.md").write_text(prompt, encoding="utf-8")
    ev = run_print(cfg, args, prompt, workdir, log_dir, "reviewer", budget, label)
    return parse(ev.get("result") or ""), ev


def decisions_file(raw: dict, items: list[dict], reviewer: str, model: str, screen_fn) -> dict:
    """The model's answer -> a review_decisions.json. A missing or malformed decision is a
    reject; an edit that trips the leakage screen is a reject, with the hits recorded."""
    out = {}
    for it in items:
        d = raw.get(it["slug"]) or {}
        dec = str(d.get("decision") or "").strip().lower()
        note = str(d.get("note") or "").strip()
        row = {"decision": dec, "note": note, "title": None, "when_to_use": None, "body": None}
        if dec not in ("approve", "edit", "reject"):
            row.update(decision="reject", note=f"no valid decision returned ({dec or 'none'})")
        elif dec == "edit":
            title = d.get("title") or it.get("title")
            when = d.get("when_to_use") or it.get("when_to_use")
            body = d.get("body") or it.get("body") or ""
            hits = screen_fn(" ".join(str(x or "") for x in (it["slug"], title, when, body)))
            if hits:
                row.update(decision="reject", leakage_hits=hits[:20],
                           note=f"edit rejected by the leakage screen ({', '.join(hits[:5])}). "
                                f"Reviewer's note: {note}")
            else:
                row.update(title=title, when_to_use=when, body=body)
        out[it["slug"]] = row
    return {"reviewer": reviewer, "model": model, "decided": time.strftime("%Y-%m-%d"),
            "decisions": out}
