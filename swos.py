#!/usr/bin/env python3
"""MikroTik SwOS Command-Line Administration Tool."""

import os
import sys

# Ensure repository root is in sys.path when invoked via symlink (e.g. /usr/local/bin/swos)
repo_dir = os.path.dirname(os.path.realpath(__file__))
if repo_dir not in sys.path:
    sys.path.insert(0, repo_dir)

from mikrotik_swos.cli import cli

if __name__ == "__main__":
    cli()
