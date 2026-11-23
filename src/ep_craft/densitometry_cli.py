#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""densitometry_cli — the `densitometry` group (alias `dens`): argparse and paths only.

Registered from `canvas_cli.add_commands` with a lazy import, the same style as the workflow
group, so importing the CLI skeleton never pulls in the math.  The group is offline: it reads the
report v2 (`fig.mw.json`) that render / sync already wrote and never launches VectorCraft.

Every command has its own `_add_<name>(actions)` helper so sibling tickets can append curve /
quantify / plot-bands / plot-curve without touching `add_commands`.  Handlers parse arguments,
validate paths and call `densitometry`; the library does all the math.  User errors are
RuntimeError / OSError / ValueError with a single-line Chinese message, which `gel_photo.main`
prints as one `[!] …` line on stderr with exit code 2; normal output is `[i] …` on stdout.
"""
import os

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
    """R5: never overwrite a file the command reads (report / document as typed or resolved)."""
    real = os.path.realpath(target)
    for src in sources:
        if src and os.path.realpath(src) == real:
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


# ---------------------------------------------------------------- registration

def add_commands(sub):
    """Register `densitometry` (alias `dens`) with its action sub-parsers."""
    group = sub.add_parser("densitometry", aliases=["dens"],
                           help="Measure and quantify bands from a report v2 (offline)")
    actions = group.add_subparsers(dest="action", required=True)
    _add_measure(actions)
