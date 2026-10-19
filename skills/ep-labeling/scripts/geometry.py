#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旋转 / 定标几何与配置里的位置口径。"""
import math

from magick_io import identify


def rotate_pt(x, y, cx, cy, ang_deg):
    a = math.radians(ang_deg)
    ca, sa = math.cos(a), math.sin(a)
    dx, dy = x - cx, y - cy
    return cx + dx * ca - dy * sa, cy + dx * sa + dy * ca


def rotated_rect_bbox(rect, angle, size, size_after):
    """rect=(x0,y0,x1,y1) 原图坐标；angle 用 magick -rotate 语义；返回旋转后坐标系里的轴对齐框。"""
    W0, H0 = size
    W1, H1 = size_after
    cx, cy = W0 / 2.0, H0 / 2.0
    nx, ny = W1 / 2.0, H1 / 2.0
    xs, ys = [], []
    for px, py in ((rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3])):
        a = math.radians(angle)
        ca, sa = math.cos(a), math.sin(a)
        dx, dy = px - cx, py - cy
        rx, ry = dx * ca - dy * sa, dx * sa + dy * ca
        xs.append(rx + nx)
        ys.append(ry + ny)
    return (min(xs), min(ys), max(xs), max(ys)), (W1, H1)


def load_dims_after(path, angle):
    W0, H0 = identify(path)
    if abs(angle) < 1e-6:
        return (W0, H0), (W0, H0)
    W1, H1 = identify(path, ["-rotate", "%.4f" % angle])
    return (W0, H0), (W1, H1)


def pos_or_frac(v, gel_a, gel_b, scale=1.0):
    """配置里的位置：<1 当「占胶面宽度的比例」，≥1 当输出像素（两种写法都能用，见文档）。"""
    v = float(v)
    return (gel_a + v * (gel_b - gel_a)) * scale if v < 1 else v

def lane_pitch(lane_x):
    """泳道带宽 = 相邻泳道中心距的中位数。"""
    if len(lane_x) < 2:
        return 0.0
    gaps = sorted(b - a for a, b in zip(lane_x, lane_x[1:]))
    return gaps[len(gaps) // 2]
