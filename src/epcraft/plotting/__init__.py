"""
plotting — the densitometry plotting subpackage (spec §4.7).

``dens_plot`` used to be one 2345-line module; it is now three modules with one job each:

    render.py   shared palette/type-size table, lazy Matplotlib loading, text measurement,
                PNG output plumbing, value → pixel mappings and number/tick helpers
    bands.py    ``plot_bands``: dens validation, group normalisation, traces, band layout
                and the band-tile drawing
    curve.py    ``plot_curve``: curve/quant validation, fit and sample markers, error bars,
                residuals and curve layout

``epcraft.dens_plot`` remains the public entry point and re-exports the names below, so
callers and the acceptance suites keep importing and patching that module.
"""

from .bands import BASELINES, KIND, band_layout, plot_bands
from .curve import CURVE_KIND, CURVE_SUFFIX, QUANT_KIND, curve_layout, plot_curve
from .quant import plot_quant, quant_layout
from .render import LaneLabelWarning

__all__ = [
    "BASELINES", "CURVE_KIND", "CURVE_SUFFIX", "KIND", "QUANT_KIND", "LaneLabelWarning",
    "band_layout", "curve_layout", "plot_bands", "plot_curve", "plot_quant", "quant_layout",
]
