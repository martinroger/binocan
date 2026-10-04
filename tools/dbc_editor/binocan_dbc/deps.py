"""cantools presence and version check, with optional install or update.

Standard library only: this module must run before cantools is importable.
cantools is installed into the interpreter that runs the tool
(``sys.executable -m pip``), so venvs and ESP-IDF Python envs behave the same.
When that interpreter is "externally managed" (PEP 668: Homebrew, Debian and
others refuse system-wide pip installs), or with ``--venv``, the tool creates a
private virtual environment in ``tools/dbc_editor/.venv``, installs cantools
there and re-runs itself with that interpreter.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import sysconfig
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

PACKAGE = "cantools"
# Version that produced the committed src/binocan.c and src/binocan.h.
MIN_VERSION = "43.0.2"
PYPI_URL = "https://pypi.org/pypi/cantools/json"

PACKAGE_PARENT = Path(__file__).resolve().parents[1]   # tools/dbc_editor
VENV_DIR = PACKAGE_PARENT / ".venv"
REEXEC_ENV = "BINOCAN_DBC_IN_VENV"                      # guards against re-exec loops


def parse_version(text: str) -> Tuple[int, ...]:
    """Turns '44.1.0' (or '44.1.0rc1') into (44, 1, 0) for ordering."""
    parts = []
    for piece in text.split("."):
        m = re.match(r"\d+", piece)
        if not m:
            break
        parts.append(int(m.group(0)))
    return tuple(parts)


def installed_version() -> Optional[str]:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python < 3.8
        return None
    try:
        return version(PACKAGE)
    except PackageNotFoundError:
        return None


def latest_version(timeout: float = 3.0) -> Optional[str]:
    """Latest release on PyPI, or None when offline or PyPI is unreachable."""
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=timeout) as resp:
            return json.load(resp)["info"]["version"]
    except Exception:
        return None


def status(check_latest: bool = True) -> dict:
    installed = installed_version()
    latest = latest_version() if check_latest else None
    return {
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "installed": installed,
        "minimum": MIN_VERSION,
        "latest": latest,
        "too_old": installed is not None
        and parse_version(installed) < parse_version(MIN_VERSION),
        "outdated": installed is not None
        and latest is not None
        and parse_version(installed) < parse_version(latest),
    }


def in_virtualenv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def externally_managed() -> bool:
    """True when pip would refuse to install here (PEP 668 marker file)."""
    if in_virtualenv():
        return False
    return (Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED").exists()


def venv_python() -> Path:
    sub = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    return VENV_DIR.joinpath(*sub)


def _in_our_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV_DIR.resolve()
    except OSError:
        return False


def _venv_cantools_version() -> Optional[str]:
    py = venv_python()
    if not py.is_file():
        return None
    try:
        out = subprocess.run(
            [str(py), "-c",
             "import importlib.metadata as m; print(m.version('%s'))" % PACKAGE],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _venv_ready() -> bool:
    v = _venv_cantools_version()
    return v is not None and parse_version(v) >= parse_version(MIN_VERSION)


def pip_install(upgrade: bool = False, python: Optional[str] = None) -> int:
    cmd = [python or sys.executable, "-m", "pip", "install"]
    if upgrade:
        cmd.append("--upgrade")
    cmd.append(f"{PACKAGE}>={MIN_VERSION}")
    print("[deps] Running: " + " ".join(cmd))
    return subprocess.call(cmd)


def setup_venv() -> bool:
    """Creates tools/dbc_editor/.venv (if needed) and installs cantools into it."""
    if not venv_python().is_file():
        cmd = [sys.executable, "-m", "venv", str(VENV_DIR)]
        print("[deps] Running: " + " ".join(cmd))
        if subprocess.call(cmd) != 0:
            print("[deps] Could not create the virtual environment. On Debian or "
                  "Ubuntu this usually needs: sudo apt install python3-venv")
            return False
    if pip_install(upgrade=True, python=str(venv_python())) != 0:
        print("[deps] Installing cantools into the virtual environment failed.")
        return False
    return _venv_ready()


def reexec_in_venv() -> None:
    """Runs the same command line with the private venv's interpreter, then exits."""
    env = dict(os.environ)
    env[REEXEC_ENV] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(PACKAGE_PARENT), env.get("PYTHONPATH", "")]))
    cmd = [str(venv_python()), "-m", "binocan_dbc"] + sys.argv[1:]
    print(f"[deps] Continuing in {VENV_DIR}")
    sys.stdout.flush()
    sys.exit(subprocess.call(cmd, env=env))


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"{question} [Y/n] y (--yes)")
        return True
    if not sys.stdin.isatty():
        print(f"{question} [Y/n] n (no terminal, pass --yes to accept)")
        return False
    answer = input(f"{question} [Y/n] ").strip().lower()
    return answer in ("", "y", "yes")


