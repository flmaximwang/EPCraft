# -*- coding: utf-8 -*-
"""Installed console entry point: ``ep-craft = ep_craft.cli:main``.

A checkout reaches the same implementation through the thin
``skills/ep-craft/scripts/ep-craft`` shim, which puts ``../src`` on ``sys.path``.
"""
from .gel_photo import main as gel_photo_main


def main(argv=None, canvas=True):
    return gel_photo_main(argv, canvas=True if canvas else None)
