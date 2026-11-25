"""Copy a study's run records into records/ for publishing, with personal details removed.

    python tools/export_records.py SOURCE_STUDY_DIR [--runs tel-h tel-hr tel-or tel-o] [--eval tel-test]

What is copied, per learning run: state.json, edit_log.jsonl, catalog_final.md, the final
catalog's item files (canonical markdown + state.json), and for each round exactly what the
skill writer and the reviewer saw and returned. For an evaluation run: its summary and frozen
catalog snapshot. Conversation transcripts are not copied here; they are published as a
release archive.

Every copied text file goes through the same replacements, and the export fails if anything
on the deny list is still present afterwards. Personal values (an email address, a user name)
are read from a local, untracked file `.scrub-local` (one `find=>replace` per line), so they
never appear in this repository.
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
OUT = HERE / "records"          # published records; results/ is for your own runs

ROUND_FILES = ("improver_prompt.md", "improver_output.json", "review.md", "review_decisions.json",
               "reviewer_prompt.md", "reviewer_output.json")
LEARN_FILES = ("state.json", "edit_log.jsonl", "catalog_final.md")

# Generic replacements; personal ones come from .scrub-local.
BASE_RULES = [
    (re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"']+", re.I), "<home>"),
    (re.compile(r"/(?:home|Users)/[^/\s\"']+"), "<home>"),
]


def local_rules() -> tuple[list, list[str]]:
    path = HERE / ".scrub-local"
    rules, deny = [], []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=>" in line and not line.lstrip().startswith("#"):
                find, repl = line.split("=>", 1)
                rules.append((re.compile(find.strip()), repl.strip()))
                deny.append(find.strip())
    return rules, deny


def scrub_text(text: str, rules: list) -> str:
    for rx, repl in BASE_RULES + rules:
        text = rx.sub(repl, text)
    return text


def copy(src: Path, dest: Path, rules: list) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix in (".md", ".json", ".jsonl", ".txt"):
        dest.write_text(scrub_text(src.read_text(encoding="utf-8"), rules), encoding="utf-8", newline="\n")
    else:
        shutil.copy2(src, dest)


def export_learn(src_runs: Path, run: str, rules: list) -> None:
    learn = src_runs / run / "learn"
    dest = OUT / run
    for name in LEARN_FILES:
        if (learn / name).exists():
            copy(learn / name, dest / name, rules)
    for rdir in sorted((learn / "rounds").glob("r*")):
        for name in ROUND_FILES:
            if (rdir / name).exists():
                copy(rdir / name, dest / "rounds" / rdir.name / name, rules)
    cat = learn / "obs" / "blobs" / "tenants"
    for f in sorted(cat.glob("*/catalog/*/*")):
        if f.is_file():
            copy(f, dest / "catalog" / f.parent.name / f.name, rules)


def export_eval(src_study: Path, run: str, rules: list) -> None:
    for f in sorted((src_study / "results" / run).glob("summary.*")):
        copy(f, OUT / run / f.name, rules)
    snap = src_study / "runs" / run / "eval" / "catalog_snapshot.json"
    if snap.exists():
        copy(snap, OUT / run / "catalog_snapshot.json", rules)


def check(deny: list[str]) -> list[str]:
    bad = []
    patterns = [re.compile(d) for d in deny] + [rx for rx, _ in BASE_RULES]
    for f in OUT.rglob("*"):
        if f.is_file() and f.suffix in (".md", ".json", ".jsonl", ".txt"):
            text = f.read_text(encoding="utf-8")
            bad += [f"{f.relative_to(HERE)}: {p.pattern}" for p in patterns if p.search(text)]
    return bad


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path, help="the study folder the runs were made in")
    ap.add_argument("--runs", nargs="*", default=["tel-h", "tel-hr", "tel-or", "tel-o"])
    ap.add_argument("--eval", nargs="*", default=["tel-test"])
    args = ap.parse_args()
    rules, deny = local_rules()
    if not deny:
        raise SystemExit("no .scrub-local file: list the personal values to remove first")
    for run in args.runs:
        export_learn(args.source / "runs", run, rules)
    for run in args.eval:
        export_eval(args.source, run, rules)
    bad = check(deny)
    if bad:
        print("\n".join(bad))
        raise SystemExit(f"{len(bad)} file(s) still contain personal details; nothing is safe to commit")
    print(f"exported to {OUT}; no personal details found")


if __name__ == "__main__":
    sys.exit(main())
