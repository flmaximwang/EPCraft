#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest（Crop 画板，C1）：crop.py 的几何往返、角度正负号、读回、frame_delta、crop_face 与旧裁切逐字节一致。

不读用户文件、不开 app；照片都是 magick 现画的小图。run() → 通过的检查数；失败抛 AssertionError。
"""
import contextlib
import hashlib
import io
import math
import os
import tempfile

import crop
import profiles
from geometry import load_dims_after
from magick_io import MAGICK, _run


PT = 72.0 / 2.54


def _size1(size0):
    """旋转画布的闭式（magick 会取整；几何自洽用不着取整）。"""
    W0, H0 = size0

    def fn(angle):
        a = math.radians(angle)
        c, s = abs(math.cos(a)), abs(math.sin(a))
        return W0 * c + H0 * s, W0 * s + H0 * c
    return fn


def _rot_pts(pts, ccw_deg, centre=None):
    """把点绕中心**视觉逆时针**转 ccw_deg（y 向下；= VectorCraft object.rotate 的正方向）。"""
    cx = centre[0] if centre else sum(p[0] for p in pts) / len(pts)
    cy = centre[1] if centre else sum(p[1] for p in pts) / len(pts)
    a = math.radians(ccw_deg)
    c, s = math.cos(a), math.sin(a)
    return [[cx + (x - cx) * c + (y - cy) * s, cy - (x - cx) * s + (y - cy) * c] for x, y in pts]


def _doc(frame_pt, image_xf, preview_size, extra_shapes=(), photo=True, crop_board=True):
    """最小的合成文档（document 那一层）：Workbench + Crop 画板，photo / crop 两个图层。"""
    def path(name, pts):
        return {"id": 90, "name": name,
                "kind": {"type": "path",
                         "path": {"subpaths": [{"anchors": [{"p": list(p)} for p in pts], "closed": True}]},
                         "live": {"shape": "rectangle"}}}
    img = {"id": 80, "name": "photo", "kind": {"type": "image", "key": "imgx", "width": preview_size[0],
                                               "height": preview_size[1], "xf": list(image_xf)}}
    boards = [{"name": "Figure"}, {"name": "Workbench"}, {"name": "Profiles"}]
    if crop_board:
        boards.append({"name": "Crop"})
    return {"artboards": boards,
            "layers": [{"id": 1, "name": "photo", "locked": True, "kind": {"type": "layer", "printable": False,
                                                                             "children": [img] if photo else []}},
                       {"id": 2, "name": "crop", "kind": {"type": "layer", "printable": False,
                                                          "children": [path("crop", frame_pt)] +
                                                          [path("x", p) for p in extra_shapes]}}]}


def _near(a, b, tol):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _old_crop(image, ang, bb, width_cm, height_cm, dpi):
    """重构前 gel_photo.gel_face 里内联的那段裁切（逐字照抄），用来比字节。"""
    (_W0, _H0), (W1, H1) = load_dims_after(image, ang)
    x0, y0 = max(0, int(round(bb[0]))), max(0, int(round(bb[1])))
    x1, y1 = min(W1, int(round(bb[2]))), min(H1, int(round(bb[3])))
    gw = int(round(width_cm / 2.54 * dpi))
    gh = int(round(height_cm / 2.54 * dpi))
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        cmd = [MAGICK, image, "-auto-orient"]
        if abs(ang) > 1e-6:
            cmd += ["-rotate", "%.4f" % ang]
        cmd += ["-crop", "%dx%d+%d+%d" % (x1 - x0, y1 - y0, x0, y0), "+repage",
                "-resize", "%dx%d!" % (gw, gh), "+profile", "*", "-depth", "8",
                "-units", "PixelsPerInch", "-density", str(dpi), tmp]
        _run(cmd)
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp)


def run():
    ok = 0

    # 1) 几何往返：frame_from_stage1 → stage1_from_frame 回到同一个角度和胶面框（≤ 0.01 px）；
    #    frame_angle = 上边的视觉逆时针角 = magick 角度（含 ±180 两端的等价）。
    size0, aspect = (4032, 3024), 6.25 / 8.6
    fn = _size1(size0)
    for ang in (0.0, 0.4, -0.4, 3.0, -3.0, 30.0, -30.0, 90.0, 180.0):
        W1, H1 = fn(ang)
        w = 2600.0
        box = (W1 / 2 - w / 2 + 37.5, H1 / 2 - w * aspect / 2 - 21.25, W1 / 2 + w / 2 + 37.5, H1 / 2 + w * aspect / 2 - 21.25)
        fp = crop.frame_from_stage1(box, ang, size0, (W1, H1))
        a2, box2 = crop.stage1_from_frame(fp, size0, fn, aspect)
        assert abs((a2 - ang + 180.0) % 360.0 - 180.0) < 1e-9, (ang, a2)
        assert _near(box2, box, 0.01), (ang, box, box2)
        assert abs((crop.frame_angle(fp) - ang + 180.0) % 360.0 - 180.0) < 1e-9, (ang, crop.frame_angle(fp))
        # 高按预设比例，不看画出来的高：把框的高拉长 3%，gel_box 不变
        tall = [list(p) for p in fp]
        ux, uy = fp[3][0] - fp[0][0], fp[3][1] - fp[0][1]
        for i in (0, 1):
            tall[i] = [tall[i][0] - 0.015 * ux, tall[i][1] - 0.015 * uy]
        for i in (2, 3):
            tall[i] = [tall[i][0] + 0.015 * ux, tall[i][1] + 0.015 * uy]
        assert _near(crop.stage1_from_frame(tall, size0, fn, aspect)[1], box, 0.01), ang
    ok += 1

    with tempfile.TemporaryDirectory() as td:
        # 2) 正负号（用 magick 真转）：框在照片上看起来逆时针歪 4°（y 向下，上边往右升高）→ angle = +4，
        #    crop_face 把它摆正：框顶内侧 12 px 的黑条在裁出来的图上左右两端一样黑，下面的灰一样灰；
        #    角度取反（−4）就是歪 8°，黑条一端掉出去。
        src = os.path.join(td, "tilt.png")
        rect = [[80, 80], [320, 80], [320, 220], [80, 220]]
        band = [[80, 80], [320, 80], [320, 92], [80, 92]]
        fr, bd = _rot_pts(rect, 4.0, (200, 150)), _rot_pts(band, 4.0, (200, 150))
        poly = lambda pts: "polygon " + " ".join("%.3f,%.3f" % (x, y) for x, y in pts)   # noqa: E731
        _run([MAGICK, "-size", "400x300", "xc:white", "-fill", "gray(50%)", "-draw", poly(fr),
              "-fill", "black", "-draw", poly(bd), "-depth", "8", src])
        assert fr[1][1] < fr[0][1], "合成的框应是上边往右升高（视觉逆时针）"
        dims = lambda a: load_dims_after(src, a)[1]                                     # noqa: E731
        ang, gb = crop.stage1_from_frame(fr, (400, 300), dims, 140.0 / 240.0)
        assert abs(ang - 4.0) < 1e-3, ang
        w, h, g = profiles.gel_gray(crop.crop_face(src, ang, gb, 2.4, 1.4, 254))
        assert (w, h) == (240, 140), (w, h)
        px = lambda x, y: g[y * w + x]                                                 # noqa: E731
        assert max(px(12, 5), px(228, 5)) < 40, (px(12, 5), px(228, 5))
        assert all(100 < px(x, 40) < 160 for x in (12, 228)) and all(100 < px(x, 130) < 160 for x in (12, 228)), \
            (px(12, 40), px(228, 40))
        mir = [[x, 300.0 - y] for x, y in fr]                     # 上下镜像：同中心同宽，看起来顺时针 4°
        a_bad, gb_bad = crop.stage1_from_frame([mir[3], mir[2], mir[1], mir[0]], (400, 300), dims, 140.0 / 240.0)
        assert abs(a_bad + 4.0) < 1e-3, a_bad
        w2, h2, g2 = profiles.gel_gray(crop.crop_face(src, a_bad, gb_bad, 2.4, 1.4, 254, warn=False))
        assert abs(g2[5 * w2 + 12] - g2[5 * w2 + 228]) > 60, "角度取反也摆正了？正负号测不出来"
        #    legacy_page_shift 就是 magick 转完留下的虚拟画布偏移（取反）；90° 的整数倍没有偏移
        for a in (0.4, -3.0, 30.0, 90.0, 180.0):
            (W0, H0), (W1, H1) = load_dims_after(src, a)
            X, Y = (int(v) for v in _run([MAGICK, src, "-auto-orient", "-rotate", "%.4f" % a, "-format", "%X %Y",
                                          "info:"], False).split())
            sx, sy = crop.legacy_page_shift(a, (W0, H0), (W1, H1))
            assert (X, Y) == (-sx, -sy), (a, (X, Y), (sx, sy))
        #    裁到的就是框画的地方（修掉的错位）：照片上一个黑点，框以它为中心、转 3°，裁出来黑点在正中
        #    （没有 +repage 的旧裁法在这里会往左上偏 ~(7.5, 10) px）
        dot = os.path.join(td, "dot.png")
        _run([MAGICK, "-size", "400x300", "xc:white", "-fill", "black", "-draw", "circle 230,170 230,176",
              "-depth", "8", dot])
        sq = _rot_pts([[170, 130], [290, 130], [290, 210], [170, 210]], 3.0, (230, 170))
        a3, gb3 = crop.stage1_from_frame(sq, (400, 300), lambda a: load_dims_after(dot, a)[1], 80.0 / 120.0)
        w3, h3, g3 = profiles.gel_gray(crop.crop_face(dot, a3, gb3, 1.2, 0.8, 254, warn=False))
        tot = sum(255 - v for v in g3)
        cx3 = sum((255 - g3[i]) * (i % w3) for i in range(len(g3))) / tot
        cy3 = sum((255 - g3[i]) * (i // w3) for i in range(len(g3))) / tot
        assert abs(cx3 - (w3 - 1) / 2.0) < 1.0 and abs(cy3 - (h3 - 1) / 2.0) < 1.0, (cx3, cy3, w3, h3)
        ok += 1

        # 3) 与修错位之前的内联裁切比（SOURCE_DATE_EPOCH 固定 PNG 里的时间戳）：不转 → 逐字节一致；
        #    转了 → 框挪 legacy_page_shift 再裁 ≈ 旧图（平均 |Δ灰度| ≤ 2，旧文档补 Crop 画板靠的就是它）；
        #    框出画布（clamp）时 warn=True 打 `[!] clipped`，warn=False 不打。
        photo = os.path.join(td, "photo.jpg")
        _run([MAGICK, "-size", "300x220", "xc:gray(230)", "-fill", "gray(70)",
              "-draw", "rotate 2 rectangle 60,40 240,190", "-fill", "gray(20)",
              "-draw", "rectangle 90,80 110,85", "-quality", "92", photo])
        old_env = os.environ.get("SOURCE_DATE_EPOCH")
        os.environ["SOURCE_DATE_EPOCH"] = "0"
        try:
            for ang, bb, dpi in ((0.0, (55.2, 41.7, 242.5, 188.9), 150), (1.131, (60.4, 50.5, 240.6, 181.3), 300),
                                 (-2.5, (58.0, 36.49, 236.51, 192.0), 150), (3.0, (-12.3, 20.0, 260.0, 260.7), 120)):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    new = crop.crop_face(photo, ang, bb, 8.6, 6.25, dpi, warn=False)
                assert buf.getvalue() == "", buf.getvalue()
                old = _old_crop(photo, ang, bb, 8.6, 6.25, dpi)
                if ang == 0.0:
                    assert new == old, (ang, bb, dpi)
                elif bb[0] >= 0:                          # clamp 的那一例两种裁法的边界本来就不同
                    sx, sy = crop.legacy_page_shift(ang, *load_dims_after(photo, ang))
                    leg = crop.crop_face(photo, ang, (bb[0] + sx, bb[1] + sy, bb[2] + sx, bb[3] + sy),
                                         8.6, 6.25, dpi, warn=False)
                    (_w, _h, ga_), (_w2, _h2, gb_) = profiles.gel_gray(leg), profiles.gel_gray(old)
                    assert len(ga_) == len(gb_) and sum(abs(p - q) for p, q in zip(ga_, gb_)) / len(ga_) <= 2.0, ang
            with contextlib.redirect_stdout(buf):
                crop.crop_face(photo, 3.0, (-12.3, 20.0, 260.0, 260.7), 8.6, 6.25, 120)
            assert "[!] clipped" in buf.getvalue(), buf.getvalue()
        finally:
            if old_env is None:
                os.environ.pop("SOURCE_DATE_EPOCH", None)
            else:
                os.environ["SOURCE_DATE_EPOCH"] = old_env
        ok += 1

        # 4) preview：尺寸是自动定向之后的（EXIF/orNT 转 90° 的图），长边限死、从不放大；photo_sha1 = sha1 前 12 位
        op = os.path.join(td, "orient.png")
        _run([MAGICK, "-size", "60x40", "xc:gray(128)", "-orient", "RightTop", op])
        jpg, (wp, hp), k = crop.preview(op, long_side=30)
        assert jpg[:2] == b"\xff\xd8" and (wp, hp) == (20, 30) and abs(k - 2.0) < 1e-9, ((wp, hp), k)
        _jpg, (wp, hp), k = crop.preview(op)
        assert (wp, hp) == (40, 60) and k == 1.0, ((wp, hp), k)
        with open(op, "rb") as f:
            assert crop.photo_sha1(op) == hashlib.sha1(f.read()).hexdigest()[:12]
        ok += 1

    # 5) layout：画板右边贴 x_right、上边 0；框在 pt 里的宽 = width_cm；预览图等比、外扩 0.3 cm
    size0, prev, aspect, width_cm = (4000, 3000), (2000, 1500), 6.25 / 8.6, 8.6
    fn = _size1(size0)
    a0 = 1.2
    W1, H1 = fn(a0)
    box = (W1 / 2 - 1300, H1 / 2 - 1300 * aspect + 40, W1 / 2 + 1300, H1 / 2 + 1300 * aspect + 40)
    fp = crop.frame_from_stage1(box, a0, size0, (W1, H1))
    L = crop.layout(fp, size0, prev, width_cm, -60.0)
    x, y, aw, ah = L["artboard"]
    m = 0.3 * PT
    assert abs(x + aw + 60.0) < 1e-9 and y == 0.0, L["artboard"]
    assert abs(math.hypot(L["frame_pt"][1][0] - L["frame_pt"][0][0], L["frame_pt"][1][1] - L["frame_pt"][0][1])
               - width_cm * PT) < 1e-9
    xf = L["image_xf"]
    assert xf[0] == xf[3] and xf[1] == xf[2] == 0.0 and abs(xf[4] - (x + m)) < 1e-9 and abs(xf[5] - m) < 1e-9
    assert abs(aw - (prev[0] * xf[0] + 2 * m)) < 1e-9 and abs(ah - (prev[1] * xf[0] + 2 * m)) < 1e-9
    assert abs(L["s"] - width_cm * PT / 2600.0) < 1e-12
    ok += 1

    # 6) read_frame：原样 → layout 的严格逆；没有 Crop 画板 → (None, [])；
    #    移动 / 旋转 / 等比缩放 / 出照片 → 无错；frame_delta 报对数（cm 按旧框方向、旧比例尺）。
    pts = L["frame_pt"]
    rd = lambda d: crop.read_frame(d, {"crop": {"photo_size": list(size0)}}, aspect)   # noqa: E731
    got, errs = rd(_doc(pts, xf, prev))
    assert errs == [] and all(_near(g_, f_, 1e-6) for g_, f_ in zip(got, fp)), (errs, got, fp)
    assert crop.frame_delta(fp, got, width_cm) is None
    assert crop.read_frame(_doc(pts, xf, prev, crop_board=False), {}, aspect) == (None, [])
    #    移动 (+5, −3) pt
    got, errs = rd(_doc([[px_ + 5.0, py_ - 3.0] for px_, py_ in pts], xf, prev))
    assert errs == [], errs
    dl = crop.frame_delta(fp, got, width_cm)
    a = math.radians(a0)
    ex = ((5 * math.cos(a) + 3 * math.sin(a)) / PT, (5 * math.sin(a) - 3 * math.cos(a)) / PT)
    assert abs(dl["dx_cm"] - ex[0]) < 1e-6 and abs(dl["dy_cm"] - ex[1]) < 1e-6, (dl, ex)
    assert abs(dl["angle_deg"]) < 1e-9 and abs(dl["scale"] - 1) < 1e-9, dl
    #    旋转：VectorCraft 里逆时针转 1° → 角度 +1（stage1 的 magick 角 = 旧 + 1）
    got, errs = rd(_doc(_rot_pts(pts, 1.0), xf, prev))
    assert errs == [], errs
    dl = crop.frame_delta(fp, got, width_cm)
    assert abs(dl["angle_deg"] - 1.0) < 1e-6 and abs(dl["dx_cm"]) < 1e-6 and abs(dl["dy_cm"]) < 1e-6, dl
    assert abs(crop.stage1_from_frame(got, size0, fn, aspect)[0] - (a0 + 1.0)) < 1e-4
    #    等比缩放 ×1.02（按住 Shift）
    c = [sum(p[i] for p in pts) / 4 for i in (0, 1)]
    got, errs = rd(_doc([[c[0] + (px_ - c[0]) * 1.02, c[1] + (py_ - c[1]) * 1.02] for px_, py_ in pts], xf, prev))
    assert errs == [], errs
    assert abs(crop.frame_delta(fp, got, width_cm)["scale"] - 1.02) < 1e-9
    #    框一半挪到照片外：不算错（crop_face 会 clamp）
    got, errs = rd(_doc([[px_ - 300.0, py_] for px_, py_ in pts], xf, prev))
    assert errs == [] and got is not None, errs
    ok += 1

    # 7) read_frame 的错误（一次全列出，有错 frame = None）：非等比拉高 → 比例错；拖了一个角 → 不是矩形；
    #    翻转 → 不是矩形（翻转）；两个框 / 没有框；照片没了；照片被非等比缩放
    ux, uy = pts[3][0] - pts[0][0], pts[3][1] - pts[0][1]
    tall = [list(p) for p in pts]
    for i in (2, 3):
        tall[i] = [tall[i][0] + 0.05 * ux, tall[i][1] + 0.05 * uy]
    got, errs = rd(_doc(tall, xf, prev))
    assert got is None and len(errs) == 1 and "高宽比" in errs[0] and "Shift" in errs[0], errs
    bent = [list(p) for p in pts]
    bent[2] = [bent[2][0] + 3.0, bent[2][1] + 2.0]
    got, errs = rd(_doc(bent, xf, prev))
    assert got is None and any("不再是矩形" in e for e in errs), errs
    flip = [pts[1], pts[0], pts[3], pts[2]]
    got, errs = rd(_doc(flip, xf, prev))
    assert got is None and any("翻转" in e for e in errs), errs
    got, errs = rd(_doc(pts, xf, prev, extra_shapes=[pts]))
    assert got is None and len(errs) == 1 and "找到 2 个对象" in errs[0], errs
    d0 = _doc(pts, xf, prev)
    d0["layers"][1]["kind"]["children"] = []
    got, errs = rd(d0)
    assert got is None and "找到 0 个对象" in errs[0], errs
    got, errs = rd(_doc(pts, xf, prev, photo=False))
    assert got is None and len(errs) == 1 and "photo" in errs[0], errs
    got, errs = rd(_doc(pts, [xf[0], 0.0, 0.0, xf[3] * 1.1, xf[4], xf[5]], prev))
    assert got is None and len(errs) == 1 and "非等比" in errs[0], errs
    #    几样错一起：照片没了 + 两个框 → 两条
    got, errs = rd(_doc(pts, xf, prev, extra_shapes=[pts], photo=False))
    assert got is None and len(errs) == 2, errs
    #    ep-meta 没有 crop
    got, errs = crop.read_frame(_doc(pts, xf, prev), {}, aspect)
    assert got is None and any("photo_size" in e for e in errs), errs
    ok += 1

    # 8) frame_delta 门槛：每个角都 ≤ 0.5 原图 px = 没改（None）；有一个角过了就报
    nudge = [list(p) for p in fp]
    nudge[2] = [nudge[2][0] + 0.4, nudge[2][1] + 0.25]
    assert crop.frame_delta(fp, nudge, width_cm) is None
    nudge[2] = [fp[2][0] + 0.6, fp[2][1]]
    assert crop.frame_delta(fp, nudge, width_cm) is not None
    ok += 1
    return ok


if __name__ == "__main__":
    print("selftest_crop %d 项通过" % run())
