#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_marker_redetect：ep-ugj「一键重检 marker ladder」的独立验收套件（测试方只写这一个文件）。

验收对象（bd show ep-ugj 的 7 条标准）
    1. sync 侧新增 `--reset-marker`（+ `--lane N`）：按**当前 Crop 框 / 当前泳道几何**在 marker 道重取
       显著度前 N 名，直接重建 band <MW> 线；名字按 markers.json 的标签顺序。
    2. 打印新旧 y 对照 + 闸门诊断（picked/need、断崖比值、第 N 名显著度），过闸门/低置信照现有语义标记，
       并写 ep-meta.marker_confidence。
    3. 一条撤销步；随后自动 sync（Figure 标签、Profiles、fig.png、fig.mw.json 一起更新）。
    4. 幂等：检测结果与现有线一致时 0 个改动。
    5. 无 --lane 时用 ep-meta.marker_lane；候选道全不过闸门时取断崖比值最好的一条，且不拒绝执行。
    6. --dry-run 只打印不写。
    7. selftest 覆盖：一次重检、同结果幂等、低置信分支。

做法：全是黑盒。夹具是**内存里现合成**的 .vectorcraft（1 张合成胶面 PNG + Workbench + Figure /
Profiles 画板），假控制口（`_FakeVC`）按 VectorCraft 0.14 的命令语义改内存文档、save 时写盘，
所以重检之后还能用 `vc_file.read` + `workbench.from_document` 原样读回来核对。不连 app、不读用户文件。

跑：PYTHONPATH=src python3 -m tests.selftest_marker_redetect
    （写这套测试时还用手改的参考实现 + 6 个变异体验过：每条断言都会在对应的破坏下变红。）
