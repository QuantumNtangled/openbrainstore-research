"""Set up this study on Windows, macOS or Linux. Safe to re-run.

    python bootstrap.py                 # from this folder, with Python 3.12 or 3.13

Clones τ²-bench into vendor/tau2-bench pinned to v1.0.1 (refusing any other commit), creates a
virtualenv and installs τ², OpenBrainStore (the public openbrainstore-core, pinned in
requirements.txt) and the test tools into it. Uses only the standard library and git.

The virtualenv defaults to .venv in this folder. On Windows it defaults to
%USERPROFILE%\\.venvs\\tau2-governed-skills instead: τ²'s dependency litellm ships files whose
paths can pass Windows' 260-character limit when the venv sits deep inside a project folder.
"""

import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
TAU2_URL = "https://github.com/sierra-research/tau2-bench.git"
TAU2_TAG = "v1.0.1"
TAU2_COMMIT = "fc0055dc4e0a316c3f83133267fbd6faaa770992"


def default_venv() -> Path:
    if os.name == "nt":
        return Path.home() / ".venvs" / "tau2-governed-skills"
    return HERE / ".venv"


def venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(cmd: list, cwd: Path | None = None, capture: bool = False) -> str:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    out = subprocess.run([str(c) for c in cmd], cwd=cwd, check=True, text=True,
                         stdout=subprocess.PIPE if capture else None)
    return out.stdout or ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venv", type=Path, default=default_venv(), help="virtualenv location")
    args = ap.parse_args()
    if sys.version_info < (3, 12) or sys.version_info >= (3, 14):
        raise SystemExit(f"Python 3.12 or 3.13 is needed; this is {sys.version.split()[0]}")

    vendor = HERE / "vendor" / "tau2-bench"
    if not (vendor / ".git").exists():
        vendor.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--filter=blob:none", "--no-checkout", TAU2_URL, vendor])
    if os.name == "nt":
        run(["git", "-C", vendor, "config", "core.longpaths", "true"])
    run(["git", "-C", vendor, "fetch", "--tags", "--quiet"])
    run(["git", "-C", vendor, "checkout", "-q", "-f", TAU2_TAG])
    head = run(["git", "-C", vendor, "rev-parse", "HEAD"], capture=True).strip()
    if head != TAU2_COMMIT:
        raise SystemExit(f"tau2-bench {TAU2_TAG} resolved to {head}, expected {TAU2_COMMIT}")

    py = venv_python(args.venv)
    if not py.exists():
        print(f"creating virtualenv at {args.venv}", flush=True)
        venv.EnvBuilder(with_pip=True).create(args.venv)
    run([py, "-m", "pip", "install", "-q", "--upgrade", "pip"])
    run([py, "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=HERE)

    print(f"\ntau2-bench {TAU2_TAG} ({TAU2_COMMIT}) and the study are installed in {args.venv}")
    print("Next, from this folder:")
    print(f'  "{py}" -m pytest tests -q        # offline check, no model calls')


if __name__ == "__main__":
    main()
