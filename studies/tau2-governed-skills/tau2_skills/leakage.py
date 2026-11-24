"""Leakage screen. A promoted skill must teach procedure, not memorize tasks, so any proposed
item whose text contains a string specific to a TRAIN or TEST task is rejected: task ids,
user ids, names, order / reservation / item / payment ids, emails, phone numbers, zip codes,
flight numbers. The denylist is built from the task files; generic ID-shaped patterns catch
identifiers no task mentions.

Also used the other way round: test_contamination() checks that nothing in the improver's
prompt is specific to a TEST task (the improver must never see test tasks)."""

import re
from dataclasses import dataclass, field

# ID-shaped tokens, whatever task they came from
GENERIC_PATTERNS = {
    "order id": re.compile(r"#W\d{4,}"),
    "user id": re.compile(r"\b[a-z]+_[a-z]+_\d{3,}\b"),
    "payment id": re.compile(r"\b(?:credit_card|paypal|gift_card|certificate)_\d{3,}\b", re.I),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
    "phone": re.compile(r"\b\d{3}[-. ]\d{3}[-. ]\d{4}\b"),
    "long number": re.compile(r"\b\d{6,}\b"),
}

_ID_TOKEN = re.compile(r"#?[A-Za-z0-9_.@+-]*\d[A-Za-z0-9_.@+-]*")
_NAME_IN_TEXT = re.compile(r"\b(?:You are|name is|named)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})")
_RES_ID = re.compile(r"\b[A-Z0-9]{6}\b")
_FLIGHT = re.compile(r"\b[A-Z]{3}\d{3}\b")
_ZIP = re.compile(r"\b(?:zip(?:\s*code)?|zipcode)\D{0,4}(\d{5})\b", re.I)

# capitalised words that are also ordinary English; a lone first/last name equal to one of
# these is not denylisted (the full name still is)
_COMMON = {"will", "may", "mark", "bill", "grace", "hope", "joy", "rose", "jack", "page",
           "case", "young", "white", "black", "brown", "green", "king", "long", "lee", "hill",
           "wood", "rich", "chase", "price", "ford", "bell", "park", "summer", "august", "june",
           "april", "dean", "faith", "sky", "star"}


@dataclass
class Denylist:
    literals: set = field(default_factory=set)       # matched case-insensitively, as tokens
    names: set = field(default_factory=set)          # single name tokens, matched case-sensitively
    task_ids: set = field(default_factory=set)

    def merge(self, other: "Denylist") -> "Denylist":
        return Denylist(self.literals | other.literals, self.names | other.names,
                        self.task_ids | other.task_ids)

    def minus(self, other: "Denylist") -> "Denylist":
        return Denylist(self.literals - other.literals, self.names - other.names,
                        self.task_ids - other.task_ids)


