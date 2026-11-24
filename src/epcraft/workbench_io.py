#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Workbench 数据交换：`workbench export` / `workbench import`（公开 epcraft 命令组）。

导出（JSON，版本化）只含**用户在 Workbench 上改得动的那组几何**，单位 cm、原点 = 胶面左上角
（与 workbench.from_document / figure.plan_sync 同一口径）：

    {"schema": "ep-craft-workbench-lanes", "version": 1,
     "doc": "/abs/fig.vectorcraft",                  # 只作说明，import 不拿它做绑定
     "gel": {"width_cm", "height_cm"},
     "window": {"top_cm", "bottom_cm"},
     "lanes": [{"left_cm", "right_cm", "trimmed", "name"}],   # 从左到右；相邻泳道首尾相接
     "marker_lane": n,
     "bands": [{"mw", "y_cm"}],                      # marker 条带线，y 升序、MW 严格降序
     "low_confidence": bool,                         # 当前 ep-meta 的低置信标记（marker_confidence.ok = false）
     "sample_bands": [{"lane": n, "y_cm"}]}          # 有 sample bands 图层时才出现

导出读**活文档**（VectorCraft 里没存的改动也算），泳道名取 label lane n 的当前文字
（doc_edit.current_names；占位 "?" 导出为 null）。不导出胶面图/画板/Figure/Profiles、
ep-meta 的 photo/dpi/preset/crop、peak_auto 等内部状态 —— import 只动上面这些键，其余原样保留。

导入先校验（schema/version、所有数字有限、胶面尺寸与目标文档一致、泳道连续、window、带序、
sample 道号）—— 任何一处不对就**什么都不改**。通过后：
- 按导入数据重建 lanes / window / marker band 线（数量变化时增删线），trim 标记、sample band 刻度；
  全部包在一个撤销组 `epcraft workbench import` 里（sync_exec）；
- 把 ep-meta 的 lane_names / prev_centres_cm / bands 重定到导入后的泳道（现有 label lane n 直接改文字），
  这样随后的 sync 不会用旧映射覆盖导入的名字；
- 源导出 low_confidence=true 会把低置信标记带到目标文档；导入本身永远不“证明”标定，也不会清掉已有的低置信标记；
- sample_bands 改了且旧文档没有 peak_auto（无法保证 sync 后刻度不丢）时明确拒绝；
- --no-sync 只落到活文档（不存盘），否则调用 args.sync（由 canvas_cli.add_commands 注入的 sync 处理函数，
  见 gel_photo.cmd_sync）刷新 Figure / Profiles / fig.png / fig.mw.json —— 本模块因此不 import gel_photo。
