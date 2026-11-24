"""What each arm appends to Claude Code's default system prompt.

Every arm: tau2's own LLMAgent system prompt (instructions + domain policy, verbatim) plus one
harness note. B and C add the OBS snippet the catalog spec suggests users put in their agent
instructions. D adds the promoted skill text, rendered exactly as get_skill returns it."""

from tau2.agent.llm_agent import AGENT_INSTRUCTION, SYSTEM_PROMPT

# The orchestrator opens every conversation with this agent line (tau2
# DEFAULT_FIRST_AGENT_MESSAGE); Claude Code can't be given a prior assistant turn, so the
# note tells it the greeting was sent.
GREETING = "Hi! How can I help you today?"
# Claude Code attaches the signed-in account's email address to the first user turn of every
# session (verified in the 2.1.316 binary: only ANTHROPIC_UNIX_SOCKET turns it off). Here that
# account is the operator's, not the customer's, so both sides are told to ignore it.
CLIENT_CONTEXT_NOTE = ("Claude Code may attach account details to a message, such as an email "
                       "address in a system reminder. They belong to the operator running this "
                       "deployment, not to the customer: never use or mention them.")
HARNESS_NOTE = (
    "<harness>\nEach user message comes from the customer. The text you write at the end of "
    "each turn is sent to the customer. The customer has already been greeted with: "
    f"\"{GREETING}\"\n{CLIENT_CONTEXT_NOTE}\n</harness>"
)
USER_SIM_NOTE = (
    "\n\n<client-note>\nThe chat client may attach its own context to messages, such as an "
    "account email address inside a system reminder. It is not part of your scenario and is not "
    "your information: ignore it, never mention it, and never give it as your own details.\n"
    "</client-note>"
)

# docs/specs/skill-catalog.md, "The snippet we suggest for CLAUDE.md, AGENTS.md or a system
# prompt", minus the remember line (no memory tools are offered here).
OBS_SNIPPET = """## OpenBrainStore
- At the start of non-trivial work, call `agent_startup` with a one-line description
  of the task. Fetch the catalog items that apply with `get_skill`, and follow the
  guardrails it returns."""
OBS_RECALL_LINE = "- Before answering questions about past work, decisions or preferences, `recall` them."


def tau2_agent_prompt(policy: str) -> str:
    return SYSTEM_PROMPT.format(domain_policy=policy, agent_instruction=AGENT_INSTRUCTION)


def agent_append_prompt(policy: str, *, obs: bool, recall: bool = False,
                        skills_md: str | None = None) -> str:
    parts = [tau2_agent_prompt(policy), HARNESS_NOTE]
    if obs:
        parts.append(OBS_SNIPPET + ("\n" + OBS_RECALL_LINE if recall else ""))
    if skills_md is not None:
        parts.append("<skills>\nThese skills were curated for this work. Follow every guardrail, "
                     "and each skill whose \"when to use\" matches the task.\n\n"
                     + skills_md + "\n</skills>")
    return "\n\n".join(parts)
