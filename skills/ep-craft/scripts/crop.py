#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Crop 画板：裁切框的几何、读回与重新裁切（spec ep-craft-crop-artboard §4）。纯 stdlib + magick。

坐标口径（与 gel_photo 文件头一致）
    - 原图 px = **-auto-orient 之后**的像素（magick_io.identify / geometry.load_dims_after 量的就是它）。
    - angle = magick `-rotate` 语义（正 = 顺时针），与 gel_area.compute / geometry.rotate_pt 同一套；
      旋转画布 = magick 扩开的画布 (W1, H1)，原图中心对到画布中心。
    - frame_px = 胶面四角 TL TR BR BL（TL–TR = 加样孔那条边），原图 px。框在照片上**看起来逆时针**
      歪了 θ，就要顺时针转 θ 才摆正 → angle = +θ。
    - 文档里是 pt（y 向下）；预览图 xf 把预览 px 映到 pt，预览 px × photo_size/(width, height) = 原图 px。

    frame_from_stage1 / stage1_from_frame   旋转画布里的胶面框 ⇄ 原图上的四角（互逆，≤ 0.01 px）
    crop_face                               正畸裁切成胶面图 PNG（gel_photo.gel_face 原来那条 magick 命令）
    preview / layout                        Crop 画板上的预览 JPEG 与摆位
    read_frame / frame_delta                从文档读回用户改过的框；和上一次比
