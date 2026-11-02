#!/usr/bin/env python3
"""Thin CLI entry point for the `deciders` command.

Resolves Hermes' home directory from $HERMES_HOME (falling back to
~/.hermes), exactly as forge.py's `_home()` does, and delegates everything
else to deciders_core.main.
"""
import os
import sys
from pathlib import Path

from deciders_core import main


def _home() -> Path:
    home = os.environ.get("HERMES_HOME")
    if home:
        return Path(home)
    return Path.home() / ".hermes"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], home=_home(), env=os.environ))
