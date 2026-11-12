#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Setuptools hook: bundle the skill tree inside the ``ep_craft`` wheel.

The repository deliberately keeps the skill as plain scripts in
``skills/ep-craft``. The installed console command needs those same scripts plus
their relative ``data/``, ``templates/`` and ``fixtures/`` siblings, so after the
normal Python package build we copy the whole skill directory to
``ep_craft/_runtime`` in the build tree. The layout inside that directory is
identical to the checkout, which keeps every existing relative path working
(``env_check.py``, ``vc_file.TEMPLATE``, selftest fixtures, …).
"""
import os
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

ROOT = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.join(ROOT, "skills", "ep-craft")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "*.bak")


class build_py(_build_py):
    def run(self):
        super().run()
        if not os.path.isdir(SKILL_ROOT):
            raise RuntimeError("missing skill directory: %s" % SKILL_ROOT)
        dst = os.path.join(self.build_lib, "ep_craft", "_runtime")
        shutil.copytree(SKILL_ROOT, dst, dirs_exist_ok=True, ignore=IGNORE)


if __name__ == "__main__":
    setup(cmdclass={"build_py": build_py})
