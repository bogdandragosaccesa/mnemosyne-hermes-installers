#!/usr/bin/env python3
"""Thin CLI entry point for the vendor-neutral forge command.

Resolves Hermes' home directory from $HERMES_HOME (falling back to
~/.hermes) and delegates everything else to forge_core.main.
"""
import os
import sys
from pathlib import Path

from forge_core import ADAPTERS, main


def _home() -> Path:
    home = os.environ.get("HERMES_HOME")
    if home:
        return Path(home)
    return Path.home() / ".hermes"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], home=_home(), adapters=ADAPTERS))
