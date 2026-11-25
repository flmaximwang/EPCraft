"""
contracts — the stored-format constants of the densitometry chain (spec §4.1, §4.4-§4.6).

Every number, kind, suffix and note template a chain file carries lives here, so `measure`,
`build_curve`, `quantify` and the CLI cannot drift apart on what they store.  Nothing in this
module computes; importing it is enough to read a dens / curve / quant file.
"""

import re

VERSION = 1
KIND = "ep-craft-densitometry"
CURVE_KIND = "ep-craft-densitometry-curve"
MIN_INK = 0.5              # gray levels; the walking threshold
FLANK_CM = 0.15            # rows taken beside a band for the flank level
SMOOTH_CM = 0.03           # half window for the edge walk (= profiles.SMOOTH_CM, mirrored here
                           #   so this module never pulls in magick_io through profiles)
FUZZ = 1e-9

MAX_DY_SPREAD = 0.10       # report y_cm is rounded to 4 dp: accept diffs within 10 % of the median
DOC_SUFFIX = ".vectorcraft"
REPORT_SUFFIX = ".mw.json"
REPORT_PATH_KEY = "_report_path"   # load_report() records the path it was handed (R6: source.report)

UNITS = {"y": "cm from gel-face top", "gray": "0-255 (lower = darker)", "ink": "baseline - gray",
         "area": "gray·cm (ink integrated over the band window)"}
WINDOW_TEXT = ("walk outward on smoothed ink while > min_ink, clamped to the midpoints of the "
               "neighbouring bands")

CURVE_SUFFIX = ".curve.json"      # default: <document stem>.curve.json, i.e. fig.dens.json → fig.curve.json
FITS = ("linear", "power")
BASELINES = ("flank", "global")

QUANT_KIND = "ep-craft-densitometry-quant"
QUANT_SUFFIX = ".quant.json"       # default: <document stem>.quant.json, i.e. fig.curve.json → fig.quant.json

# ONE template for the "skipped standard lanes" note: `quantify` writes it, `format_quant` reads the
# count back out of it (`_SKIP_NOTE_RE` is compiled from this very string, never hand-written), because
# §4.6's exhaustive key list has no field for it and the note is the only record.
SKIP_NOTE = "已跳过 %d 条标准道（泳道 %s）"
_SKIP_NOTE_RE = re.compile(re.escape(SKIP_NOTE).replace("%d", "(\\d+)").replace("%s", "(.+)"))

# ONE template for the "no residual scatter" note: a fit without a usable `residual_sd` gets no
# invented uncertainty, and this sentence says why (ep-c1i §4).  It is emitted only when at least
# one band actually received a mass — otherwise there was no uncertainty to withhold and the note
# would describe nothing the reader can see.
NO_SCATTER_NOTE = ("标曲没有可用的残差散布（fit_params 缺 residual_sd / sxx / mean_x 或 n ≤ 2），"
                   "样本质量与浓度的不确定度不给数")
