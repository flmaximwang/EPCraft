#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 figure.plan_sync 的 ops 通过控制口落到**活的** VectorCraft 文档上（V7）。纯 stdlib。

语义与 figure.apply_ops_json 一一对应（那边改文件 JSON，这边发命令）：

    artboard  → artboard.setProps {index, x, y, width, height, moveArt:false}
    delete    → edit.clear {ids}
    create    → 在规格的图层里新建（text.create / shape.line / path.create / 剪切组），再 object.setProps 起名
    move      → object.move {ids, dx, dy}
    rename    → object.setProps {id, name}
    set_text  → text.setText {id, text}
    stroke    → paint.setStroke {ids, color:[r,g,b]}
    reshape   → path.setAnchors {id, subpaths}

整次 sync 在**一个**撤销组 `ep-labeling sync` 里（vc_control.Client.undo_group）：任何一步失败 → cancel
回滚、原错抛出；成功 → 用户一次 ⌘Z 撤掉整次 sync。

实测（VectorCraft 0.14.0，见 references/vectorcraft.md）决定的做法：
- 锁定图层里的对象照样能 edit.clear / object.move / object.setProps / path.setAnchors / text.setText /
  paint.setStroke；只有**新建**不行：当前层锁定或隐藏时，新对象悄悄落进上一个可用的当前层。
  所以只给 create 的目标层临时解锁（隐藏的临时显示），整组最后锁回去；收尾再读一次 document.json，
  新对象不在该在的层就 layer.move 过去（兜底）。
- text.create / shape.line / path.create 不收名字 → 建完 object.setProps 起名；字体一律 Arial。
- 剪切组：胶面图（同一个 images key）已在 gel 层里 → object.transform {copy:true} 复制一份并一步放到
  目标 xf，再画矩形 path，object.clippingMask.make {ids:[矩形, 图]}（最上面的矩形当剪切路径、自动去掉描边填色）。
- 对 ids 上色（paint.setStroke 等）会顺手改掉 app 的「新对象默认颜色」，撤销也不回来；layer.setCurrent
  不是撤销步。所以收尾把选择清空、当前层与默认颜色恢复成 sync 之前的样子。
