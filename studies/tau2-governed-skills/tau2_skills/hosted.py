"""OpenBrainStore at openbrainstore.com/mcp for every agent in a run (`--obs-url`).

Each setup a run measures gets a **slot**: an agent profile `obs-study-slot-<n>` in your
account, with an agent key bound to it in `OBS_KEY_<n>`. Before a setup's conversations run,
the harness publishes that setup's skills under slot-prefixed names (`s<n>-<slug>`), assigns
exactly those to the slot's profile, and retires whatever the slot no longer uses. It does this
with its own key, `OBS_ADMIN_KEY`, which needs the Load, Propose and Approve skills permissions
and is never given to an agent. Skills assigned to a profile are served only to that profile's
agents, so your own agents and the other slots never see them.

The harness still keeps its own record of every proposal and decision (runs/<run>/learn/obs);
the account is what the agents talk to. Keys are read from the environment only and never
written anywhere: agents get them through Claude Code's `${VAR}` expansion (see obs.py).
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path

from .common import read_json, write_json

ADMIN_ENV = "OBS_ADMIN_KEY"
MAX_SLOTS = 4
ROLE_TITLE = "Customer support agent"
ROLE_WHEN = "A customer support conversation."
ROLE_BODY = "Follow the domain policy. Use the catalog items below when they apply."


def slot_profile(n: int) -> str:
    return f"obs-study-slot-{n}"


def slot_key_env(n: int) -> str:
    return f"OBS_KEY_{n}"


def slot_slug(n: int, slug: str) -> str:
    return f"s{n}-{slug}"


class HostedError(RuntimeError):
    pass


def _fingerprint(item: dict) -> str:
    text = "\n".join(str(item.get(k, "")) for k in ("kind", "title", "when_to_use", "body"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def setup_help(url: str, slots: int = MAX_SLOTS) -> str:
    app = url.rsplit("/mcp", 1)[0] + "/app"
    return "\n".join([
        f"A hosted run needs these agent keys from {app} (Agents > Create key). Put each one in",
        "your shell's environment under the name shown, never in a file.",
        "",
        f"1. {ADMIN_ENV}: no profile; permissions Load skills, Propose skills and Approve skills.",
        "   The harness publishes each setup's skills with it; no agent is ever given it.",
        "2. Run `python -m tau2_skills hosted-setup --obs-url` once. With the key above it creates the",
        f"   agent profiles {slot_profile(1)} to {slot_profile(slots)}.",
        *[f"{n + 2}. {slot_key_env(n)}: profile {slot_profile(n)}; permission Load skills only."
          for n in range(1, slots + 1)],
        "",
        "Learning runs use slot 1. A test-split run gives one slot to each arm that uses OpenBrainStore",
        "(B and each C), in --arms order.",
    ])


class HostedCatalog:
    def __init__(self, url: str, state_path: Path):
        self.url = url
        self.state_path = Path(state_path)

    # ---- transport

    @staticmethod
    def _key(env: str) -> str:
        key = os.environ.get(env)
        if not key:
            raise SystemExit(f"{env} is not set. Run `python -m tau2_skills hosted-setup --obs-url` "
                             "for the keys a hosted run needs.")
        return key

    def call(self, env: str, calls: list[tuple[str, dict]]) -> list[str]:
        """Call tools in one MCP session as the key in `env`; returns each result's text."""
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        key = self._key(env)

        async def run():
            out = []
            async with httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=120) as hc:
                async with streamable_http_client(self.url, http_client=hc) as (r, w, _):
                    async with ClientSession(r, w) as s:
                        await s.initialize()
                        for name, args in calls:
                            res = await s.call_tool(name, args)
                            text = res.content[0].text if res.content else ""
                            if res.isError:
                                raise HostedError(f"{name} failed for {env}: {text[:400]}")
                            out.append(text)
            return out

        return asyncio.run(run())

    # ---- slots

    def setup(self, slots: int = MAX_SLOTS) -> list[str]:
        """Create the slot profiles (empty) that don't exist yet; safe to re-run."""
        state = read_json(self.state_path, {}) or {}
        made = []
        for n in range(1, slots + 1):
            if state.get(str(n), {}).get("profile_ready"):
                continue
            self._set_profile(n, [])
            state.setdefault(str(n), {})["profile_ready"] = True
            write_json(self.state_path, state)
            made.append(slot_profile(n))
        return made

    def _set_profile(self, n: int, slugs: list[str]) -> None:
        self.call(ADMIN_ENV, [
            ("propose_skill", {"slug": slot_profile(n), "title": ROLE_TITLE, "when_to_use": ROLE_WHEN,
                               "body": ROLE_BODY, "kind": "agent", "skills": sorted(slugs)}),
            ("approve_skill", {"slug": slot_profile(n)})])

    def publish(self, n: int, items: list[dict]) -> list[str]:
        """Make slot n serve exactly `items` (dicts with slug, title, when_to_use, body, kind).
        Order matters, so nothing is ever visible outside the slot: retire what the slot drops
        while it is still assigned, propose new text as drafts, point the profile at the new
        set, then approve the drafts. Returns the slot's slugs."""
        if not 1 <= n <= MAX_SLOTS:
            raise SystemExit(f"slot {n}: a hosted run has slots 1-{MAX_SLOTS}")
        state = read_json(self.state_path, {}) or {}
        slot = state.setdefault(str(n), {})
        have: dict = slot.setdefault("items", {})
        want = {slot_slug(n, i["slug"]): i for i in items if i.get("kind") != "agent"}
        for ps in sorted(set(have) - set(want)):
            try:
                self.call(ADMIN_ENV, [("retire_skill", {"slug": ps})])
            except HostedError as e:
                if "no promoted version" not in str(e):
                    raise
            have.pop(ps)
            write_json(self.state_path, state)
        changed = [ps for ps, i in want.items() if have.get(ps) != _fingerprint(i)]
        if changed:
            self.call(ADMIN_ENV, [("propose_skill", {
                "slug": ps, "title": want[ps]["title"], "when_to_use": want[ps]["when_to_use"],
                "body": want[ps]["body"], "kind": want[ps].get("kind") or "skill"}) for ps in changed])
        if sorted(want) != slot.get("profile_skills") or not slot.get("profile_ready"):
            self._set_profile(n, list(want))
            slot["profile_skills"], slot["profile_ready"] = sorted(want), True
            write_json(self.state_path, state)
        if changed:
            self.call(ADMIN_ENV, [("approve_skill", {"slug": ps}) for ps in changed])
            have.update({ps: _fingerprint(want[ps]) for ps in changed})
            write_json(self.state_path, state)
        return sorted(want)

    def served(self, n: int) -> dict:
        """What an agent in slot n is given at startup: its role and the item refs."""
        text = self.call(slot_key_env(n), [("agent_startup", {
            "task": "harness check", "harness": "tau2-study-harness", "format": "json"})])[0]
        s = json.loads(text)
        refs = [g["ref"] for g in s.get("guardrails") or []] + [i["ref"] for i in s.get("index") or []]
        return {"role": (s.get("agent") or {}).get("ref"), "refs": sorted(refs)}

    def verify(self, n: int, slugs: list[str]) -> dict:
        """Check slot n serves exactly `slugs` under its own profile; stop the run otherwise."""
        got = self.served(n)
        role = (got["role"] or "").split(":", 1)[-1].split("@", 1)[0]
        if role != slot_profile(n):
            raise SystemExit(f"{slot_key_env(n)} isn't bound to the profile {slot_profile(n)} "
                             f"(it serves {got['role'] or 'no profile'}). Edit the key in the Agents tab.")
        served = {r.split(":", 1)[-1].split("@", 1)[0] for r in got["refs"]}
        if served != set(slugs):
            extra, missing = sorted(served - set(slugs)), sorted(set(slugs) - served)
            raise SystemExit(
                f"slot {n} serves {extra or 'nothing extra'} beyond what this setup should have"
                f"{'' if not missing else f' and is missing {missing}'}. Skills in your account that "
                "aren't assigned to any profile are given to every agent, so a study run needs "
                "an account (or a sign-in) whose other skills are all assigned to profiles.")
        return got
