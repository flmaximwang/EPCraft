#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_lane_quad_acceptance — independent acceptance suite for ep-t27 (trapezoid lanes).

Written from the ticket and the conventions, not from the implementation: every expected number
below is re-derived here (separator interpolation, midpoint-of-centres seams, pixel-centre
integration spans), so the suite can disagree with the code.

What is asserted
----------------
1. Read-back, rectangular input: the lane dicts stay exactly the old three keys and the old
   numbers; nothing gains a quad key it does not need.
2. Read-back, slanted separator: the two ends of the drawn line are kept. A separator drawn
   *only* across the window (not the whole gel) is extended along its own line to y = 0 and
   y = H, so the vertices are the line's own values, never a midpoint. Neighbour lanes stay
   untouched.
3. Profile sampling: for a quad the integration span is L(y)..R(y) per row, with the same
   pixel-centre rule and the same y grid as before; a fixture is used where the old
   midpoint-of-the-ends rectangle would have swallowed ink from the neighbouring lane and the
   per-row quad does not. The scalar `lane_profile(...)` keeps behaving like the old one.
4. Crossing separators: read-back refuses, names the offending pair of separators, and leaves
   the document byte-identical (no partial model).
5. validate(): a quad that is too narrow at the top or at the bottom, and a quad whose two
   edges cross, are refused; the old message for a degenerate rectangle is unchanged.
6. Figure: the gel clip is a quadrilateral (not a rectangle); its top edge is the seam from the
   top-row centres and its bottom edge the seam from the bottom-row centres, so a trimmed
   neighbour still splices by the midpoint rule; a lane label sits at the lane's gel-top centre.
7. Regression: a rectangular model produces the geometry captured from the pre-change
   implementation (fixture `ep-t27_rect_golden.json`), the clip stays a live rectangle, and a
   re-plan is empty.
8. Sync: two trapezoids with the same bounding box but different vertices are distinguished
   (the clip is reshaped to the new vertices), and the second plan is empty.
9. Workbench IO: the export stays `version 1`; a rectangular export is byte-for-byte the old
   one. A quad export carries the four optional keys, round-trips (import = no ops when the
   document already matches), and refuses narrow / discontinuous / crossing quads. An old
   rectangular export is still read as rectangles.
10. fig.mw.json: quad lanes carry the four keys, `left_cm` / `right_cm` are the gel-top values,
    and `notes` says the lane is a trapezoid with both extents. The rectangular report is
    unchanged byte for byte, and the Profiles captions / axis are identical between the
    rectangular and the trapezoid render.
11. Writer: a quad model rendered by `workbench.add_workbench` / `figure.build_document` is
    read back unchanged (slanted separators, no flattening), and re-planning that document is
    empty.

Run:  PYTHONPATH=src python -m tests.selftest_lane_quad_acceptance
"""
import base64
import copy
import json
import math
import os
import re

from epcraft import figure, lane_select, profiles, vc_file, workbench, workbench_io

PT = vc_file.PT_PER_CM
HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "ep-t27_rect_golden.json")
TOL = 1e-4          # cm / pt tolerance for geometry read back through documents


# ---------------------------------------------------------------- helpers

def _golden():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _tops(model):
    """Refresh the stored gel-top centres so a slanted model's ep-meta is self-consistent."""
    model["meta"]["prev_centres_cm"] = [round((ln.get("left_top_cm", ln["left_cm"])
                                               + ln.get("right_top_cm", ln["right_cm"])) / 2.0, 4)
                                        for ln in model["lanes"]]
    return model


def _model_rect():
    return copy.deepcopy(_golden()["model"])


def _model_quad():
    """Golden rectangular model with separators 3 (and therefore lanes 2/3) slanted 2.30 → 2.60."""
    g = _golden()
    model = copy.deepcopy(g["model"])
    model["lanes"][1] = dict(model["lanes"][1], left_top_cm=1.25, left_bottom_cm=1.25,
                            right_top_cm=2.30, right_bottom_cm=2.60)
    model["lanes"][2] = dict(model["lanes"][2], left_top_cm=2.30, left_bottom_cm=2.60,
                            right_top_cm=3.35, right_bottom_cm=3.35)
    return _tops(model)


def _model_quad_swapped():
    """Same bounding box as _model_quad, opposite slant: 2.60 at the top, 2.30 at the bottom.

    `left_cm` / `right_cm` are the gel-top values, so they move with the top row (the contract)."""
    model = copy.deepcopy(_golden()["model"])
    model["lanes"][1] = dict(model["lanes"][1], left_cm=1.25, left_top_cm=1.25, left_bottom_cm=1.25,
                            right_cm=2.60, right_top_cm=2.60, right_bottom_cm=2.30)
    model["lanes"][2] = dict(model["lanes"][2], left_cm=2.60, left_top_cm=2.60, left_bottom_cm=2.30,
                            right_top_cm=3.35, right_bottom_cm=3.35)
    return _tops(model)


def _bounds(ln, y_cm, height_cm):
    """Independent re-derivation of the lane edge positions at one row (rect = constant)."""
    lt = ln.get("left_top_cm", ln["left_cm"])
    rt = ln.get("right_top_cm", ln["right_cm"])
    lb = ln.get("left_bottom_cm", ln["left_cm"])
    rb = ln.get("right_bottom_cm", ln["right_cm"])
    t = 0.0 if height_cm <= 0 else float(y_cm) / float(height_cm)
    return lt + (lb - lt) * t, rt + (rb - rt) * t


def _row_span(a_cm, b_cm, n, ppc):
    """Pixel-centre rule of the sampler: ceil(a·ppc − 0.5) .. floor(b·ppc − 0.5), nearest column fallback."""
    lo = max(0, int(math.ceil(a_cm * ppc - 0.5)))
    hi = min(n - 1, int(math.floor(b_cm * ppc - 0.5)))
    if hi < lo:
        c = min(n - 1, max(0, int(round((a_cm + b_cm) / 2.0 * ppc - 0.5))))
        lo = hi = c
    return lo, hi


def _centres(model, y_cm, height_cm):
    return [round((_bounds(ln, y_cm, height_cm)[0] + _bounds(ln, y_cm, height_cm)[1]) / 2.0, 4)
            for ln in model["lanes"]]


def _expected_segs(model, keep, gap_cm):
    """Independent re-derivation of the Figure strips.

    Seams are midpoints of adjacent lane centres, evaluated at the gel top row and at the gel bottom
    row (the old rule, now per row). The layout accumulates the *top* widths, and the gel image is
    placed at `dst − src_x0_top`, so source x maps to `dst + (x − src_x0_top)`; the bottom edge of a
    strip therefore sits at `dst + (b0 − t0) .. dst + (b1 − t0)`."""
    H = float(model["gel"]["height_cm"])
    W = float(model["gel"]["width_cm"])
    ct, cb = _centres(model, 0.0, H), _centres(model, H, H)
    n = len(ct)
    ks = sorted(set(int(k) for k in keep))
    runs = []
    for k in ks:
        if runs and k == runs[-1][-1] + 1:
            runs[-1].append(k)
        else:
            runs.append([k])

    def edge(c, first, last, last_index):
        x0 = 0.0 if first == 1 else (c[first - 2] + c[first - 1]) / 2.0
        x1 = W if last == last_index else (c[last - 1] + c[last]) / 2.0
        return x0, x1

    out, cursor = [], 0.0
    for i, run in enumerate(runs):
        first, last = run[0], run[-1]
        t0, t1 = edge(ct, first, last, n)
        b0, b1 = edge(cb, first, last, n)
        if i:
            cursor += gap_cm
        out.append({"lanes": run, "dst": cursor, "t0": t0, "t1": t1, "b0": b0, "b1": b1,
                    "top_w": t1 - t0})
        cursor += t1 - t0
    return out, round(cursor, 6)


