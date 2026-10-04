"""cantools presence and version check, with optional install or update.

Standard library only: this module must run before cantools is importable.
Everything is installed into the interpreter that runs the tool
(``sys.executable -m pip``), so venvs and ESP-IDF Python envs behave the same.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from typing import Optional, Tuple

PACKAGE = "cantools"
# Version that produced the committed src/binocan.c and src/binocan.h.
MIN_VERSION = "43.0.2"
PYPI_URL = "https://pypi.org/pypi/cantools/json"


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


def pip_install(upgrade: bool = False) -> int:
    cmd = [sys.executable, "-m", "pip", "install"]
    if upgrade:
        cmd.append("--upgrade")
    cmd.append(f"{PACKAGE}>={MIN_VERSION}")
    print("[deps] Running: " + " ".join(cmd))
    return subprocess.call(cmd)


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"{question} [Y/n] y (--yes)")
        return True
    if not sys.stdin.isatty():
        print(f"{question} [Y/n] n (no terminal, pass --yes to accept)")
        return False
    answer = input(f"{question} [Y/n] ").strip().lower()
    return answer in ("", "y", "yes")


def ensure_cantools(assume_yes: bool = False, check_latest: bool = True) -> bool:
    """Makes sure a usable cantools is importable. Returns False if it is not."""
    if sys.version_info < (3, 10):
        print(f"[deps] cantools {MIN_VERSION} and later need Python 3.10+; "
              f"this is {sys.version.split()[0]}.")
        return False
    st = status(check_latest=check_latest)

    if st["installed"] is None:
        print(f"[deps] {PACKAGE} is not installed for {st['python_executable']}.")
        if not _confirm(f"Install {PACKAGE}>={MIN_VERSION} now?", assume_yes):
            return False
        if pip_install() != 0:
            print("[deps] Install failed.")
            return False
        return _importable()

    if st["too_old"]:
        print(
            f"[deps] {PACKAGE} {st['installed']} is older than the minimum "
            f"{MIN_VERSION} used for the committed C files."
        )
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
        if _confirm(f"Update {PACKAGE} now?", assume_yes):
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
