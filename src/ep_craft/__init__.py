# -*- coding: utf-8 -*-
"""ep-craft runtime package: electrophoresis gel figures on VectorCraft.

The package lives in the repository-root ``src/ep_craft``. Entry points are the
installed console command ``ep-craft`` and ``python3 -m ep_craft`` with this
package importable; the skill directory only carries docs (no scripts, no path
bootstrapping). Runtime modules use package-relative imports.

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
