"""Offline smoke checks for the installed EPCraft package."""

from .. import data_dir
from .. import densitometry
from ..vc import file as vc_file
from ..config import load_gel_size


def selftest():
    """Exercise bundled resources and a pure-Python calculation."""
    load_gel_size(data_dir() / "gel_size.json")
    vc_file.new_doc("EPCraft selftest")
    area = densitometry.area_of([0.0, 0.5, 1.0], [0.0, 2.0, 0.0], 0.0, 1.0)
    if area != 1.0:
        raise RuntimeError(f"Densitometry smoke check expected area 1.0, got {area}")
    print("EPCraft selftest: preset, document template and densitometry passed")
    return 0