def _walk(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield from _walk_kv(k, v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk(v)


def _walk_kv(k, v):
    if isinstance(v, (dict, list)):
        yield from _walk(v)
    elif v is not None:
        yield str(k), str(v)


def task_denylist(task) -> Denylist:
    d = task if isinstance(task, dict) else task.model_dump(mode="json")
    dl = Denylist()
    tid = str(d["id"])
    dl.task_ids.add(tid)
    if not tid.isdigit():
        dl.literals.add(tid.lower())
    texts = []
    for k, v in _walk({"u": d.get("user_scenario"), "e": d.get("evaluation_criteria"),
                       "i": d.get("initial_state"), "t": d.get("ticket")}):
        texts.append(v)
        kl = k.lower()
        if kl in ("first_name", "last_name", "name") and len(v) >= 2 and v[:1].isupper():
            for part in v.split():
                if len(part) >= 4 and part.lower() not in _COMMON:
                    dl.names.add(part)
        if (kl.endswith("_id") or kl.endswith("id") or kl in ("zip", "email", "phone_number",
                                                              "flight_number")) and len(v) >= 4:
            dl.literals.add(v.lower())
    for text in texts:
        for tok in _ID_TOKEN.findall(text):
            tok = tok.strip(".,;:()[]'\"")
            if len(tok) >= 5 and any(c.isdigit() for c in tok) and not tok.replace(".", "").isdigit() \
                    or tok.startswith("#") or ("@" in tok):
                dl.literals.add(tok.lower())
            elif tok.isdigit() and len(tok) >= 5:
                dl.literals.add(tok)
        for m in _NAME_IN_TEXT.finditer(text):
            full = m.group(1)
            dl.literals.add(full.lower())
            for part in full.split():
                if len(part) >= 4 and part.lower() not in _COMMON:
                    dl.names.add(part)
        for m in _ZIP.finditer(text):
            dl.literals.add(m.group(1))
        for m in _FLIGHT.finditer(text):
            dl.literals.add(m.group(0).lower())
        for m in _RES_ID.finditer(text):
            tok = m.group(0)
            if any(c.isdigit() for c in tok) or text.count(tok) and kl_is_reservation(text, tok):
                dl.literals.add(tok.lower())
    for k, v in _walk(d.get("evaluation_criteria") or {}):
        if k.lower() == "reservation_id":
            dl.literals.add(v.lower())
    dl.literals = {x for x in dl.literals if len(x) >= 4}
    return dl


def kl_is_reservation(text: str, tok: str) -> bool:
    i = text.find(tok)
    window = text[max(0, i - 40): i].lower()
    return "reservation" in window or "booking" in window or "confirmation" in window


def build_denylist(tasks) -> Denylist:
    out = Denylist()
    for t in tasks:
        out = out.merge(task_denylist(t))
    return out


def screen(text: str, dl: Denylist, generic: bool = True) -> list[str]:
    """Every task-specific string found in `text` (as it appears there). Empty = clean."""
    hits = []
    low = text.lower()
    for lit in dl.literals:
        for m in re.finditer(r"(?<![A-Za-z0-9_])" + re.escape(lit) + r"(?![A-Za-z0-9_])", low):
            hits.append(text[m.start():m.end()])
    for name in dl.names:
        for m in re.finditer(r"\b" + re.escape(name) + r"\b", text):
            hits.append(m.group(0))
    for tid in dl.task_ids:
        for m in re.finditer(r"\btask[\s_#:-]*(?:id[\s:#]*)?" + re.escape(tid) + r"\b", text, re.I):
            hits.append(m.group(0))
    if generic:
        for label, rx in GENERIC_PATTERNS.items():
            hits += [m.group(0) for m in rx.finditer(text)]
    return sorted(set(hits))


def test_only_denylist(test_tasks, train_tasks, allowed_text: str) -> Denylist:
    """Strings specific to TEST tasks that appear in no TRAIN task and not in `allowed_text`
    (the shared policy): their presence in an improver prompt means test data leaked in."""
    dl = build_denylist(test_tasks).minus(build_denylist(train_tasks))
    low = allowed_text.lower()
    dl.literals = {x for x in dl.literals if x not in low}
    dl.names = {n for n in dl.names if n not in allowed_text}
    dl.task_ids = set()   # bare numeric ids are not distinctive in free text
    return dl


def test_contamination(prompt: str, test_tasks, train_tasks, policy: str) -> list[str]:
    return screen(prompt, test_only_denylist(test_tasks, train_tasks, policy), generic=False)


def redact(text: str, dl: Denylist, placeholder: str = "<redacted>") -> tuple[str, int]:
    """Replace every task-specific string of `dl` in `text` (same matching as screen,
    without the generic patterns). Train trajectories legitimately contain shared
    database values (a product id the agent looked up) that a TEST task also names;
    the improver never needs specific ids, so they are masked instead of aborting."""
    count = 0

    def sub(rx, s):
        nonlocal count
        out, n = rx.subn(placeholder, s)
        count += n
        return out

    for lit in sorted(dl.literals, key=len, reverse=True):
        text = sub(re.compile(r"(?<![A-Za-z0-9_])" + re.escape(lit) + r"(?![A-Za-z0-9_])", re.I), text)
    for name in sorted(dl.names, key=len, reverse=True):
        text = sub(re.compile(r"\b" + re.escape(name) + r"\b"), text)
    return text, count
