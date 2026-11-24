# -*- coding: utf-8 -*-
"""epcraft runtime package: electrophoresis gel labeling and offline densitometry on VectorCraft.

The package lives in the repository-root ``src/epcraft``. Entry points are the
installed console command ``epcraft`` and ``python3 -m epcraft`` with this
package importable; the skill directory only carries docs (no scripts, no path
bootstrapping). Runtime modules use package-relative imports.

Bundled resources are package data (one source of truth, no copies):

    epcraft/data/        marker and gel presets
    epcraft/templates/   VectorCraft object templates
    epcraft/fixtures/    offline selftest fixtures
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
