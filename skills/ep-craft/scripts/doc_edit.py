#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`edit`：agent 常做的几种文档改动 → sync_exec 能执行的 ops（与 plan_sync 同一套 op）。纯 stdlib。

    trim          → 清空 trimmed 图层，每条要拿掉的泳道画一条 trim 短线（与 render 画的同位置同样式）
    labels        → 有 `label lane n` 的改文字（字体、字号、颜色、角度都不动）；没有的写进 ep-meta 的
                    lane_names，随后的 sync 按 lane_angles 新建（所以新标签和别的标签同角度）
    marker_lane   → 全部 band 线搬到那条泳道：y 不变，两端 = 泳道边 ± GRAB_CM

泳道号 = Workbench 上**现在**的泳道（lanes 图层的分隔线从左往右）；标签对象的号是上一次 sync 时的，
按 ep-meta 的 prev_centres_cm 用 workbench.match_lanes 对过去（与 plan_sync 一样）。
"""
import json

import vc_file
import workbench

PT = vc_file.PT_PER_CM


def current_names(document, model):
    """现在每条泳道的名字（从左往右）：有标签对象取它的文字，没有取 ep-meta 的 lane_names，都没有 = ""。"""
    _match, by_name, old = _labels(document, model)
    out = []
    for j, i in enumerate(_match, 1):
        n = by_name.get("label lane %d" % (i + 1)) if i is not None else None
        if n is not None:
            out.append(vc_file.text_info(n)["text"])
        else:
            out.append((old[i] if i is not None and i < len(old) else "") or "")
    return out


def plan_edit(document, model, trim=None, names=None, marker_lane=None):
    """→ (ops, 给人看的几行)。

    model = workbench.from_document(document) 的模型；trim = 要拿掉的泳道号集合（None = 不动）；
    names = {泳道号: 新名字}（None / {} = 不动）；marker_lane = band 线搬去的泳道号（None = 不动）。
    什么都不用改 → ([], [])（幂等）。参数越界 → WorkbenchError。
    """
    n = len(model["lanes"])
    gx, gy = _origin(document)
    X = lambda cm: gx + cm * PT                          # noqa: E731
    Y = lambda cm: gy + cm * PT                          # noqa: E731
    L = {l["name"]: l for l in document["layers"]}
    ops, lines = [], []

    if trim is not None:
        trim = sorted({int(t) for t in trim})
        bad = [t for t in trim if not 1 <= t <= n]
        if bad:
            raise workbench.WorkbenchError("要拿掉的泳道 %s 超出 1–%d" % (bad, n))
        if len(trim) == n:
            raise workbench.WorkbenchError("所有泳道都在 trimmed 里 —— Figure 至少要留一条")
        if trim != [i for i, ln in enumerate(model["lanes"], 1) if ln["trimmed"]]:
            ops += [{"op": "delete", "id": c["id"]} for c in vc_file.lay_children(L["trimmed"])]
            for t in trim:
                a, b = model["lanes"][t - 1]["left_cm"], model["lanes"][t - 1]["right_cm"]
                q = (b - a) / 4.0
                ops.append({"op": "create", "layer": "trimmed", "spec": {
                    "kind": "line", "layer": "trimmed", "name": "trim",
                    "a": [X(a + q), Y(-workbench.TRIM_Y_CM)], "b": [X(b - q), Y(-workbench.TRIM_Y_CM)],
                    "rgb": list(workbench.RGB["trimmed"]), "width": 1.5}})
            lines.append("trimmed（不进 Figure）→ %s" % trim)

    if marker_lane is not None:
        ml = int(marker_lane)
        if not 1 <= ml <= n:
            raise workbench.WorkbenchError("marker 泳道 %d 超出 1–%d" % (ml, n))
        ln = model["lanes"][ml - 1]
        x0, x1 = X(ln["left_cm"] - workbench.GRAB_CM), X(ln["right_cm"] + workbench.GRAB_CM)
        moved = 0
        for c in vc_file.lay_children(L["marker bands"]):
            (ax, ay), (bx, by) = vc_file.line_ends(c)
            y = (ay + by) / 2.0
            if max(abs(ax - x0), abs(bx - x1), abs(ay - y), abs(by - y)) > 0.01:
                ops.append({"op": "reshape", "id": c["id"], "points": [[x0, y], [x1, y]], "closed": False})
                moved += 1
        if moved:
            lines.append("band 线（%d 条）搬到泳道 %d，y 不变" % (moved, ml))

    if names:
        names = {int(j): str(t) for j, t in names.items()}
        bad = [j for j in names if not 1 <= j <= n]
        if bad:
            raise workbench.WorkbenchError("泳道名的泳道号 %s 超出 1–%d" % (sorted(bad), n))
        match, by_name, old = _labels(document, model)
        cur = current_names(document, model)
        meta = _meta(document)
        new_old = list(old)
        changed = []
        for j in sorted(names):
            text = names[j]
            if cur[j - 1] == text:
                continue
            i = match[j - 1]
            if i is None:
                raise workbench.WorkbenchError("泳道 %d 是上次 sync 之后新加的 —— 先 sync 一次再改它的名字" % j)
            node = by_name.get("label lane %d" % (i + 1))
            if node is not None:
                ops.append({"op": "set_text", "id": node["id"], "text": text})
            else:
                new_old += [""] * (i + 1 - len(new_old))
                new_old[i] = text
            changed.append("%d=%s" % (j, text or "（空）"))
        if new_old != old and meta is not None:
            node_, m = meta
            m["lane_names"] = new_old
            ops.append({"op": "set_text", "id": node_["id"],
                        "text": json.dumps(m, ensure_ascii=False, separators=(",", ":"))})
        if changed:
            lines.append("泳道名：%s" % "，".join(changed))
    return ops, lines


def shifted(names, k):
    """泳道名整体右移 k 格（负 = 左移）：移出去的丢掉，空出来的格 = ""。"""
    n = len(names)
    return [names[j - k] if 0 <= j - k < n else "" for j in range(n)]


def _origin(document):
    """Workbench 胶面图左上角（pt）：cm → pt 的原点（与 workbench.from_document 同一口径）。"""
    lay = vc_file.layer_by_name(document, "gel")
    img = next(c for c in vc_file.lay_children(lay) if c["kind"].get("type") == "image" and c.get("name") == "gel")
    return img["kind"]["xf"][4], img["kind"]["xf"][5]


def _meta(document):
    """(ep-meta 文字对象, 它的 JSON) 或 None。"""
    lay = vc_file.layer_by_name(document, "ep-meta")
    node = next((c for c in (vc_file.lay_children(lay) if lay else []) if c.get("name") == "ep-meta"), None)
    if node is None:
        return None
    try:
        return node, json.loads(vc_file.text_info(node)["text"])
    except ValueError:
        return None


def _labels(document, model):
    """→ (现在第 j 道 ↔ 上次第 i 道的对应表, {标签名: 对象}, ep-meta 的 lane_names)。"""
    meta = model.get("meta") or {}
    prev = meta.get("prev_centres_cm") or []
    new_c = workbench.centres(model)
    match = workbench.match_lanes(prev, new_c) if prev else list(range(len(new_c)))
    by_name = {}
    for c in vc_file.lay_children(vc_file.layer_by_name(document, "labels")):
        if c.get("name") and c["kind"]["type"] == "text":
            by_name.setdefault(c["name"], c)
    return match, by_name, list(meta.get("lane_names") or [])