def _anchors(node):
    """Path node → its anchors in pt (works for a plain path, a curve, a clip path)."""
    return [tuple(a["p"]) for a in node["kind"]["path"]["subpaths"][0]["anchors"]]


def _seg_quad(node):
    """Clip node → its four anchors (TL, TR, BR, BL) in pt."""
    return _anchors(node)


def _clip_path(group):
    """The clipping path node inside a clip group."""
    return next(c for c in group["kind"]["children"] if c["kind"].get("clipping"))


def _clip_anchors(group):
    """Clip group → its clipping path anchors (TL, TR, BR, BL) in pt."""
    return _anchors(_clip_path(group))


def _segs(doc):
    """The gel-segment clip groups of a Figure document, in order."""
    return [n for n in vc_file.lay_children(vc_file.layer_by_name(doc, "gel"))
            if (n.get("name") or "").startswith("gel seg ")]


def _curve_points(node):
    return _anchors(node)


def _near(got, want, tol=TOL):
    return abs(got - want) <= tol


def _errors(model):
    """workbench.validate(...) returns the list of problems; join them for substring assertions."""
    errs = workbench.validate(model)
    assert isinstance(errs, list), "validate 应当返回错误列表，收到 %r" % (errs,)
    return "\n".join(errs)


def _expect(fn, *frags):
    """The call must fail, and the message must contain every fragment (Chinese text as shipped)."""
    try:
        fn()
    except (RuntimeError, ValueError, workbench.WorkbenchError) as exc:
        msg = str(exc)
        for frag in frags:
            assert frag in msg, "错误里没有 %r：%s" % (frag, msg)
        return msg
    raise AssertionError("应当报错（应当含 %r）" % (frags,))


def _expect_err(fn, *frags):
    return _expect(fn, *frags)


def _reduced(document):
    """Document → the geometry the acceptance compares (names, anchors, texts, clip/image of groups)."""
    out = {}
    for lay in document["layers"]:
        items = []
        for n in vc_file.lay_children(lay):
            k = n["kind"]
            if k["type"] == "group":
                items.append({"name": n.get("name"), "kind": "clip-group",
                              "clip": [_seg_quad(c) for c in k["children"]
                                       if c["kind"].get("clipping")],
                              "image": [[round(v, 6) for v in c["kind"]["xf"]] for c in k["children"]
                                        if c["kind"].get("type") == "image"]})
            elif k["type"] == "text":
                t = vc_file.text_info(n)
                items.append({"name": n.get("name"), "kind": "text", "text": t["text"],
                              "x": round(t["x"], 6), "y": round(t["y"], 6)})
            elif k["type"] == "image":
                items.append({"name": n.get("name"), "kind": "image",
                              "xf": [round(v, 6) for v in k["xf"]], "w": k["width"], "h": k["height"]})
            else:
                items.append({"name": n.get("name"), "kind": "path", "anchors": _seg_quad(n)})
        out[lay["name"]] = items
    return out