"""
import contextlib
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import zlib

from epcraft import figure
from epcraft import gel_session
from epcraft import gel_photo
from epcraft import profiles
from epcraft import vc_control
from epcraft import vc_file
from epcraft import workbench


# ---------------------------------------------------------------- 合成胶面图

PPC = 100.0                       # px / cm（= 254 dpi，省得 dpi 与 px_per_cm 两头算）
W_CM, H_CM = 5.0, 3.5
DPI = 254
LANES = [(0.10, 1.7333), (1.7333, 3.3667), (3.3667, 5.10)]
WIN = (0.15, 3.35)
CENTRES = [round((a + b) / 2.0, 4) for a, b in LANES]

# marker 道（泳道 1）：3 条主带 + 2 条浅带 —— 第 3 名 85 对第 4 名 20 → 断崖 4.25×，过闸门
MARK_PASS = [(0.80, 95, 0.05, 0.04), (1.60, 110, 0.05, 0.04), (2.40, 85, 0.05, 0.04),
             (0.45, 20, 0.04, 0.03), (1.15, 18, 0.04, 0.03)]
# 低置信：前 5 名显著度差不多，第 3 名 / 第 4 名 ≈ 1.0× —— 断崖不够
MARK_FLAT = [(0.80, 95, 0.05, 0.04), (1.60, 100, 0.05, 0.04), (2.40, 90, 0.05, 0.04),
             (0.45, 88, 0.04, 0.03), (1.15, 92, 0.04, 0.03), (2.00, 87, 0.04, 0.03)]
# 三道全不过闸门的夹具：泳道 1 靠「断崖不够」（ratio 1.55，三道里最高），泳道 2/3 靠「显著度下限」
# → 验「候选道全不过闸门时取断崖比值最好的一条」且不拒绝执行。
MARK_NOBODY = [(0.80, 90, 0.05, 0.04), (1.60, 85, 0.05, 0.04), (2.40, 88, 0.05, 0.04),
               (0.45, 55, 0.04, 0.03), (1.15, 54, 0.04, 0.03)]
FAINT2 = [(0.70, 1.2, 0.05, 0.04), (1.55, 1.0, 0.05, 0.04), (2.55, 1.1, 0.05, 0.04)]
FAINT3 = [(0.90, 1.0, 0.05, 0.04), (1.70, 1.2, 0.05, 0.04), (2.60, 1.1, 0.05, 0.04)]
SAMPLE = [(1.10, 60, 0.04, 0.03), (1.95, 80, 0.04, 0.03), (2.70, 55, 0.04, 0.03)]
SAMPLE2 = [(0.70, 70, 0.04, 0.03), (1.55, 50, 0.04, 0.03), (2.55, 90, 0.04, 0.03)]
LABELS = (("250 kD", 250.0), ("150 kD", 150.0), ("100 kD", 100.0))
STALE_SHIFT_CM = 0.30          # 夹具里旧 band 线相对当前极小的偏移（远大于 SNAP_CM=0.06 → 吸不上）
PAD_CM = 0.30                  # Crop 夹具的照片在胶面四周留的空白（框要挪得动）


def _chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def png_bytes(w, h, rgb_rows=None, dpi=DPI):
    """8-bit RGB PNG（zlib）；rgb_rows = 每行一个 (r,g,b)，缺省白。带 pHYs，便于核对 dpi。"""
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        c = rgb_rows[y] if rgb_rows else (255, 255, 255)
        raw += bytes(c) * w
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _chunk(b"pHYs", struct.pack(">IIB", int(round(dpi / 0.0254)), int(round(dpi / 0.0254)), 1))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))


def gel_png(marker, sample=SAMPLE, sample2=SAMPLE2, bg=235, marker_lane=1, pad_cm=0.0):
    """合成一张胶面图：列恒定的灰度行 → lane_profile 读回来与这里画的一模一样。
    marker_lane = 梯子画在第几道（其余道画成样品带），换道重检的用例靠它。
    pad_cm > 0 → 四周各留这么多 cm 的空白（只给「框挪得动」的夹具用：带仍画在同一位置）。"""
    W, H = int(round((W_CM + 2 * pad_cm) * PPC)), int(round((H_CM + 2 * pad_cm) * PPC))
    off = int(round(pad_cm * PPC))                    # 带的 y 相对网格的偏移
    grid = [[bg] * W for _ in range(H)]

    def draw(bands, lane_idx):
        a, b = LANES[lane_idx]
        lo, hi = off + max(0, int(round(a * PPC))), min(W, off + int(round(b * PPC)))
        for (y0, depth, half, soft) in bands:
            for py in range(H):
                dy = abs((py + 0.5 - off) / PPC - y0)
                if dy > half + soft:
                    continue
                f = 1.0 if dy <= half else 1.0 - (dy - half) / soft
                g = int(round(bg - depth * f))
                row = grid[py]
                for px in range(lo, hi):
                    if g < row[px]:
                        row[px] = g

    others = {1: sample, 2: sample2, 3: sample}
    for lane in (1, 2, 3):
        draw(marker if lane == marker_lane else others[lane], lane - 1)
    return _png_from_grid(grid)


def _png_from_grid(grid):
    """灰度网格 → 8-bit RGB PNG（每行先写 filter 字节 0）。"""
    W, H = len(grid[0]), len(grid)
    raw = bytearray()
    for row in grid:
        raw.append(0)
        for v in row:
            raw += bytes((v, v, v))
    return _png_blob(W, H, bytes(raw))


def _png_blob(W, H, raw):
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            + _chunk(b"pHYs", struct.pack(">IIB", int(round(DPI / 0.0254)), int(round(DPI / 0.0254)), 1))
            + _chunk(b"IDAT", zlib.compress(raw, 6))
            + _chunk(b"IEND", b""))


def detected(marker, lane=1, sample=SAMPLE, sample2=SAMPLE2):
    """（测试的独立算法）对合成图算某条道的 3 个最强局部极小 —— 用产品自己的剖面/叫带。"""
    png = gel_png(marker, sample=sample, sample2=sample2, marker_lane=lane)
    w, h, gray = profiles.gel_gray(png)
    a, b = LANES[lane - 1]
    ys, gs = profiles.lane_profile(gray, w, h, PPC, a, b, WIN[0], WIN[1])
    rows, info = profiles.pick_marker(ys, gs, PPC, len(LABELS))
    return rows, info


def lane_gate(png, lane):
    """量这张图上第 lane 道的闸门（梯子画在哪道由 gel_png 决定，这里只负责「量」）。"""
    w, h, gray = profiles.gel_gray(png)
    a, b = LANES[lane - 1]
    ys, gs = profiles.lane_profile(gray, w, h, PPC, a, b, WIN[0], WIN[1])
    rows, info = profiles.pick_marker(ys, gs, PPC, len(LABELS))
    return rows, info


def _conf(ok, picked, need, gate=None, ratio=None, nth=None, nxt=None):
    return {"ok": ok, "picked": picked, "need": need, "gate": gate,
            "ratio": ratio, "nth": nth, "next": nxt, "notes": []}


def gated(marker, lane=1):
    """合成图 + 该道闸门结果 → 初始 ep-meta.marker_confidence（和 render 写的同形）。"""
    rows, info = detected(marker, lane)
    return _conf(bool(info["ok"]), len(rows), len(LABELS), info["reason"] or None,
                 info.get("ratio"), info.get("nth"), info.get("next"))


def build_doc(path, marker=MARK_PASS, band_ys=None, confidence=None, marker_lane=1,
              lane_names=("M", "1", "2"), labels=LABELS, layout="stack", ladder_lane=None,
              sample=SAMPLE, sample2=SAMPLE2):
    """写一份与新 render 同形的文档（无 Crop 画板 = 旧文档；照片路径故意不存在 → 只提示不补画板）。

    band_ys = 现有 band <MW> 线的 y（cm，自上而下按 labels 顺序）；缺省 = 当前图上真正叫出的位置。
    """
    ladder = ladder_lane or marker_lane
    png = gel_png(marker, sample=sample, sample2=sample2, marker_lane=ladder)
    rows, info = detected(marker, ladder, sample=sample, sample2=sample2)
    if band_ys is None:          # 默认是「挪过 Crop 之后」的旧线：整体偏低 3 mm（3 条都吸不上）
        band_ys = [round(r["y_cm"] + STALE_SHIFT_CM, 4) for r in rows][:len(labels)]
    doc = vc_file.new_doc(os.path.splitext(os.path.basename(path))[0])
    key, gw, gh = vc_file.add_image(doc, png)
    meta = {"skill": "ep-craft", "skill_version": "0.1.0", "photo": "/nonexistent/photo.jpg",
            "dpi": DPI, "gel_preset": "selftest-marker-redetect", "profiles_layout": layout,
            "lane_names": list(lane_names), "lane_angles": [0.0] * len(LANES),
            "marker_lane": marker_lane,
            "marker_labels": [{"text": t, "mws": [m]} for t, m in labels],
            "prev_centres_cm": CENTRES,
            "marker_confidence": confidence if confidence is not None else gated(marker, ladder)}
    model = {"gel": {"width_cm": W_CM, "height_cm": H_CM},
             "window": {"top_cm": WIN[0], "bottom_cm": WIN[1]},
             "lanes": [{"left_cm": a, "right_cm": b, "trimmed": False} for a, b in LANES],
             "bands": [{"mw": m, "y_cm": round(float(y), 4)} for (_t, m), y in zip(labels, band_ys)],
             "marker_lane": marker_lane, "meta": meta}
    raw = {}
    report = gel_session.mark_low_confidence(
        model, profiles.analyze(model, png, list(lane_names), overrides={}, raw=raw))
    meta.update(gel_session.meta_state(model, raw))      # 与 render 写下来的完全同形（bands / peak_auto）
    doc = figure.build_document(model, report, png, os.path.splitext(os.path.basename(path))[0])
    vc_file.write(doc, path)
    return path


def build_crop_doc(path, workdir, stale=STALE_SHIFT_CM):
    """写一份**带真 Crop 画板**的文档：照片就是那张合成胶面图（整张 = 胶面），ep-meta 记着上次的
    crop.frame_px（= 整张照片），文档里的框也摆在那儿。Crop 那条验收要的就是这种真夹具：
    sync 会读回 Crop 框、和 ep-meta 的 frame_px 比、必要时按框重裁 —— 照片路径必须是活的。

    → (path, photo, 旧 bands 的 y)
    """
    from epcraft import crop
    photo = os.path.join(workdir, "photo.png")
    png = gel_png(MARK_PASS, sample=SAMPLE, sample2=SAMPLE2, marker_lane=1, pad_cm=PAD_CM)
    with open(photo, "wb") as f:                 # 照片 = 胶面 + 四周 0.3 cm 空白（框才挪得动）
        f.write(png)
    pw, ph = vc_file.png_size(png)
    _check(abs(pw * 2.54 / DPI - (W_CM + 2 * PAD_CM)) < 0.01,
           "合成照片尺寸 %r 与 dpi/cm 对不上" % (vc_file.png_size(png),))
    off_px = int(round(PAD_CM * PPC))            # 胶面在照片里的偏移
    w0, h0 = int(round(W_CM * DPI / 2.54)), int(round(H_CM * DPI / 2.54))
    face = crop.crop_face(photo, 0.0, (off_px, off_px, off_px + w0, off_px + h0), W_CM, H_CM,
                          DPI, warn=False)
    _check(vc_file.png_size(face) == (w0, h0), "裁出的胶面图尺寸不对：%r" % (vc_file.png_size(face),))
    jpeg, psize, _k = crop.preview(photo)
    frame_px = [[off_px, off_px], [off_px + w0, off_px], [off_px + w0, off_px + h0], [off_px, off_px + h0]]
    lo = figure.crop_layout(frame_px, (w0, h0), psize, W_CM)
    rows, _info = detected(MARK_PASS, 1)
    band_ys = [round(r["y_cm"] + stale, 4) for r in rows][:len(LABELS)]
    meta = {"skill": "ep-craft", "skill_version": "0.1.0", "photo": photo, "dpi": DPI,
            "gel_preset": "selftest-marker-redetect", "profiles_layout": "stack",
            "lane_names": ["M", "1", "2"], "lane_angles": [0.0] * len(LANES), "marker_lane": 1,
            "marker_labels": [{"text": t, "mws": [m]} for t, m in LABELS],
            "prev_centres_cm": CENTRES, "marker_confidence": gated(MARK_PASS, 1),
            "crop": {"photo_size": [pw, ph], "photo_sha1": crop.photo_sha1(photo),
                     "frame_px": [list(p) for p in frame_px], "stage1": {}}}
    model = {"gel": {"width_cm": W_CM, "height_cm": H_CM},
             "window": {"top_cm": WIN[0], "bottom_cm": WIN[1]},
             "lanes": [{"left_cm": a, "right_cm": b, "trimmed": False} for a, b in LANES],
             "bands": [{"mw": m, "y_cm": round(float(y), 4)} for (_t, m), y in zip(LABELS, band_ys)],
             "marker_lane": 1, "meta": meta}
    raw = {}
    report = gel_session.mark_low_confidence(
        model, profiles.analyze(model, face, ["M", "1", "2"], overrides={}, raw=raw))
    meta.update(gel_session.meta_state(model, raw))
    doc = figure.build_document(model, report, face, os.path.splitext(os.path.basename(path))[0],
                                layout="stack", crop={"jpeg": jpeg, "preview_size": list(psize),
                                                      "photo_size": [pw, ph],
                                                      "frame_px": [list(p) for p in frame_px]})
    vc_file.write(doc, path)
    return path, photo, band_ys


def _move_crop_frame(path, dx_cm, dy_cm=0.0):
    """把文档里 Crop 画板的框整体平移多少 cm（= 用户在 Workbench 上挪了一下框；胶面图还是旧的那张）。

    框在文档里是 pt，而 pt 与照片像素严格成比例（figure.crop_layout: s = width_cm×PT / W0），
    所以「挪 dx cm」= 挪 dx×PT_PER_CM pt，读回（crop.read_frame）就是 dx×DPI/2.54 原图 px。"""
    from epcraft import crop
    top, blobs = vc_file.read(path)
    doc = top["document"]
    node = vc_file.lay_children(vc_file.layer_by_name(doc, "crop"))[0]
    dx_pt, dy_pt = dx_cm * vc_file.PT_PER_CM, dy_cm * vc_file.PT_PER_CM
    for a in node["kind"]["path"]["subpaths"][0]["anchors"]:
        a["p"] = [a["p"][0] + dx_pt, a["p"][1] + dy_pt]
    vc_file.write({"top": top, "blobs": blobs, "tpl": vc_file._template(vc_file.TEMPLATE),
                   "next": max([n["id"] for _l, n in vc_file.walk(doc)]
                               + [l["id"] for l in doc["layers"]] + [0]) + 1}, path)
    meta = vc_file.doc_meta(vc_file.read(path)[0]["document"])[1]
    frame, errs = crop.read_frame(doc, meta, H_CM * PPC / float(W_CM * PPC))
    _check(errs == [], "挪过框之后 Crop 读不回来了：%r" % (errs,))
    return frame, meta


# ---------------------------------------------------------------- 假控制口

class _Err(Exception):
    """假 app 拒绝某条命令（等价于控制口回 ok=false）。"""


class _FakeVC:
    """VectorCraft 0.14 控制口的替身：命令按实测语义落在内存文档上，save 时写盘。

    只实现 sync（以及 marker 重检）会用到的那批命令；别的命令直接报错 —— 让越界的路径暴露出来，
    而不是被悄悄吞掉。图层语义按实测来：新对象落进**当前层**（锁定/隐藏的当前层收不到，落到上一个
    可用层），`layer.move` 再把它挪进目标层（sync_exec 的落层核对依赖这一条）。
    """

    def __init__(self, path, dpi=DPI):
        self.path = os.path.abspath(path)
        top, blobs = vc_file.read(path)
        self.doc = {"top": top, "blobs": blobs, "tpl": vc_file._template(vc_file.TEMPLATE),
                    "next": max([n["id"] for _l, n in vc_file.walk(top["document"])]
                                + [l["id"] for l in top["document"]["layers"]] + [0]) + 1}
        self.log = []
        self.paint = {"appearanceItem": None, "fill": "#ffffff", "fillActive": True,
                      "stroke": "#000000", "strokeWidth": 1.0}
        self.current = next(l["id"] for l in top["document"]["layers"] if l["name"] == "labels")
        self.good = self.current
        self.relinked = []
        self.opened = []
        self.saved = []
        self.exports = []

    # ---- 测试用的观察口
    def cmds(self):
        return [c for c, _p in self.log]

    def group_labels(self):
        return [p.get("label") for c, p in self.log if c == "edit.undoGroup.begin"]

    def between_group(self, i=0):
        names = self.cmds()
        begins = [k for k, c in enumerate(names) if c == "edit.undoGroup.begin"]
        ends = [k for k, c in enumerate(names) if c == "edit.undoGroup.end"]
        if not begins or len(ends) < len(begins):
            return []
        return self.log[begins[i] + 1:ends[i]]

    # ---- 文档小工具
    def _lay(self, lid):
        for lay in self.doc["top"]["document"]["layers"]:
            if lay["id"] == lid:
                return lay
        raise _Err("no layer %r" % (lid,))

    def _find(self, nid, what="object"):
        for _lay, node in vc_file.walk(self.doc["top"]["document"]):
            if node["id"] == nid:
                return node
        raise _Err("no %s %r" % (what, nid))

    def _emit(self, node):
        lay = self._lay(self.current) if self._is_open(self.current) else self._lay(self.good)
        vc_file.lay_children(lay).append(node)
        return {"id": node["id"]}

    def _is_open(self, lid):
        vis, locked, _pr = vc_file.layer_flags(self._lay(lid))
        return vis and not locked

    # ---- Client 接口
    def call(self, method, params=None):
        self.log.append((method, params or {}))
        if method == "ui.inspect":
            return {"documents": [{"title": os.path.basename(self.path), "dirty": False}]}
        if method == "ui.dialog.cancel":
            return None
        raise _Err("unsupported call %r" % (method,))

    def inspect(self, depth=0):
        self.log.append(("document.inspect", {"depth": depth}))
        return {"path": self.path, "currentLayer": self.current, "paint": dict(self.paint),
                "artboards": [{"name": ab.get("name"),
                               "width": ab["rect"]["x1"] - ab["rect"]["x0"],
                               "height": ab["rect"]["y1"] - ab["rect"]["y0"]}
                              for ab in self.doc["top"]["document"]["artboards"]]}

    def document_json(self):
        self.log.append(("document.json", {}))
        return self.doc["top"]["document"]

    def open(self, path):
        self.log.append(("file.open", {"path": path}))
        self.opened.append(path)
        return {"warnings": []}

    def clear_dialogs(self, tries=6):
        return None

    def batch(self, commands, label="epcraft"):
        raise _Err("command.batch 不该在重检里出现")

    @contextlib.contextmanager
    def undo_group(self, label="epcraft sync"):
        self.execute("edit.undoGroup.begin", {"label": label})
        try:
            yield self
        except Exception:
            self.execute("edit.undoGroup.cancel", {})
            raise
        self.execute("edit.undoGroup.end", {})

    def save(self, path, timeout=60.0):
        self.log.append(("document.save", {"path": path}))
        vc_file.write(self.doc, path)
        self.saved.append(path)
        return self.doc["top"], self.doc["blobs"]

    def export_png(self, path, artboard, dpi, background="white", timeout=60.0):
        self.log.append(("document.export", {"path": path, "artboard": artboard, "ppi": dpi}))
        ab = self.inspect(0)["artboards"][artboard]
        w, h = int(round(ab["width"] * dpi / 72.0)), int(round(ab["height"] * dpi / 72.0))
        with open(path, "wb") as f:
            f.write(png_bytes(w, h, dpi=dpi))
        self.exports.append((path, w, h, dpi))
        return w, h, dpi

    # ---- 命令
    def execute(self, command, params=None):
        p = params or {}
        self.log.append((command, p))
        fn = getattr(self, "_c_" + command.replace(".", "_").replace("-", "_"), None)
        if fn is None:
            raise _Err("假控制口没实现 %s（重检不该走到这里）" % command)
        return fn(p)

    def _c_document_activate(self, p):
        return None

    def _c_select_set(self, p):
        return None

    def _c_edit_undoGroup_begin(self, p):
        return None

    def _c_edit_undoGroup_end(self, p):
        return None

    def _c_edit_undoGroup_cancel(self, p):
        return None

    def _c_document_node(self, p):
        return {"node": self._find(p["id"])}

    def _c_layer_setCurrent(self, p):
        self.current = p["id"]
        if self._is_open(p["id"]):
            self.good = p["id"]
        return {"id": p["id"]}

    def _c_layer_setProps(self, p):
        lay = self._lay(p["id"])
        for k in ("locked", "visible"):
            if k in p:
                lay[k] = p[k]
        return None

    def _c_layer_new(self, p):
        lay = vc_file.add_layer(self.doc, p.get("name", ""),
                                printable=p.get("printable", True),
                                visible=p.get("visible", True),
                                locked=p.get("locked", False),
                                color=p.get("color", 0))
        return {"id": lay["id"]}

    def _c_layer_move(self, p):
        target = self._lay(p["target"])
        for nid in p["ids"]:
            node = self._find(nid)
            for lay in self.doc["top"]["document"]["layers"]:
                kids = vc_file.lay_children(lay)
                hit = [n for n in kids if n is node]
                if hit:
                    kids.remove(node)
                    break
            vc_file.lay_children(target).append(node)
        return None

    def _c_edit_clear(self, p):
        ids = set(p["ids"])
        for lay in self.doc["top"]["document"]["layers"]:
            kids = vc_file.lay_children(lay)
            kids[:] = [n for n in kids if n["id"] not in ids]

    def _c_text_create(self, p):
        justify = {"left": "Left", "center": "Center", "right": "Right"}.get(
            str(p.get("justify", "Center")).lower(), "Center")
        n = vc_file.text(self.doc, "", p["x"], p["y"], p["text"], size=p.get("size", 8.0),
                         font="Arial", rgb=p.get("color", (0, 0, 0)), justify=justify)
        return self._emit(n)

    def _c_shape_line(self, p):
        n = vc_file.line(self.doc, "", (p["x1"], p["y1"]), (p["x2"], p["y2"]))
        return self._emit(n)

    def _c_path_create(self, p):
        pts = [(a["x"], a["y"]) for a in p["anchors"]]
        n = vc_file.polyline(self.doc, "", pts)
        if p.get("closed"):
            n["kind"]["path"]["subpaths"][0]["closed"] = True
        return self._emit(n)

    def _c_object_move(self, p):
        for nid in p["ids"]:
            node = self._find(nid)
            k = node["kind"]
            if k.get("xf"):
                k["xf"][4] += p["dx"]
                k["xf"][5] += p["dy"]
            for sp in (k.get("path") or {}).get("subpaths") or []:
                for a in sp["anchors"]:
                    a["p"][0] += p["dx"]
                    a["p"][1] += p["dy"]
        return None

    def _c_object_setProps(self, p):
        self._find(p["id"])["name"] = p.get("name", "")
        return None

    def _c_text_setText(self, p):
        node = self._find(p["id"])
        node["kind"]["runs"][0]["text"] = p["text"]
        return None

    def _c_paint_setStroke(self, p):
        for nid in p.get("ids") or []:
            node = self._find(nid)
            st = next(it for it in node["appearance"]["items"] if it["kind"] == "stroke")
            st["paint"] = ({"type": "none"} if p.get("none")
                           else {"type": "solid", "color": {"model": "rgb", "r": p["color"][0],
                                                            "g": p["color"][1], "b": p["color"][2]}})
        return None

    def _c_paint_setFill(self, p):
        if "ids" not in p:
            return None
        for nid in p.get("ids") or []:
            node = self._find(nid)
            for it in node["appearance"]["items"]:
                if it["kind"] == "fill":
                    it["paint"] = {"type": "none"} if p.get("none") else it["paint"]
        return None

    def _c_stroke_set(self, p):
        for nid in p.get("ids") or []:
            node = self._find(nid)
            st = next(it for it in node["appearance"]["items"] if it["kind"] == "stroke")
            st["width"] = p["weight"]
        return None

    def _c_path_setAnchors(self, p):
        node = self._find(p["id"])
        node["kind"]["path"]["subpaths"] = [{"anchors": [{"p": [a["x"], a["y"]]} for a in sp["anchors"]],
                                             "closed": sp.get("closed", False)}
                                            for sp in p["subpaths"]]
        return None

    def _c_artboard_setProps(self, p):
        ab = self.doc["top"]["document"]["artboards"][p["index"]]
        if "x" in p:
            ab["rect"] = {"x0": p["x"], "y0": p["y"], "x1": p["x"] + p["width"], "y1": p["y"] + p["height"]}
        return None

    def _c_links_relink(self, p):
        data = None
        if os.path.exists(p["path"]):
            with open(p["path"], "rb") as f:
                data = f.read()
        self.relinked.append((p["path"], data is not None, data))
        return {"relinked": list(p["ids"]), "notFound": []}

    def _c_links_embed(self, p):
        return {"embedded": list(p["ids"]), "missing": []}


class _FakeVC2(_FakeVC):
    """带真 relink 的假控制口：links.relink 把新胶面图字节嵌进文档（id / xf 不变，images key 按内容
    换新 —— 与真实 VectorCraft 0.14 一致，sync_exec 的模块注释写着「relink 之后 images key 变了」）。
    Crop 那条验收要的正是这一点：重裁之后**下一次**读回必须看到新像素，否则文档永远不是不动点。"""

    def _c_links_relink(self, p):
        r = super()._c_links_relink(p)
        data = self.relinked[-1][2]
        if data is not None:
            ex = self.execute
            for i in p["ids"]:
                node = self._find(i)
                if node is None or node["kind"].get("type") != "image":
                    continue
                old_key = node["kind"]["key"]
                key = "img" + __import__("hashlib").sha1(data).hexdigest()[:16]
                self.doc["blobs"][key] = data
                self.doc["top"]["images"][key] = {"mime": "image/png"}
                node["kind"]["key"] = key
                node["kind"]["width"], node["kind"]["height"] = vc_file.png_size(data)
                if old_key != key and old_key not in [n["kind"].get("key") for _l, n in
                                                     vc_file.walk(self.doc["top"]["document"])]:
                    self.doc["top"]["images"].pop(old_key, None)
                    self.doc["blobs"].pop(old_key, None)
        return r

    def _find(self, i):
        for _l, n in vc_file.walk(self.doc["top"]["document"]):
            if n.get("id") == i:
                return n
        return None


# ---------------------------------------------------------------- 跑一遍 CLI

def _run(argv, fake):
    """把 vc_control.connect 换成本地假控制口，跑 gel_photo.main → (rc, stdout, stderr)。"""
    real = vc_control.connect
    vc_control.connect = lambda port=None, path=None, launch=True, **kw: (fake, False)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = gel_photo.main(list(argv), canvas=True)
    except SystemExit as e:                       # argparse 用法错误
        return e.code, out.getvalue(), err.getvalue()
    finally:
        vc_control.connect = real
    return rc, out.getvalue(), err.getvalue()


def _sync(path, fake, *flags):
    return _run(["workbench", "sync", path, "--port", "1"] + list(flags), fake)


def _readback(path):
    """文档 → (model, meta)；顺带核对 Workbench 读回不报错（结构合法才有 band 线）。"""
    top, _blobs = vc_file.read(path)
    doc = top["document"]
    model, _notes = workbench.from_document(doc)
    return model, vc_file.doc_meta(doc)[1]


def _bands(path):
    model, _meta = _readback(path)
    return [(b["mw"], b["y_cm"]) for b in model["bands"]], model


def _band_lines(doc):
    """marker bands 图层里的 (名字, y_cm, 左 x_cm, 右 x_cm)。"""
    lay = vc_file.layer_by_name(doc, "marker bands")
    ox, oy = vc_file.doc_origin(doc)
    pt = vc_file.PT_PER_CM
    out = []
    for n in vc_file.lay_children(lay):
        (x0, y0), (x1, _y1) = vc_file.line_ends(n)
        out.append((n.get("name"), round((y0 - oy) / pt, 4), round((x0 - ox) / pt, 4), round((x1 - ox) / pt, 4)))
    return out


def _label_texts(doc):
    lay = vc_file.layer_by_name(doc, "labels")
    return sorted((n.get("name"), vc_file.text_info(n)["text"]) for n in vc_file.lay_children(lay)
                  if n["kind"].get("type") == "text")


# ---------------------------------------------------------------- 断言小工具

_FAILS = []


def _check(cond, msg):
    if not cond:
        _FAILS.append(msg)
    return bool(cond)


def _frag(text, *pieces):
    return all(p in text for p in pieces)


_NUM = __import__("re").compile(r"-?\d+(?:\.\d+)?")


def _num_near(text, value, tol=0.006):
    """文字里有没有这个数（%.2f/%.4f/%g 都行 —— 按值比，不按字面比）。"""
    return any(abs(float(m.group(0)) - float(value)) <= tol for m in _NUM.finditer(text))


# ---------------------------------------------------------------- 用例

def test_parser_flags(work):
    """--reset-marker / --lane 注册在所有 sync 组上；--lane 单给是用法错误（标准 1、5）。"""
    d = os.path.join(work, "doc.vectorcraft")
    build_doc(d)
    fake = _FakeVC(d)
    for group in ("figure", "crop", "workbench", "profile"):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            ap = gel_photo.build_parser(canvas=True)
        try:
            ns = ap.parse_args([group, "sync", d, "--reset-marker", "--lane", "2"])
        except SystemExit:
            _check(False, "%s sync 不认 --reset-marker/--lane" % group)
            continue
        _check(getattr(ns, "reset_marker", None) is True,
               "%s sync --reset-marker 没解析成 True（dest=reset_marker?）" % group)
        _check(ns.lane == 2, "%s sync --lane 2 没解析成 2（%r）" % (group, ns.lane))
        _check(getattr(ns, "func", None) is not None, "%s sync 没有 func" % group)
        # 不带 --reset-marker 的 --lane 是用法错误；不带 --lane 时默认 None
        rc, out, err = _run([group, "sync", d, "--lane", "2", "--dry-run"], fake)
        _check(rc not in (0,), "%s sync --lane 没有 --reset-marker 竟然没报用法错误（rc=%r）" % (group, rc))
        _check(bool((out + err).strip()),
               "%s sync --lane 没有 --reset-marker 时一句解释都没有" % group)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            ap = gel_photo.build_parser(canvas=True)
            ns = ap.parse_args([group, "sync", d])
        _check(not getattr(ns, "reset_marker", False) and getattr(ns, "lane", None) is None,
               "%s sync 的默认值不对（reset_marker=%r lane=%r）"
               % (group, getattr(ns, "reset_marker", None), getattr(ns, "lane", None)))


def test_redetect_moves_bands(work):
    """标准 1/2/3：重检把 band 线搬到当前图上最强的前 N 个极小，打印新旧 y 与闸门诊断，
    一条撤销步，并且同一个进程里 Figure 标签 / Profiles / fig.png / fig.mw.json 全都更新。"""
    d = os.path.join(work, "stale.vectorcraft")
    # 旧线整体偏低 3 mm（吸不上）；ep-meta 里那份 marker_confidence 也是旧的（上一次低置信的残留）——
    # 重检必须把它换成这一次的诊断，而不是原样留着。
    build_doc(d, confidence=_conf(False, 5, 3, "上一次的闸门原因", 1.02, 12.0, 11.8))
    png_out, mw_out = d[:-len(".vectorcraft")] + ".png", d[:-len(".vectorcraft")] + ".mw.json"
    before = _bands(d)[0]
    # 夹具自证：这条「挪过 Crop」的旧线，普通 sync（±0.06 cm 吸附）根本吸不动 —— 正是本 bead 要解决的场景。
    # 若有人把夹具改成线本来就压在极小上，这条会立刻红：那时候「重检有用」也就无从验证了。
    plain = os.path.join(work, "stale-plain.vectorcraft")
    shutil.copyfile(d, plain)
    pfake = _FakeVC(plain)
    prc, pout, perr = _sync(plain, pfake)
    _check(prc == 0, "普通 sync 在夹具上就失败了（rc=%r, stderr=%r）" % (prc, perr[:300]))
    _check([round(y, 2) for _mw, y in _bands(plain)[0]] == [round(y, 2) for _mw, y in before],
           "夹具自证失败：普通 sync 已经把线挪到了极小上，重检用例失去意义")
    _check("没有局部极小" in pout,
           "夹具自证失败：普通 sync 没说这些线吸不上：%r" % pout[:500])
    rows, info = detected(MARK_PASS)
    want = [(mw, round(r["y_cm"], 2)) for (mw, _y), r in zip(before, rows)]
    # 夹具自证（名字按标签序列、不是按显著度排名）：前 3 名的显著度不是自上而下单调的 ——
    # 最强的在中段（1.60 cm），第 2 强的在最上面。若有人按显著度排名去挂 MW，validate 会当场炸。
    proms = [round(r["prominence"], 2) for r in rows]
    _check(proms != sorted(proms, reverse=True),
           "夹具自证失败：前三名的显著度正好自上而下递减，考不出「名字按标签序列」这件事：%r" % (proms,))

    before_model, before_meta = _readback(d)         # 重检之前的样子（样品带峰 / 泳道几何 / 名字）
    fake = _FakeVC(d)
    mtime_before = {p: os.path.getmtime(p) for p in (d, png_out, mw_out) if os.path.exists(p)}
    rc, out, err = _sync(d, fake, "--reset-marker")
    _check(rc == 0, "重检返回 %r（stderr=%r）" % (rc, err[:400]))
    doc = vc_file.read(d)[0]["document"]

    # ---- 打印：泳道来源、每带新旧 y、闸门诊断
    _check("marker 重检" in out, "没有 marker 重检这一行：%r" % out[:400])
    head = [ln for ln in out.splitlines() if "marker 重检" in ln]
    _check(len(head) == 1 and "泳道 1" in head[0] and "ep-meta.marker_lane" in head[0],
           "没写清用的是哪条泳道、是从 ep-meta.marker_lane 取的：%r" % (head or out[:400],))
    for (mw, old_y), (_m, new_y) in zip(before, want):
        _check(_num_near(out, old_y) and _num_near(out, new_y),
               "band %g 的新旧 y 对照没打出来（旧 %.2f → 新 %.2f）：%r" % (mw, old_y, new_y, out[:800]))
    _check((_frag(out, "picked") or ("%d/%d" % (3, 3)) in out),
           "闸门诊断没写 picked/need：%r" % out[:800])
    _check(("断崖" in out) and _num_near(out, info["ratio"]),
           "闸门诊断没写断崖比值 %.2f：%r" % (info["ratio"], out[:800]))
    _check(_num_near(out, info["nth"]), "闸门诊断没写第 N 名显著度 %.2f：%r" % (info["nth"], out[:800]))
    _check(gel_session.LOW_CONFIDENCE_NOTE not in out,
           "过了闸门却打了低置信：%r" % out[:800])

    # ---- 文档：band 线的名字与 y 都按 markers.json 的顺序重建
    got, model = _bands(d)
    _check([(int(mw), round(y, 2)) for mw, y in got] == [(int(m), y) for m, y in want],
           "band 线没重建到当前图的极小上：%r（要 %r）" % (got, want))
    _check(all(got[i][0] > got[i + 1][0] for i in range(len(got) - 1)),
           "名字没按标签序列自上而下挂（MW 应当随 y 递减，而不是按显著度排名）：%r" % (got,))
    lines = _band_lines(doc)
    _check(sorted(nm for nm, _y, _a, _b in lines) == ["band 100", "band 150", "band 250"],
           "band 线名字不是 markers.json 的标签顺序：%r" % (lines,))
    ml = model["meta"]["marker_lane"]
    left, right = model["lanes"][ml - 1]["left_cm"], model["lanes"][ml - 1]["right_cm"]
    for _nm, _y, x0, x1 in lines:
        _check(abs(x0 - (left - workbench.GRAB_CM)) < 0.02 and abs(x1 - (right + workbench.GRAB_CM)) < 0.02,
               "band 线没铺在当前 marker 泳道 %d 上（%.2f–%.2f）：%r" % (ml, left, right, lines))

    # ---- ep-meta.marker_confidence：同形 schema，数值来自这次重检
    meta = _readback(d)[1]
    conf = meta.get("marker_confidence") or {}
    _check(set(conf) >= {"ok", "picked", "need", "gate", "ratio", "nth", "next", "notes"},
           "ep-meta.marker_confidence 不是既有 schema：%r" % (conf,))
    _check(conf.get("ok") is True, "过了闸门却写成低置信：%r" % (conf,))
    _check(conf.get("need") == 3 and conf.get("picked") == 3,
           "picked/need 不对：%r" % (conf,))
    _check(conf.get("ratio") == info["ratio"] and conf.get("nth") == info["nth"],
           "ratio/nth 与重检的对不上：%r vs %r" % (conf, info))
    _check(meta.get("bands") == [[float(m), round(y, 4)] for m, y in got],
           "ep-meta.bands 没跟着 band 线更新：%r vs %r" % (meta.get("bands"), got))

    # ---- 一条撤销步；撤了之后不会半新半旧
    _check(fake.group_labels() == ["epcraft sync"],
           "改动不在唯一一个撤销步「epcraft sync」里：%r" % (fake.group_labels(),))

    # ---- 自动 sync：Figure 标签 / Profiles / PNG / mw.json 一起更新
    _check(os.path.exists(png_out) and os.path.getmtime(png_out) > mtime_before.get(png_out, 0),
           "fig.png 没重新导出")
    _check(os.path.exists(mw_out) and os.path.getmtime(mw_out) > mtime_before.get(mw_out, 0),
           "fig.mw.json 没重新写")
    mw = json.load(open(mw_out, encoding="utf-8")) if os.path.exists(mw_out) else {}
    _check(mw.get("version") == 2, "fig.mw.json 不是 v2：%r" % (mw.get("version")))
    marker_lane = [ln for ln in mw.get("lanes") or [] if ln.get("marker")]
    _check(marker_lane and [round(b["y_cm"], 2) for b in marker_lane[0]["bands"]] == [y for _m, y in want],
           "fig.mw.json 里 marker 道的带还是旧位置：%r" % (marker_lane and marker_lane[0]["bands"]))
    _check(not (mw.get("calibration") or {}).get("low_confidence"),
           "过了闸门却写了低置信：%r" % (mw.get("calibration"),))
    labels = _label_texts(doc)
    _check(("label band 250", "250 kD") in labels, "Figure 的 band 标签没跟上：%r" % (labels,))
    _check([(e["text"], tuple(e["mws"])) for e in meta["marker_labels"]]
           == [(t, (mw,)) for t, mw in LABELS],
           "ep-meta.marker_labels 被重检改掉了（名字应当按标签序列原样保留）：%r" % (meta["marker_labels"],))

    # ---- 重检只管 marker 道：样品带的峰、泳道几何、泳道名字都不许动
    _check(meta.get("peak_auto") == before_meta.get("peak_auto"),
           "重检顺带改了样品带的峰（peak_auto）：%r vs %r"
           % (before_meta.get("peak_auto"), meta.get("peak_auto")))
    _check([(ln["left_cm"], ln["right_cm"]) for ln in model["lanes"]]
           == [(ln["left_cm"], ln["right_cm"]) for ln in before_model["lanes"]],
           "重检把泳道边线挪了：%r" % (model["lanes"],))
    _check(meta.get("lane_names") == before_meta.get("lane_names"),
           "重检把泳道名字改了：%r vs %r" % (before_meta.get("lane_names"), meta.get("lane_names")))


def test_idempotent(work):
    """标准 4：检测结果与现有线一致 → 0 个改动（不写盘、不导出、不起撤销步）。"""
    d = os.path.join(work, "fresh.vectorcraft")
    rows, _info = detected(MARK_PASS)
    build_doc(d, band_ys=[r["y_cm"] for r in rows])      # 初始线正好在当前图的极小上
    before = open(d, "rb").read()
    fake = _FakeVC(d)
    rc, out, err = _sync(d, fake, "--reset-marker")
    _check(rc == 0, "重检返回 %r（stderr=%r）" % (rc, err[:400]))
    _check("0 个改动" in out, "同结果却没打 0 个改动：%r" % out[:600])
    _check(fake.opened == [] and "edit.undoGroup.begin" not in fake.cmds(),
           "同结果还动了文档：%r" % (fake.cmds()[:8],))
    _check(open(d, "rb").read() == before, "同结果重检把文档写坏了（字节变了）")
    got, _m = _bands(d)
    _check([round(y, 2) for _mw, y in got] == [round(r["y_cm"], 2) for r in rows],
           "同结果重检却改了 y：%r vs %r" % (got, [round(r["y_cm"], 4) for r in rows]))
    # 再跑一次仍然 0 改动（幂等的幂等）
    fake2 = _FakeVC(d)
    rc2, out2, _e2 = _sync(d, fake2, "--reset-marker")
    _check(rc2 == 0 and "0 个改动" in out2, "第二次重检不是 0 改动（rc=%r）：%r" % (rc2, out2[:400]))
    _check("edit.undoGroup.begin" not in fake2.cmds(), "第二次重检还起了撤销步")


def test_low_confidence(work):
    """标准 2/5/7：闸门不过照现有语义标低置信、照常执行（不拒绝）；band 线仍然落在最强极小上。"""
    d = os.path.join(work, "flat.vectorcraft")
    build_doc(d, marker=MARK_FLAT)
    png_out, mw_out = d[:-len(".vectorcraft")] + ".png", d[:-len(".vectorcraft")] + ".mw.json"
    rows, info = detected(MARK_FLAT)
    _check(info["ok"] is False, "低置信夹具的闸门竟然是过的：%r" % (info,))
    fake = _FakeVC(d)
    rc, out, err = _sync(d, fake, "--reset-marker")
    _check(rc == 0, "低置信分支被拒绝了（rc=%r, stderr=%r）" % (rc, err[:400]))
    _check(("不过" in out) or ("低置信" in out), "低置信没打出来：%r" % out[:600])
    _check((_frag(out, "picked") or ("%d/%d" % (3, 3)) in out) and _num_near(out, info["ratio"]),
           "低置信分支的诊断没写 picked/need 或断崖比值：%r" % out[:800])
    meta = _readback(d)[1]
    conf = meta.get("marker_confidence") or {}
    _check(conf.get("ok") is False, "低置信没写进 ep-meta.marker_confidence：%r" % (conf,))
    _check(bool(conf.get("gate")), "低置信没记闸门原因：%r" % (conf,))
    _check(conf.get("need") == 3 and conf.get("picked") == 3, "低置信分支的 picked/need 不对：%r" % (conf,))
    mw = json.load(open(mw_out, encoding="utf-8")) if os.path.exists(mw_out) else {}
    _check(mw.get("calibration", {}).get("low_confidence") is True,
           "fig.mw.json 没标 calibration.low_confidence：%r" % (mw.get("calibration"),))
    _check(any(gel_session.LOW_CONFIDENCE_NOTE in str(n) for n in mw.get("notes") or []),
           "fig.mw.json 的 notes 里没有低置信那句话：%r" % (mw.get("notes"),))
    got, _m = _bands(d)
    _check([round(y, 2) for _mw, y in got] == [round(r["y_cm"], 2) for r in rows],
           "低置信分支的 band 线没落在最强极小上：%r vs %r" % (got, [round(r['y_cm'], 2) for r in rows]))
    _check(fake.group_labels() == ["epcraft sync"], "低置信分支不是一条撤销步：%r" % (fake.group_labels(),))


def test_dry_run(work):
    """标准 6：--dry-run 只打印（不存盘、不导出、不起撤销步，磁盘上一个字节都不变）。"""
    d = os.path.join(work, "dry.vectorcraft")
    build_doc(d, band_ys=[1.10, 1.90, 2.70])
    png_out, mw_out = d[:-len(".vectorcraft")] + ".png", d[:-len(".vectorcraft")] + ".mw.json"
    snap = {p: open(p, "rb").read() for p in (d,) if os.path.exists(p)}
    fake = _FakeVC(d)
    rc, out, err = _sync(d, fake, "--reset-marker", "--dry-run")
    _check(rc == 0, "--dry-run 返回 %r（stderr=%r）" % (rc, err[:400]))
    _check("marker 重检" in out and _frag(out, "picked"), "--dry-run 没打印重检与诊断：%r" % out[:600])
    ops = None
    for i, ch in enumerate(out):                 # 取最后一段从行首 [ 开始的 JSON（前面还有 [i] 行）
        if ch != "[" or (i and out[i - 1] != "\n"):
            continue
        try:
            ops = json.loads(out[i:])
        except ValueError:
            continue
        break
    if ops is None:
        # 实现可能把 JSON 打在最后一段；退一步只要求出现 op 关键字
        _check('"op"' in out, "--dry-run 没打印计划里的 ops：%r" % out[-600:])
    else:
        _check(any(o.get("op") in ("delete", "create", "move", "rename", "set_text", "stroke", "reshape")
                   for o in ops), "--dry-run 的 ops 里没有 band 线的改动：%r" % (ops[:3],))
    _check(fake.saved == [] and fake.exports == [],
           "--dry-run 竟然存盘/导出了：save=%r export=%r" % (fake.saved, fake.exports))
    _check("edit.undoGroup.begin" not in fake.cmds(), "--dry-run 竟然起了撤销步")
    _check(open(d, "rb").read() == snap[d], "--dry-run 改了文档")


def test_lane_option(work):
    """标准 5：--lane N 换一条泳道重检（band 线铺到那条道、ep-meta.marker_lane 跟着改），
    并且重检后那条道就是新的 marker 道。"""
    d = os.path.join(work, "lane2.vectorcraft")
    build_doc(d, ladder_lane=2, band_ys=[1.09, 1.95, 2.69])   # 梯子在 2 号道，band 线还在 1 号道
    fake = _FakeVC(d)
    _check(_readback(d)[1].get("marker_lane") == 1, "夹具前提不成立：一开始 marker 道不是 1")
    rows2, info2 = detected(MARK_PASS, 2)
    rc, out, err = _sync(d, fake, "--reset-marker", "--lane", "2")
    _check(rc == 0, "--lane 2 返回 %r（stderr=%r）" % (rc, err[:400]))
    head = [ln for ln in out.splitlines() if "marker 重检" in ln]
    _check(len(head) == 1 and "泳道 2" in head[0] and "--lane" in head[0] and "ep-meta" not in head[0],
           "没写清是 --lane 指定的泳道 2：%r" % (head or out[:400],))
    model, meta = _readback(d)
    _check(meta.get("marker_lane") == 2, "ep-meta.marker_lane 没改成 2：%r" % (meta.get("marker_lane"),))
    left, right = model["lanes"][1]["left_cm"], model["lanes"][1]["right_cm"]
    lines = _band_lines(vc_file.read(d)[0]["document"])
    _check(len(lines) == 3, "换道之后的 band 线数量不对：%r" % (lines,))
    for _nm, _y, x0, x1 in lines:
        _check(abs(x0 - (left - workbench.GRAB_CM)) < 0.02 and abs(x1 - (right + workbench.GRAB_CM)) < 0.02,
               "band 线没铺到泳道 2（%.2f–%.2f）：%r" % (left, right, lines))
    _check(all(model["bands"][i]["mw"] > model["bands"][i + 1]["mw"] for i in range(len(model["bands"]) - 1)),
           "换道后 band 线的 MW 顺序乱了：%r" % (model["bands"],))
    _check([round(b["y_cm"], 2) for b in model["bands"]] == [round(r["y_cm"], 2) for r in rows2],
           "band 线的 y 不是 2 号道上叫出来的极小：%r vs %r"
           % ([b["y_cm"] for b in model["bands"]], [r["y_cm"] for r in rows2]))
    conf = meta.get("marker_confidence") or {}
    _check(conf.get("need") == 3 and conf.get("picked") == 3, "--lane 2 后 picked/need 不对：%r" % (conf,))
    # 换过道之后再重检一次（还是 --lane 2）应当已经收敛：0 个改动、不再动文档
    fake2 = _FakeVC(d)
    rc2, out2, _e2 = _sync(d, fake2, "--reset-marker", "--lane", "2")
    _check(rc2 == 0 and "0 个改动" in out2,
           "换道之后再重检不是 0 个改动（rc=%r）：%r" % (rc2, out2[:400]))
    _check("edit.undoGroup.begin" not in fake2.cmds(), "换道之后再重检还起了撤销步")


def test_fallback_best_ratio(work):
    """标准 5 后半：所有候选道都不过闸门时取断崖比值最好的一条，且**不拒绝执行**。
    夹具里三道全不过：泳道 1 断崖 1.55×（三道里最高），泳道 2/3 卡在显著度下限 —— 所以无论实现
    只在 ep-meta.marker_lane 上试、还是把所有泳道都试一遍，「比值最好」都指向泳道 1。"""
    d = os.path.join(work, "fallback.vectorcraft")
    build_doc(d, marker=MARK_NOBODY, sample=FAINT2, sample2=FAINT3,
              confidence=_conf(False, 3, 3, "低置信夹具"))
    rows, _info = detected(MARK_NOBODY)
    fake = _FakeVC(d)
    rc, out, err = _sync(d, fake, "--reset-marker")
    _check(rc == 0, "候选道不过闸门时被拒绝了（rc=%r, stderr=%r）" % (rc, err[:300]))
    _check(("按断崖" in out) or ("ratio" in out) or ("低置信" in out),
           "没说明选了哪条道继续：%r" % out[:600])
    # 三道里泳道 1 的断崖比值最高（1.55），另外两道卡在显著度下限 —— 选 1 才是「比值最好」
    png = gel_png(MARK_NOBODY, sample=FAINT2, sample2=FAINT3)
    l1 = lane_gate(png, 1)[1]
    l2 = lane_gate(png, 2)[1]
    l3 = lane_gate(png, 3)[1]
    _check(l1["ratio"] > max(l2["ratio"], l3["ratio"]),
           "夹具自证失败：泳道 1 的断崖比值不是三道里最高的（%r / %r / %r）"
           % (l1["ratio"], l2["ratio"], l3["ratio"]))
    _check(fake.group_labels() == ["epcraft sync"], "回退执行不是一条撤销步：%r" % (fake.group_labels(),))
    meta = _readback(d)[1]
    _check(meta.get("marker_lane") == 1, "回退没有保留一条 marker 道：%r" % (meta.get("marker_lane"),))
    _check((meta.get("marker_confidence") or {}).get("ok") is False,
           "回退执行后应当仍是低置信：%r" % (meta.get("marker_confidence"),))
    got, _m = _bands(d)
    _check([round(y, 2) for _mw, y in got] == [round(r["y_cm"], 2) for r in rows],
           "回退执行的 band 线没落在当前图的极小上：%r vs %r" % (got, [round(r["y_cm"], 2) for r in rows]))


def test_edit_then_sync(work):
    """回归：cmd_edit / workbench import 末尾手搓的 sync Namespace 也得带上新开关，
    否则加了 --reset-marker 之后 `workbench edit` 会直接 AttributeError。"""
    d = os.path.join(work, "edit.vectorcraft")
    build_doc(d)
    fake = _FakeVC(d)
    rc, out, err = _run(["workbench", "edit", d, "--label", "2=B", "--port", "1"], fake)
    _check(rc == 0, "workbench edit 挂了（rc=%r, stderr=%r）" % (rc, err[-400:]))
    _check("Traceback" not in err, "workbench edit 抛异常了：%r" % err[-400:])
    _check("epcraft sync" in out or "已更新" in out or "最新" in out,
           "workbench edit 之后没接上 sync：%r" % out[-400:])


def test_bad_lane(work):
    """护栏：--lane 指向不存在的泳道时，要么拒绝（rc≠0 且写明是哪条道），要么根本不执行 ——
    但绝不能半途写坏文档（撤销组里报错后文件仍要能被 Workbench 读回）。"""
    d = os.path.join(work, "badlane.vectorcraft")
    build_doc(d)
    snap = open(d, "rb").read()
    fake = _FakeVC(d)
    rc, out, err = _sync(d, fake, "--reset-marker", "--lane", "99")
    if rc == 0:
        # 实现了「执行但落回某条道」也不能崩：文档必须仍然结构合法
        try:
            model, meta = _readback(d)
        except Exception as e:                                  # noqa: BLE001
            _check(False, "--lane 99 执行后文档读不回来了：%s" % e)
            return
        _check(1 <= (meta.get("marker_lane") or 0) <= len(model["lanes"]),
               "--lane 99 之后 ep-meta.marker_lane 不是合法泳道：%r" % (meta.get("marker_lane"),))
    else:
        _check("99" in (out + err), "--lane 99 被拒了却没说清是哪条道：%r" % ((out + err)[-400:],))
        _check(open(d, "rb").read() == snap, "--lane 99 被拒了却改了文档")
        _check("edit.undoGroup.begin" not in fake.cmds() or "edit.undoGroup.cancel" in fake.cmds(),
               "--lane 99 起了一半撤销组就退出：%r" % (fake.cmds()[-(len(fake.cmds()) - max(0, len(fake.cmds()) - 6)):],))


def test_crop_frame(work):
    """标准 1 的前半：band 线的 y 必须从**当前 Crop 框**裁出来的胶面图里叫峰 —— 框挪了、
    胶面图还没重裁时，重检要先按新框重裁（relink 同一撤销步）再叫峰，不能拿嵌入的那张旧图叫。"""
    d, photo, old_ys = build_crop_doc(os.path.join(work, "crop.vectorcraft"), work)
    _check(os.path.isfile(photo), "夹具没写出照片")
    fake = _FakeVC2(d)
    # 先证明「框没动」时 sync 不重裁：plain sync 也 0 改动、文件不变（Crop 这条路径纯幂等）
    snap = open(d, "rb").read()
    rc, out, err = _sync(d, fake)
    _check(rc == 0, "plain sync 返回 %r（stderr=%r）" % (rc, err[:400]))
    _check(open(d, "rb").read() == snap or "0 个改动" in out,
           "框没动时 sync 动了文档：%r" % out[:400])

    # 框在照片上右移 0.10 cm（35.3 px @254 dpi）：文档里的胶面图仍是旧框裁的那张
    off_px = round(PAD_CM * PPC)
    frame, meta_before = _move_crop_frame(d, 0.10, 0.10)   # 斜着挪：y 也变（否则新旧图 y 一样，分不出）
    shift_px = 0.10 * DPI / 2.54                    # 0.10 cm @254 dpi = 10 原图 px
    _check(abs(frame[0][0] - (off_px + shift_px)) < 0.5, "夹具没把框挪到位：%r（要 %.2f px）"
           % (frame[0], off_px + shift_px))
    _check(all(0.0 <= p[0] <= (W_CM + 2 * PAD_CM) * DPI / 2.54
               and 0.0 <= p[1] <= (H_CM + 2 * PAD_CM) * DPI / 2.54 for p in frame),
           "挪完框出界了（夹具前提坏了）：%r" % (frame,))
    _check(frame != [list(p) for p in meta_before["crop"]["frame_px"]],
           "夹具前提不成立：文档里的框与 ep-meta 记的旧框一样（没挪成）：%r" % (frame,))
    old_model, old_meta = _readback(d)
    lines_before = _band_lines(vc_file.read(d)[0]["document"])

    fake = _FakeVC2(d)
    rc, out, err = _sync(d, fake, "--reset-marker")
    _check(rc == 0, "框挪过之后 --reset-marker 返回 %r（stderr=%r）" % (rc, err[:500]))
    _check(_frag(out, "marker 重检"), "没打印重检头：%r" % out[:400])
    _check(_frag(out, "裁切框"), "框挪了却没重裁（没打「裁切框：…重新裁了胶面图」）：%r" % out[:600])
    _check([c for c in fake.cmds() if c.startswith("links.")][:2] == ["links.relink", "links.embed"],
           "重新裁了胶面图却没 relink 到文档里的图：%r" % fake.cmds()[:8])
    _check(fake.group_labels() == ["epcraft sync"],
           "重裁 + 重检不在同一个撤销步里：%r" % (fake.group_labels(),))

    # 叫峰用的必须是**新裁**那张图：band 线的 y 与「按新框重裁 → 独立叫峰」的结果一致。
    # 独立 oracle：我自己拿新框裁一遍（不走 sync 的那条路径），再在产物上量 y。
    from epcraft import crop as _crop
    fp_new, _e = _crop.read_frame(vc_file.read(d)[0]["document"],
                                  _readback(d)[1], H_CM / float(W_CM))
    x0, y0 = int(round(fp_new[0][0])), int(round(fp_new[0][1]))
    x1, y1 = int(round(fp_new[2][0])), int(round(fp_new[2][1]))
    off = int(round(PAD_CM * PPC))
    face_new = _crop.crop_face(photo, 0.0, (x0, y0, x1, y1), W_CM, H_CM, DPI, warn=False)
    wpx2, hpx2, gray2 = profiles.gel_gray(face_new)
    a, b = LANES[0]
    ys2, gs2 = profiles.lane_profile(gray2, wpx2, hpx2, PPC, a, b, WIN[0], WIN[1])
    rows_new, info_new = profiles.pick_marker(ys2, gs2, PPC, len(LABELS))
    new_ys = [r["y_cm"] for r in rows_new][:len(LABELS)]
    got = [y for (_n, y, _a, _b) in _band_lines(vc_file.read(d)[0]["document"])]
    # 前提：新框下的 y 与旧线确实不同（否则这一条不区分「拿旧图叫峰」）
    _check(any(abs(n - o) > 0.02 for n, o in zip(new_ys, old_ys)),
           "夹具不区分新/旧图（新框下检测出的 y 与旧线一样：%r vs %r）" % (new_ys, old_ys))
    _check(len(got) == len(new_ys) and all(any(abs(g - n) <= 0.02 for n in new_ys) for g in got),
           "band 线不在新框下检测出的 y 上（说明叫峰用的是旧图）：线=%r 新框检测=%r 旧线=%r"
           % (got, new_ys, old_ys))
    model1, meta1 = _readback(d)
    fp, errs = __import__("epcraft.crop", fromlist=["crop"]).read_frame(
        vc_file.read(d)[0]["document"], meta1, H_CM / float(W_CM))
    _check(errs == [], "重检后 Crop 读不回来：%r" % (errs,))
    _check(meta1["crop"]["frame_px"] == [list(p) for p in fp],
           "ep-meta.crop.frame_px 没跟着写回新框：%r vs %r" % (meta1["crop"]["frame_px"], fp))
    _check(meta1["crop"]["photo_sha1"] == meta_before["crop"]["photo_sha1"],
           "重裁把 photo_sha1 改了：%r" % (meta1["crop"].get("photo_sha1"),))
    # 样品道跟着框一起平移：框往下挪了 0.10 cm → 新裁出的胶面图里所有东西的 y 都小 0.10 cm。
    # 所以 peak_auto 必须**跟着**动同样多（不动 = 留在旧图上，与 band 线脱节 → 下一次 sync 又要改）。
    dy_cm = 0.10
    pa0, pa1 = old_meta.get("peak_auto") or {}, meta1.get("peak_auto") or {}
    _check(sorted(pa0) == sorted(pa1) and pa0, "peak_auto 的泳道集合变了：%r → %r" % (sorted(pa0), sorted(pa1)))
    for k in sorted(pa0):
        _check(len(pa0[k]) == len(pa1.get(k, [])),
               "泳道 %s 的 sample 峰数量变了：%r → %r" % (k, pa0[k], pa1.get(k)))
        _check(all(abs(float(a) - float(b) - dy_cm) <= 0.02
                   for a, b in zip(pa0[k], pa1.get(k, []))),
               "泳道 %s 的 sample 峰没跟着框平移 %.2f cm（留在旧图上了）：%r → %r"
               % (k, dy_cm, pa0[k], pa1.get(k)))

    # 再跑一次：框已经是新的 → 0 个改动、不起撤销步、字节不变（幂等）
    snap2 = open(d, "rb").read()
    fake2 = _FakeVC2(d)
    rc2, out2, err2 = _sync(d, fake2, "--reset-marker")
    _check(rc2 == 0, "第二次重检返回 %r（stderr=%r）" % (rc2, err2[:400]))
    _check(_frag(out2, "0 个改动") or _frag(out2, "一致"),
           "第二次重检不是 0 改动：%r" % out2[:600])
    _check(fake2.group_labels() == [], "第二次重检又起了撤销步：%r" % (fake2.group_labels(),))
    _check(open(d, "rb").read() == snap2, "第二次重检改动了文档")
    _check(len(lines_before) == len(_band_lines(vc_file.read(d)[0]["document"])),
           "band 线条数变了")


TESTS = [("parser_flags", test_parser_flags),
         ("redetect_moves_bands", test_redetect_moves_bands),
         ("idempotent", test_idempotent),
         ("low_confidence", test_low_confidence),
         ("dry_run", test_dry_run),
         ("lane_option", test_lane_option),
         ("fallback_best_ratio", test_fallback_best_ratio),
         ("bad_lane", test_bad_lane),
         ("crop_frame", test_crop_frame),
         ("edit_then_sync", test_edit_then_sync)]


def run(verbose=True):
    """跑全套 → (通过数, 失败列表)。失败信息是人能读的句子（每条都带用例名）。"""
    del _FAILS[:]
    work = tempfile.mkdtemp(prefix="ep-ugj-accept-")
    passed = 0
    try:
        for name, fn in TESTS:
            sub = os.path.join(work, name)
            os.makedirs(sub)
            n0 = len(_FAILS)
            try:
                fn(sub)
            except Exception as e:                       # noqa: BLE001 —— 验收：任何异常都算失败
                import traceback
                _FAILS.append("%s 抛异常：%s\n%s" % (name, e, traceback.format_exc(limit=4)))
            ok = len(_FAILS) == n0
            passed += 1 if ok else 0
            if verbose:
                print("  %-22s %s" % (name, "ok" if ok else "FAIL"))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return passed, list(_FAILS)


def selftest():
    """Aggregate entry for python3 -m tests: run all cases and raise on any failure."""
    n, fails = run(verbose=False)
    if fails:
        raise AssertionError("test_marker_redetect %d/%d 失败：\n%s" % (len(fails), len(TESTS), "\n".join(fails)))
    print("test_marker_redetect %d/%d 通过" % (n, len(TESTS)))
    return n


if __name__ == "__main__":
    n, fails = run()
    print("test_marker_redetect：%d/%d 项通过" % (n, len(TESTS)))
    for f in fails:
        print("[!] %s" % f)
    sys.exit(0 if not fails else 1)
