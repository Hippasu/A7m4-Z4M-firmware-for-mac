"""Public CLI entry — diagnose only (no staged install playbook)."""

from __future__ import annotations

import argparse
import json
import sys

from . import doctor


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="m4probe", description="ZM4 public diagnose shell")
    p.add_argument("--doctor", action="store_true", help="run environment self-check")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    if args.doctor or True:
        result = doctor.check(persist=False)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            print(doctor.format_text(result))
        return 0 if all(c.get("ok") for c in result.get("checks", [])) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