"""
import argparse
import json
import math
import os
import sys

from . import doc_edit
from . import figure
from . import gel_session
from . import vc_file
from . import workbench

SCHEMA = "ep-craft-workbench-lanes"
VERSION = 1
GEL_TOL_CM = 0.01                     # 导出与目标文档的胶面尺寸容差（from_document 两位/四位小数口径）
CM_TOL = 0.0005                       # 几何比较容差（0.5 µm，远小于任何可画出来的移动）
MIN_LANE_CM = 0.02                    # 与 workbench.validate 同一条：两边界几乎重合 = 不是泳道
PT = vc_file.PT_PER_CM
LABEL = "epcraft workbench import"
_MISSING = object()


# ---------------------------------------------------------------- 读活文档

def _abs_doc(doc):
    path = os.path.abspath(doc)
    if not os.path.exists(path):
        raise RuntimeError("找不到 %s" % path)
    return path


def _live_document(path, port, launch=True):
    """让 VectorCraft 打开/激活 path，返回 (client, document)。与 cmd_sync / cmd_edit 同一套。"""
    from . import vc_control
    client, _launched = vc_control.connect(port, path=path, launch=launch)
    gel_session.ensure_open(client, path)
    return client, client.document_json()


def _read_model(document):
    model, notes = workbench.from_document(document)
    return model, notes


# ---------------------------------------------------------------- 导出

def _json_name(text):
    """label lane n 的当前文字 → JSON 里的 name：占位 "?" = 还没起名 → null。"""
    return None if text == figure.UNNAMED else text


def _low_confidence(meta):
    """ep-meta → 导出的 low_confidence：marker_confidence.ok = false 即低置信（没有置信信息 = 不算低置信）。"""
    mc = meta.get("marker_confidence")
    return bool(isinstance(mc, dict) and not mc.get("ok", True))


def build_export(document, model=None):
    """活文档 → 导出 dict（纯数据，不摸文件）。"""
    if model is None:
        model, _notes = workbench.from_document(document)
    names = doc_edit.current_names(document, model)
    lanes = []
    for i, ln in enumerate(model["lanes"]):
        lanes.append({"left_cm": round(float(ln["left_cm"]), 4),
                      "right_cm": round(float(ln["right_cm"]), 4),
                      "trimmed": bool(ln["trimmed"]),
                      "name": _json_name(names[i] if i < len(names) else "")})
    meta = model.get("meta") or {}
    data = {
        "schema": SCHEMA,
        "version": VERSION,
        "doc": os.path.abspath(document.get("path") or "") if document.get("path") else "",
        "gel": {"width_cm": round(float(model["gel"]["width_cm"]), 4),
                "height_cm": round(float(model["gel"]["height_cm"]), 4)},
        "window": {"top_cm": round(float(model["window"]["top_cm"]), 4),
                   "bottom_cm": round(float(model["window"]["bottom_cm"]), 4)},
        "lanes": lanes,
        "marker_lane": int(model["marker_lane"]),
        "bands": [{"mw": float(b["mw"]), "y_cm": round(float(b["y_cm"]), 4)} for b in model["bands"]],
        "low_confidence": _low_confidence(meta),
    }
    # sample bands：只有文档里真有这个图层时才导出（缺键 = import 不碰刻度）
    if vc_file.layer_by_name(document, "sample bands") is not None:
        ticks = []
        for x, y, _node in workbench.sample_ticks(document):
            lane = workbench.lane_at(model, x)
            if lane is not None and lane != model["marker_lane"]:
                ticks.append({"lane": int(lane), "y_cm": round(float(y), 4)})
        data["sample_bands"] = sorted(ticks, key=lambda t: (t["lane"], t["y_cm"]))
    return data


def _write_json(path, text, doc_path):
    out = os.path.abspath(path)
    if os.path.isdir(out):
        raise ValueError("--out 是目录（要给文件路径）：%s" % out)
    if os.path.realpath(out) == os.path.realpath(doc_path):
        raise ValueError("--out 不能覆盖源文档 %s（JSON 不能写回 .vectorcraft）" % doc_path)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, out)
    return out


def cmd_export(args):
    """`epcraft workbench export DOC [--out PATH] [--port PORT]`：活文档 → JSON（省略 --out 打 stdout）。"""
    path = _abs_doc(args.doc)
    client, document = _live_document(path, getattr(args, "port", None) or 7797, launch=True)
    model, notes = _read_model(document)
    data = build_export(document, model)
    data["doc"] = path
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    out = getattr(args, "out", None)
    quiet = sys.stderr if not out else sys.stdout
    for n in notes:
        print("[i] %s" % n, file=quiet)
    if out:
        dest = _write_json(out, text, path)
        print("[i] 导出 %d 条泳道、%d 条 marker 带 → %s" % (len(data["lanes"]), len(data["bands"]), dest))
    else:
        sys.stdout.write(text)
    return 0


# ---------------------------------------------------------------- 校验

def _num(value, where, errors, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append("%s 必须是有限数字（收到 %r）" % (where, value))
        return None
    v = float(value)
    if not math.isfinite(v):
        errors.append("%s 必须是有限数字（收到 %r）" % (where, value))
        return None
    if positive and v <= 0:
        errors.append("%s 必须大于 0（收到 %r）" % (where, value))
        return None
    return v


def _int(value, where, errors):
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append("%s 必须是整数（收到 %r）" % (where, value))
        return None
    return value


def _require(obj, key, where, errors):
    if key not in obj:
        errors.append("%s 缺少 %r" % (where, key))
        return _MISSING
    return obj[key]


def _raise(errors, what="Workbench 导出数据不合法（什么都没改）"):
    raise RuntimeError("%s：\n- %s" % (what, "\n- ".join(errors)))


def _valid_peak_auto(meta):
    return isinstance(meta.get("peak_auto"), dict)


def validate_export(data, target_model):
    """导入 JSON + 目标文档模型 → 规范化后的 dict；任何错误一次列全，绝不部分接受。"""
    if not isinstance(data, dict):
        raise RuntimeError("Workbench 导出必须是一个 JSON 对象（收到 %s）" % type(data).__name__)
    if data.get("schema") != SCHEMA:
        raise RuntimeError("不是 epcraft Workbench 导出：schema = %r，要 %r" % (data.get("schema"), SCHEMA))
    version = data.get("version")
    if isinstance(version, bool) or version != VERSION:
        raise RuntimeError("不支持的 Workbench 导出版本 %r；这个脚本只认 version %d" % (version, VERSION))

    errs = []
    for key in ("gel", "window", "lanes", "bands", "marker_lane"):
        _require(data, key, "导出", errs)
    if errs:
        _raise(errs)

    target = target_model["gel"]
    gel = data.get("gel")
    if not isinstance(gel, dict):
        errs.append("gel 必须是对象（width_cm / height_cm）")
        gw = gh = None
    else:
        gw = _num(_require(gel, "width_cm", "gel", errs), "gel.width_cm", errs, positive=True)
        gh = _num(_require(gel, "height_cm", "gel", errs), "gel.height_cm", errs, positive=True)
    if gw is not None and abs(gw - float(target["width_cm"])) > GEL_TOL_CM:
        errs.append("导出的胶面宽 %.4f cm 与目标文档的 %.4f cm 不同 —— 这份导出是另一块胶做的，"
                    "先从目标文档 export" % (gw, float(target["width_cm"])))
    if gh is not None and abs(gh - float(target["height_cm"])) > GEL_TOL_CM:
        errs.append("导出的胶面高 %.4f cm 与目标文档的 %.4f cm 不同 —— 这份导出是另一块胶做的，"
                    "先从目标文档 export" % (gh, float(target["height_cm"])))

    window = data.get("window")
    if not isinstance(window, dict):
        errs.append("window 必须是对象（top_cm / bottom_cm）")
        top = bottom = None
    else:
        top = _num(_require(window, "top_cm", "window", errs), "window.top_cm", errs)
        bottom = _num(_require(window, "bottom_cm", "window", errs), "window.bottom_cm", errs)
        if top is not None and bottom is not None:
            if top >= bottom:
                errs.append("window.top_cm 必须小于 bottom_cm（%.4f >= %.4f）" % (top, bottom))
            if gh is not None and (top < -CM_TOL or bottom > gh + CM_TOL):
                errs.append("window 超出胶面：%.4f–%.4f cm，胶高 %.4f cm" % (top, bottom, gh))

    raw_lanes = data.get("lanes")
    lanes = []
    if not isinstance(raw_lanes, list) or not raw_lanes:
        errs.append("lanes 必须是非空数组")
    else:
        for i, raw in enumerate(raw_lanes, 1):
            if not isinstance(raw, dict):
                errs.append("lanes[%d] 必须是对象" % i)
                continue
            left = _num(_require(raw, "left_cm", "lanes[%d]" % i, errs), "lanes[%d].left_cm" % i, errs)
            right = _num(_require(raw, "right_cm", "lanes[%d]" % i, errs), "lanes[%d].right_cm" % i, errs)
            trimmed = raw.get("trimmed")
            if not isinstance(trimmed, bool):
                errs.append("lanes[%d].trimmed 必须是 true/false（收到 %r）" % (i, trimmed))
                trimmed = False
            name = raw.get("name", _MISSING)
            if name is not _MISSING and name is not None and not isinstance(name, str):
                errs.append("lanes[%d].name 必须是字符串或 null（收到 %r）" % (i, name))
                name = None
            if left is not None and right is not None and right - left < MIN_LANE_CM:
                errs.append("泳道 %d 的两条边线几乎重合（%.4f cm < %.4f）" % (i, right - left, MIN_LANE_CM))
            lanes.append({"left_cm": left, "right_cm": right, "trimmed": trimmed,
                          "name": None if name is _MISSING or name is None else name,
                          "name_given": name is not _MISSING})
        if lanes and all(ln["trimmed"] for ln in lanes):
            errs.append("所有泳道都在 trimmed 里 —— Figure 至少要留一条")
        for i in range(1, len(lanes)):
            a, b = lanes[i - 1], lanes[i]
            if a["right_cm"] is not None and b["left_cm"] is not None \
                    and abs(b["left_cm"] - a["right_cm"]) > 1e-4:
                errs.append("泳道 %d 与泳道 %d 不连续：left %.4f != 上一道的 right %.4f"
                            % (i, i + 1, b["left_cm"], a["right_cm"]))

    marker_lane = _int(_require(data, "marker_lane", "导出", errs), "marker_lane", errs)
    if marker_lane is not None and not 1 <= marker_lane <= len(raw_lanes if isinstance(raw_lanes, list) else []):
        errs.append("marker_lane %d 超出 1–%d" % (marker_lane, len(raw_lanes or [])))

    raw_bands = data.get("bands")
    bands = []
    if not isinstance(raw_bands, list):
        errs.append("bands 必须是数组")
    else:
        # 0 / 1 / 2 条 band 线都合法（ep-bdt）：标定点少 → 分子量估不出，报告标低置信，不是导出错误。
        prev_y = prev_mw = None
        for i, raw in enumerate(raw_bands, 1):
            if not isinstance(raw, dict):
                errs.append("bands[%d] 必须是对象" % i)
                continue
            mw = _num(_require(raw, "mw", "bands[%d]" % i, errs), "bands[%d].mw" % i, errs, positive=True)
            y = _num(_require(raw, "y_cm", "bands[%d]" % i, errs), "bands[%d].y_cm" % i, errs)
            if y is not None and prev_y is not None and y <= prev_y + 1e-9:
                errs.append("bands 必须按 y 严格升序（band %d 的 y=%.4f 不大于上一条 %.4f）" % (i, y, prev_y))
            if mw is not None and prev_mw is not None and mw >= prev_mw:
                errs.append("band %d（MW %s）与上一条（MW %s）不满足自上而下分子量严格减小"
                            % (i, ("%g" % mw), ("%g" % prev_mw)))
            if y is not None:
                prev_y = y
            if mw is not None:
                prev_mw = mw
            bands.append({"mw": mw, "y_cm": y})

    # 旧导出里这一项叫 draft（布尔）：兼容读入，输出永远只写 low_confidence。
    # 旧文档的 ep-meta 里 draft 是字典，压到导出时也只当「有标记」= true。
    if "low_confidence" in data:
        low = data["low_confidence"]
    else:
        legacy = data.get("draft", False)
        low = bool(legacy) if isinstance(legacy, dict) else legacy
    if not isinstance(low, bool):
        errs.append("low_confidence 必须是 true/false（收到 %r）" % (low,))

    sample = data.get("sample_bands", _MISSING)
    sample_out = None
    if sample is not _MISSING:
        sample_out = []
        if not isinstance(sample, list):
            errs.append("sample_bands 必须是数组（{lane, y_cm}）")
        else:
            for i, raw in enumerate(sample, 1):
                if not isinstance(raw, dict):
                    errs.append("sample_bands[%d] 必须是对象" % i)
                    continue
                lane = _int(_require(raw, "lane", "sample_bands[%d]" % i, errs), "sample_bands[%d].lane" % i, errs)
                y = _num(_require(raw, "y_cm", "sample_bands[%d]" % i, errs), "sample_bands[%d].y_cm" % i, errs)
                if lane is not None and not 1 <= lane <= len(raw_lanes or []):
                    errs.append("sample_bands[%d].lane %d 超出 1–%d" % (i, lane, len(raw_lanes or [])))
                elif lane is not None and marker_lane is not None and lane == marker_lane:
                    errs.append("sample_bands[%d] 落在 marker 泳道 %d 里（marker 带由 band <MW> 线表示）"
                                % (i, marker_lane))
                if lane is not None and y is not None:
                    sample_out.append({"lane": lane, "y_cm": round(y, 4)})
        sample_out = sorted({(t["lane"], t["y_cm"]) for t in sample_out})
        sample_out = [{"lane": lane, "y_cm": y} for lane, y in sample_out]

    if errs:
        _raise(errs)
    return {"schema": SCHEMA, "version": VERSION,
            "gel": {"width_cm": gw, "height_cm": gh},
            "window": {"top_cm": round(top, 4), "bottom_cm": round(bottom, 4)},
            "lanes": lanes, "marker_lane": marker_lane,
            "bands": [{"mw": float(b["mw"]), "y_cm": round(float(b["y_cm"]), 4)} for b in bands],
            "low_confidence": bool(low), "sample_bands": sample_out}


# ---------------------------------------------------------------- 计划导入（纯函数）

def _line_nodes(document, layer_name):
    """图层里能当直线读的对象（与 workbench.from_document 的 lines() 同口径，含组内对象）。"""
    lay = vc_file.layer_by_name(document, layer_name)
    if lay is None:
        return []
    out = []
    for _top, n in vc_file.walk({"layers": [lay]}):
        k = n.get("kind") or {}
        subs = (k.get("path") or {}).get("subpaths") or []
        if k.get("type") == "path" and len(subs) == 1 and not subs[0].get("closed"):
            out.append(n)
    return out


def _mid(n):
    (x0, y0), (x1, y1) = vc_file.line_ends(n)
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0, abs(x1 - x0) <= abs(y1 - y0), n)


def _same_pt(a, b, tol=CM_TOL * PT):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _reshape(node, a, b, ops):
    got = list(vc_file.line_ends(node))
    if _same_pt(tuple(got[0]), a) and _same_pt(tuple(got[1]), b):
        return False
    ops.append({"op": "reshape", "id": node["id"], "points": [list(a), list(b)], "closed": False})
    return True


def _line_spec(layer, name, a, b, rgb, width):
    return {"kind": "line", "layer": layer, "name": name, "a": [a[0], a[1]], "b": [b[0], b[1]],
            "rgb": list(rgb), "width": float(width)}


def _valid_ticks(document, model):
    """文档里归 agent 管的 sample band 刻度 → [(lane, y_cm, x_cm)]（marker / 道外的由 foreign 保留）。"""
    out = []
    for x, y, _node in workbench.sample_ticks(document):
        lane = workbench.lane_at(model, x)
        if lane is not None and lane != model["marker_lane"]:
            out.append((lane, round(float(y), 4), round(float(x), 4)))
    return sorted(out)


def _tick_specs(lanes, ticks):
    specs = []
    by_lane = {}
    for lane, y in ticks:
        by_lane.setdefault(lane, []).append(y)
    for lane in sorted(by_lane):
        ln = lanes[lane - 1]
        cx = (ln["left_cm"] + ln["right_cm"]) / 2.0
        for k, y in enumerate(sorted(by_lane[lane]), 1):
            specs.append(_line_spec("sample bands", "peak %d.%d" % (lane, k),
                                    (cx - 0.08, y), (cx + 0.08, y), (0.1, 0.6, 0.1), workbench.LINE_W))
    return specs


def _trim_specs(lanes):
    specs = []
    for i, ln in enumerate(lanes, 1):
        if not ln["trimmed"]:
            continue
        a, b = ln["left_cm"], ln["right_cm"]
        q = (b - a) / 4.0
        specs.append(_line_spec("trimmed", "trim", (a + q, -workbench.TRIM_Y_CM),
                                (b - q, -workbench.TRIM_Y_CM), workbench.RGB["trimmed"], 1.5))
    return specs


def _trim_equal(current, lanes, X, Y):
    want = _trim_specs(lanes)
    if len(current) != len(want):
        return False
    cur = sorted((vc_file.line_ends(n) for n in current), key=lambda e: min(p[0] for p in e))
    for (a, b), sp in zip(cur, want):
        if not (_same_pt(tuple(a), (X(sp["a"][0]), Y(sp["a"][1])))
                and _same_pt(tuple(b), (X(sp["b"][0]), Y(sp["b"][1])))):
            return False
    return True


def _band_specs(lanes, marker_lane, bands):
    ml = lanes[marker_lane - 1]
    return [_line_spec("marker bands", "band %g" % b["mw"],
                       (ml["left_cm"] - workbench.GRAB_CM, b["y_cm"]),
                       (ml["right_cm"] + workbench.GRAB_CM, b["y_cm"]),
                       workbench.RGB["band"], workbench.LINE_W)
            for b in bands]


def plan_import(document, model, data):
    """(ops, 新 ep-meta, 摘要行, 警告行)。纯函数；planning 完之后才允许碰文档。"""
    ops, lines, warns = [], [], []
    gx, gy = vc_file.doc_origin(document)
    X = lambda cm: gx + cm * PT                             # noqa: E731
    Y = lambda cm: gy + cm * PT                             # noqa: E731
    W = data["gel"]["width_cm"]
    win = data["window"]
    lanes = data["lanes"]
    bands = data["bands"]
    marker_lane = data["marker_lane"]
    top, bottom = sorted((win["top_cm"], win["bottom_cm"]))

    # ---- 积分窗：两条横线的 y 换成导入值（保留它们各自的横向长度）
    wnodes = sorted(_line_nodes(document, "window"), key=lambda n: _mid(n)[1])
    if len(wnodes) != 2:
        raise RuntimeError("window 图层应当恰好有两条横线（现在 %d 条）—— 先修好再 import" % len(wnodes))
    moved = 0
    for n, cm in zip(wnodes, (top, bottom)):
        (x0, _y0), (x1, _y1) = vc_file.line_ends(n)
        y = Y(cm)
        moved += _reshape(n, (x0, y), (x1, y), ops)
    if moved:
        lines.append("积分窗：%.4f–%.4f cm" % (top, bottom))

    # ---- 泳道边线：按 x 排序；数量变化时多删少补
    lnodes = sorted(_line_nodes(document, "lanes"), key=lambda n: _mid(n)[0])
    bounds = [lanes[0]["left_cm"]] + [ln["right_cm"] for ln in lanes]
    targets = [((X(x), Y(top)), (X(x), Y(bottom))) for x in bounds]
    for n, (a, b) in zip(lnodes, targets):
        _reshape(n, a, b, ops)
    for n in lnodes[len(targets):]:
        ops.append({"op": "delete", "id": n["id"]})
    for x in bounds[len(lnodes):]:
        sp = _line_spec("lanes", "lane", (x, top), (x, bottom),
                        workbench.RGB["lane"], workbench.LINE_W)
        ops.append({"op": "create", "layer": "lanes", "spec": _spec_pt(sp, X, Y)})
    if len(lnodes) != len(targets):
        lines.append("泳道边线：%d → %d 条（%d 条新建 / %d 条删除）"
                     % (len(lnodes), len(targets), max(0, len(targets) - len(lnodes)),
                        max(0, len(lnodes) - len(targets))))

    # ---- marker 带线：位置或 MW 变了就整组重建（线很短，重建比逐条配对稳）
    bnodes = sorted(_line_nodes(document, "marker bands"), key=lambda n: _mid(n)[1])
    want_b = _band_specs(lanes, marker_lane, bands)
    same_b = len(bnodes) == len(want_b)
    if same_b:
        for n, sp in zip(bnodes, want_b):
            ((ax, ay), (bx, by)) = vc_file.line_ends(n)
            if n.get("name") != sp["name"] or not (
                    _same_pt((ax, ay), (X(sp["a"][0]), Y(sp["a"][1])))
                    and _same_pt((bx, by), (X(sp["b"][0]), Y(sp["b"][1])))):
                same_b = False
                break
    if not same_b:
        ops += [{"op": "delete", "id": n["id"]} for n in bnodes]
        for sp in want_b:
            ops.append({"op": "create", "layer": "marker bands", "spec": _spec_pt(sp, X, Y)})
        lines.append("marker 带：%d 条（重建：%s）" % (len(bands), "，".join(
            "band %g @ %.2f" % (b["mw"], b["y_cm"]) for b in bands)))

    # ---- trim 标记：位置/集合变了就重建
    layer_trim = vc_file.layer_by_name(document, "trimmed")
    trim_nodes = _line_nodes(document, "trimmed")
    if not _trim_equal(trim_nodes, lanes, X, Y):
        if layer_trim is not None:
            for n in vc_file.lay_children(layer_trim):
                ops.append({"op": "delete", "id": n["id"]})
        if _trim_specs(lanes):
            if layer_trim is None:
                ops.append(dict(workbench.layer_props("trimmed"), op="layer"))
            for sp in _trim_specs(lanes):
                ops.append({"op": "create", "layer": "trimmed", "spec": _spec_pt(sp, X, Y)})
        lines.append("trim 标记：%s" % ([i for i, ln in enumerate(lanes, 1) if ln["trimmed"]] or "无"))

    # ---- sample bands：有 key 才动；旧文档没有 peak_auto 时，只允许“不改刻度”的导入
    sample = data["sample_bands"]
    if sample is not None:
        desired = [(t["lane"], t["y_cm"]) for t in sample]
        current = _valid_ticks(document, model)
        centres = [(ln["left_cm"] + ln["right_cm"]) / 2.0 for ln in lanes]
        want = [(lane, y) for lane, y in desired]
        have = [(lane, y) for lane, y, _x in current]
        changed = sorted(want) != sorted(have) or any(
            abs(x - centres[lane - 1]) > CM_TOL for lane, _y, x in current)
        if changed and not _valid_peak_auto(model.get("meta") or {}):
            raise RuntimeError("这份文档是旧版 render 的，ep-meta 里没有 peak_auto —— 导入新的 sample bands 后，"
                               "默认 sync 会把刻度按检测器重算，无法保证你导入的刻度不丢。\n"
                               "先在 app 里跑一次 sync 让 ep-meta 补上 peak_auto，再 import；或这份导出不要改 sample_bands。")
        if changed:
            layer_sb = vc_file.layer_by_name(document, "sample bands")
            if desired and layer_sb is None:
                ops.append(dict(workbench.layer_props("sample bands"), op="layer"))
            if layer_sb is not None:
                foreign = figure._foreign_tick_ids(document, model)
                for n in vc_file.lay_children(layer_sb):
                    if n["id"] not in foreign:
                        ops.append({"op": "delete", "id": n["id"]})
            for sp in _tick_specs(lanes, desired):
                ops.append({"op": "create", "layer": "sample bands", "spec": _spec_pt(sp, X, Y)})
            lines.append("样品带刻度：%d 条" % len(desired))
        if _valid_peak_auto(model.get("meta") or {}):
            smodel = {"gel": data["gel"], "window": {"top_cm": top, "bottom_cm": bottom},
                      "lanes": lanes, "bands": bands, "marker_lane": marker_lane,
                      "meta": dict(model.get("meta") or {})}
            ticks = [((ln["left_cm"] + ln["right_cm"]) / 2.0, float(y), None)
                     for lane, y in desired for ln in [lanes[lane - 1]]]
            overrides, _w = gel_session.peak_override_state(smodel, ticks, reset_lanes=())
            data = dict(data, _peak_overrides=overrides)

    # ---- 名字 / ep-meta：重定 lane_names、prev_centres_cm、bands（低置信只进不出）
    cur_names = doc_edit.current_names(document, model)
    names = []
    for j, ln in enumerate(lanes):
        if ln.get("name_given"):
            names.append(ln["name"])
        else:
            names.append(cur_names[j] if j < len(cur_names) else None)
    label_layer = vc_file.layer_by_name(document, "labels")
    by_name = {}
    for n in (vc_file.lay_children(label_layer) if label_layer else []):
        if n.get("name") and n.get("kind", {}).get("type") == "text":
            by_name.setdefault(n["name"], n)
    renamed = []
    for j, name in enumerate(names, 1):
        node = by_name.get("label lane %d" % j)
        text = figure.UNNAMED if name is None else name
        if node is not None and vc_file.text_info(node)["text"] != text:
            ops.append({"op": "set_text", "id": node["id"], "text": text})
            renamed.append("%d=%s" % (j, name if name is not None else "?"))
    if renamed:
        lines.append("泳道名：%s" % "，".join(renamed))

    meta_node, old_meta = vc_file.doc_meta(document)
    merged = dict(old_meta)
    merged["lane_names"] = names
    merged["prev_centres_cm"] = [round((ln["left_cm"] + ln["right_cm"]) / 2.0, 4) for ln in lanes]
    merged["bands"] = [[float(b["mw"]), float(b["y_cm"])] for b in bands]
    if data["low_confidence"]:
        # 源导出是低置信：目标也标低置信；导入永远不“证明”标定，false 也不清除目标已有的标记
        mc = dict(merged.get("marker_confidence") or {})
        mc.update(ok=False, gate=mc.get("gate") or "workbench import：源导出标低置信")
        mc.setdefault("picked", None)
        mc.setdefault("need", None)
        mc.setdefault("notes", [])
        merged["marker_confidence"] = mc
    overrides = data.get("_peak_overrides") if isinstance(data.get("_peak_overrides"), dict) else None
    if overrides is not None:
        if overrides:
            merged["peak_overrides"] = overrides
        else:
            merged.pop("peak_overrides", None)
    if merged != old_meta:
        if meta_node is None:
            raise RuntimeError("文档没有 ep-meta 文字对象 —— 无法重定泳道名 / 低置信标记；先 render 或 sync 一次")
        ops.append({"op": "set_text", "id": meta_node["id"],
                    "text": json.dumps(merged, ensure_ascii=False, separators=(",", ":"))})
    return ops, merged, lines, warns


def _spec_pt(sp, X, Y):
    """plan 里的厘米 spec → sync_exec 用的 pt spec。"""
    return dict(sp, a=[X(sp["a"][0]), Y(sp["a"][1])], b=[X(sp["b"][0]), Y(sp["b"][1])])


# ---------------------------------------------------------------- 导入命令

def _load_json(path):
    if not os.path.exists(path):
        raise RuntimeError("找不到导入文件 %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except ValueError as e:
        raise ValueError("导入文件不是合法 JSON：%s（%s）" % (path, e))


def cmd_import(args):
    """`epcraft workbench import DOC --input PATH [--no-sync] [--dpi DPI] [--port PORT]`。"""
    no_sync = getattr(args, "no_sync", False)
    sync = getattr(args, "sync", None)
    if not no_sync and not callable(sync):
        raise RuntimeError("workbench import 需要 sync 处理函数（由 canvas_cli.add_commands 注入 args.sync）")
    path = _abs_doc(args.doc)
    data = _load_json(os.path.abspath(args.input))
    client, document = _live_document(path, getattr(args, "port", None) or 7797, launch=True)
    model, notes = _read_model(document)
    for n in notes:
        print("[i] %s" % n)
    clean = validate_export(data, model)
    ops, _meta, lines, warns = plan_import(document, model, clean)
    for w in warns:
        print("[!] %s" % w)
    for line in lines:
        print("[i] %s" % line)
    if ops:
        from . import sync_exec
        done = sync_exec.apply_ops_live(client, ops, document, label=LABEL)
        print("[i] 已写入：%s（一个撤销步「%s」，⌘Z 可整体撤回）" % (sync_exec.describe(done), LABEL))
    else:
        print("[i] 文档已经是最新的（0 个改动）")
    if no_sync:
        print("[i] 未 sync（--no-sync）：改动在 VectorCraft 里还没存盘")
        return 0
    return sync(argparse.Namespace(doc=path, port=getattr(args, "port", None) or 7797,
                                   dpi=getattr(args, "dpi", 300), dry_run=False, reset_peaks=None))


# ---------------------------------------------------------------- CLI（canvas_cli 负责注册；这里给旧式/自检用）
def add_parsers(workbench_sub, sync=None):
    """旧式路径的注册入口（canvas_cli 不用它）；sync 同 cmd_import 的 args.sync。"""
    we = workbench_sub.add_parser("export", help="把 Workbench 的泳道/窗口/带数据导出为带版本的 JSON")
    we.add_argument("doc")
    we.add_argument("--out", help="JSON 输出路径（省略 = stdout）")
    we.add_argument("--port", type=int, default=7797)
    we.set_defaults(func=cmd_export)
    wi = workbench_sub.add_parser("import", help="从 JSON 恢复 Workbench 数据并校验后再写入")
    wi.add_argument("doc")
    wi.add_argument("--input", required=True)
    wi.add_argument("--no-sync", action="store_true")
    wi.add_argument("--dpi", type=int, default=300)
    wi.add_argument("--port", type=int, default=7797)
    wi.set_defaults(func=cmd_import, sync=sync)
    return workbench_sub
