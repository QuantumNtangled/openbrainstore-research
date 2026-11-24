"""Train/test splits. tau2 v1.0.1 ships an official split_tasks.json for every domain used
here (retail 74/40, airline 30/20, telecom 74/40 of the 114-task base set). The seeded
fallback exists for domains without one and is never used for those three."""

import hashlib
import json
import random

from .common import VENDOR_DIR


def split_file(domain: str):
    return VENDOR_DIR / "data" / "tau2" / "domains" / domain / "split_tasks.json"


def official_split(domain: str) -> dict[str, list[str]] | None:
    p = split_file(domain)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def seeded_split(task_ids: list[str], test_fraction: float = 0.35, seed: int = 20261124) -> dict:
    """Deterministic fallback: sort ids, shuffle with a fixed seed, cut."""
    ids = sorted(task_ids)
    random.Random(seed).shuffle(ids)
    n_test = round(len(ids) * test_fraction)
    return {"train": sorted(ids[n_test:]), "test": sorted(ids[:n_test]), "seed": seed}


def split_ids(domain: str, split: str) -> list[str]:
    sp = official_split(domain)
    if sp is None:
        from tau2.runner import load_tasks
        sp = seeded_split([t.id for t in load_tasks(domain)])
    if split not in ("train", "test"):
        raise ValueError("split must be train or test")
    return list(sp[split])


def load_split_tasks(domain: str, split: str, limit: int | None = None, seed: int = 0,
                     only: list[str] | None = None):
    """Tasks of one split, in the split file's order. `limit` takes a seeded sample (the same
    sample for every arm and round). `only` restricts to explicit ids, which must belong to
    the split - so a TEST id can never sneak into a train run, and vice versa."""
    from tau2.runner import load_tasks
    ids = split_ids(domain, split)
    if only:
        bad = [i for i in only if i not in ids]
        if bad:
            raise SystemExit(f"task ids not in the {domain} {split} split: {bad}")
        ids = [i for i in ids if i in set(only)]
    if limit is not None and limit < len(ids):
        ids = sorted(ids, key=lambda i: hashlib.sha256(f"{seed}:{i}".encode()).hexdigest())[:limit]
    by_id = {t.id: t for t in load_tasks(domain)}
    return [by_id[i] for i in ids]