def _compare_reduced(got, want, what):
    assert sorted(got) == sorted(want), "%s：图层 %r != %r" % (what, sorted(got), sorted(want))
    for lay in sorted(want):
        a, b = got[lay], want[lay]
        assert len(a) == len(b), "%s / %s 图层：%d 个对象 != %d" % (what, lay, len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            kind = y["kind"]
            if kind == "text":
                # ep-meta is JSON: compare its meaning, not its key order or separators
                want_t, got_t = y["text"], x.get("text")
                try:
                    same_text = json.loads(got_t) == json.loads(want_t)
                except (TypeError, ValueError):
                    same_text = got_t == want_t
                assert same_text and _near(x["x"], y["x"]) and _near(x["y"], y["y"]), \
                    "%s / %s 第 %d 个文字：%r != %r" % (what, lay, i, x, y)
            elif kind == "clip-group":
                assert len(x["clip"]) == len(y["clip"]) and len(x["image"]) == len(y["image"]), \
                       "%s / %s 第 %d 个剪切组形状不对：%r" % (what, lay, i, x)
                for ca, cb in zip(x["clip"], y["clip"]):
                    assert all(_near(p[0], q[0]) and _near(p[1], q[1]) for p, q in zip(ca, cb)), \
                       "%s / %s 第 %d 个剪切路径：%r != %r" % (what, lay, i, ca, cb)
                for ia, ib in zip(x["image"], y["image"]):
                    assert all(_near(p, q) for p, q in zip(ia, ib)), \
                       "%s / %s 第 %d 个图片 xf：%r != %r" % (what, lay, i, ia, ib)
            elif kind == "image":
                assert all(_near(p, q) for p, q in zip(x["xf"], y["xf"])) and \
                       (x["w"], x["h"]) == (y["w"], y["h"]), "%s / %s 第 %d 个图片：%r != %r" % (what, lay, i, x, y)
            else:
                assert x.get("name") == y.get("name") and \
                    all(_near(p[0], q[0]) and _near(p[1], q[1]) for p, q in zip(x["anchors"], y["anchors"])), \
                    "%s / %s 第 %d 条路径：%r != %r" % (what, lay, i, x, y)


def _fig_for(model, report, png, layout=None):
    doc = figure.build_document(model, report, png, "t27", layout=layout)
    top, _blobs = vc_file.loads(vc_file.dumps(doc))       # the re-loaded page, as plan_sync sees it
    return top["document"], doc


def _wb_for(model, png):
    doc = vc_file.new_doc("t27-wb")
    key, w, h = vc_file.add_image(doc, png)
    layers = workbench.make_layers(doc)
    workbench.add_workbench(doc, layers, model, key, w, h, x0=14.0)
    for j, text in enumerate(["M", "1", "2", "3"], 1):
        vc_file.lay_children(layers["labels"]).append(
            vc_file.text(doc, "label lane %d" % j, 20.0 + 20.0 * j, 4.0, text, size=6))
    return json.loads(vc_file.dumps(doc))["document"], doc


def _slant_doc(sep_index, x_top, x_bottom, window=None):
    """Golden Workbench document with one separator re-drawn through two explicit points.

    sep_index is 1-based over the lanes-layer separators, left to right. window = (top_cm, bottom_cm)
    moves the two window lines too, so the separator can be drawn across a window narrower than the
    gel (the read-back then has to extend it along its own line to y = 0 and y = H)."""
    g = _golden()
    doc = copy.deepcopy(g["wb_doc"])
    gx, gy = 14.0 + workbench.MARGIN_CM * PT, workbench.MARGIN_CM * PT
    win = window or (g["model"]["window"]["top_cm"], g["model"]["window"]["bottom_cm"])
    if window:
        wl = vc_file.lay_children(vc_file.layer_by_name(doc, "window"))
        for node, cm in zip(sorted(wl, key=lambda n: vc_file.line_ends(n)[0][1]), win):
            (x0, _y0), (x1, _y1) = vc_file.line_ends(node)
            node["kind"]["path"]["subpaths"][0]["anchors"] = [{"p": [x0, gy + cm * PT]},
                                                             {"p": [x1, gy + cm * PT]}]
    nodes = sorted(vc_file.lay_children(vc_file.layer_by_name(doc, "lanes")),
                   key=lambda n: vc_file.line_ends(n)[0][0])
    node = nodes[sep_index - 1]
    top, bottom = win
    node["kind"]["path"]["subpaths"][0]["anchors"] = [
        {"p": [gx + x_top * PT, gy + top * PT]}, {"p": [gx + x_bottom * PT, gy + bottom * PT]}]
    return doc, (gx, gy), win


# ---------------------------------------------------------------- 1. read-back, rectangle

def check_rect_readback_is_unchanged():
    g = _golden()
    model, notes = workbench.from_document(g["wb_doc"])
    assert notes == [], notes
    assert model["lanes"] == g["model"]["lanes"], (model["lanes"], g["model"]["lanes"])
    for ln in model["lanes"]:
        assert set(ln) == {"left_cm", "right_cm", "trimmed"}, \
            "矩形泳道不该多出键：%r" % (ln,)
    assert workbench.centres(model) == g["model"]["meta"]["prev_centres_cm"], \
        (workbench.centres(model), g["model"]["meta"]["prev_centres_cm"])
    assert workbench.validate(model) == [], workbench.validate(model)
    # the lane bounds of a rectangle do not depend on the row
    for ln in model["lanes"]:
        for y in (0.0, 1.5, model["gel"]["height_cm"]):
            got = workbench.lane_bounds(ln, y, model["gel"]["height_cm"])
            assert _near(got[0], ln["left_cm"]) and _near(got[1], ln["right_cm"]), (y, got, ln)


# ---------------------------------------------------------------- 2. read-back, slant

def check_slant_readback_keeps_both_ends():
    g = _golden()
    doc, (gx, gy), _win = _slant_doc(3, 2.30, 2.60)
    model, notes = workbench.from_document(doc)
    assert notes == [], notes
    H = model["gel"]["height_cm"]
    l2, l3 = model["lanes"][1], model["lanes"][2]
    for ln, lt, lb, rt, rb in ((l2, 1.25, 1.25, 2.30, 2.60), (l3, 2.30, 2.60, 3.35, 3.35)):
        for key, want in (("left_top_cm", lt), ("left_bottom_cm", lb),
                          ("right_top_cm", rt), ("right_bottom_cm", rb)):
            assert key in ln, "斜线夹出的泳道缺 %s：%r" % (key, ln)
            assert _near(ln[key], want), "%s = %r，应为 %r（%r）" % (key, ln[key], want, ln)
        assert _near(ln["left_cm"], lt) and _near(ln["right_cm"], rt), \
            "left_cm/right_cm 应是胶顶（y=0）值：%r" % (ln,)
    # both ends differ: the lane really is a trapezoid, not a constant width
    assert not _near(l2["right_top_cm"], l2["right_bottom_cm"]), l2
    # untouched neighbours keep the old three keys
    for i in (0, 3):
        assert set(model["lanes"][i]) == {"left_cm", "right_cm", "trimmed"}, model["lanes"][i]
    # per-row bounds are the linear interpolation of the two ends
    for y in (0.0, 0.7, 1.25, H):
        lo, hi = workbench.lane_bounds(l3, y, H)
        assert _near(lo, 2.30 + (2.60 - 2.30) * (y / H)), (y, lo)
        assert _near(hi, 3.35), (y, hi)
        want = (2.30 + (2.60 - 2.30) * (y / H) + 3.35) / 2.0
        assert _near(workbench.lane_centre_at(l3, y, H), want), (y, workbench.lane_centre_at(l3, y, H), want)


def check_window_only_separator_is_extended_along_its_line():
    """A separator drawn only across a window inside the gel is extended along its own line."""
    doc, (gx, gy), (top, bottom) = _slant_doc(3, 2.30, 2.60, window=(0.40, 2.60))
    model, notes = workbench.from_document(doc)
    assert notes == [], notes
    H = model["gel"]["height_cm"]
    t = (top, 2.30), (bottom, 2.60)                    # the drawn segment's own two points
    slope = (2.60 - 2.30) / (2.60 - 0.40)
    want_top = 2.30 - slope * 0.40                     # extended to y = 0
    want_bottom = 2.60 + slope * (H - 2.60)            # extended to y = H
    l3 = model["lanes"][2]
    assert _near(l3["left_top_cm"], want_top, 2e-3), (l3, want_top)
    assert _near(l3["left_bottom_cm"], want_bottom, 2e-3), (l3, want_bottom)
    l2 = model["lanes"][1]
    assert _near(l2["right_top_cm"], want_top, 2e-3) and _near(l2["right_bottom_cm"], want_bottom, 2e-3), l2


# ---------------------------------------------------------------- 3. per-row profile

def _discriminating_case():
    """Gel 5.0 × 3.0 cm at 100 px/cm: lane 2's right edge slants 2.30 (top) → 2.58 (bottom).

    A dark patch sits at x ∈ [2.40, 2.43] cm, y ∈ [0.30, 0.80] cm. Over those rows the true right
    edge is 2.328..2.375, so the patch is outside lane 2 — but the old midpoint rectangle (2.44)
    still covers it and pulls neighbour ink in. That is the leak the ticket is about."""
    W, H, PPC = 5.0, 3.0, 100.0
    w, h = int(W * PPC), int(H * PPC)
    gray = bytearray([205] * (w * h))
    for y in range(int(math.ceil(0.30 * PPC)), int(math.floor(0.80 * PPC)) + 1):
        for x in range(int(math.ceil(2.40 * PPC)), int(math.floor(2.43 * PPC)) + 1):
            gray[y * w + x] = 25
    lane = {"left_cm": 1.25, "right_cm": 2.30, "trimmed": False,
            "left_top_cm": 1.25, "left_bottom_cm": 1.25,
            "right_top_cm": 2.30, "right_bottom_cm": 2.58}
    return gray, w, h, PPC, lane, W, H


def check_quad_profile_is_per_row():
    gray, w, h, ppc, lane, W, H = _discriminating_case()
    ys, gs = profiles.sample_lane(gray, w, h, ppc, lane, 0.0, H)
    # same y grid as before (pixel centres)
    assert len(ys) == h and _near(ys[0], 0.5 / ppc, 1e-9) and _near(ys[-1], (h - 0.5) / ppc, 1e-9), \
        (len(ys), ys[:1], ys[-1:])
    want = []
    for i, y in enumerate(ys):
        lo_cm, hi_cm = _bounds(lane, y, H)
        xa, xb = _row_span(lo_cm, hi_cm, w, ppc)
        row = i * w
        want.append(sum(gray[row + x] for x in range(xa, xb + 1)) / float(xb - xa + 1))
    bad = [(ys[i], gs[i], want[i]) for i in range(len(gs)) if abs(gs[i] - want[i]) > 1e-9]
    assert not bad, "每行积分区间不是 L(y)..R(y)：%r" % (bad[:5],)
    # the fixture really is discriminating: the old midpoint rectangle would take the neighbour's ink
    old_hi = (lane["right_top_cm"] + lane["right_bottom_cm"]) / 2.0      # 2.44, what the bug did
    xa, xb = _row_span(lane["left_cm"], old_hi, w, ppc)
    worst = None
    for i, y in enumerate(ys):
        if not (0.30 <= y <= 0.80):
            continue
        row = i * w
        old = sum(gray[row + x] for x in range(xa, xb + 1)) / float(xb - xa + 1)
        drop = gs[i] - old          # how much brighter the quad window is than the old rectangle
        if worst is None or drop > worst[0]:
            worst = (drop, y, old, gs[i])
    assert worst is not None and worst[0] >= 2.0, \
        "夹具不够区分：旧矩形最多只暗 %.2f 灰度级（y=%.2f）" % (worst or (0.0, 0.0, 0, 0))[:2]
    # inside the patch rows the quad window sees pure background: no neighbour ink at all
    inside = [i for i, y in enumerate(ys) if 0.31 <= y <= 0.79]
    assert inside and all(abs(gs[i] - 205.0) < 1e-9 for i in inside), \
        [(ys[i], gs[i]) for i in inside if abs(gs[i] - 205.0) >= 1e-9][:3]
    # a constant-width (rectangle) lane still returns a constant width profile: the row spans are
    # identical at every y, i.e. the sampler did not turn every lane into a trapezoid
    rect_lane = {"left_cm": 1.25, "right_cm": 2.44, "trimmed": False}
    ys_r, gs_r = profiles.sample_lane(gray, w, h, ppc, rect_lane, 0.0, H)
    xa, xb = _row_span(1.25, 2.44, w, ppc)
    want_r = [sum(gray[i * w + x] for x in range(xa, xb + 1)) / float(xb - xa + 1) for i in range(h)]
    assert all(_near(a, b, 1e-9) for a, b in zip(gs_r, want_r)), "矩形泳道的剖面变了"


def check_lane_profile_scalars_still_work():
    """The old scalar entry point keeps the old behaviour (rectangles go through the same path)."""
    gray, w, h, ppc, lane, W, H = _discriminating_case()
    ys, gs = profiles.lane_profile(gray, w, h, ppc, 1.25, 2.44, 0.0, H)
    assert len(ys) == h
    xa, xb = _row_span(1.25, 2.44, w, ppc)
    want = [sum(gray[i * w + x] for x in range(xa, xb + 1)) / float(xb - xa + 1) for i in range(h)]
    assert all(_near(a, b, 1e-9) for a, b in zip(gs, want)), "标量入口的剖面变了"
    # a rectangle passed as a lane dict samples exactly like the scalars
    ys2, gs2 = profiles.sample_lane(gray, w, h, ppc, {"left_cm": 1.25, "right_cm": 2.44, "trimmed": False},
                                    0.0, H)
    assert (ys2, gs2) == (ys, gs), "矩形泳道字典与标量入口的剖面不一致"


# ---------------------------------------------------------------- 4. crossing separators

def _crossing_doc():
    """Separators 3 and 4 cross inside the gel (2.30→3.50 and 3.35→2.10)."""
    g = _golden()
    doc = copy.deepcopy(g["wb_doc"])
    gx, gy = 14.0 + workbench.MARGIN_CM * PT, workbench.MARGIN_CM * PT
    nodes = sorted(vc_file.lay_children(vc_file.layer_by_name(doc, "lanes")),
                   key=lambda n: vc_file.line_ends(n)[0][0])
    for node, (x_top, x_bottom) in ((nodes[2], (2.30, 3.50)), (nodes[3], (3.35, 2.10))):
        node["kind"]["path"]["subpaths"][0]["anchors"] = [
            {"p": [gx + x_top * PT, gy]}, {"p": [gx + x_bottom * PT, gy + 3.0 * PT]}]
    return doc


def check_crossing_separators_refuse_and_change_nothing():
    doc = _crossing_doc()
    before = json.dumps(doc, sort_keys=True)
    msg = _expect(lambda: workbench.from_document(doc), "3", "4")
    assert ("相交" in msg) or ("交叉" in msg) or ("交错" in msg), msg
    assert json.dumps(doc, sort_keys=True) == before, "报错时改了文档"
    # validate() on the equivalent model says the same thing (it returns the list, never raises)
    model = _golden()["model"]
    crossed = copy.deepcopy(model)
    crossed["lanes"][2].update(left_top_cm=2.30, left_bottom_cm=3.50,
                               right_top_cm=3.35, right_bottom_cm=2.10)
    crossed["lanes"][3].update(left_top_cm=3.35, left_bottom_cm=2.10)
    msg = _errors(crossed)
    assert "3" in msg and "4" in msg, msg
    assert ("相交" in msg) or ("交叉" in msg) or ("交错" in msg), msg


def check_non_crossing_slant_is_accepted():
    """Two slanted separators that do not cross must be read normally (no false positive)."""
    g = _golden()
    doc = copy.deepcopy(g["wb_doc"])
    gx, gy = 14.0 + workbench.MARGIN_CM * PT, workbench.MARGIN_CM * PT
    nodes = sorted(vc_file.lay_children(vc_file.layer_by_name(doc, "lanes")),
                   key=lambda n: vc_file.line_ends(n)[0][0])
    for node, (x_top, x_bottom) in ((nodes[2], (2.20, 2.55)), (nodes[3], (3.35, 3.60))):
        node["kind"]["path"]["subpaths"][0]["anchors"] = [
            {"p": [gx + x_top * PT, gy]}, {"p": [gx + x_bottom * PT, gy + 3.0 * PT]}]
    model, notes = workbench.from_document(doc)
    assert notes == [], notes
    assert not workbench.validate(model), workbench.validate(model)
    l3 = model["lanes"][2]
    assert _near(l3["left_top_cm"], 2.20) and _near(l3["left_bottom_cm"], 2.55), l3
    assert _near(l3["right_top_cm"], 3.35) and _near(l3["right_bottom_cm"], 3.60), l3


# ---------------------------------------------------------------- 5. validate rules

def check_validate_quad_rules():
    m = _model_quad()
    assert _errors(m) == "", _errors(m)
    narrow_top = copy.deepcopy(m)
    narrow_top["lanes"][2].update(left_top_cm=3.34, left_cm=3.34)      # 3.34 → 3.35 at the top = 0.01 cm
    msg = _errors(narrow_top)
    assert "泳道 3" in msg and "几乎重合" in msg, msg
    assert "上边" in msg or "胶顶" in msg, msg
    narrow_bottom = copy.deepcopy(m)
    narrow_bottom["lanes"][1].update(right_bottom_cm=1.26)             # 1.25 → 1.26 at the bottom = 0.01 cm
    msg = _errors(narrow_bottom)
    assert "泳道 2" in msg and "几乎重合" in msg, msg
    assert "下边" in msg or "胶底" in msg, msg
    crossed = copy.deepcopy(m)
    crossed["lanes"][2].update(left_top_cm=2.30, left_bottom_cm=3.50,
                               right_top_cm=3.35, right_bottom_cm=2.10)
    msg = _errors(crossed)
    assert "3" in msg and "4" in msg, msg
    assert ("相交" in msg) or ("交叉" in msg) or ("交错" in msg), msg
    # the old rectangle message stays exactly as it was (no 上边/下边 words)
    rect = copy.deepcopy(_model_rect())
    rect["lanes"][1]["right_cm"] = 1.26
    msg = _errors(rect)
    assert "泳道 2 的两条边线几乎重合" in msg and "删掉多出来的那条线" in msg, msg
    assert "上边" not in msg and "下边" not in msg, msg


# ---------------------------------------------------------------- 6/7. Figure

def check_quad_clip_and_seams():
    """Quad clip: top edge from the top-row seams, bottom edge from the bottom-row seams."""
    g = _golden()
    png = base64.b64decode(g["png1_b64"])
    model = _model_quad()
    doc, _doc = _fig_for(model, g["report"], png)
    segs = _segs(doc)
    assert [n["name"] for n in segs] == ["gel seg 1", "gel seg 2"], [n["name"] for n in segs]
    keep = [1, 2, 4]                                     # lane 3 is trimmed in the golden model
    want, gel_w = _expected_segs(model, keep, figure.SPLICE_GAP_CM)
    assert len(want) == len(segs)
    # the Figure gel face origin: seg 1's left edge is the gel's left edge
    gx, gy = _clip_anchors(segs[0])[0]
    Hpt = model["gel"]["height_cm"] * PT
    for node, seg in zip(segs, want):
        tl, tr, br, bl = _clip_anchors(node)
        want_tl = (gx + seg["dst"] * PT, gy)
        want_tr = (gx + (seg["dst"] + (seg["t1"] - seg["t0"])) * PT, gy)
        want_br = (gx + (seg["dst"] + (seg["b1"] - seg["t0"])) * PT, gy + Hpt)
        want_bl = (gx + (seg["dst"] + (seg["b0"] - seg["t0"])) * PT, gy + Hpt)
        for got, exp, what in ((tl, want_tl, "TL"), (tr, want_tr, "TR"), (br, want_br, "BR"),
                               (bl, want_bl, "BL")):
            assert _near(got[0], exp[0]) and _near(got[1], exp[1]), \
                "%s 的 %s = %r，应为 %r" % (node["name"], what, got, exp)
        # the clip is a quadrilateral exactly when the expected edges differ per row
        top_w, bot_w = abs(tr[0] - tl[0]), abs(br[0] - bl[0])
        want_top, want_bot = (seg["t1"] - seg["t0"]) * PT, (seg["b1"] - seg["b0"]) * PT
        assert _near(top_w, want_top) and _near(bot_w, want_bot), \
            "%s 的宽 %.3f/%.3f 应为 %.3f/%.3f" % (node["name"], top_w, bot_w, want_top, want_bot)
        live = _clip_path(node)["kind"].get("live")
        if abs(want_top - want_bot) > 1e-6:
            assert live is None or live.get("shape") != "rectangle", \
                "%s 的剪切路径仍是 live 矩形：%r" % (node["name"], live)
        else:
            assert live is not None and live.get("shape") == "rectangle", \
                "%s 两边等宽，剪切路径应仍是 live 矩形（导出才逐像素一致）：%r" % (node["name"], live)
    # the first strip's right edge: 2.30 at the top (midpoint of the top centres of lanes 2 and 3),
    # 2.45 at the bottom (midpoint of their bottom centres) — the splice rule, now per row
    ct, cb = _centres(model, 0.0, model["gel"]["height_cm"]), \
        _centres(model, model["gel"]["height_cm"], model["gel"]["height_cm"])
    assert _near((ct[1] + ct[2]) / 2.0, 2.30) and _near((cb[1] + cb[2]) / 2.0, 2.45), (ct, cb)
    tl, tr, br, bl = _clip_anchors(segs[0])
    assert _near((tr[0] - gx) / PT, 2.30) and _near((br[0] - gx) / PT, 2.45), (tr, br)


def _model_double_slant():
    """Separators 2 (1.25 → 1.05) and 3 (2.30 → 2.60) both slanted: lane 2's top centre is not the
    average of its two ends, so a label anchored anywhere but the gel-top row lands elsewhere."""
    m = _model_quad()
    m["lanes"][0] = dict(m["lanes"][0], right_top_cm=1.25, right_bottom_cm=1.05)
    m["lanes"][1].update(left_top_cm=1.25, left_bottom_cm=1.05, left_cm=1.25)
    return m


def check_lane_label_sits_at_top_centre():
    g = _golden()
    png = base64.b64decode(g["png1_b64"])
    model = _model_double_slant()
    doc, _doc = _fig_for(model, g["report"], png)
    gx = _clip_anchors(_segs(doc)[0])[0][0]
    labels = {n["name"]: vc_file.text_info(n)
              for n in vc_file.lay_children(vc_file.layer_by_name(doc, "labels"))
              if n["kind"].get("type") == "text"}
    want, _w = _expected_segs(model, [1, 2, 4], figure.SPLICE_GAP_CM)
    ct = _centres(model, 0.0, model["gel"]["height_cm"])
    average = [(ln.get("left_cm", ln.get("left_top_cm")) + ln.get("right_cm", ln.get("right_top_cm"))) / 2.0
               for ln in model["lanes"]]
    for lane in (1, 2, 4):
        seg = [x for x in want if lane in x["lanes"]][0]
        want_x = gx + (seg["dst"] + (ct[lane - 1] - seg["t0"])) * PT
        got = labels["label lane %d" % lane]["x"]
        assert _near(got, want_x, 0.02), "泳道 %d 标签 x = %r，应为胶顶中心 %r（中段中心 %r）" % (
            lane, got, want_x, gx + (seg["dst"] + (average[lane - 1] - seg["t0"])) * PT)
    # the trimmed lane keeps no label
    assert "label lane 3" not in labels, sorted(labels)
    # the analysis tags (lane numbers above the gel, on the Workbench board) use the same
    # gel-top centres; their origin is the Workbench gel image, not the Figure gel
    wb_gel = [n for n in vc_file.lay_children(vc_file.layer_by_name(doc, "gel"))
              if n["kind"].get("type") == "image" and n.get("name") == "gel"][0]
    wx = wb_gel["kind"]["xf"][4]
    tags = {n["name"]: vc_file.text_info(n) for n in vc_file.lay_children(
        vc_file.layer_by_name(doc, "analysis")) if n["kind"].get("type") == "text"}
    for lane in (1, 2, 3, 4):
        assert _near(tags["tag %d" % lane]["x"], wx + ct[lane - 1] * PT, 0.02), \
            "tag %d 不在胶顶中心：%r 应为 %r" % (lane, tags["tag %d" % lane]["x"],
                                             wx + ct[lane - 1] * PT)


def check_strips_keep_the_old_seam_rule():
    """lane_select.strips: today's dicts for rectangles, both rows for a slanted segment."""
    g = _golden()
    model = _model_rect()
    W = model["gel"]["width_cm"]
    ct = workbench.centres(model)
    old = lane_select.strips([1, 2, 4], ct, W)
    assert old == [{"lanes": [1, 2], "src_x0_cm": 0.0, "src_x1_cm": 2.30},
                   {"lanes": [4], "src_x0_cm": 3.40, "src_x1_cm": 5.0}], old
    for seg in old:
        # a rectangle is the same at both rows: if the bottom keys are present they must agree
        for k in ("src_x0_bottom_cm", "src_x1_bottom_cm"):
            if k in seg:
                top = seg[k.replace("_bottom_cm", "_cm")]
                assert _near(seg[k], top), "矩形泳道的 %s = %r 与上行的 %r 不一致" % (k, seg[k], top)
    # bottom-row centres of a rectangle are the same numbers → passing them changes nothing
    cb = workbench.centres(model, y_cm=model["gel"]["height_cm"])
    assert cb == ct
    try:
        same = lane_select.strips([1, 2, 4], ct, W, cb)
    except TypeError:
        raise AssertionError("lane_select.strips 没有接受 centres_bottom_cm 参数（契约要求）")
    for a, b in zip(same, old):
        for k in ("lanes", "src_x0_cm", "src_x1_cm"):
            assert a[k] == b[k], "矩形泳道传入上下两行中心后 %s 变了：%r != %r" % (k, a[k], b[k])
        assert _near(a.get("src_x0_bottom_cm", a["src_x0_cm"]), b["src_x0_cm"])
        assert _near(a.get("src_x1_bottom_cm", a["src_x1_cm"]), b["src_x1_cm"])
    # a slanted model: the segment carries its own bottom edges, seams still midpoints of centres
    quad = _model_quad()
    H = quad["gel"]["height_cm"]
    ct2 = workbench.centres(quad, y_cm=0.0)
    cb2 = workbench.centres(quad, y_cm=H)
    segs = lane_select.strips([1, 2, 4], ct2, W, cb2)
    assert [s["lanes"] for s in segs] == [[1, 2], [4]], segs
    want0 = (0.0, 2.30, 0.0, 2.45)
    want1 = (3.40, 5.0, 3.475, 5.0)
    for seg, want in zip(segs, (want0, want1)):
        got = (seg["src_x0_cm"], seg["src_x1_cm"],
               seg["src_x0_bottom_cm"], seg["src_x1_bottom_cm"])
        assert all(_near(a, b) for a, b in zip(got, want)), \
            "段 %r 的两行边缘 %r 应为 %r" % (seg["lanes"], got, want)
        # the gel is full width at both rows, so a first/last segment must span 0..W there too
        if seg["lanes"][0] == 1:
            assert _near(seg["src_x0_cm"], 0.0) and _near(seg["src_x0_bottom_cm"], 0.0), seg
        if seg["lanes"][-1] == len(quad["lanes"]):
            assert _near(seg["src_x1_cm"], W) and _near(seg["src_x1_bottom_cm"], W), seg


def check_rect_render_is_unchanged():
    """Trapezoid degree 0 → the geometry captured from the old implementation, to the last pt."""
    g = _golden()
    png = base64.b64decode(g["png1_b64"])
    model = _model_rect()
    doc, _doc = _fig_for(model, g["report"], png)
    _compare_reduced(_reduced(doc), g["fig_layers"], "Figure")
    assert [a["name"] for a in doc["artboards"]] == [a["name"] for a in g["fig_doc"]["artboards"]]
    for got, want in zip(doc["artboards"], g["fig_doc"]["artboards"]):
        for k in ("x0", "y0", "x1", "y1"):
            assert _near(got["rect"][k], want["rect"][k], 1e-6), (got["name"], k, got["rect"], want["rect"])
    # a rectangle clip must stay a live rectangle (that is what makes the export pixel-identical)
    for node in [n for n in vc_file.lay_children(vc_file.layer_by_name(doc, "gel"))
                 if (n.get("name") or "").startswith("gel seg ")]:
        clip = next(c for c in node["kind"]["children"] if c["kind"].get("clipping"))
        assert clip["kind"]["live"].get("shape") == "rectangle", \
            "矩形情况下的剪切路径成了普通路径：%r" % (clip["kind"].get("live"),)
    # the Workbench writer is unchanged too
    wbdoc, _d = _wb_for(model, png)
    _compare_reduced(_reduced(wbdoc), g["wb_layers"], "Workbench")
    # and re-planning the unchanged document does nothing
    ops, _meta = figure.plan_sync(doc, model, g["report"])
    assert ops == [], ops[:3]


def check_sync_distinguishes_equal_bbox_trapezoids():
    g = _golden()
    png = base64.b64decode(g["png1_b64"])
    m_a, m_b = _model_quad(), _model_quad_swapped()
    doc_a, _d = _fig_for(m_a, g["report"], png)
    report_b = profiles.analyze(m_b, png, ["M", "1", "2", "3"])
    ops, _meta = figure.plan_sync(doc_a, m_b, report_b)
    # expected seg 1 vertices for the swapped slant: top-right 2.45, bottom-right 2.30
    want, _w = _expected_segs(m_b, [1, 2, 4], figure.SPLICE_GAP_CM)
    seg1 = [n for n in _segs(doc_a) if n.get("name") == "gel seg 1"][0]
    gx, gy = _clip_anchors(seg1)[0]
    Hpt = m_b["gel"]["height_cm"] * PT
    s = want[0]
    exp = [(gx + s["dst"] * PT, gy),
           (gx + (s["dst"] + (s["t1"] - s["t0"])) * PT, gy),
           (gx + (s["dst"] + (s["b1"] - s["t0"])) * PT, gy + Hpt),
           (gx + (s["dst"] + (s["b0"] - s["t0"])) * PT, gy + Hpt)]
    reshapes = [o for o in ops if o["op"] == "reshape"]
    assert reshapes, "两个梯形 bbox 相同、顶点不同，sync 却没reshape：%r" % (ops[:5],)
    hit = [o for o in reshapes if len(o["points"]) == 4 and
           all(_near(p[0], q[0]) and _near(p[1], q[1]) for p, q in zip(o["points"], exp))]
    assert hit, "没有把胶段剪成新梯形：%r / 期望 %r" % ([o["points"] for o in reshapes[:2]], exp)
    # applying it and planning again converges
    top = {"document": copy.deepcopy(doc_a)}
    figure.apply_ops_json(top, ops)
    doc_a2 = top["document"]
    ops2, _m2 = figure.plan_sync(doc_a2, m_b, report_b)
    assert ops2 == [], ops2[:3]
    # the first plan on the document that already matches is empty
    doc_b, _d = _fig_for(m_b, report_b, png)
    assert figure.plan_sync(doc_b, m_b, report_b)[0] == []


# ---------------------------------------------------------------- 9. IO

def check_io_backwards_compatible():
    g = _golden()
    model, _n = workbench.from_document(g["wb_doc"])
    ex = json.loads(json.dumps(workbench_io.build_export(g["wb_doc"], model)))
    assert ex == g["export"], "矩形导出的 JSON 变了：\n%r\n%r" % (ex, g["export"])
    for ln in ex["lanes"]:
        assert set(ln) == {"left_cm", "right_cm", "trimmed", "name"}, \
            "矩形导出的泳道多了键：%r" % (ln,)
    assert ex["version"] == 1, ex["version"]
    good = workbench_io.validate_export(ex, model)
    assert set(good["lanes"][0]) >= {"left_cm", "right_cm", "trimmed"}, good["lanes"][0]
    ops, _meta, _lines, _warns = workbench_io.plan_import(g["wb_doc"], model, good)
    assert ops == [], ops[:3]
    # an old v1 payload (no quad keys at all) is read as rectangles against a quad model too
    quad = _model_quad()
    old = json.loads(json.dumps(g["export"]))
    for ln in old["lanes"]:
        for k in ("left_top_cm", "left_bottom_cm", "right_top_cm", "right_bottom_cm"):
            ln.pop(k, None)
    norm = workbench_io.validate_export(old, quad)
    for ln in norm["lanes"]:
        assert "left_top_cm" not in ln or ln["left_top_cm"] in (None, ln["left_cm"]), ln


def check_io_quad_round_trip():
    g = _golden()
    png = base64.b64decode(g["png1_b64"])
    quad = _model_quad()
    wbdoc, _d = _wb_for(quad, png)
    ex = json.loads(json.dumps(workbench_io.build_export(wbdoc, quad)))
    assert ex["version"] == 1, ex["version"]
    l2, l3 = ex["lanes"][1], ex["lanes"][2]
    for ln in (l2, l3):
        for k in ("left_top_cm", "left_bottom_cm", "right_top_cm", "right_bottom_cm"):
            assert k in ln, "导出的梯形泳道缺 %s：%r" % (k, ln)
    assert _near(l2["right_top_cm"], 2.30) and _near(l2["right_bottom_cm"], 2.60), l2
    assert _near(l3["left_top_cm"], 2.30) and _near(l3["left_bottom_cm"], 2.60), l3
    norm = workbench_io.validate_export(ex, quad)
    ops, _meta, _lines, _warns = workbench_io.plan_import(wbdoc, quad, norm)
    assert ops == [], "梯形导出原样导入却要改文档：%r" % (ops[:4],)
    # the round trip through JSON keeps the numbers
    again = workbench_io.validate_export(json.loads(json.dumps(ex)), quad)
    assert again["lanes"] == norm["lanes"], (again["lanes"], norm["lanes"])


def check_io_rejects_bad_quads():
    g = _golden()
    quad = _model_quad()
    png = base64.b64decode(g["png1_b64"])
    wbdoc, _d = _wb_for(quad, png)
    good = json.loads(json.dumps(workbench_io.build_export(wbdoc, quad)))
    # a top edge narrower than the 0.02 cm minimum
    bad = copy.deepcopy(good)
    bad["lanes"][2]["left_top_cm"] = 3.34
    _expect(lambda: workbench_io.validate_export(bad, quad), "几乎重合")
    # a top edge that does not meet the previous lane
    bad = copy.deepcopy(good)
    bad["lanes"][2]["left_top_cm"] = 2.45
    _expect(lambda: workbench_io.validate_export(bad, quad), "不连续")
    # a payload that gives only some of the four quad keys must be refused cleanly
    bad = copy.deepcopy(good)
    bad["lanes"][1].pop("right_bottom_cm")
    msg = _expect(lambda: workbench_io.validate_export(bad, quad), "left_top_cm")
    assert ("梯形" in msg) or ("四个" in msg) or ("要么" in msg), msg
    assert "right_bottom_cm" in msg, msg
    # separators 3 and 4 cross inside the gel (continuity kept, so it is purely a crossing)
    bad = copy.deepcopy(good)
    bad["lanes"][1]["right_bottom_cm"] = 3.50                        # sep 3: 2.30 → 3.50
    bad["lanes"][2]["left_bottom_cm"] = 3.50
    bad["lanes"][2]["right_bottom_cm"] = 2.10                        # sep 4: 3.35 → 2.10
    bad["lanes"][3] = dict(bad["lanes"][3], left_top_cm=3.35, left_bottom_cm=2.10,
                           right_top_cm=4.60, right_bottom_cm=4.60)
    msg = _expect(lambda: workbench_io.validate_export(bad, quad), "3", "4")
    assert ("相交" in msg) or ("交叉" in msg) or ("交错" in msg), msg
    # plan_import refuses the same payload before producing any op
    try:
        ops = workbench_io.plan_import(wbdoc, quad, bad)[0]
    except (RuntimeError, ValueError, workbench.WorkbenchError) as exc:
        msg = str(exc)
        assert ("相交" in msg) or ("交叉" in msg) or ("交错" in msg), msg
        assert "3" in msg and "4" in msg, msg
    else:
        raise AssertionError("交叉的导入没报错，还给了 %d 个操作" % len(ops))


# ---------------------------------------------------------------- 10. report

def check_report_carries_quads_and_notes():
    g = _golden()
    png = base64.b64decode(g["png1_b64"])
    quad = _model_quad()
    rep = profiles.analyze(quad, png, ["M", "1", "2", "3"])
    l2, l3 = rep["lanes"][1], rep["lanes"][2]
    for ln in (l2, l3):
        for k in ("left_top_cm", "left_bottom_cm", "right_top_cm", "right_bottom_cm"):
            assert k in ln, "报告里的梯形泳道缺 %s：%r" % (k, ln)
    assert _near(l2["right_top_cm"], 2.30) and _near(l2["right_bottom_cm"], 2.60), l2
    assert _near(l2["right_cm"], 2.30), "报告的 right_cm 应是胶顶值：%r" % (l2,)
    assert _near(l3["left_cm"], 2.30), "报告的 left_cm 应是胶顶值：%r" % (l3,)
    notes = [n for n in rep["notes"] if "梯形" in n]
    assert len(notes) == 2, "每条梯形泳道应当各有一条梯形说明：%r" % (rep["notes"],)
    for i in (2, 3):
        hit = [n for n in notes if ("泳道 %d" % i) in n]
        assert hit, "没有提到泳道 %d 的梯形说明：%r" % (i, notes)
        nums = re.findall(r"\d+\.\d+", hit[0])
        assert any(abs(float(x) - 2.30) < 1e-3 for x in nums) and \
               any(abs(float(x) - 2.60) < 1e-3 for x in nums), "说明里没有两个端点的位置：%s" % hit[0]
    # rectangles keep the old report, byte for byte
    rep_rect = profiles.analyze(_model_rect(), png, ["M", "1", "2", "3"])
    assert json.dumps(rep_rect, sort_keys=True) == json.dumps(g["report"], sort_keys=True), \
        "矩形输入的 report 变了"
    for ln in rep_rect["lanes"]:
        assert not any(k.endswith("_top_cm") or k.endswith("_bottom_cm") for k in ln), ln


def check_profiles_board_is_unaffected():
    """Captions and the y axis of the Profiles board do not depend on the trapezoid."""
    g = _golden()
    png = base64.b64decode(g["png1_b64"])
    rect_doc, _d = _fig_for(_model_rect(), g["report"], png)
    quad = _model_quad()
    rep = profiles.analyze(quad, png, ["M", "1", "2", "3"])
    quad_doc, _d = _fig_for(quad, rep, png)
    a = {n["name"]: vc_file.text_info(n)
         for n in vc_file.lay_children(vc_file.layer_by_name(rect_doc, "profiles"))
         if n["kind"].get("type") == "text"}
    b = {n["name"]: vc_file.text_info(n)
         for n in vc_file.lay_children(vc_file.layer_by_name(quad_doc, "profiles"))
         if n["kind"].get("type") == "text"}
    assert sorted(a) == sorted(b), "%r\n%r" % (sorted(a), sorted(b))
    for nm in sorted(a):
        if nm.startswith("caption") or nm.startswith("axis"):
            assert a[nm]["text"] == b[nm]["text"] and _near(a[nm]["x"], b[nm]["x"], 0.02) \
                and _near(a[nm]["y"], b[nm]["y"], 0.02), "%s 变了：%r / %r" % (nm, a[nm], b[nm])
    # the x mapping (window top..bottom → plot width) is the same: both curves start and end on it
    for nm in ("curve 1", "curve 2"):
        pa = _curve_points(next(n for n in vc_file.lay_children(
            vc_file.layer_by_name(rect_doc, "profiles")) if n["name"] == nm))
        pb = _curve_points(next(n for n in vc_file.lay_children(
            vc_file.layer_by_name(quad_doc, "profiles")) if n["name"] == nm))
        assert _near(pa[0][0], pb[0][0], 0.02) and _near(pa[-1][0], pb[-1][0], 0.02), (nm, pa[:1], pb[:1])
        # the row grid (y of the curve) belongs to the Profiles board, not to the lane geometry
        assert len(pa) >= 2 and len(pb) >= 2




# ---------------------------------------------------------------- 11. writer round trip

def check_quad_survives_render_and_readback():
    g = _golden()
    png = base64.b64decode(g["png1_b64"])
    quad = _model_double_slant()          # two slanted separators → a true trapezoid in the middle
    doc, _d = _fig_for(quad, g["report"], png)
    lanes = vc_file.lay_children(vc_file.layer_by_name(doc, "lanes"))
    # the writer must draw slanted separators: some lane line has two different x values
    slanted = [n for n in lanes if abs(_anchors(n)[0][0] - _anchors(n)[1][0]) > 1.0]
    assert slanted, "render 把斜的分隔线画直了：没有一条两端 x 不同的分隔线"
    gel_img = [n for n in vc_file.lay_children(vc_file.layer_by_name(doc, "gel"))
               if n["kind"].get("type") == "image" and n.get("name") == "gel"][0]
    gx, gy = gel_img["kind"]["xf"][4], gel_img["kind"]["xf"][5]   # Workbench gel face origin
    # separator 2 is 1.25 → 1.05, separator 3 is 2.30 → 2.60 (both 1-based over the separators)
    for top, bottom in ((1.25, 1.05), (2.30, 2.60)):
        hit = [n for n in lanes if _near((_anchors(n)[0][0] - gx) / PT, top, 1e-3)]
        assert hit, "分隔线（胶顶 %.2f cm）没画出来" % top
        (xa, ya), (xb, yb) = _anchors(hit[0])
        assert _near((xb - gx) / PT, bottom, 1e-3), \
            "分隔线应当从 %.2f 斜到 %.2f，画出来是 %.4f → %.4f" % (
                top, bottom, (xa - gx) / PT, (xb - gx) / PT)
        assert _near((yb - ya) / PT, quad["window"]["bottom_cm"] - quad["window"]["top_cm"], 1e-3), \
            "分隔线只画 window 那一段：%.4f cm" % ((yb - ya) / PT)
    # the Workbench document reads back as the same quad
    back, notes = workbench.from_document(doc)
    assert notes == [], notes
    assert len(back["lanes"]) == len(quad["lanes"])
    for i, (ln, want) in enumerate(zip(back["lanes"], quad["lanes"]), 1):
        for k in ("left_cm", "right_cm", "trimmed"):
            assert _near(ln[k], want[k], 1e-3), (i, k, ln[k], want[k])
        quad_keys = ("left_top_cm", "left_bottom_cm", "right_top_cm", "right_bottom_cm")
        for k in quad_keys:
            if k in want:
                assert _near(ln.get(k, -1), want[k], 1e-3), (i, k, ln, want)
        if not any(k in want for k in quad_keys):
            assert not any(kk in ln for kk in quad_keys), \
                "矩形泳道在 render/读回之后就变成了梯形：%r" % (ln,)
    assert not workbench.validate(back), workbench.validate(back)
    ops, _m = figure.plan_sync(doc, quad, g["report"])
    assert ops == [], "刚 render 的梯形文档 re-plan 不幂等：%r" % (ops[:4],)


CHECKS = (
    check_rect_readback_is_unchanged,
    check_slant_readback_keeps_both_ends,
    check_window_only_separator_is_extended_along_its_line,
    check_quad_profile_is_per_row,
    check_lane_profile_scalars_still_work,
    check_crossing_separators_refuse_and_change_nothing,
    check_non_crossing_slant_is_accepted,
    check_validate_quad_rules,
    check_quad_clip_and_seams,
    check_lane_label_sits_at_top_centre,
    check_strips_keep_the_old_seam_rule,
    check_rect_render_is_unchanged,
    check_sync_distinguishes_equal_bbox_trapezoids,
    check_io_backwards_compatible,
    check_io_quad_round_trip,
    check_io_rejects_bad_quads,
    check_report_carries_quads_and_notes,
    check_profiles_board_is_unaffected,
    check_quad_survives_render_and_readback,
)


def run():
    """Run the acceptance suite; returns the number of passed checks."""
    for check in CHECKS:
        check()
    print("selftest_lane_quad_acceptance %d/%d 通过" % (len(CHECKS), len(CHECKS)))
    return len(CHECKS)


if __name__ == "__main__":
    try:
        run()
    except (AssertionError, NotImplementedError) as exc:
        import sys
        sys.stderr.write("[!] %s\n" % exc)
        raise SystemExit(1)
