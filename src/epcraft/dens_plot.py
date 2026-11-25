#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dens_plot — public facade for the densitometry plotting subpackage (spec §4.7).

The CLI and existing callers import the plotting functions here.

The implementation lives in three modules:

    epcraft.plotting.render   palette / type sizes, lazy matplotlib loading, text measurement,
                              PNG output plumbing, value → pixel mappings, number + tick helpers
    epcraft.plotting.bands    ``plot_bands``: dens validation, group normalisation, traces,
                              band layout and the band-tile drawing
    epcraft.plotting.curve    ``plot_curve``: curve / quant validation, the out-of-range
                              status strip, replicate error bars, curve layout and drawing

Two public pictures, both drawn directly with matplotlib's Agg backend (no pyplot, no GUI,
no ImageMagick/MVG):

``plot_bands(dens, out, baseline="both", lanes=None, cols=2, width=1440, *, groups=None)``
    Local band-peak traces, one tile per lane (or per named group).  The x axis is the true
    migration distance from the dens.json profile (``y_cm``), the y axis is the active
    baseline minus the raw gray value (a signed ink trace).  All traces in the figure share a
    zero line and one y scale, so a standard peak and a sample peak can be compared directly.
    Single-lane tiles show all bands in the lane palette and shade each signed integration
    window. Group tiles overlay the selected ``(lane, band)`` traces and mark the window
    endpoints. A third tuple item supplies a concentration for the group's coolwarm scale.

``plot_curve(curve, out, quant=None, width=1020)``
    The standard curve (and optional sample markers) with a residual strip that shares the
    area axis.  Replicate bars are horizontal on the area axis because ``curve.points[].sd``
    is the sample SD of replicate areas, not of masses.

Only ``matplotlib`` is optional.  It is imported lazily inside the two plot functions, so
``import epcraft.dens_plot`` and the non-plot CLI commands keep working without the extra;
a plot call without it raises one RuntimeError whose message contains ``epcraft[plot]``.
The module also exposes ``LaneLabelWarning``.  Every render that has to replace a non-English
lane label with its numeric fallback (``Lane N`` for bands, ``L<n>`` for quant samples) emits
exactly one warning per affected lane through ``warnings.warn_explicit`` (the normal Python
warning channel, visible on stderr by default), so a CLI user can suppress it with
``warnings.simplefilter`` and a test can record it with ``warnings.catch_warnings(record=True)``.

All figure prose is English.  Standard unit symbols that Arial covers (µ, ·, ±, ², →) are
used; user lane labels are only drawn when they are printable ASCII, otherwise the stable
English fallback is used and the warning above is emitted.

This file is a facade only: it re-exports names, it does not define behaviour.  Internal
helpers live in the owning submodule — patch or import them there
(``epcraft.plotting.render`` for the shared drawing machinery, ``epcraft.plotting.bands`` /
``epcraft.plotting.curve`` for one picture).  Patching ``epcraft.dens_plot.plot_bands`` still
reaches the CLI, because it binds this module's attribute at call time.
"""
from .plotting import (
    BASELINES,
    CURVE_KIND,
    CURVE_SUFFIX,
    KIND,
    QUANT_KIND,
    LaneLabelWarning,
    band_layout,
    curve_layout,
    plot_bands,
    plot_curve,
)
