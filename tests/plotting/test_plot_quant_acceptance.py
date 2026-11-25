#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_plot_quant_acceptance — independent acceptance tests for `plot-quant`."""

import contextlib
import json
import os
import tempfile
import unittest.mock
import warnings

os.environ.setdefault("MPLBACKEND", "Agg")

from epcraft import cli, densitometry


def _mpl():
    import matplotlib
    matplotlib.use("Agg", force=True)
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    return Figure, FigureCanvasAgg


@contextlib.contextmanager
def _capture_figures():
    Figure, FigureCanvasAgg = _mpl()
    captured = []
    orig = Figure.savefig

    def spy(self, fname, *args, **kwargs):
        captured.append((self, str(fname)))
        return orig(self, fname, *args, **kwargs)

    with unittest.mock.patch.object(Figure, "savefig", spy):
        yield captured


def _fig_texts(fig):
    return [t.get_text() for t in fig.texts]


def _ratio_quant():
    return {
        "kind": densitometry.QUANT_KIND,
        "version": 1,
        "baseline": "flank",
        "fit": "linear",
        "unit": "mg/mL",
        "load_cv_pct": 3.0,
        "samples": [
            {
                "name": "gPFD_HisProbe",
                "band_mw": None,
                "members": [
                    {
                        "lane": 12, "band": 2, "ratio": 1.0, "area": 1.03,
                        "equiv": 0.313, "equiv_sd": 0.098,
                        "conc": 0.313, "conc_sd": 0.098,
                        "out_of_range": False, "used": True,
                    },
                    {
                        "lane": 13, "band": 2, "ratio": 2.0, "area": 1.922,
                        "equiv": 0.691, "equiv_sd": 0.095,
                        "conc": 0.346, "conc_sd": 0.048,
                        "out_of_range": False, "used": False,
                    },
                    {
                        "lane": 14, "band": 3, "ratio": 3.0, "area": 2.476,
                        "equiv": 0.927, "equiv_sd": 0.096,
                        "conc": 0.309, "conc_sd": 0.032,
                        "out_of_range": False, "used": True,
                    },
                ],
                "est": {
                    "method": "ratio",
                    "conc": 0.309,
                    "conc_sd": 0.001,
                    "sem": 0.001,
                    "common_pct": 8.49,
                    "n": 3,
                    "n_used": 2,
                    "mean": {"conc": 0.309, "sd": 0.002, "sem": 0.001},
                    "ratio": {
                        "conc": 0.309, "slope_sd": 0.001, "r2": 1.0,
                        "intercept": None, "intercept_sd": None, "nonlinear": False,
                    },
                    "notes": [],
                },
            }
        ],
    }


def _mean_and_single_quant():
    return {
        "kind": densitometry.QUANT_KIND,
        "version": 1,
        "baseline": "flank",
        "fit": "linear",
        "unit": "mg/mL",
        "load_cv_pct": 3.0,
        "samples": [
            {
                "name": "gPFD_wt",
                "band_mw": None,
                "members": [
                    {
                        "lane": 11, "band": 1, "ratio": 1.0, "area": 1.3,
                        "equiv": 0.423, "equiv_sd": 0.061,
                        "conc": 0.423, "conc_sd": 0.061,
                        "out_of_range": False, "used": True,
                    },
                    {
                        "lane": 12, "band": 1, "ratio": 1.0, "area": 2.1,
                        "equiv": 0.745, "equiv_sd": 0.059,
                        "conc": 0.745, "conc_sd": 0.059,
                        "out_of_range": False, "used": True,
                    },
                ],
                "est": {
                    "method": "mean",
                    "conc": 0.584,
                    "conc_sd": 0.161,
                    "sem": 0.161,
                    "common_pct": 4.84,
                    "n": 2,
                    "n_used": 2,
                    "mean": {"conc": 0.584, "sd": 0.228, "sem": 0.161},
                    "ratio": None,
                    "notes": [],
                },
            },
            {
                "name": "SingleLane",
                "band_mw": None,
                "members": [
                    {
                        "lane": 15, "band": 1, "ratio": 1.0, "area": 1.5,
                        "equiv": 0.510, "equiv_sd": 0.055,
                        "conc": 0.510, "conc_sd": 0.055,
                        "out_of_range": False, "used": True,
                    },
                ],
                "est": {
                    "method": "single",
                    "conc": 0.510,
                    "conc_sd": None,
                    "sem": None,
                    "common_pct": 4.84,
                    "n": 1,
                    "n_used": 1,
                    "mean": None,
                    "ratio": None,
                    "notes": [],
                },
            },
        ],
    }


