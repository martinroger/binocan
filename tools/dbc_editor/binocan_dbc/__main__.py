"""Command line entry point: python -m binocan_dbc <command> [options]."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, deps

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DBC = REPO_ROOT / "binocan.dbc"
DEFAULT_C_DIR = REPO_ROOT / "src"


def _dbc_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("dbc", nargs="?", type=Path, default=DEFAULT_DBC,
                   help=f"DBC file (default: {DEFAULT_DBC.name} at the repo root)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m binocan_dbc",
                                     description="Binocan DBC editor and tools")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-y", "--yes", action="store_true",
                        help="answer yes to cantools install or update prompts")
    parser.add_argument("--no-update-check", action="store_true",
                        help="skip the PyPI query for a newer cantools")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("edit", help="open the DBC in the browser UI")
    _dbc_arg(p)
    p.add_argument("--port", type=int, default=0, help="port (default: any free port)")
    p.add_argument("--no-browser", action="store_true", help="print the URL only")
    p.add_argument("--c-dir", type=Path, default=DEFAULT_C_DIR,
                   help="where Generate writes the C files (default: src/)")

    p = sub.add_parser("check", help="run the consistency checks")
    _dbc_arg(p)

    p = sub.add_parser("generate", help="regenerate the C sources with cantools")
    _dbc_arg(p)
    p.add_argument("--c-dir", type=Path, default=DEFAULT_C_DIR,
                   help="output directory (default: src/)")
    p.add_argument("--dry-run", action="store_true", help="report changes, write nothing")

    p = sub.add_parser("busload", help="theoretical busload of the DBC")
    _dbc_arg(p)
    p.add_argument("--baud", type=int, default=None,
                   help="bitrate in bit/s (default: the DBC's Baudrate, else 500000)")
    p.add_argument("-o", "--override", nargs="*", default=[], metavar="ID:MS",
                   help="what-if cycle times, e.g. 0x100:20 0x300:50")

    sub.add_parser("doctor", help="print Python, pip and cantools versions")
    return parser


def cmd_doctor(args) -> int:
    st = deps.status(check_latest=not args.no_update_check)
    print(f"Python    : {st['python']} ({st['python_executable']})")
    print(f"cantools  : {st['installed'] or 'not installed'} (minimum {st['minimum']})")
    print(f"PyPI      : {st['latest'] or 'not checked or unreachable'}")
    if st["installed"] is None or st["too_old"]:
        print("Status    : run any command to install or upgrade cantools")
        return 1
    print("Status    : update available" if st["outdated"] else "Status    : OK")
    return 0


def cmd_check(args) -> int:
    from . import model, validate

    db, _ = model.load(args.dbc)
    issues = validate.check(db)
    for i in issues:
        print(f"{i['level']:<8} {i['where']}: {i['text']}")
    errors = sum(1 for i in issues if i["level"] == "error")
    warnings = len(issues) - errors
    print(f"{args.dbc.name}: {len(db.messages)} messages, {errors} errors, {warnings} warnings")
    return 1 if errors else 0


def cmd_generate(args) -> int:
    from . import generate, model, validate

    db, _ = model.load(args.dbc)
    errors = [i for i in validate.check(db) if i["level"] == "error"]
    if errors:
        for i in errors:
            print(f"error    {i['where']}: {i['text']}")
        print("Not generating: fix the errors above first.")
        return 1
    res = generate.generate(args.dbc, args.c_dir, dry_run=args.dry_run)
    verb = "would write" if args.dry_run else "wrote"
    for f in res["created"] + res["changed"]:
        print(f"{verb} {f}")
    if not res["created"] and not res["changed"]:
        print("C sources already match the DBC; nothing written.")
    return 0


def cmd_busload(args) -> int:
    from . import busload, model

    overrides = {}
    for item in args.override:
        fid, _, ms = item.partition(":")
        overrides[int(fid, 0)] = int(ms)
    db, text = model.load(args.dbc)
    res = busload.compute(db, args.baud, overrides, text)
    print(busload.format_table(res, args.dbc.name))
    return 0


def cmd_edit(args) -> int:
    from . import server

    server.serve(args.dbc, args.c_dir, port=args.port, open_browser=not args.no_browser)
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return cmd_doctor(args)
    if not deps.ensure_cantools(assume_yes=args.yes, check_latest=not args.no_update_check):
        print("cantools is required; stopping.")
        return 2
    if hasattr(args, "dbc") and not args.dbc.is_file():
        print(f"DBC file not found: {args.dbc}")
        return 2
    return {"check": cmd_check, "generate": cmd_generate, "busload": cmd_busload,
            "edit": cmd_edit}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
