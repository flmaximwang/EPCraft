#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""densitometry_cli — the `densitometry` group (alias `dens`): argparse and paths only.

Registered from `canvas_cli.add_commands` with a lazy import, the same style as the workflow
group, so importing the CLI skeleton never pulls in the math.  The group is offline: it reads the
report v2 (`fig.mw.json`) that render / sync already wrote and never launches VectorCraft.

Every command has its own `_add_<name>(actions)` helper so sibling tickets can append curve /
quantify without touching `add_commands`.  Handlers parse arguments,
validate paths and call `densitometry`; the library does all the math.  User errors are
RuntimeError / OSError / ValueError with a single-line Chinese message, which `gel_photo.main`
prints as one `[!] …` line on stderr with exit code 2; normal output is `[i] …` on stdout.
"""
import math
import os
import re

from . import densitometry

OUT_SUFFIX = ".dens.json"          # default: <report stem>.dens.json (fig.mw.json → fig.dens.json)


# ---------------------------------------------------------------- shared path checks

def _out_path(report, out):
    """dens.json output path (rulings R5 / R12).

    The default is the report stem + OUT_SUFFIX (`fig.mw.json` → `fig.dens.json`, i.e. the `.mw`
    of the report name is dropped as well); an explicit `--out` is used as typed (made absolute).
    A directory target, a missing output directory and a target that resolves to any file the
    command reads are refused — the output directory is never created implicitly.
    """
    report_abs = os.path.abspath(report)
    if out is None:
        stem = report_abs[:-5] if report_abs.lower().endswith(".json") else report_abs
        if stem.lower().endswith(densitometry.REPORT_SUFFIX[:-5]):     # ".mw" of fig.mw.json
            stem = stem[:-len(densitometry.REPORT_SUFFIX[:-5])]
        target = stem + OUT_SUFFIX
    else:
        target = os.path.abspath(out)
    if os.path.isdir(target):
        raise ValueError("--out 指向的是目录 %s（要写文件路径）" % target)
    parent = os.path.dirname(target) or "."
    if not os.path.isdir(parent):
        raise ValueError("输出目录 %s 不存在（不自动创建，先建好它）" % parent)
    return target


def _refuse_overwrite(target, sources):
    """R5: never overwrite a file the command reads, including through a hard link."""
    real = os.path.realpath(target)
    for src in filter(None, sources):
        aliases_input = os.path.realpath(src) == real
        if not aliases_input:
            try:
                aliases_input = os.path.samefile(target, src)
            except FileNotFoundError:
                continue  # A new output has no inode to compare.
        if aliases_input:
            raise ValueError("--out 不能指向输入文件 %s（它会覆盖本命令读的东西）" % src)


def _lane_line(lane):
    """`泳道 9 S1：2 带，面积 12.4 / 8.1 gray·cm` (flank / global, ruling R6 for a None label)."""
    head = "泳道 %d" % lane["lane"]
    label = lane.get("label")
    if label:
        head += " %s" % label
    return "%s：%d 带，面积 %.1f / %.1f gray·cm" % (
        head, len(lane["bands"]), lane["area_flank"], lane["area_global"])


# ---------------------------------------------------------------- measure

def cmd_measure(args):
    """Report v2 → dens.json, then print the per-lane table (spec §3.1)."""
    report = densitometry.load_report(args.report)
    resolved = densitometry.report_path(args.report)          # the file that was actually read
    out = _out_path(resolved, args.out)
    _refuse_overwrite(out, (resolved, args.report))
    lanes = densitometry.parse_lanes(args.lanes, len(report["lanes"])) if args.lanes is not None else None
    dens = densitometry.measure(report, lanes=lanes, flank_cm=args.flank_cm, min_ink=args.min_ink,
                                include_marker=args.include_marker, include_trimmed=args.include_trimmed)
    densitometry.write_json(dens, out)
    print("[i] 报告 %s：量了 %d 条泳道，面积按 flank / global 顺序（gray·cm）"
          % (resolved, len(dens["lanes"])))
    for lane in dens["lanes"]:
        print(_lane_line(lane))
    for note in dens["notes"]:
        print("[i] %s" % note)
    print("[i] 写出 %s" % out)
    return 0


def _add_measure(actions):
    """`measure` — the only command ticket .1 owns."""
    p = actions.add_parser("measure", help="Measure band areas on both baselines from a report v2")
    p.add_argument("report", help="report v2 (fig.mw.json), or the .vectorcraft document it belongs to")
    p.add_argument("--out", help="dens.json path (default: <report stem>.dens.json)")
    p.add_argument("--flank-cm", type=float, default=densitometry.FLANK_CM,
                   help="rows taken beside a band for the flank level, cm (default %(default)s)")
    p.add_argument("--min-ink", type=float, default=densitometry.MIN_INK,
                   help="walk threshold in gray levels (default %(default)s)")
    p.add_argument("--lanes", help="lane numbers to measure, e.g. 1,2,8-10 (default: sample lanes)")
    p.add_argument("--include-marker", action="store_true", help="also measure the marker lane")
    p.add_argument("--include-trimmed", action="store_true", help="also measure trimmed lanes")
    p.set_defaults(func=cmd_measure)


# ---------------------------------------------------------------- curve

def _curve_out_path(dens, out):
    """curve.json output path (rulings R5 / R12): the chain suffix swapped, next to the dens.

    The document stem carries the chain, so `fig.mw.json` → `fig.dens.json` → `fig.curve.json`
    (a `--dens` as typed, in its own directory).  Falls back to dropping the final extension when
    the dens path does not end in `.dens.json` (`mydens.json` / `mydens` → `mydens.curve.json`).

    Same three refusals as `_out_path`: a directory target, a missing output directory, and (done
    by the caller through `_refuse_overwrite`) a target that is one of the files read.
    """
    if out is None:
        dens_abs = os.path.abspath(dens)
        if dens_abs.lower().endswith(OUT_SUFFIX):
            stem = dens_abs[:-len(OUT_SUFFIX)]
        else:
            stem = os.path.splitext(dens_abs)[0]
        target = stem + densitometry.CURVE_SUFFIX
    else:
        target = os.path.abspath(out)
    if os.path.isdir(target):
        raise ValueError("--out 指向的是目录 %s（要写文件路径）" % target)
    parent = os.path.dirname(target) or "."
    if not os.path.isdir(parent):
        raise ValueError("输出目录 %s 不存在（不自动创建，先建好它）" % parent)
    return target


# ---------------------------------------------------------------- plot-bands (ticket ep-bqv.2)

BANDS_SUFFIX = ".bands.png"        # default: <dens stem>.bands.png (fig.dens.json → fig.bands.png)


def _bands_path(dens_path, out):
    """plot-bands output path (lead ruling for ticket .2, rulings R12 / R5).

    The default is derived from the dens path as typed: a `*.dens.json` name loses exactly
    that suffix (`fig.dens.json` → `fig.bands.png`, same directory); any other name loses its
    final extension and gains `.bands.png` (`mydens.json` → `mydens.bands.png`).  An explicit
    `--out` is used as typed (made absolute).  A directory target or a missing output
    directory is refused — the directory is never created implicitly.
    """
    if out is None:
        text = str(dens_path)
        if text.lower().endswith(OUT_SUFFIX):
            target = text[:-len(OUT_SUFFIX)] + BANDS_SUFFIX
        else:
            stem = os.path.splitext(text)[0]
            target = stem + BANDS_SUFFIX
        target = os.path.abspath(target)
    else:
        target = os.path.abspath(out)
    if os.path.isdir(target):
        raise ValueError("--out 指向的是目录 %s（要写文件路径）" % target)
    parent = os.path.dirname(target) or "."
    if not os.path.isdir(parent):
        raise ValueError("输出目录 %s 不存在（不自动创建，先建好它）" % parent)
    return target


def cmd_curve(args):
    """dens.json + standard lanes → curve.json, then print the table (spec §3.2)."""
    dens = densitometry.load_json(args.dens, densitometry.KIND)
    out = _curve_out_path(args.dens, args.out)
    _refuse_overwrite(out, (args.dens,))
    std = densitometry.parse_std(args.std)
    curve = densitometry.build_curve(dens, std, unit=args.unit, fit=args.fit,
                                     baseline=args.baseline, drop=args.drop or (),
                                     dens_path=args.dens, std_text=args.std)
    densitometry.write_json(curve, out)
    print(densitometry.format_curve(curve))
    print("[i] 写出 %s" % out)
    return 0


def _add_curve(actions):
    """`curve` — standard curve from the standard lanes (spec §3.2)."""
    p = actions.add_parser("curve", help="Fit a standard curve from the standard lanes of a dens.json")
    p.add_argument("--dens", required=True, help="dens.json written by `measure`")
    p.add_argument("--std", required=True,
                   help="standard lanes, e.g. 2=20,3:2=10,4:*=5 (lane[:band]=mass; default the "
                        "lane's strongest band, :* the whole-lane total)")
    p.add_argument("--out", help="curve.json path (default: fig.dens.json → fig.curve.json)")
    p.add_argument("--baseline", choices=list(densitometry.BASELINES), default="flank",
                   help="baseline column that drives the curve (default %(default)s)")
    p.add_argument("--fit", choices=list(densitometry.FITS), default="linear",
                   help="fit model (default %(default)s)")
    p.add_argument("--unit", default="ug", help="mass unit recorded in the curve (default %(default)s)")
    p.add_argument("--drop", type=int, action="append", default=[],
                   help="standard lane to keep as `used: false` (repeatable)")
    p.set_defaults(func=cmd_curve)


def _parse_plot_group(text):
    """Parse a named group of lane[:band][@concentration] entries."""
    title, separator, raw_members = text.partition("=")
    title = title.strip()
    if not separator or not title or not title.isascii() or not title.isprintable():
        raise ValueError("--group expects an English name and lane list, e.g. Standards=2,3,4")
    members = []
    for token in raw_members.split(","):
        peak, at, value = token.strip().partition("@")
        match = re.fullmatch(r"([1-9][0-9]*)(?::([1-9][0-9]*))?", peak.strip())
        if not match:
            raise ValueError("--group %r expects lane[:band][@concentration] entries, got %r"
                             % (title, token))
        lane, band = match.groups()
        member = (int(lane), int(band) if band else None)
        if at:
            try:
                concentration = float(value)
            except ValueError:
                concentration = math.nan
            if not math.isfinite(concentration) or concentration < 0:
                raise ValueError("--group %r expects a finite, nonnegative concentration, got %r"
                                 % (title, value))
            member += (concentration,)
        members.append(member)
    if any(len(member) != len(members[0]) for member in members):
        raise ValueError("--group %r requires a concentration for every member or none" % title)
    return title, members


def cmd_plot_bands(args):
    """dens.json → PNG grid of local peaks by lane or named group."""
    from . import dens_plot

    dens_path = os.path.abspath(args.dens)
    dens = densitometry.load_json(dens_path, densitometry.KIND)
    out = _bands_path(dens_path, args.out)
    _refuse_overwrite(out, (dens_path,))
    lanes = None
    if args.lanes is not None:
        available = [int(ln["lane"]) for ln in dens.get("lanes") or []
                     if isinstance(ln, dict) and isinstance(ln.get("lane"), int)]
        count = max(available) if available else 1
        lanes = [no for no in densitometry.parse_lanes(args.lanes, count) if no in available]
        if not lanes:
            raise RuntimeError("--lanes 选了 %s，但这份 dens 里只有泳道 %s"
                               % (args.lanes, ", ".join(str(v) for v in available) or "（空）"))
    groups = [_parse_plot_group(text) for text in args.group] if args.group else None
    w, h = dens_plot.plot_bands(dens, out, baseline=args.baseline, lanes=lanes,
                                cols=args.cols, width=args.width, groups=groups)
    if groups is not None:
        selection = "%d groups" % len(groups)
    else:
        selection = "%d 条泳道" % (len(lanes) if lanes is not None else len(dens.get("lanes") or []))
    print("[i] dens %s：%s → %s（%dx%d 像素，baseline %s）"
          % (dens_path, selection, out, w, h, args.baseline))
    return 0


def _add_plot_bands(actions):
    """`plot-bands` — local peak traces aligned to a shared zero baseline."""
    p = actions.add_parser("plot-bands", help="Render local band peaks from a dens.json to PNG")
    p.add_argument("--dens", required=True, help="dens.json written by measure")
    p.add_argument("--out", help="output PNG (default: <dens stem>.bands.png)")
    selection = p.add_mutually_exclusive_group()
    selection.add_argument("--lanes", help="lane numbers to plot, e.g. 1,2,8-10 (default: all in the dens)")
    selection.add_argument("--group", action="append", metavar="NAME=LANE[:BAND][@CONC],…",
                           help="overlay named peaks (repeatable); omit :band to select by area; "
                           "supply @concentration for every peak in a group to color by concentration")
    p.add_argument("--baseline", choices=["flank", "global", "both"], default="both",
                   help="baseline correction to display (default %(default)s)")
    p.add_argument("--cols", type=int, default=2, help="columns in the grid (default %(default)s)")
    p.add_argument("--width", type=int, default=1440, help="image width in pixels (default %(default)s)")
    p.set_defaults(func=cmd_plot_bands)


# ---------------------------------------------------------------- plot-curve (ticket ep-bqv.4)

CURVE_OUT_SUFFIX = ".curve.png"    # default: fig.curve.json -> fig.curve.png (the chain stem)
CURVE_SUFFIX = getattr(densitometry, "CURVE_SUFFIX", ".curve.json")


def _curve_png_path(curve_path, out):
    """plot-curve output path (lead ruling, rulings R12 / R5).

    The chain suffix is swapped on the document stem, exactly as `cmd_curve` does for its
    default: a `*.curve.json` name loses that suffix and gains `.curve.png` (`fig.curve.json`
    → `fig.curve.png`, NOT `fig.curve.curve.png`); any other name loses its final extension
    and gains `.curve.png` (`c.json` → `c.curve.png`, `c` → `c.curve.png`).  An explicit
    `--out` is used as typed (made absolute).  A directory target or a missing output
    directory is refused — the directory is never created implicitly.
    """
    if out is None:
        text = str(curve_path)
        if text.lower().endswith(CURVE_SUFFIX):
            target = text[:-len(CURVE_SUFFIX)] + CURVE_OUT_SUFFIX
        else:
            target = os.path.splitext(text)[0] + CURVE_OUT_SUFFIX
        target = os.path.abspath(target)
    else:
        target = os.path.abspath(out)
    if os.path.isdir(target):
        raise ValueError("--out 指向的是目录 %s（要写文件路径）" % target)
    parent = os.path.dirname(target) or "."
    if not os.path.isdir(parent):
        raise ValueError("输出目录 %s 不存在（不自动创建，先建好它）" % parent)
    return target


def cmd_plot_curve(args):
    """curve.json (and optional quant.json) → PNG with the fit and the residual strip (§4.7)."""
    from . import dens_plot

    curve_path = os.path.abspath(args.curve)
    curve = densitometry.load_json(curve_path, dens_plot.CURVE_KIND)
    quant = None
    quant_path = None
    if args.quant is not None:
        quant_path = os.path.abspath(args.quant)
        quant = densitometry.load_json(quant_path, dens_plot.QUANT_KIND)
    out = _curve_png_path(curve_path, args.out)
    _refuse_overwrite(out, (curve_path, quant_path))
    w, h = dens_plot.plot_curve(curve, out, quant=quant, width=args.width)
    print("[i] 写出 %s（%dx%d 像素，fit %s）" % (out, w, h, curve.get("fit")))
    return 0


def _add_plot_curve(actions):
    """`plot-curve` — the standard curve, its residuals and (optionally) the samples (§4.7)."""
    p = actions.add_parser("plot-curve",
                           help="Render a curve.json as a PNG (fit line, residuals, optional samples)")
    p.add_argument("--curve", required=True, help="curve.json written by `curve`")
    p.add_argument("--quant", help="quant.json written by `quantify`: adds the sample squares")
    p.add_argument("--out", help="output PNG (default: fig.curve.json -> fig.curve.png)")
    p.add_argument("--width", type=int, default=1600, help="image width in pixels (default %(default)s)")
    p.set_defaults(func=cmd_plot_curve)


# ---------------------------------------------------------------- quantify

def _quant_out_path(curve, out):
    """quant.json output path (rulings R5 / R12): the chain suffix swapped, next to the curve.

    Mirrors `_curve_out_path`: `fig.curve.json` → `fig.quant.json` (the document stem carries the
    chain, NOT `fig.curve.quant.json`).  Falls back to dropping the final extension when the curve
    path does not end in `.curve.json` (`c.json` → `c.quant.json`).

    Same three refusals as the other chain writers: a directory target, a missing output directory,
    and (done by the caller through `_refuse_overwrite`) a target that is one of the files read.
    """
    if out is None:
        curve_abs = os.path.abspath(curve)
        if curve_abs.lower().endswith(densitometry.CURVE_SUFFIX):
            stem = curve_abs[:-len(densitometry.CURVE_SUFFIX)]
        else:
            stem = os.path.splitext(curve_abs)[0]
        target = stem + densitometry.QUANT_SUFFIX
    else:
        target = os.path.abspath(out)
    if os.path.isdir(target):
        raise ValueError("--out 指向的是目录 %s（要写文件路径）" % target)
    parent = os.path.dirname(target) or "."
    if not os.path.isdir(parent):
        raise ValueError("输出目录 %s 不存在（不自动创建，先建好它）" % parent)
    return target


def cmd_quantify(args):
    """dens.json + curve.json (+ `--load`) → quant.json, then print the table (spec §3.3).

    Every refusal stays one `[!] …` line with exit 2: `load_json` / `quantify` / `parse_load` raise
    RuntimeError, the path helpers raise ValueError, and `gel_photo.main` catches both.
    """
    dens = densitometry.load_json(args.dens, densitometry.KIND)
    curve = densitometry.load_json(args.curve, densitometry.CURVE_KIND)
    out = _quant_out_path(args.curve, args.out)
    _refuse_overwrite(out, (args.dens, args.curve))
    load = densitometry.parse_load(args.load)
    quant = densitometry.quantify(dens, curve, load, dens_path=args.dens, curve_path=args.curve,
                                  load_text=args.load)
    densitometry.write_json(quant, out)
    print(densitometry.format_quant(quant, curve=curve))
    print("[i] 写出 %s" % out)
    return 0


def _add_quantify(actions):
    """`quantify` — area → mass against the standard curve (spec §3.3)."""
    p = actions.add_parser("quantify",
                           help="Convert band areas to protein mass with the standard curve")
    p.add_argument("--dens", required=True, help="dens.json written by `measure`")
    p.add_argument("--curve", required=True, help="curve.json written by `curve`")
    p.add_argument("--out", help="quant.json path (default: fig.curve.json → fig.quant.json)")
    p.add_argument("--load", help="volume loaded per lane in uL, e.g. 9=2,10=2 (default: unknown)")
    p.set_defaults(func=cmd_quantify)


# ---------------------------------------------------------------- registration

def add_commands(sub):
    """Register `densitometry` (alias `dens`) with its action sub-parsers."""
    group = sub.add_parser("densitometry", aliases=["dens"],
                           help="Measure and quantify bands from a report v2 (offline)")
    actions = group.add_subparsers(dest="action", required=True)
    _add_measure(actions)
    _add_curve(actions)
    _add_plot_bands(actions)
    _add_plot_curve(actions)
    _add_quantify(actions)
