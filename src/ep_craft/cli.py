# -*- coding: utf-8 -*-
"""Installed console entry point: ``ep-craft = ep_craft.cli:main``.

A checkout reaches the same implementation with ``PYTHONPATH=src python3 -m
ep_craft`` (see ``__main__.py``); installed environments use the console script.
"""
from .gel_photo import main as gel_photo_main


def main(argv=None, canvas=True):
    return gel_photo_main(argv, canvas=True if canvas else None)