"""
import hashlib
import math
import os
import tempfile

import vc_file
from gel_area import _check_canvas
from geometry import load_dims_after
from magick_io import MAGICK, _run, identify


PT_PER_CM = 72.0 / 2.54
MARGIN_CM = 0.3          # 画板 = 预览图外扩 0.3 cm
MOVE_TOL_PX = 0.5        # frame_delta：每个角都没动过 0.5 原图 px = 没改
CORNER_TOL_DEG = 0.5     # 读回：角偏离 90° 超过这么多 = 不是矩形
SIDE_TOL = 0.005         # 读回：对边长差超过 0.5 % = 不是矩形
ASPECT_TOL = 0.01        # 读回：h/w 与预设差超过 1 % = 没按 Shift 等比缩放
PHOTO_TOL = 0.005        # 读回：预览图两个方向的缩放差超过 0.5 % = 被非等比缩放过
# 2026-10-23 之前 render 的裁切是 `-rotate A -crop …`，中间没有 +repage：magick 转完留下虚拟画布偏移
# (−(W1−W0)/2, −(H1−H0)/2)（90° 的整数倍是 0；实测 IM 7.1.1），-crop 的 +x+y 是相对这个偏移量的，
# 所以实际裁到的像素比 gel_box 往右下错了 ((W1−W0)/2, (H1−H0)/2)（1° / 4032×3024 约 1 mm）。
# 现在 crop_face 在 -rotate 后补 +repage，裁到的就是 gel_box；旧文档里内嵌的胶面图仍是错位裁的，
# 补 Crop 画板时用 legacy_page_shift 把框画在它真正裁到的地方。


# ---------------------------------------------------------------- 几何

def _rot(x, y, ang_deg):
    """绕原点转（geometry.rotate_pt 同一个矩阵：y 向下时正角 = 顺时针）。"""
    a = math.radians(ang_deg)
    ca, sa = math.cos(a), math.sin(a)
    return x * ca - y * sa, x * sa + y * ca


def legacy_page_shift(angle, size0, size1):
    """旧版 render（没有 +repage）裁到的像素相对 gel_box 的错位 (dx, dy)，旋转画布 px。只给旧文档用。"""
    a = round(float(angle), 4)                      # magick 拿到的是 "%.4f"
    if a % 90.0 == 0.0:                              # 不转 / 90° 的整数倍：magick 不留偏移
        return 0.0, 0.0
    return (size1[0] - size0[0]) / 2.0, (size1[1] - size0[1]) / 2.0


def frame_from_stage1(gel_box, angle, size0, size1):
    """旋转画布里的胶面框 → 原图上的四角 [[x, y]] × 4（TL TR BR BL）= crop_face 裁到的那块。

    geometry.rotated_rect_bbox 的逆：减画布中心、转 −angle、加原图中心。"""
    (W0, H0), (W1, H1) = size0, size1
    x0, y0, x1, y1 = gel_box
    out = []
    for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        dx, dy = _rot(px - W1 / 2.0, py - H1 / 2.0, -angle)
        out.append([dx + W0 / 2.0, dy + H0 / 2.0])
    return out


def frame_angle(frame_px):
    """上边 TL→TR 看起来逆时针转了多少度（y 向下）= 摆正它要的 magick 角度，范围 (−180, 180]。"""
    (x0, y0), (x1, y1) = frame_px[0], frame_px[1]
    return math.degrees(math.atan2(-(y1 - y0), x1 - x0))


def stage1_from_frame(frame_px, size0, size1_fn, face_aspect):
    """原图四角 → (angle, gel_box)。

    angle 取上边的视觉逆时针角，按 magick 的精度（"%.4f"）取整，让几何与 crop_face 真正转的角度一致；
    size1_fn(angle) → 旋转画布 (W1, H1)（生产用 load_dims_after，测试用闭式）。
    框中心转进旋转画布；宽 = |TR − TL|，
    高 = 宽 × face_aspect（**按预设比例，不看画出来的高**）。"""
    ang = round(frame_angle(frame_px), 4)
    if ang == 0.0:
        ang = 0.0                                   # 去掉 −0.0
    W0, H0 = size0
    W1, H1 = size1_fn(ang)
    cx0 = sum(p[0] for p in frame_px) / 4.0
    cy0 = sum(p[1] for p in frame_px) / 4.0
    dx, dy = _rot(cx0 - W0 / 2.0, cy0 - H0 / 2.0, ang)
    cx, cy = dx + W1 / 2.0, dy + H1 / 2.0
    w = math.hypot(frame_px[1][0] - frame_px[0][0], frame_px[1][1] - frame_px[0][1])
    h = w * face_aspect
    return ang, (cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)


# ---------------------------------------------------------------- 裁切

def crop_face(image, angle, gel_box, width_cm, height_cm, dpi, warn=True):
    """正畸裁切成「胶面图」PNG 字节：宽 width_cm、高 height_cm，dpi 像素/英寸。

    magick：-rotate → +repage（去掉转完留下的画布偏移，裁到的就是 gel_box）→ clamp 到旋转画布 → -crop →
    +repage → -resize WxH! → +profile → -depth 8 → -density。框出画布时 warn=True 打一条 `[!] clipped`（gel_area 同一行；render 里 compute 已经打过，
    gel_face 传 False）。"""
    (_W0, _H0), (W1, H1) = load_dims_after(image, angle)
    bb = gel_box
    if warn:
        _check_canvas(bb, (W1, H1))
    x0, y0 = max(0, int(round(bb[0]))), max(0, int(round(bb[1])))
    x1, y1 = min(W1, int(round(bb[2]))), min(H1, int(round(bb[3])))
    gw = int(round(width_cm / 2.54 * dpi))
    gh = int(round(height_cm / 2.54 * dpi))
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        cmd = [MAGICK, image, "-auto-orient"]
        if abs(angle) > 1e-6:
            cmd += ["-rotate", "%.4f" % angle, "+repage"]
        # 别用 -strip：IM7 里它会把后面设的 dpi 一起丢掉（实测）；+profile 去色彩配置
        cmd += ["-crop", "%dx%d+%d+%d" % (x1 - x0, y1 - y0, x0, y0), "+repage",
                "-resize", "%dx%d!" % (gw, gh), "+profile", "*", "-depth", "8",
                "-units", "PixelsPerInch", "-density", str(dpi), tmp]
        _run(cmd)
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp)


def photo_sha1(path):
    """照片文件 sha1 的前 12 位十六进制（sync 用它认出照片换没换过）。"""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


# ---------------------------------------------------------------- 预览与摆位

def preview(image, long_side=2000):
    """→ (jpeg 字节, (Wp, Hp), k)：自动定向、去色彩配置、质量 85，长边 ≤ long_side，从不放大；
    k = W0 / Wp（每个预览 px 是几个原图 px，≥ 1）。"""
    W0, _H0 = identify(image)
    fd, tmp = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        _run([MAGICK, image, "-auto-orient", "-resize", "%dx%d>" % (long_side, long_side),
              "+profile", "*", "-quality", "85", tmp])
        Wp, Hp = identify(tmp)
        with open(tmp, "rb") as f:
            data = f.read()
    finally:
        os.unlink(tmp)
    return data, (Wp, Hp), W0 / float(Wp)


def layout(frame_px, size0, preview_size, width_cm, x_right):
    """Crop 画板的摆位（pt）。s = 每原图 px 几 pt，让框和 Workbench 上的胶面一样宽；
    预览图 xf = [s·k, 0, 0, s·k, X, Y]（k = W0/Wp）；画板 = 预览图外扩 0.3 cm，右边贴 x_right、上边 y = 0。

    → {"artboard": (x, y, w, h), "image_xf": [...], "frame_pt": [[x, y]] × 4, "s": s}。
    frame_pt 用 read_frame 的映射的严格逆（纵向按 Hp/H0，不是 1/k），读回来就是原来的 frame_px。"""
    W0, H0 = size0
    Wp, Hp = preview_size
    fw = math.hypot(frame_px[1][0] - frame_px[0][0], frame_px[1][1] - frame_px[0][1])
    s = width_cm * PT_PER_CM / fw
    k = W0 / float(Wp)
    m = MARGIN_CM * PT_PER_CM
    iw, ih = Wp * s * k, Hp * s * k
    ab = (x_right - iw - 2 * m, 0.0, iw + 2 * m, ih + 2 * m)
    X, Y = ab[0] + m, m
    sk = s * k
    pts = [[X + sk * (x * Wp / float(W0)), Y + sk * (y * Hp / float(H0))] for x, y in frame_px]
    return {"artboard": ab, "image_xf": [sk, 0.0, 0.0, sk, X, Y], "frame_pt": pts, "s": s}


# ---------------------------------------------------------------- 读回

def _objects(lay):
    """图层里的非组对象（进组）。"""
    return [n for l, n in vc_file.walk({"layers": [lay]}) if n["kind"].get("type") != "group"]


def _layer(document, name, errs):
    """名字唯一的顶层图层；重名记一条错、返回 None（没有就 None，由调用方按「找到 0 个」报）。"""
    try:
        return vc_file.layer_by_name(document, name)
    except RuntimeError as e:
        errs.append("%s 图层：%s" % (name, e))
        return None


def _corners(node):
    """闭合的单子路径 4 锚点对象 → 四个 (x, y) pt；别的 → None。"""
    k = node["kind"]
    if k.get("type") != "path":
        return None
    sps = (k.get("path") or {}).get("subpaths") or []
    if len(sps) != 1 or not sps[0].get("closed") or len(sps[0].get("anchors") or []) != 4:
        return None
    return [tuple(float(v) for v in a["p"]) for a in sps[0]["anchors"]]


def _shape_errors(fp, face_aspect):
    """原图 px 四角 → 形状错误（不是矩形 / 比例不对），每类最多一条。"""
    errs = []
    sides = [math.hypot(fp[(i + 1) % 4][0] - fp[i][0], fp[(i + 1) % 4][1] - fp[i][1]) for i in range(4)]
    if min(sides) < 1e-6:
        return ["裁切框有一条边长为 0 —— 撤销那一步；只能移动 / 旋转 / 缩放"]
    worst, flipped = 0.0, False
    for i in range(4):
        a, b, c = fp[i - 1], fp[i], fp[(i + 1) % 4]
        u = (a[0] - b[0], a[1] - b[1])
        v = (c[0] - b[0], c[1] - b[1])
        cos = (u[0] * v[0] + u[1] * v[1]) / (math.hypot(*u) * math.hypot(*v))
        worst = max(worst, abs(math.degrees(math.acos(max(-1.0, min(1.0, cos)))) - 90.0))
        e1 = (b[0] - a[0], b[1] - a[1])
        if e1[0] * v[1] - e1[1] * v[0] < 0:            # y 向下时 TL→TR→BR 应是顺时针（叉积 > 0）
            flipped = True
    dside = max(abs(sides[0] - sides[2]) / max(sides[0], sides[2]),
                abs(sides[1] - sides[3]) / max(sides[1], sides[3]))
    if flipped:
        errs.append("裁切框被翻转了（角的顺序不再是 左上→右上→右下→左下）—— 撤销翻转；只能移动 / 旋转 / 缩放")
    elif worst > CORNER_TOL_DEG or dside > SIDE_TOL:
        errs.append("裁切框不再是矩形（角偏 %.2f°、对边差 %.2f%%，限 %.1f° / %.1f%%）—— 撤销变形；只能移动 / 旋转 / 缩放"
                    % (worst, dside * 100, CORNER_TOL_DEG, SIDE_TOL * 100))
    w = (sides[0] + sides[2]) / 2.0
    h = (sides[1] + sides[3]) / 2.0
    if face_aspect and abs(h / w - face_aspect) / face_aspect > ASPECT_TOL:
        errs.append("裁切框高宽比 %.4f，预设是 %.4f（差 %.1f%%，限 %.0f%%）—— 按住 Shift 等比缩放"
                    % (h / w, face_aspect, abs(h / w - face_aspect) / face_aspect * 100, ASPECT_TOL * 100))
    return errs


def read_frame(document, meta, face_aspect):
    """活文档 / 存盘文档（top["document"] 那一层）→ (frame_px 或 None, errors[])。

    None = 没有 `Crop` 画板（旧文档，errors 为空）。四角取 `crop` 图层那个闭合 4 锚点路径的锚点
    （锚点总在，不看 live），pt → 预览 px 用 `photo` 图层 `photo` 图片 xf 的逆，→ 原图 px 再乘
    photo_size / (width, height)。错误一次全列出（调用方前缀 `Crop：`），有错时 frame_px = None。
    框落到照片外**不算**错（crop_face 照 render 的规矩 clamp）。"""
    if not any(ab.get("name") == "Crop" for ab in document.get("artboards") or []):
        return None, []
    errs = []
    crop = (meta or {}).get("crop") or {}
    size0 = crop.get("photo_size")
    if not size0 or len(size0) != 2:
        errs.append("ep-meta 里没有 crop.photo_size（不是本 skill 写的 Crop 画板？）—— 重新 render")

    # 照片：photo 图层里叫 photo 的图片，等比缩放
    to_px = None
    play = _layer(document, "photo", errs)
    imgs = [n for n in (_objects(play) if play else [])
            if n["kind"].get("type") == "image" and n.get("name") == "photo"]
    if len(imgs) != 1:
        errs.append("photo 图层里要有一张叫 photo 的照片（找到 %d 张）—— 重新 render" % len(imgs))
    else:
        k = imgs[0]["kind"]
        a, b, c, d, e, f = (float(v) for v in k["xf"])
        sx, sy, det = math.hypot(a, b), math.hypot(c, d), a * d - b * c
        if abs(det) < 1e-12 or abs(sx - sy) / max(sx, sy) > PHOTO_TOL or abs(a * c + b * d) / (sx * sy) > PHOTO_TOL:
            errs.append("photo 照片被非等比缩放 / 斜切过（xf %s）—— 重新 render" % [round(v, 4) for v in (a, b, c, d)])
        elif size0 and len(size0) == 2:
            fx, fy = float(size0[0]) / float(k["width"]), float(size0[1]) / float(k["height"])

            def to_px(x, y, a=a, b=b, c=c, d=d, e=e, f=f, det=det, fx=fx, fy=fy):
                u = (d * (x - e) - c * (y - f)) / det
                v = (-b * (x - e) + a * (y - f)) / det
                return [u * fx, v * fy]

    # 框：crop 图层里恰好一个闭合 4 角形状
    clay = _layer(document, "crop", errs)
    objs = _objects(clay) if clay else []
    shapes = [o for o in objs if _corners(o)]
    pts = None
    if len(objs) != 1 or len(shapes) != 1:
        errs.append("crop 图层里要有且只有一个闭合 4 角的框（找到 %d 个对象，其中 %d 个是闭合 4 角形状）"
                    "—— 删掉多余的 / 撤销删除" % (len(objs), len(shapes)))
    else:
        pts = _corners(shapes[0])
    if pts is None or to_px is None:
        return None, errs
    fp = [to_px(x, y) for x, y in pts]
    errs += _shape_errors(fp, face_aspect)
    return (None if errs else fp), errs


def frame_delta(old_px, new_px, width_cm):
    """→ None（每个角都没动过 0.5 原图 px）或 {"angle_deg", "dx_cm", "dy_cm", "scale"}：

    angle_deg = 新框比旧框多（视觉）逆时针转的度数（= magick 角度之差；VectorCraft object.rotate 也是
    逆时针为正）；dx/dy = 框中心的位移，按**旧框**的胶面方向（x 沿孔那条边、y 向下）、旧比例尺
    （|TR − TL| / width_cm 原图 px/cm）折成胶面 cm；scale = 新宽 / 旧宽。"""
    if all(math.hypot(n[0] - o[0], n[1] - o[1]) <= MOVE_TOL_PX for o, n in zip(old_px, new_px)):
        return None
    a0, a1 = frame_angle(old_px), frame_angle(new_px)
    da = (a1 - a0 + 180.0) % 360.0 - 180.0
    w0 = math.hypot(old_px[1][0] - old_px[0][0], old_px[1][1] - old_px[0][1])
    w1 = math.hypot(new_px[1][0] - new_px[0][0], new_px[1][1] - new_px[0][1])
    c0 = [sum(p[i] for p in old_px) / 4.0 for i in (0, 1)]
    c1 = [sum(p[i] for p in new_px) / 4.0 for i in (0, 1)]
    dx, dy = _rot(c1[0] - c0[0], c1[1] - c0[1], a0)   # 原图方向 → 旧框（旋转画布）方向
    ppc = w0 / float(width_cm)
    return {"angle_deg": da, "dx_cm": dx / ppc, "dy_cm": dy / ppc, "scale": w1 / w0}