def _offer_venv(reason: str, assume_yes: bool) -> bool:
    """Asks to use the private venv; on success this re-runs the command and exits."""
    print(f"[deps] {reason}")
    if not _confirm(f"Create a private virtual environment at {VENV_DIR} "
                    f"and install {PACKAGE}>={MIN_VERSION} there?", assume_yes):
        return False
    if not setup_venv():
        return False
    reexec_in_venv()
    return True  # not reached


def ensure_cantools(assume_yes: bool = False, check_latest: bool = True,
                    use_venv: bool = False) -> bool:
    """Makes sure a usable cantools is importable. Returns False if it is not.

    May re-run the whole command inside the private venv and exit instead.
    """
    if sys.version_info < (3, 10):
        print(f"[deps] cantools {MIN_VERSION} and later need Python 3.10+; "
              f"this is {sys.version.split()[0]}.")
        return False

    can_hop = not os.environ.get(REEXEC_ENV) and not _in_our_venv()
    if use_venv and can_hop:
        if _venv_ready() or _offer_venv("Using the private virtual environment (--venv).",
                                        assume_yes):
            reexec_in_venv()
        return False

    st = status(check_latest=check_latest)

    if st["installed"] is None:
        if can_hop and _venv_ready():
            reexec_in_venv()
        print(f"[deps] {PACKAGE} is not installed for {st['python_executable']}.")
        if externally_managed():
            return can_hop and _offer_venv(
                "This Python is externally managed (PEP 668), so pip cannot install "
                "into it.", assume_yes)
        if not _confirm(f"Install {PACKAGE}>={MIN_VERSION} now?", assume_yes):
            return False
        if pip_install() != 0:
            print("[deps] Install failed.")
            return can_hop and _offer_venv("pip could not install into this Python.",
                                           assume_yes)
        return _importable()

    if st["too_old"]:
        print(
            f"[deps] {PACKAGE} {st['installed']} is older than the minimum "
            f"{MIN_VERSION} used for the committed C files."
        )
        if externally_managed():
            return can_hop and _offer_venv(
                "This Python is externally managed, so it cannot be upgraded in place.",
                assume_yes)
        if not _confirm(f"Upgrade {PACKAGE} now?", assume_yes):
            return False
        if pip_install(upgrade=True) != 0:
            print("[deps] Upgrade failed.")
            return False
        return _importable()

    if st["outdated"]:
        print(
            f"[deps] {PACKAGE} {st['installed']} is installed; "
            f"{st['latest']} is available."
        )
        if externally_managed():
            print("[deps] This Python is externally managed, so not updating it. "
                  "Update with your package manager, or run with --venv.")
        elif _confirm(f"Update {PACKAGE} now?", assume_yes):
            if pip_install(upgrade=True) != 0:
                print("[deps] Update failed, carrying on with the installed version.")
            else:
                print(
                    "[deps] Updated. Regenerated C files will carry the new "
                    "cantools version stamp; review the diff before committing."
                )

    return _importable()


def _importable() -> bool:
    import importlib

    importlib.invalidate_caches()
    try:
        importlib.import_module(PACKAGE)
    except ImportError:
        print(f"[deps] {PACKAGE} still cannot be imported by {sys.executable}.")
        return False
    return True