"""
import contextlib

import vc_control
import vc_file


LABEL = "ep-labeling sync"
OP_KINDS = ("artboard", "delete", "create", "move", "rename", "set_text", "stroke", "reshape")


def apply_ops_live(client, ops, document, label=LABEL):
    """ops（figure.plan_sync 的结果）→ 活文档。document = 规划时读的 client.document_json()。

    返回 {op 种类: 条数}（没有的种类不列）；外加 "created_ids": [新对象 id…]。ops 为空 → 什么都不发。
    """
    summary = {}
    for op in ops:
        if op.get("op") not in OP_KINDS:
            raise vc_control.VCError("未知 op %r（sync_exec 只认 %s）" % (op.get("op"), "/".join(OP_KINDS)))
        summary[op["op"]] = summary.get(op["op"], 0) + 1
    if not ops:
        return dict(summary, created_ids=[])

    layers = {l["name"]: l for l in document["layers"]}
    for op in ops:
        if op["op"] == "create" and op["layer"] not in layers:
            raise vc_control.VCError("文档里没有图层 %r（create %r）" % (op["layer"], op["spec"].get("name")))
    state = _ui_state(client)
    created = []                                     # (新 id, 目标图层名)
    try:
        with client.undo_group(label):
            opened = _open_layers(client, layers, {op["layer"] for op in ops if op["op"] == "create"})
            try:
                cur = [None]
                for op in ops:
                    _apply_one(client, op, layers, document, cur, created)
                _place_check(client, layers, created)
            except BaseException:
                with contextlib.suppress(vc_control.VCError):
                    _close_layers(client, opened)
                raise
            _close_layers(client, opened)
    finally:
        with contextlib.suppress(vc_control.VCError):
            _restore_ui(client, state)
    return dict(summary, created_ids=[i for i, _l in created])


def describe(summary):
    """summary → 一行人话：`create 12, delete 10, move 3`。"""
    parts = ["%s %d" % (k, summary[k]) for k in OP_KINDS if summary.get(k)]
    return ", ".join(parts) if parts else "no changes"


# ---------------------------------------------------------------- 单条 op

def _apply_one(client, op, layers, document, cur, created):
    o = op["op"]
    ex = client.execute
    if o == "artboard":
        x, y, w, h = op["rect"]
        ex("artboard.setProps", {"index": op["index"], "x": x, "y": y, "width": w, "height": h,
                                 "moveArt": False})
    elif o == "delete":
        ex("edit.clear", {"ids": [op["id"]]})
    elif o == "move":
        ex("object.move", {"ids": [op["id"]], "dx": op["dx"], "dy": op["dy"]})
    elif o == "rename":
        ex("object.setProps", {"id": op["id"], "name": op["name"]})
    elif o == "set_text":
        ex("text.setText", {"id": op["id"], "text": op["text"]})
    elif o == "stroke":
        ex("paint.setStroke", {"ids": [op["id"]], "color": list(op["rgb"])})
    elif o == "reshape":
        ex("path.setAnchors", {"id": op["id"], "subpaths": [
            {"anchors": [{"x": p[0], "y": p[1]} for p in op["points"]], "closed": bool(op["closed"])}]})
    elif o == "create":
        lay = layers[op["layer"]]
        if cur[0] != lay["id"]:
            ex("layer.setCurrent", {"id": lay["id"]})
            cur[0] = lay["id"]
        ex("select.set", {"ids": []})
        new_id = _create(client, op["spec"], document)
        ex("object.setProps", {"id": new_id, "name": op["spec"]["name"]})
        created.append((new_id, op["layer"]))


def _create(client, sp, document):
    """在当前层新建一条规格（figure.node 的控制口版），返回新对象 id（剪切组 = 组的 id）。"""
    ex = client.execute
    k = sp["kind"]
    if k == "text":
        r = ex("text.create", {"x": sp["x"], "y": sp["y"], "text": sp["text"], "size": sp["size"],
                               "font": "Arial", "justify": sp["justify"].lower(), "color": list(sp["rgb"]),
                               "charAlign": "romanBaseline"})
        if abs(float(sp.get("angle") or 0.0)) > 1e-9:     # 绕锚点相对转（新建的字本来就是正的）
            # 不用 absolute：0.14 实测 absolute:0 对已经是 90° 的字是空操作（见 references/vectorcraft.md §2）
            ex("object.rotate", {"ids": [r["id"]], "angle": float(sp["angle"]),
                                 "origin": [sp["x"], sp["y"]]})
        return r["id"]
    if k in ("line", "polyline"):
        if k == "line":
            (x1, y1), (x2, y2) = sp["a"], sp["b"]
            nid = ex("shape.line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})["id"]
        else:
            nid = ex("path.create", {"anchors": [{"x": x, "y": y} for x, y in sp["points"]],
                                     "closed": False})["id"]
        ex("paint.setFill", {"ids": [nid], "none": True})
        ex("paint.setStroke", {"ids": [nid], "color": list(sp["rgb"])})
        ex("stroke.set", {"ids": [nid], "weight": float(sp["width"])})
        return nid
    if k == "clip":
        src = _source_image(document, sp["key"])
        ix, iy, s = sp["image"]
        m = _compose((s, 0.0, 0.0, s, ix, iy), _invert(src["kind"]["xf"]))
        img = ex("object.transform", {"ids": [src["id"]], "matrix": list(m), "copy": True})["ids"][0]
        x, y, w, h = sp["rect"]
        rect = ex("path.create", {"anchors": [{"x": x, "y": y}, {"x": x + w, "y": y},
                                              {"x": x + w, "y": y + h}, {"x": x, "y": y + h}],
                                  "closed": True})["id"]
        return ex("object.clippingMask.make", {"ids": [rect, img]})["id"]
    raise vc_control.VCError("未知规格 kind=%r（%r）" % (k, sp.get("name")))


def _source_image(document, key):
    """文档里已有的、用同一个 images key 的图（优先 gel 层里叫 gel 的那张 Workbench 胶面图）。"""
    best = None
    for lay, n in vc_file.walk(document):
        if n["kind"].get("type") == "image" and n["kind"].get("key") == key:
            if lay["name"] == "gel" and n.get("name") == "gel":
                return n
            best = best or n
    if best is None:
        raise vc_control.VCError("文档里找不到胶面图（images key %s）—— 剪切组没法复制它；"
                                 "Workbench 的 gel 图被删了？撤销后重跑 sync" % key)
    return best


# ---------------------------------------------------------------- 仿射（[a,b,c,d,e,f]：x' = a x + c y + e，y' = b x + d y + f）

def _compose(m, n):
    """m ∘ n（先 n 后 m）。"""
    a, b, c, d, e, f = m
    p, q, r, s, t, u = n
    return (a * p + c * q, b * p + d * q, a * r + c * s, b * r + d * s, a * t + c * u + e, b * t + d * u + f)


def _invert(m):
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        raise vc_control.VCError("胶面图的变换不可逆（xf=%r）" % (m,))
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return (ia, ib, ic, id_, -(ia * e + ic * f), -(ib * e + id_ * f))


# ---------------------------------------------------------------- 图层开关、落层核对、界面状态

def _open_layers(client, layers, names):
    """create 的目标层若锁定/隐藏 → 临时解锁/显示。返回 [(图层 id, 要恢复的 props)]。"""
    opened = []
    for nm in sorted(names):
        visible, locked, _printable = vc_file.layer_flags(layers[nm])
        undo = {}
        if locked:
            undo["locked"] = True
        if not visible:
            undo["visible"] = False
        if undo:
            client.execute("layer.setProps", dict({"id": layers[nm]["id"]},
                                                  **{k: not v for k, v in undo.items()}))
            opened.append((layers[nm]["id"], undo))
    return opened


def _close_layers(client, opened):
    for lid, props in opened:
        client.execute("layer.setProps", dict({"id": lid}, **props))


def _place_check(client, layers, created):
    """新对象必须在规格的图层里（顶层）；不在 → layer.move 过去（锁/隐的当前层吞对象的兜底）。"""
    if not created:
        return
    where = {}
    for lay in client.document_json()["layers"]:
        for n in vc_file.lay_children(lay):
            where[n["id"]] = lay["name"]
    for nid, lay in created:
        if where.get(nid) != lay:
            client.execute("layer.move", {"ids": [nid], "target": layers[lay]["id"], "place": "inside"})


def _ui_state(client):
    ins = client.inspect(0) or {}
    return {"layer": ins.get("currentLayer"), "paint": ins.get("paint")}


def _restore_ui(client, state):
    """清选择，再把当前层、新对象默认填色/描边/线宽恢复成 sync 之前（不进撤销历史）。"""
    client.execute("select.set", {"ids": []})
    if state.get("layer") is not None:
        client.execute("layer.setCurrent", {"id": state["layer"]})
    p = state.get("paint")
    if not p:
        return

    def paint(cmd, v):
        client.execute(cmd, {"none": True} if not v or v == "None" else {"color": v})
    steps = [("paint.setStroke", p.get("stroke")), ("paint.setFill", p.get("fill"))]
    if not p.get("fillActive", True):
        steps.reverse()                              # 最后设的那个成为活动的（focus）
    for cmd, v in steps:
        paint(cmd, v)
    if p.get("strokeWidth") is not None:
        client.execute("stroke.set", {"weight": p["strokeWidth"]})
