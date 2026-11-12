#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Thin checkout shim for the legacy flat CLI.

Kept so ``python3 skills/ep-craft/scripts/gel_photo.py probe|render|sync|edit|
doctor|selftest`` keeps working. The implementation lives in
``skills/ep-craft/src/ep_craft/gel_photo.py``; this file only bootstraps the
source directory onto ``sys.path`` and forwards ``sys.argv``.

It also auto-detects the canvas groups, so ``python3 scripts/gel_photo.py
figure|workbench|crop|profile|launch …`` works without the console install.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from ep_craft.gel_photo import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
