"""OBS in-process, for the harness's own (human-equivalent) catalog work: proposing drafts
through the same function the propose_skill MCP tool uses, promoting and rejecting through
the CLI's governance functions, logging failure episodes, and copying a catalog between data
dirs. The agents under test reach OBS only over MCP (`obs serve`, see episode.py).

OBS reads its configuration from environment variables at call time, so every call here runs
under one process-wide lock with that store's variables set."""

import contextlib
import os
import shutil
import sys
import threading
from pathlib import Path

from .common import OBS_USER

_LOCK = threading.RLock()
_ENV_KEYS = ("OBS_DATA_DIR", "OBS_BACKEND", "OBS_CATALOG", "OBS_USER", "OBS_BLOB", "OBS_AUTH",
             "OBS_VIEWS")


def obs_env(data_dir: Path) -> dict:
    return {"OBS_DATA_DIR": str(data_dir), "OBS_BACKEND": "sqlite", "OBS_CATALOG": "all",
            "OBS_USER": OBS_USER, "OBS_BLOB": "fs", "PYTHONUTF8": "1"}


def obs_mcp_server(data_dir: Path) -> dict:
    """The --mcp-config entry for a local stdio OBS server on this data dir."""
    return {"type": "stdio", "command": sys.executable,
            "args": ["-m", "openbrainstore.cli", "serve"], "env": obs_env(data_dir)}


class ObsStore:
    def __init__(self, data_dir: Path, user: str = OBS_USER):
        self.data_dir = Path(data_dir)
        self.user = user
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def _env(self):
        with _LOCK:
            saved = {k: os.environ.get(k) for k in _ENV_KEYS}
            for k in ("OBS_AUTH", "OBS_VIEWS"):
                os.environ.pop(k, None)
            os.environ.update({k: v for k, v in obs_env(self.data_dir).items() if k.startswith("OBS_")})
            os.environ["OBS_USER"] = self.user
            try:
                yield
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    @staticmethod
    def _mods():
        import logging

        from openbrainstore import catalog, server
        from openbrainstore.backends import get_backend
        logging.getLogger("mcp").setLevel(logging.WARNING)   # FastMCP's import turns on INFO
        return catalog, server, get_backend

    # ---- agent-facing functions, called in-process (the MCP tools' own implementations)

    def startup(self, task: str = "", mode: str = "ground", fmt: str = "json"):
        with self._env():
            _, server, _ = self._mods()
            return server.agent_startup(task=task, harness="tau2-study-harness", mode=mode, format=fmt)

    def propose(self, slug: str, title: str, when_to_use: str, body: str, kind: str = "skill",
                evidence: list[str] | None = None) -> dict:
        with self._env():
            _, server, _ = self._mods()
            return server.propose_skill(slug=slug, title=title, when_to_use=when_to_use, body=body,
                                        kind=kind, evidence=evidence or [])

    def log_failure(self, task_summary: str, summary: str, score: float) -> str:
        """An optimize-mode session plus log_episode, so proposals can cite real episode ids."""
        with self._env():
            _, server, _ = self._mods()
            ses = server.agent_startup(task=task_summary[:500], harness="tau2-study-harness",
                                       mode="optimize", format="json")
            out = server.log_episode(session_id=ses["session_id"], outcome="failure",
                                     summary=summary[:8000], score=float(score))
            return out["episode_id"]

    # ---- governance (the CLI's human-only functions)

    def promote(self, slug: str, version: int | None, by: str) -> dict:
        with self._env():
            catalog, _, get_backend = self._mods()
            with get_backend() as b:
                return catalog.promote_version(b, self.user, slug, version, by=by)

    def reject(self, slug: str, version: int | None, by: str) -> dict:
        with self._env():
            catalog, _, get_backend = self._mods()
            with get_backend() as b:
                return catalog.reject(b, self.user, slug, version, by=by)

    def reindex(self) -> dict:
        with self._env():
            catalog, _, get_backend = self._mods()
            with get_backend() as b:
                return catalog.reindex(b, self.user)

    def items(self, status: str | None = None) -> list[dict]:
        with self._env():
            catalog, _, get_backend = self._mods()
            with get_backend() as b:
                return catalog.list_items(b, self.user, status=status, include_body=True)

    def history(self, slug: str) -> list[dict]:
        with self._env():
            catalog, _, get_backend = self._mods()
            with get_backend() as b:
                return catalog.item_versions(b, self.user, slug)["history"]

    def stats(self) -> list[dict]:
        with self._env():
            catalog, _, get_backend = self._mods()
            with get_backend() as b:
                return catalog.stats(b, self.user)

    def import_dir(self, path: Path) -> dict:
        with self._env():
            catalog, _, get_backend = self._mods()
            with get_backend() as b:
                return catalog.import_dir(b, self.user, str(path), promote=True)

    def promoted_markdown(self) -> str:
        """The promoted items rendered the way get_skill renders them (guardrails first)."""
        with self._env():
            catalog, _, _ = self._mods()
        rows = [r for r in self.items(status="promoted") if r["kind"] != "agent"]
        rows.sort(key=lambda r: (r["kind"] != "guardrail", r["slug"]))
        res = {"items": [{"ref": r["ref"], "kind": r["kind"], "title": r["title"],
                          "when_to_use": r["when_to_use"], "body": r["body"]} for r in rows],
               "missing": []}
        return catalog.render_skills(res) if rows else ""

    # ---- copying (hard rule #6: blobs are canonical, the projection is rebuilt by reindex)

    def _catalog_blobs(self) -> Path:
        return self.data_dir / "blobs" / "tenants" / self.user / "catalog"

    def copy_catalog_to(self, dest: "ObsStore") -> None:
        src = self._catalog_blobs()
        dst = dest._catalog_blobs()
        if dst.exists():
            shutil.rmtree(dst)
        if src.exists():
            shutil.copytree(src, dst)
        dest.reindex()
