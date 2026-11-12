# -*- coding: utf-8 -*-
"""Installed wrapper for the ep-craft skill.

The skill itself stays a plain directory of stdlib-only scripts (``skills/ep-craft``).
The wheel build copies that directory under ``ep_craft/_runtime`` so the scripts keep
resolving their relative resources exactly as they do in a checkout:

    _runtime/scripts/*.py      runtime modules (flat imports, unchanged)
    _runtime/data/*.json       marker / gel presets
    _runtime/templates/*       VectorCraft template
    _runtime/fixtures/*        offline test fixtures

Nothing from the skill is installed as a top-level module: only this small
``ep_craft`` package appears in site-packages.
"""
from pathlib import Path

__version__ = "0.1.0"


def bundled_root():
    """Absolute path of the bundled skill tree."""
    return Path(__file__).resolve().parent / "_runtime"


def scripts_dir():
    """Absolute path of the bundled ``scripts/`` directory."""
    return bundled_root() / "scripts"


def data_dir():
    """Absolute path of the bundled ``data/`` directory (marker / gel presets)."""
    return bundled_root() / "data"


def templates_dir():
    return bundled_root() / "templates"


def fixtures_dir():
    # In the checkout the fixtures live inside scripts/ (selftest resolves them
    # relative to its own file), so keep that exact layout in the bundle.
    return scripts_dir() / "fixtures"
