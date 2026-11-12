# -*- coding: utf-8 -*-
"""ep-craft runtime package: electrophoresis gel figures on VectorCraft.

The package is self-contained inside the skill tree (``skills/ep-craft/src/ep_craft``)
so symlinking ``skills/ep-craft`` alone keeps working. Runtime modules use package
relative imports; the only path bootstrap lives in the two thin shims under
``skills/ep-craft/scripts/`` and in the installed console entry point.

Bundled resources are package data (one source of truth, no copies):

    ep_craft/data/        marker and gel presets
    ep_craft/templates/   VectorCraft object templates
    ep_craft/fixtures/    offline selftest fixtures
"""
from pathlib import Path

__version__ = "0.1.0"

PACKAGE_ROOT = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_ROOT / "data"
TEMPLATES_DIR = PACKAGE_ROOT / "templates"
FIXTURES_DIR = PACKAGE_ROOT / "fixtures"


def package_root():
    return PACKAGE_ROOT


def data_dir():
    """Bundled marker / gel preset directory (also present in an installed wheel)."""
    return DATA_DIR


def templates_dir():
    return TEMPLATES_DIR


def fixtures_dir():
    return FIXTURES_DIR
