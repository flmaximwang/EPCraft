#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest（V7a）：sync_exec.apply_ops_live 对着一个**有状态的**假控制口跑，不需要 app。

假 app 模拟 VectorCraft 0.14 实测的行为：图层有锁/隐、有当前层；锁定或隐藏的当前层收不到新对象
（落进上一个可用的当前层）；锁定层里的对象照样能改；undoGroup.end {cancel} 记下回滚。
Crop 画板（C4）：relink / backfill 两种 op 的命令序列；gel_photo._read_all（重新裁切、框没动、照片
不见了或换过、旧文档补画板）用 magick 现画的小照片和合成文档跑，Workbench 读回那一步用桩替掉。
run() → 通过的检查数；失败抛 AssertionError。
"""
import contextlib
import io
import json
import math
import os
import socketserver
import tempfile
import threading

import sync_exec
import vc_control


# ---------------------------------------------------------------- 假 app

class _FakeApp:
    """一行请求一行回包的假控制口，带一点文档状态（图层、对象归属）。"""

    def __init__(self, layers, objects, current, fail=None, swallow=None):
        # layers = [(id, name, locked, visible)]；objects = {对象 id: 图层 id}；fail = 让哪条命令报错；
        # swallow = 新对象一律落进这个图层 id（模拟「当前层吞对象」，验兜底 layer.move）
        self.layers = {lid: {"name": nm, "locked": lk, "visible": vis} for lid, nm, lk, vis in layers}
        self.order = [lid for lid, _n, _l, _v in layers]
        self.obj = dict(objects)
        self.current, self.good = current, current
        self.next = 1000
        self.log = []                                  # [(command, params)]（engine.execute 的）
        self.fail, self.swallow = fail, swallow
        self.paint = {"appearanceItem": None, "fill": "#ffffff", "fillActive": True,
                      "stroke": "#000000", "strokeWidth": 1.0}
        self.nodes = {}                                # 新建对象的 kind（document.node 回它）
        self.relinked = []                             # links.relink 时临时 PNG 的 (路径, 当时在不在, 字节)
        self.place_xf = [1.0, 0.0, 0.0, 1.0, 3.0, 4.0]   # file.place 落下的 xf（「视图中心」，不在目标处）
        fake = self

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                req = json.loads(self.rfile.readline())
                p = req.get("params") or {}
                cmd, params = (p["command"], p.get("params") or {}) if req["method"] == "engine.execute" \
                    else (req["method"], p)
                try:
                    out = {"id": req["id"], "ok": True, "result": fake.handle(cmd, params)}
                except _Err as e:
                    out = {"id": req["id"], "ok": False, "error": str(e)}
                self.wfile.write((json.dumps(out) + "\n").encode())

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def cmds(self):
        return [c for c, _p in self.log]

    def between_group(self):
        """undoGroup.begin 与 end 之间的命令（不含两端）。"""
        names = self.cmds()
        i = names.index("edit.undoGroup.begin")
        j = names.index("edit.undoGroup.end")
        return self.log[i + 1:j]

    def _new(self):
        self.next += 1
        lay = self.swallow if self.swallow is not None else (
            self.current if self._open(self.current) else self.good)
        self.obj[self.next] = lay
        return self.next

    def _open(self, lid):
        L = self.layers[lid]
        return not L["locked"] and L["visible"]

    def handle(self, cmd, p):
        self.log.append((cmd, p))
        if cmd == self.fail:
            raise _Err("injected failure in %s" % cmd)
        if cmd == "document.inspect":
            return {"currentLayer": self.current, "paint": dict(self.paint)}
        if cmd == "document.json":
            return {"layers": [{"id": lid, "name": self.layers[lid]["name"],
                                "kind": {"type": "layer", "children": [
                                    {"id": o, "kind": {"type": "path"}} for o, l in sorted(self.obj.items()) if l == lid]}}
                               for lid in self.order]}
        if cmd == "layer.setCurrent":
            self.current = p["id"]
            if self._open(p["id"]):
                self.good = p["id"]
            return {"id": p["id"], "layer": p["id"]}
        if cmd == "layer.setProps":
            for k in ("locked", "visible"):
                if k in p:
                    self.layers[p["id"]][k] = p[k]
            return None
        if cmd in ("text.create", "shape.line", "path.create"):
            return {"id": self._new()}
        if cmd == "links.relink":
            path = p["path"]
            data = open(path, "rb").read() if os.path.exists(path) else None
            self.relinked.append((path, data is not None, data))
            return {"relinked": list(p["ids"]), "notFound": []}
        if cmd == "links.embed":
            return {"embedded": list(p["ids"]), "missing": []}
        if cmd == "artboard.new":
            return None
        if cmd == "layer.new":
            self.next += 1
            self.layers[self.next] = {"name": p["name"], "locked": p.get("locked", False),
                                      "visible": p.get("visible", True)}
            self.order.append(self.next)
            return {"id": self.next}
        if cmd == "file.place":
            nid = self._new()
            self.nodes[nid] = {"type": "image", "xf": list(self.place_xf)}
            return {"ids": [nid]}
        if cmd == "shape.rectangle":                   # 实测：rotation 绕中心、**顺时针**为正
            nid = self._new()
            x, y, w, h, r = p["x"], p["y"], p["width"], p["height"], math.radians(p.get("rotation", 0.0))
            cx, cy = x + w / 2.0, y + h / 2.0
            pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
            pts = [[cx + (px - cx) * math.cos(r) - (py - cy) * math.sin(r),
                    cy + (px - cx) * math.sin(r) + (py - cy) * math.cos(r)] for px, py in pts]
            self.nodes[nid] = {"type": "path", "live": {"shape": "rectangle"},
                               "path": {"subpaths": [{"anchors": [{"p": q} for q in pts], "closed": True}]}}
            return {"id": nid}
        if cmd == "document.node":
            return {"id": p["id"], "kind": self.nodes[p["id"]]}
        if cmd == "object.transform" and not p.get("copy"):
            k = self.nodes[p["ids"][0]]
            k["xf"] = list(sync_exec._compose(p["matrix"], k["xf"]))
            return {"ids": p["ids"]}
        if cmd == "object.transform":
            assert p.get("copy") is True
            self.next += 1
            self.obj[self.next] = self.obj[p["ids"][0]]
            return {"ids": [self.next]}
        if cmd == "object.clippingMask.make":
            lay = self.obj[p["ids"][0]]
            for i in p["ids"]:
                del self.obj[i]
            self.next += 1
            self.obj[self.next] = lay
            return {"id": self.next}
        if cmd == "layer.move":
            for i in p["ids"]:
                self.obj[i] = p["target"]
            return {"ids": p["ids"]}
        if cmd in ("edit.clear",):
            for i in p["ids"]:
                if i not in self.obj:
                    raise _Err("invalid parameters for `edit.clear`: no such object id %d" % i)
                del self.obj[i]
            return None
        if cmd == "paint.setStroke" and "ids" not in p:
            self.paint["stroke"] = p.get("color", "None")
        if cmd == "paint.setFill" and "ids" not in p:
            self.paint["fill"] = "None" if p.get("none") else p.get("color")
        return None


class _Err(Exception):
    pass


# 图层：gel(锁) analysis(锁) lanes labels profiles(锁) ep-meta(锁、隐)
LAYERS = [(1, "gel", True, True), (2, "analysis", True, True), (4, "lanes", False, True),
          (7, "labels", False, True), (8, "profiles", True, True), (9, "ep-meta", True, False)]
OBJECTS = {10: 1, 50: 1, 63: 2, 64: 2, 30: 4, 31: 4, 70: 7, 71: 7, 100: 8, 120: 9}
GEL_S = 0.25
DOCUMENT = {"layers": [
    {"id": lid, "name": nm, "locked": lk, "visible": vis, "kind": {"type": "layer", "printable": True, "children": (
        [{"id": 10, "name": "gel", "kind": {"type": "image", "key": "imgK", "width": 860, "height": 625,
                                            "xf": [GEL_S, 0.0, 0.0, GEL_S, 17.0, 17.0]}}] if nm == "gel" else [])}}
    for lid, nm, lk, vis in LAYERS]}


def _fake(**kw):
    return _FakeApp(LAYERS, OBJECTS, current=7, **kw)


# ---------------------------------------------------------------- 检查

def run():
    ok = 0

    # 1) 非新建的 op：每种一条命令、参数与 apply_ops_json 同义；全在一个 `ep-craft sync` 撤销组里；
    #    不碰图层锁；返回每种的条数；收尾清选择、恢复当前层（7）。
    fa = _fake()
    try:
        c = vc_control.Client(fa.port, timeout=5)
        ops = [{"op": "artboard", "index": 0, "rect": [0.0, 271.0, 300.5, 210.0]},
               {"op": "delete", "id": 63},
               {"op": "move", "id": 70, "dx": 1.5, "dy": -2.0},
               {"op": "rename", "id": 71, "name": "label lane 3"},
               {"op": "set_text", "id": 120, "text": "{\"a\":1}"},
               {"op": "stroke", "id": 30, "rgb": [0.6, 0.6, 0.6]},
               {"op": "reshape", "id": 51, "points": [[0, 1], [2, 1], [2, 3], [0, 3]], "closed": True}]
        s = sync_exec.apply_ops_live(c, ops, DOCUMENT)
        assert {k: v for k, v in s.items() if k != "created_ids"} == {
            "artboard": 1, "delete": 1, "move": 1, "rename": 1, "set_text": 1, "stroke": 1, "reshape": 1}, s
        assert sync_exec.describe(s) == "artboard 1, delete 1, move 1, rename 1, set_text 1, stroke 1, reshape 1"
        assert fa.log[0][0] == "document.inspect"
        assert fa.log[1] == ("edit.undoGroup.begin", {"label": "ep-craft sync"}), fa.log[1]
        assert fa.between_group() == [
            ("artboard.setProps", {"index": 0, "x": 0.0, "y": 271.0, "width": 300.5, "height": 210.0, "moveArt": False}),
            ("edit.clear", {"ids": [63]}),
            ("object.move", {"ids": [70], "dx": 1.5, "dy": -2.0}),
            ("object.setProps", {"id": 71, "name": "label lane 3"}),
            ("text.setText", {"id": 120, "text": "{\"a\":1}"}),
            ("paint.setStroke", {"ids": [30], "color": [0.6, 0.6, 0.6]}),
            ("path.setAnchors", {"id": 51, "subpaths": [{"anchors": [{"x": 0, "y": 1}, {"x": 2, "y": 1}, {"x": 2, "y": 3},
                                                                     {"x": 0, "y": 3}], "closed": True}]}),
        ], fa.between_group()
        end = fa.cmds().index("edit.undoGroup.end")
        assert fa.log[end] == ("edit.undoGroup.end", {}), fa.log[end]
        tail = fa.log[end + 1:]
        assert tail[0] == ("select.set", {"ids": []}) and tail[1] == ("layer.setCurrent", {"id": 7}), tail
        assert "layer.setProps" not in fa.cmds(), "没有新建时不该动图层锁"
    finally:
        fa.close()
    ok += 1

    # 2) 新建文字（labels，不锁）：setCurrent → 清选择 → text.create（Arial、justify 小写、颜色 0–1、基线）→ 起名。
    fa = _fake()
    try:
        c = vc_control.Client(fa.port, timeout=5)
        sp = {"kind": "text", "layer": "labels", "name": "label lane 5", "text": "?", "size": 8.0,
              "x": 40.0, "y": 300.0, "justify": "Center", "rgb": (0, 0, 0)}
        s = sync_exec.apply_ops_live(c, [{"op": "create", "layer": "labels", "spec": sp}], DOCUMENT)
        nid = s["created_ids"][0]
        assert fa.between_group() == [
            ("layer.setCurrent", {"id": 7}),
            ("select.set", {"ids": []}),
            ("text.create", {"x": 40.0, "y": 300.0, "text": "?", "size": 8.0, "font": "Arial", "justify": "center",
                             "color": [0, 0, 0], "charAlign": "romanBaseline"}),
            ("object.setProps", {"id": nid, "name": "label lane 5"}),
            ("document.json", {}),
        ], fa.between_group()
        assert fa.obj[nid] == 7 and "layer.move" not in fa.cmds()
    finally:
        fa.close()
    ok += 1

    # 3) 新建进锁定层（analysis 的线、profiles 的折线）：先解锁、建、起名、上色（无填充）、最后锁回；
    #    新对象真的落在目标层（假 app 里锁定的当前层收不到对象 → 没解锁就会落错层）；
    #    隐藏的目标层临时显示、再藏回去。
    fa = _fake()
    try:
        c = vc_control.Client(fa.port, timeout=5)
        ops = [{"op": "create", "layer": "analysis", "spec": {
                   "kind": "line", "layer": "analysis", "name": "peak 3", "a": (1.0, 2.0), "b": (5.0, 2.0),
                   "rgb": (0.1, 0.6, 0.1), "width": 0.5}},
               {"op": "create", "layer": "profiles", "spec": {
                   "kind": "polyline", "layer": "profiles", "name": "curve 3", "points": [(0, 0), (1, 2), (3, 1)],
                   "rgb": (0, 0, 0), "width": 0.6}},
               {"op": "create", "layer": "ep-meta", "spec": {
                   "kind": "text", "layer": "ep-meta", "name": "ep-meta", "text": "{}", "size": 4.0,
                   "x": 0.0, "y": 0.0, "justify": "Left", "rgb": (0, 0, 0)}}]
        s = sync_exec.apply_ops_live(c, ops, DOCUMENT)
        line_id, poly_id, meta_id = s["created_ids"]
        g = fa.between_group()
        names = [cm for cm, _p in g]
        assert g[:3] == [("layer.setProps", {"id": 2, "locked": False}),
                         ("layer.setProps", {"id": 9, "locked": False, "visible": True}),
                         ("layer.setProps", {"id": 8, "locked": False})], g[:3]
        assert g[-3:] == [("layer.setProps", {"id": 2, "locked": True}),
                          ("layer.setProps", {"id": 9, "locked": True, "visible": False}),
                          ("layer.setProps", {"id": 8, "locked": True})], g[-3:]
        assert ("shape.line", {"x1": 1.0, "y1": 2.0, "x2": 5.0, "y2": 2.0}) in g
        assert ("path.create", {"anchors": [{"x": 0, "y": 0}, {"x": 1, "y": 2}, {"x": 3, "y": 1}], "closed": False}) in g
        for nid, nm, rgb, w in ((line_id, "peak 3", [0.1, 0.6, 0.1], 0.5), (poly_id, "curve 3", [0, 0, 0], 0.6)):
            assert ("object.setProps", {"id": nid, "name": nm}) in g
            assert ("paint.setFill", {"ids": [nid], "none": True}) in g
            assert ("paint.setStroke", {"ids": [nid], "color": rgb}) in g
            assert ("stroke.set", {"ids": [nid], "weight": w}) in g
        assert ("object.setProps", {"id": meta_id, "name": "ep-meta"}) in g
        assert names.index("layer.setCurrent") > names.index("layer.setProps")
        assert (fa.obj[line_id], fa.obj[poly_id], fa.obj[meta_id]) == (2, 8, 9), "新对象没落进目标层"
        assert all(fa.layers[i]["locked"] for i in (2, 8, 9)) and not fa.layers[9]["visible"], "图层没锁回/藏回"
        assert "layer.move" not in names
    finally:
        fa.close()
    ok += 1

    # 4) 剪切组：复制胶面图（object.transform copy，矩阵把源 xf 变成目标 [s,0,0,s,ix,iy]）→ 矩形 path →
    #    clippingMask.make [矩形, 图] → 组起名；gel 层先解锁后锁回。
    fa = _fake()
    try:
        c = vc_control.Client(fa.port, timeout=5)
        sp = {"kind": "clip", "layer": "gel", "name": "gel seg 2", "rect": (100.0, 300.0, 40.0, 177.0),
              "image": (80.0, 300.0, GEL_S), "key": "imgK", "px": (860, 625)}
        s = sync_exec.apply_ops_live(c, [{"op": "create", "layer": "gel", "spec": sp}], DOCUMENT)
        g = fa.between_group()
        tr = next(p for cm, p in g if cm == "object.transform")
        assert tr["ids"] == [10] and tr["copy"] is True
        a, b, cc, d, e, f = tr["matrix"]
        src = (GEL_S, 0.0, 0.0, GEL_S, 17.0, 17.0)
        got = (a * src[0] + cc * src[1], b * src[0] + d * src[1], a * src[2] + cc * src[3], b * src[2] + d * src[3],
               a * src[4] + cc * src[5] + e, b * src[4] + d * src[5] + f)
        assert all(abs(x - y) < 1e-9 for x, y in zip(got, (GEL_S, 0, 0, GEL_S, 80.0, 300.0))), got
        img_id = 1001                                    # 假 app 发的第一个新 id = 复制出的图
        rect = next(p for cm, p in g if cm == "path.create")
        assert rect == {"anchors": [{"x": 100.0, "y": 300.0}, {"x": 140.0, "y": 300.0}, {"x": 140.0, "y": 477.0},
                                    {"x": 100.0, "y": 477.0}], "closed": True}, rect
        mk = next(p for cm, p in g if cm == "object.clippingMask.make")
        assert mk["ids"][1] == img_id and len(mk["ids"]) == 2, mk
        gid = s["created_ids"][0]
        assert ("object.setProps", {"id": gid, "name": "gel seg 2"}) in g and fa.obj[gid] == 1
        assert g[0] == ("layer.setProps", {"id": 1, "locked": False}) and \
            g[-1] == ("layer.setProps", {"id": 1, "locked": True}), (g[0], g[-1])
        bad = {"layers": [dict(DOCUMENT["layers"][0], kind={"type": "layer", "children": []})] + DOCUMENT["layers"][1:]}
        try:
            sync_exec.apply_ops_live(c, [{"op": "create", "layer": "gel", "spec": sp}], bad)
            raise AssertionError("没有胶面图却没报错")
        except vc_control.VCError as e:
            assert "imgK" in str(e), e
    finally:
        fa.close()
    ok += 1

    # 5) 失败：中途一条命令报错 → undoGroup.end {cancel:true}、图层锁回、界面恢复，原错抛出；
    #    没有不带 cancel 的 end。
    fa = _fake(fail="object.move")
    try:
        c = vc_control.Client(fa.port, timeout=5)
        ops = [{"op": "create", "layer": "analysis", "spec": {
                   "kind": "text", "layer": "analysis", "name": "tag 1", "text": "1", "size": 6.0,
                   "x": 1.0, "y": 2.0, "justify": "Center", "rgb": (0.35, 0.35, 0.35)}},
               {"op": "move", "id": 70, "dx": 1.0, "dy": 0.0},
               {"op": "rename", "id": 71, "name": "never"}]
        try:
            sync_exec.apply_ops_live(c, ops, DOCUMENT)
            raise AssertionError("失败的命令没把错抛出来")
        except vc_control.VCError as e:
            assert "injected failure" in str(e) and "object.move" in str(e), e
        ends = [p for cm, p in fa.log if cm == "edit.undoGroup.end"]
        assert ends == [{"cancel": True}], ends
        assert ("object.setProps", {"id": 71, "name": "never"}) not in fa.log, "出错之后还在发命令"
        assert fa.layers[2]["locked"], "出错后图层没锁回"
        names = fa.cmds()
        assert names.index("edit.undoGroup.end") < names.index("layer.setCurrent", names.index("edit.undoGroup.end")), \
            "出错后没恢复当前层"
    finally:
        fa.close()
    ok += 1

    # 6) 兜底：新对象被别的层吞了（模拟锁/隐当前层的坑）→ 收尾 layer.move 到目标层；
    #    空 ops → 一条命令都不发；未知 op / 文档里没有的图层 → 发命令之前就报错。
    fa = _fake(swallow=7)
    try:
        c = vc_control.Client(fa.port, timeout=5)
        sp = {"kind": "line", "layer": "analysis", "name": "peak 1", "a": (0, 0), "b": (1, 0),
              "rgb": (0, 0, 0), "width": 0.5}
        s = sync_exec.apply_ops_live(c, [{"op": "create", "layer": "analysis", "spec": sp}], DOCUMENT)
        nid = s["created_ids"][0]
        assert ("layer.move", {"ids": [nid], "target": 2, "place": "inside"}) in fa.log and fa.obj[nid] == 2
        n0 = len(fa.log)
        assert sync_exec.apply_ops_live(c, [], DOCUMENT) == {"created_ids": []} and len(fa.log) == n0
        for bad_ops, word in (([{"op": "explode", "id": 1}], "explode"),
                              ([{"op": "create", "layer": "nope", "spec": dict(sp, name="x")}], "nope")):
            try:
                sync_exec.apply_ops_live(c, bad_ops, DOCUMENT)
                raise AssertionError("坏 op 没报错")
            except vc_control.VCError as e:
                assert word in str(e), e
        assert len(fa.log) == n0, "坏 op 之前就发了命令"
    finally:
        fa.close()
    ok += 1

    # 7) 默认颜色恢复：对 ids 上色会改 app 的新对象默认色 → 收尾按 sync 前的 paint 设回（活动的那个最后设）。
    fa = _fake()
    try:
        fa.paint = {"appearanceItem": None, "fill": "None", "fillActive": False, "stroke": "#1a991a", "strokeWidth": 0.5}
        c = vc_control.Client(fa.port, timeout=5)
        sync_exec.apply_ops_live(c, [{"op": "stroke", "id": 30, "rgb": [0, 0.7, 1]}], DOCUMENT)
        end = fa.cmds().index("edit.undoGroup.end")
        assert fa.log[end + 3:] == [("paint.setFill", {"none": True}), ("paint.setStroke", {"color": "#1a991a"}),
                                    ("stroke.set", {"weight": 0.5})], fa.log[end + 1:]
    finally:
        fa.close()
    ok += 1
    # 8) 新建带角度的文字：text.create 之后紧接 object.rotate（绕锚点、绝对角度，同在一个撤销组内）；
    #    角度 0 不发 rotate（旧文档的命令序列逐条不变）。
    fa = _fake()
    try:
        c = vc_control.Client(fa.port, timeout=5)
        sp = {"kind": "text", "layer": "labels", "name": "label lane 6", "text": "X", "size": 8.0,
              "x": 55.0, "y": 300.0, "justify": "Center", "rgb": (0, 0, 0), "angle": 90.0}
        s = sync_exec.apply_ops_live(c, [{"op": "create", "layer": "labels", "spec": sp}], DOCUMENT)
        nid = s["created_ids"][0]
        g = fa.between_group()
        i = [cm for cm, _p in g].index("text.create")
        assert g[i + 1] == ("object.rotate", {"ids": [nid], "angle": 90.0, "origin": [55.0, 300.0]}), g[i:i + 3]
        n0 = len(fa.log)
        sync_exec.apply_ops_live(c, [{"op": "create", "layer": "labels", "spec": dict(sp, angle=0.0)}], DOCUMENT)
        assert "object.rotate" not in fa.cmds()[n0:], "角度 0 也发了 rotate"
    finally:
        fa.close()
    ok += 1
    ok += _crop_ops()
    ok += _crop_read()
    return ok


# ---------------------------------------------------------------- Crop 画板（C4）

def _crop_ops():
    ok = 0
    # 9) relink：不管 ops 里排在哪，都在撤销组里**最先**：links.relink（临时 PNG 还在、字节就是 op 的，
    #    preserve:"transforms"）→ links.embed；之后新建的胶段照旧复制 Workbench 那张图的对象（id 10），
    #    复制品因此带上新像素；临时文件在组结束后删掉。relink 没换全 → 整组 cancel。
    fa = _fake()
    try:
        c = vc_control.Client(fa.port, timeout=5)
        sp = {"kind": "clip", "layer": "gel", "name": "gel seg 2", "rect": (100.0, 300.0, 40.0, 177.0),
              "image": (80.0, 300.0, GEL_S), "key": "imgK", "px": (860, 625)}
        ops = [{"op": "set_text", "id": 120, "text": "{}"},
               {"op": "create", "layer": "gel", "spec": sp},
               {"op": "relink", "ids": [10, 50], "png": b"\x89PNG fake"}]
        s = sync_exec.apply_ops_live(c, ops, DOCUMENT)
        g = fa.between_group()
        names = [cm for cm, _p in g]
        i = names.index("links.relink")                 # 之前只有 create 目标层的临时解锁
        assert set(names[:i]) <= {"layer.setProps"} and names[i + 1] == "links.embed", names
        assert g[i][1]["ids"] == [10, 50] and g[i][1]["preserve"] == "transforms", g[i]
        assert g[i + 1][1] == {"ids": [10, 50]}, g[i + 1]
        path, existed, data = fa.relinked[0]
        assert existed and data == b"\x89PNG fake" and path.endswith(".png"), fa.relinked
        assert not os.path.exists(path), "临时 PNG 没删"
        tr = next(p for cm, p in g if cm == "object.transform")
        assert tr["ids"] == [10] and names.index("object.transform") > names.index("links.embed")
        assert s["relink"] == 1 and sync_exec.describe(s).startswith("relink 1, "), s
    finally:
        fa.close()
    fa = _fake()
    try:
        c = vc_control.Client(fa.port, timeout=5)

        def half(cmd, p, _h=fa.handle):
            r = _h(cmd, p)
            return {"relinked": p["ids"][:1]} if cmd == "links.relink" else r
        fa.handle = half
        try:
            sync_exec.apply_ops_live(c, [{"op": "relink", "ids": [10, 50], "png": b"x"}], DOCUMENT)
            raise AssertionError("relink 没换全却没报错")
        except vc_control.VCError as e:
            assert "links.relink" in str(e), e
        assert [p for cm, p in fa.log if cm == "edit.undoGroup.end"] == [{"cancel": True}]
        assert not os.path.exists(fa.relinked[0][0]), "出错后临时 PNG 没删"
    finally:
        fa.close()
    ok += 1

    # 10) backfill：画板 → photo 层（可见、不锁、设为当前）→ file.place 预览 → object.transform 把 xf 精确
    #     拉到 image_xf（横竖缩放可以差一点点）→ 起名 photo → crop 层 → shape.rectangle（rotation = −视觉逆时针角）
    #     → 锚点读回已对上（不发 path.setAnchors）→ 无填充、绿描边 0.75、起名 crop → 最后锁 photo 层。
    fa = _fake()
    try:
        c = vc_control.Client(fa.port, timeout=5)
        a = math.radians(3.0)
        w, h = 200.0, 140.0
        cx, cy = -300.0, 150.0
        pts = [[cx + u * math.cos(a) + v * math.sin(a), cy - u * math.sin(a) + v * math.cos(a)]
               for u, v in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))]
        xf = [0.2, 0.0, 0.0, 0.20003, -420.0, 8.5]
        op = {"op": "backfill", "artboard": [-429.0, 0.0, 369.0, 318.0], "jpeg": b"\xff\xd8jpeg",
              "preview_size": [2000, 1500], "image_xf": xf, "frame_pt": pts}
        s = sync_exec.apply_ops_live(c, [op, {"op": "set_text", "id": 120, "text": "{}"}], DOCUMENT)
        g = fa.between_group()
        names = [cm for cm, _p in g]
        want = ["artboard.new", "layer.new", "layer.setCurrent", "select.set", "file.place", "document.node",
                "object.transform", "object.setProps", "layer.new", "layer.setCurrent", "select.set",
                "shape.rectangle", "document.node", "paint.setFill", "paint.setStroke", "stroke.set",
                "object.setProps", "text.setText", "document.json", "layer.setProps"]
        assert names == want, names
        assert g[0][1] == {"x": -429.0, "y": 0.0, "width": 369.0, "height": 318.0, "name": "Crop"}, g[0]
        lp, lc = [p for cm, p in g if cm == "layer.new"]
        assert lp["name"] == "photo" and lc["name"] == "crop" and all(
            q["printable"] is False and q["visible"] is True and q["locked"] is False and q["top"] for q in (lp, lc))
        img, rid = s["created_ids"]
        assert all(abs(u - v) < 1e-9 for u, v in zip(fa.nodes[img]["xf"], xf)), fa.nodes[img]["xf"]
        place = next(p for cm, p in g if cm == "file.place")
        assert place["dataBase64"] == "/9hqcGVn" and place["name"].endswith(".jpg"), place
        rect = next(p for cm, p in g if cm == "shape.rectangle")
        assert abs(rect["rotation"] + 3.0) < 1e-9 and abs(rect["width"] - w) < 1e-9, rect
        got = [q["p"] for q in fa.nodes[rid]["path"]["subpaths"][0]["anchors"]]
        assert all(abs(u[0] - v[0]) < 1e-9 and abs(u[1] - v[1]) < 1e-9 for u, v in zip(got, pts)), got
        assert ("object.setProps", {"id": img, "name": "photo"}) in g and ("object.setProps", {"id": rid, "name": "crop"}) in g
        assert ("paint.setFill", {"ids": [rid], "none": True}) in g
        assert ("paint.setStroke", {"ids": [rid], "color": [0.0, 0.75, 0.25]}) in g
        assert ("stroke.set", {"ids": [rid], "weight": 0.75}) in g
        photo_lid = [lid for lid, L in fa.layers.items() if L["name"] == "photo"][0]
        assert g[-1] == ("layer.setProps", {"id": photo_lid, "locked": True}) and fa.layers[photo_lid]["locked"]
        assert fa.obj[img] == photo_lid and fa.obj[rid] != photo_lid and "layer.move" not in names
        assert s["backfill"] == 1, s
    finally:
        fa.close()
    ok += 1
    return ok


def _crop_read():
    """gel_photo._read_all：Crop 画板的读回、重新裁切、报错与旧文档补画板（照片是 magick 现画的）。"""
    import crop
    import figure
    import gel_photo
    import vc_file
    import workbench
    from magick_io import MAGICK, _run
    ok = 0
    dpi, wpx, hpx = 127, 250, 175                      # 胶面图 250×175 px @127 dpi = 5.00 × 3.50 cm（补画板按 0.01 cm 取整预设）
    width_cm = wpx * 2.54 / dpi
    with tempfile.TemporaryDirectory() as td:
        photo = os.path.join(td, "gel.png")
        _run([MAGICK, "-size", "480x360", "xc:gray(235)", "-fill", "gray(90)",
              "-draw", "rotate 1.5 rectangle 120,70 380,252", "-fill", "gray(30)",
              "-draw", "rectangle 170,120 180,200", "-depth", "8", photo])
        args = gel_photo._default_detect_args()
        preset = {"name": "t", "width_cm": width_cm, "height_cm": hpx * 2.54 / dpi, "lane_pitch_cm": 0.0}
        with contextlib.redirect_stdout(io.StringIO()):
            embedded, _ga, st = gel_photo.gel_face(photo, preset, args, dpi)
        assert vc_file.png_size(embedded) == (wpx, hpx)
        frame_px = crop.frame_from_stage1(st["gel_box"], st["angle"], st["size0"], st["size1"])
        jpeg, psize, _k = crop.preview(photo)
        lo = figure.crop_layout(frame_px, st["size0"], psize, width_cm)
        meta0 = {"photo": photo, "dpi": dpi,
                 "crop": {"photo_size": list(st["size0"]), "photo_sha1": crop.photo_sha1(photo),
                          "frame_px": [list(p) for p in frame_px], "stage1": {}}}

        def doc(frame_pt, meta, crop_board=True):
            def text(t):
                return {"id": 120, "name": "ep-meta", "kind": {"type": "text", "xf": [1, 0, 0, 1, 0, 0],
                                                              "runs": [{"text": t, "style": {}}]}}
            gel = {"id": 10, "name": "gel", "kind": {"type": "image", "key": "K", "width": wpx, "height": hpx,
                                                    "xf": [72.0 / dpi, 0, 0, 72.0 / dpi, 17.0, 17.0]}}
            seg = {"id": 60, "name": "gel seg 1", "kind": {"type": "group", "clip": True, "children": [
                {"id": 61, "kind": {"type": "path", "clipping": True}},
                {"id": 62, "kind": {"type": "image", "key": "K", "width": wpx, "height": hpx,
                                    "xf": [72.0 / dpi, 0, 0, 72.0 / dpi, 17.0, 300.0]}}]}}
            layers = [{"id": 1, "name": "gel", "kind": {"type": "layer", "children": [gel, seg]}},
                      {"id": 9, "name": "ep-meta", "kind": {"type": "layer", "children": [
                          text(json.dumps(meta, ensure_ascii=False))]}}]
            boards = [{"name": "Figure"}, {"name": "Workbench"}, {"name": "Profiles"}]
            if crop_board:
                boards.append({"name": "Crop"})
                layers += [{"id": 20, "name": "photo", "locked": True, "kind": {"type": "layer", "children": [
                               {"id": 80, "name": "photo", "kind": {"type": "image", "key": "J", "width": psize[0],
                                                                    "height": psize[1], "xf": lo["image_xf"]}}]}},
                           {"id": 21, "name": "crop", "kind": {"type": "layer", "children": [
                               {"id": 90, "name": "crop", "kind": {"type": "path", "path": {"subpaths": [
                                   {"anchors": [{"p": list(p)} for p in frame_pt], "closed": True}]}}}]}}]
            return {"artboards": boards, "layers": layers}

        real = workbench.from_document
        wb_errs = []

        def fake_from_document(d):                       # Workbench 那一步的桩：meta 照读，错误按需注入
            meta = gel_photo._doc_meta(d)
            if wb_errs:
                raise workbench.WorkbenchError("Workbench 有 %d 处要改（什么都没动）：\n- %s"
                                               % (len(wb_errs), "\n- ".join(wb_errs)))
            return {"gel": {}, "lanes": [], "meta": meta}, []
        workbench.from_document = fake_from_document
        try:
            blobs = {"K": embedded}
            read = lambda d: gel_photo._read_all(d, blobs, dpi)          # noqa: E731

            # 11) 框没动：没有 op、没有行、分析用文档里那张图、meta 不改（幂等）
            _m, _n, cs = read(doc(lo["frame_pt"], meta0))
            assert cs["ops"] == [] and cs["lines"] == [] and cs["crop"] is None and cs["png"] is embedded, cs
            ok += 1

            # 12) 框逆时针转 1°：按新框从原图重裁（同像素尺寸、像素变了）→ relink [Workbench 的 gel, 胶段里的图]；
            #     meta 的 frame_px = 读回的新框（不取整）；摘要一行。新 op 跑在假 app 上：relink → embed 在最前。
            c0 = [sum(p[i] for p in lo["frame_pt"]) / 4.0 for i in (0, 1)]
            a = math.radians(1.0)
            rot = [[c0[0] + (x - c0[0]) * math.cos(a) + (y - c0[1]) * math.sin(a),
                    c0[1] - (x - c0[0]) * math.sin(a) + (y - c0[1]) * math.cos(a)] for x, y in lo["frame_pt"]]
            d1 = doc(rot, meta0)
            _m, _n, cs = read(d1)
            assert [op["op"] for op in cs["ops"]] == ["relink"] and cs["ops"][0]["ids"] == [10, 62], cs["ops"]
            new = cs["png"]
            assert new is cs["ops"][0]["png"] and new != embedded and vc_file.png_size(new) == (wpx, hpx)
            fp, _e = crop.read_frame(d1, meta0, hpx / float(wpx))
            assert cs["crop"]["frame_px"] == [list(p) for p in fp] and cs["crop"]["photo_sha1"] == meta0["crop"]["photo_sha1"]
            assert cs["lines"] == ["裁切框：转 +1.00°，移 +0.00/+0.00 cm，宽 ×1.000（重新裁了胶面图）"], cs["lines"]
            assert gel_photo._printable(cs["ops"])[0]["png"] == "<%d bytes>" % len(new)
            #     下一次（meta 已是新框）= 没有改动
            _m, _n, cs2 = read(doc(rot, dict(meta0, crop=cs["crop"])))
            assert cs2["ops"] == [] and cs2["png"] is embedded
            fa = _fake()
            try:
                c = vc_control.Client(fa.port, timeout=5)
                sync_exec.apply_ops_live(c, cs["ops"] + [{"op": "set_text", "id": 120, "text": "{}"}], DOCUMENT)
                assert [cm for cm, _p in fa.between_group()][:3] == ["links.relink", "links.embed", "text.setText"]
                assert fa.relinked[0][2] == new
            finally:
                fa.close()
            ok += 1

            # 13) 框动了但照片不见了 / 换过了 → WorkbenchError（Crop： 前缀，与 Workbench 的错一起列），什么都不发
            for meta, word in ((dict(meta0, photo=os.path.join(td, "nope.png")), "找不到"),
                               (dict(meta0, crop=dict(meta0["crop"], photo_sha1="000000000000")), "换过了")):
                for wbe in ([], ["window 图层应当恰好有两条横线"]):
                    wb_errs[:] = wbe
                    try:
                        read(doc(rot, meta))
                        raise AssertionError("照片不对却没报错")
                    except workbench.WorkbenchError as e:
                        msg = str(e)
                        assert msg.startswith("Workbench 有 %d 处要改（什么都没动）" % (1 + len(wbe))), msg
                        assert "- Crop：照片 " in msg and word in msg and all(w in msg for w in wbe), msg
            wb_errs[:] = ["window 图层应当恰好有两条横线"]
            bad = [list(p) for p in rot]
            bad[2] = [bad[2][0] + 4.0, bad[2][1] + 3.0]
            try:
                read(doc(bad, meta0))
                raise AssertionError("框变形了却没报错")
            except workbench.WorkbenchError as e:
                assert "Workbench 有 2 处要改" in str(e) and "- Crop：裁切框不再是矩形" in str(e), e
            wb_errs[:] = []
            ok += 1

            # 14) 旧文档（没有 Crop 画板、meta 没有 crop；胶面图是修错位之前、没有 +repage 的旧版裁的）：
            #     照片在、默认参数重裁 ≈ 文档里那张 → backfill op + 新 meta.crop（frame_px = 旧图真正裁到的那块）；
            #     文档里的图对不上 / 照片不在 → 一条提示、没有 op。
            import selftest_crop
            assert abs(st["angle"]) > 0.5, st["angle"]           # 照片要是歪的，错位才测得出来
            keep = blobs["K"]
            blobs["K"] = embedded = selftest_crop._old_crop(photo, st["angle"], st["gel_box"], width_cm,
                                                            hpx * 2.54 / dpi, dpi)
            sx, sy = crop.legacy_page_shift(st["angle"], st["size0"], st["size1"])
            assert abs(sx) + abs(sy) > 1.0, (sx, sy)
            b = st["gel_box"]
            frame_px = crop.frame_from_stage1((b[0] + sx, b[1] + sy, b[2] + sx, b[3] + sy), st["angle"],
                                              st["size0"], st["size1"])
            lo = figure.crop_layout(frame_px, st["size0"], psize, width_cm)
            meta_old = {"photo": photo, "dpi": dpi}
            _m, notes, cs = read(doc(lo["frame_pt"], meta_old, crop_board=False))
            assert [op["op"] for op in cs["ops"]] == ["backfill"] and notes == [], (cs["ops"], notes)
            bf = cs["ops"][0]
            near = lambda u, v: all(abs(x - y) < 1e-6 for x, y in zip(u, v))       # noqa: E731
            assert near(bf["image_xf"], lo["image_xf"]) and all(map(near, bf["frame_pt"], lo["frame_pt"])), bf
            assert bf["jpeg"] == jpeg
            assert bf["preview_size"] == list(psize) and abs(bf["artboard"][0] + bf["artboard"][2] + figure.BOARD_GAP) < 1e-2
            assert all(map(near, cs["crop"]["frame_px"], frame_px)) and cs["crop"]["photo_size"] == list(st["size0"])
            assert cs["crop"]["photo_sha1"] == crop.photo_sha1(photo) and cs["crop"]["stage1"]["cue"] == "dark"
            assert cs["lines"] == ["已补上 Crop 画板"] and cs["png"] is embedded
            #     补上之后的文档（框 = 刚写的）再读 = 没有改动
            _m, _n, cs2 = read(doc(bf["frame_pt"], dict(meta_old, crop=cs["crop"])))
            assert cs2["ops"] == []
            #     修错位之后裁的图（框没挪 legacy_page_shift）就对不上旧版那张：确认上面比的确实是旧裁法
            assert not gel_photo._same_gel(keep, embedded) or (sx, sy) == (0.0, 0.0)
            other = crop.crop_face(photo, st["angle"] + 2.0, st["gel_box"], width_cm, hpx * 2.54 / dpi, dpi, warn=False)
            blobs["K"] = other
            _m, notes, cs = read(doc(lo["frame_pt"], meta_old, crop_board=False))
            assert cs["ops"] == [] and cs["crop"] is None and notes == [
                "旧文档没有 Crop 画板（当时的裁切参数没记下来）；要调裁切就重新 render"], notes
            blobs["K"] = keep
            gone = os.path.join(td, "moved.png")
            _m, notes, cs = read(doc(lo["frame_pt"], dict(meta_old, photo=gone), crop_board=False))
            assert cs["ops"] == [] and notes == ["旧文档没有 Crop 画板（照片 %s 不在了）；要调裁切就重新 render" % gone]
            ok += 1
        finally:
            workbench.from_document = real
    return ok


if __name__ == "__main__":
    print("selftest_sync %d 项通过" % run())
