# -*- coding: utf-8 -*-
"""Console entry point for the installed ``ep-craft`` command.

The bundled scripts use flat imports (``import gel_photo``, ``from config import …``)
and resolve their data/templates/fixtures relative to their own directory. We therefore
put the bundled ``scripts/`` directory at the front of ``sys.path`` and hand off to the
same ``gel_photo.main(canvas=True)`` used by ``scripts/ep-craft`` in a checkout.
"""
import os
import sys

from . import scripts_dir

# Set once for code that wants to know it is running from the bundled wheel
# (e.g. selftest_cli's executable-bit check, which cannot rely on zip permissions).
BUNDLED_ENV = "EP_CRAFT_BUNDLED"


def main(argv=None):
    scripts = scripts_dir()
    entry = scripts / "gel_photo.py"
    if not entry.is_file():
        raise SystemExit("[!] broken ep-craft installation: missing %s" % entry)

    os.environ.setdefault(BUNDLED_ENV, "1")
    scripts_str = str(scripts)
    if not sys.path or sys.path[0] != scripts_str:
        sys.path.insert(0, scripts_str)

    import gel_photo  # noqa: E402  (import after sys.path setup, by design)

    return gel_photo.main(argv, canvas=True)


if __name__ == "__main__":
    sys.exit(main())
