"""
densitometry — the offline math of the densitometry chain.

    report v2 (fig.mw.json) ──► measure() ──► dens.json ──► build_curve() ──► curve.json
                                                      └──► quantify() ──► quant.json

Everything is a pure function over dicts (spec `docs/specs/ep-craft-densitometry.md` §2.1-§2.9,
§4.1-§4.6): no VectorCraft, no ImageMagick, no I/O beyond json.load / json.dump.  The CLI owns
argparse and paths (`densitometry_cli.py`); the plots own matplotlib (`dens_plot.py`).

This package used to be one 1878-line module.  It is now one module per stage of the chain plus
the contracts and helpers they share:

    contracts.py  every stored-format constant: kinds, versions, suffixes, thresholds and the
                  note templates `measure` / `curve` / `quantify` write (§4.1, §4.4-§4.6)
    values.py     scalar and row helpers: rounding without -0, median, lane-list runs, the
                  uniform-y contract, the numeric contract of every chain file
    bands.py      §4.1 band math: smoothing, the ink walk, integration windows, the flank ramp
    lanes.py      §4.1 / R8 lane grammar (`--lanes`)
    json_io.py    the chain's JSON I/O plus the report v2 validation `measure` relies on
    measure.py    §4.2-§4.4: report v2 → dens.json (areas on both baselines)
    curve.py      §2.6, §2.7, §2.9, §4.5: dens.json + `--std` → curve.json
    layout.py     display width and number formatters shared by the two terminal tables (§5)
    quantify.py   §2.8, §3.3, §4.6: dens.json + curve.json + `--load` → quant.json

Two baselines, always both, per band (decision §2.2):
  * global — the report's top-hat `baseline` array (half window 0.30 cm).  It dips under a dense
    band, so `ink` from it under-reports the dark end of a titration.
  * flank — a straight ramp in y through the median gray of the rows beside the band (§2.3).
Both are integrated over their own window: walk outward on ink smoothed over a SMOOTH_CM half
window while it stays above min_ink, clamped to the midpoints of the neighbouring bands in the
same lane (§2.4, ruling R7).

Engineering notes are English, user-facing strings (JSON `notes`, error messages) are Chinese,
like the rest of the package (ruling R1).  Rounding happens only when the returned dict is built
(ruling R2); `walk_window` / `area_of` / `flank_ramp` / `smooth` stay full precision.

This file is a facade only: it re-exports names, it does not define behaviour.  Internal helpers
live in the owning module — patch or import them there (`epcraft.densitometry.contracts` for the
stored format, `...values` / `...bands` for the math primitives, `...layout` for the number
formatters).  Patching `epcraft.densitometry.measure` still reaches the CLI, because it binds
this module's attribute at call time.
"""

from .bands import _flank_levels, _ramp_at, _ramp_dict, area_of, flank_ramp, smooth, walk_window
from .contracts import (BASELINES, CURVE_KIND, CURVE_SUFFIX, DOC_SUFFIX, FITS, FLANK_CM, FUZZ,
                        KIND, MAX_DY_SPREAD, MIN_INK, NO_SCATTER_NOTE, QUANT_KIND, QUANT_SUFFIX,
                        REPORT_PATH_KEY, REPORT_SUFFIX, SKIP_NOTE, SMOOTH_CM, UNITS, VERSION,
                        WINDOW_TEXT)
from .curve import _linfit, _profile_arrays, build_curve, format_curve, parse_std
from .json_io import load_json, load_report, report_path, write_json
from .lanes import parse_lanes
from .layout import _dwidth
from .measure import measure
from .quantify import format_quant, parse_load, quantify

__all__ = [
    "BASELINES", "CURVE_KIND", "CURVE_SUFFIX", "DOC_SUFFIX", "FITS", "FLANK_CM", "FUZZ", "KIND",
    "MAX_DY_SPREAD", "MIN_INK", "NO_SCATTER_NOTE", "QUANT_KIND", "QUANT_SUFFIX", "REPORT_PATH_KEY",
    "REPORT_SUFFIX", "SKIP_NOTE", "SMOOTH_CM", "UNITS", "VERSION", "WINDOW_TEXT",
    "area_of", "build_curve", "flank_ramp", "format_curve", "format_quant", "load_json",
    "load_report", "measure", "parse_lanes", "parse_load", "parse_std", "quantify", "report_path",
    "smooth", "walk_window", "write_json",
    "_dwidth", "_flank_levels", "_linfit", "_profile_arrays", "_ramp_at", "_ramp_dict",
]