def _check_ratio_mode():
    from epcraft import plotting as dens_plot
    q = _ratio_quant()
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "fig.quant.png")
        with _capture_figures() as cap:
            w, h = dens_plot.plot_quant(q, out, width=1020)
        assert w == 1020 and h > 400, f"unexpected size {(w, h)}"
        assert os.path.isfile(out)
        assert [f for f in os.listdir(td) if f.startswith(".dens-plot-")] == []
        fig, _ = cap[0]
        ax = fig.axes[0]
        # Errorbar collections + fit line + 3 sample points
        assert len(ax.collections) >= 3, f"expected errorbars, got {len(ax.collections)} collections"
        texts = "\n".join(_fig_texts(fig))
        assert "±" in texts, f"missing ± in texts: {texts}"
        assert "0.309 ± 0.001" in texts, f"missing fitted conc ± err: {texts}"
        assert "r² = 1.0000" in texts, f"missing r²: {texts}"
        assert "common ±8.49%" in texts, f"missing common_pct: {texts}"
        leg_texts = [t.get_text() for t in ax.get_legend().get_texts()]
        assert "dropped sample" in leg_texts and "±1 SD" in leg_texts, leg_texts


def _check_mean_and_grid_mode():
    from epcraft import plotting as dens_plot
    q = _mean_and_single_quant()
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "multi.quant.png")
        with _capture_figures() as cap:
            w, h = dens_plot.plot_quant(q, out, width=1200, cols=2)
        assert w == 1200 and h > 400
        fig, _ = cap[0]
        assert len(fig.axes) == 2, f"expected 2 panels, got {len(fig.axes)}"
        texts = "\n".join(_fig_texts(fig))
        assert "0.584 ± 0.161" in texts, texts
        assert "SD = ±0.228" in texts, texts
        assert "0.51 ± 0.055" in texts, texts


def _check_legacy_lanes_and_non_ascii_warning():
    from epcraft import plotting as dens_plot
    legacy = {
        "kind": densitometry.QUANT_KIND,
        "baseline": "flank",
        "fit": "linear",
        "unit": "ug",
        "lanes": [
            {"lane": 5, "label": "样品A", "bands": [{"index": 1, "area": 1.2, "mass": 0.45, "mass_sd": 0.04, "out_of_range": False}]}
        ],
    }
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "legacy.quant.png")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            w, h = dens_plot.plot_quant(legacy, out, width=800)
        assert w == 800 and h > 300
        assert any(issubclass(w.category, dens_plot.LaneLabelWarning) for w in caught), caught


def _check_cli_and_errors():
    from epcraft import plotting as dens_plot
    q = _ratio_quant()
    with tempfile.TemporaryDirectory() as td:
        qpath = os.path.join(td, "fig.quant.json")
        with open(qpath, "w", encoding="utf-8") as fh:
            json.dump(q, fh)
        rc = cli.main(["dens", "plot-quant", "--quant", qpath])
        assert rc == 0
        default_png = os.path.join(td, "fig.quant.png")
        assert os.path.isfile(default_png)

        bad_out = os.path.join(td, "out.jpg")
        try:
            dens_plot.plot_quant(q, bad_out)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for non-.png out")


def run():
    checks = [
        _check_ratio_mode,
        _check_mean_and_grid_mode,
        _check_legacy_lanes_and_non_ascii_warning,
        _check_cli_and_errors,
    ]
    for fn in checks:
        fn()
    print(f"test_plot_quant_acceptance {len(checks)}/{len(checks)} 通过")
    return len(checks)


if __name__ == "__main__":
    run()
