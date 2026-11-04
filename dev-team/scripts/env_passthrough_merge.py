#!/usr/bin/env python3
"""Merge variable names into a Hermes ``terminal.env_passthrough`` list.

Both installers (install-dev-team-unix.sh, install-dev-team-windows.ps1) call
this when ``--forge-token-source`` / ``-ForgeTokenSource`` is given, so the
devops profile's existing passthrough list is APPENDED to rather than
replaced. One implementation, shared, instead of two list merges written
twice in two languages.

Interface (fixed; the installers rely on it):
  * current value: JSON on **stdin** -- exactly what
    ``hermes -p <profile> config get terminal.env_passthrough --json`` prints.
    Empty input, whitespace, or ``null`` all mean "the key is not set yet".
    Never on argv: argv carries variable NAMES only.
  * argv: one or more variable names, each matching ``[A-Z_][A-Z0-9_]*``.
  * stdout: ONE line of compact JSON -- the existing list with each name
    appended only when absent, existing order preserved. Appending nothing
    prints the input list back unchanged, which is what makes a re-run of the
    installer with the same flag value a no-op.
  * exit: 0 success. 2 usage error, malformed JSON, a JSON value that is not
    a list of strings, or a bad variable name -- with a message on stderr and
    NOTHING on stdout.

Fails closed on malformed input rather than treating it as an empty list:
guessing would silently drop whatever the profile already passes through.

Python 3 standard library only -- the CI `scripts` job runs this suite under
``python3 -I`` in a venv built ``--without-pip``.
"""
from __future__ import annotations

import json
import re
import sys

_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")

USAGE = "usage: env_passthrough_merge.py NAME [NAME ...]  (current list as JSON on stdin)"


def _die(message: str) -> int:
    sys.stderr.write(message.rstrip("\n") + "\n")
    return 2


def merge(raw: str, names: list[str]) -> list[str]:
    """The existing list from `raw` with each of `names` appended if absent.

    Raises ValueError on anything that is not an absent value or a JSON list
    of strings.
    """
    text = (raw or "").strip()
    if not text or text == "null":
        current: list = []
    else:
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"the current value is not valid JSON: {exc}") from exc
        if parsed is None:
            parsed = []
        if not isinstance(parsed, list):
            raise ValueError(
                f"the current value is a {type(parsed).__name__}, not a list")
        for entry in parsed:
            if not isinstance(entry, str):
                raise ValueError(
                    f"the current list holds a {type(entry).__name__}, not a string")
        current = list(parsed)
    for name in names:
        if name not in current:
            current.append(name)
    return current


def main(argv: list[str], stdin=sys.stdin, stdout=sys.stdout) -> int:
    names = list(argv)
    if not names:
        return _die(USAGE)
    for name in names:
        if not _NAME.match(name):
            return _die(f"not a usable environment variable name: {name!r}\n{USAGE}")
    try:
        merged = merge(stdin.read(), names)
    except ValueError as exc:
        return _die(f"cannot merge terminal.env_passthrough: {exc}")
    stdout.write(json.dumps(merged) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
