#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest（V7a）：sync_exec.apply_ops_live 对着一个**有状态的**假控制口跑，不需要 app。

假 app 模拟 VectorCraft 0.14 实测的行为：图层有锁/隐、有当前层；锁定或隐藏的当前层收不到新对象
（落进上一个可用的当前层）；锁定层里的对象照样能改；undoGroup.end {cancel} 记下回滚。
run() → 通过的检查数；失败抛 AssertionError。
"""
import json
import socketserver
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

    # 1) 非新建的 op：每种一条命令、参数与 apply_ops_json 同义；全在一个 `ep-labeling sync` 撤销组里；
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
        assert fa.log[1] == ("edit.undoGroup.begin", {"label": "ep-labeling sync"}), fa.log[1]
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
    return ok


if __name__ == "__main__":
    print("selftest_sync %d 项通过" % run())
