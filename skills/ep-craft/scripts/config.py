#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""marker / 泳道 / 胶体尺寸配置的读取与校验。"""
import json
import os


def load_cfg(path, what):
    """读配置并校验 labels 非空。"""
    if not os.path.exists(path):
        raise RuntimeError("找不到%s：%s（用 --markers / --lanes 指到你的配置）" % (what, path))
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    labels = cfg.get("labels") or []
    if not labels:
        raise RuntimeError("%s 里没有 labels" % path)
    return cfg, labels


def as_entry(e, defaults):
    """labels 项可以是字符串，也可以是 {text,size,color,font}。"""
    if isinstance(e, str):
        return {"text": e}
    return dict(e)


def parse_gel_cm(s, gel):
    """--gel-cm 宽x高：只覆盖**已加载预设**的 width_cm / height_cm。

    它不是预设的替代品（T1）：没有 --gel-size 时调用方必须先用 spec 那一行报错。
    """
    v = [float(t) for t in s.lower().replace("x", " x ").replace("*", " x ").split() if t != "x"]
    if len(v) != 2:
        raise RuntimeError("--gel-cm 要写成 宽x高，如 8.6x7.2（cm）")
    out = dict(gel)
    out["width_cm"], out["height_cm"] = v[0], v[1]
    old = str(gel.get("_source", "") or "")
    out["_source"] = ("%s；" % old if old else "") + "宽高由 --gel-cm %s 覆盖" % s
    return out


def load_markers(path, preset=None):
    """--markers 的文件：单个配置（dict）直接用；预设库（list）要 --preset 选一个。

    preset 可以是 1 起的序号，或 `_preset` 描述里出现过的子串（须唯一命中）。
    返回 (cfg, labels, 说明)。
    """
    if not os.path.exists(path):
        raise RuntimeError("找不到 marker 配置：%s" % path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        cfg, note = data, path
    else:
        names = [str(p.get("_preset", ""))[:60] for p in data]
        listing = "\n".join("  %d. %s" % (i, n) for i, n in enumerate(names, 1))
        if preset is None:
            raise RuntimeError("%s 是预设库（%d 个），要 --preset <序号或描述里的关键词>：\n%s"
                               % (path, len(data), listing))
        if str(preset).isdigit() and 1 <= int(preset) <= len(data):
            cfg = data[int(preset) - 1]
        else:
            hit = [p for p in data if str(preset).lower() in str(p.get("_preset", "")).lower()]
            if len(hit) != 1:
                raise RuntimeError("--preset %r 命中 %d 个（要恰好 1 个）：\n%s" % (preset, len(hit), listing))
            cfg = hit[0]
        note = "%s 的预设 %s" % (path, str(cfg.get("_preset", ""))[:40])
    labels = cfg.get("labels") or []
    if not labels:
        raise RuntimeError("%s 里没有 labels" % path)
    return cfg, labels, note


GEL_REQUIRED = ("name", "width_cm", "height_cm", "lane_count", "lane_pitch_cm")
GEL_ANCHORS = ("bottom", "top", "center")


def load_gel_size(path):
    """胶体物理尺寸预设 JSON（字段见 specs/ep-craft-gel-prior.md §3）。

    必需：name / width_cm / height_cm / lane_count / lane_pitch_cm。
    加载时校验不变式 lane_count × lane_pitch_cm ≈ width_cm（容差 10%）。
    可选：trim_top_cm / trim_bottom_cm / anchor（bottom|top|center，默认 bottom）/
          _source / _说明。
    """
    if not os.path.isfile(path):
        raise RuntimeError("找不到胶体尺寸文件：%s（用 --gel-size 指到 data/gel_size.json）" % path)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in GEL_REQUIRED
               if k not in cfg or cfg[k] is None or cfg[k] == ""]
    if missing:
        raise RuntimeError("%s 缺必需键：%s（必需 %s）"
                           % (path, " / ".join(missing), " / ".join(GEL_REQUIRED)))
    try:
        width = float(cfg["width_cm"])
        lane_count = int(cfg["lane_count"])
        pitch = float(cfg["lane_pitch_cm"])
    except (TypeError, ValueError):
        raise RuntimeError("%s 的 width_cm / lane_count / lane_pitch_cm 必须是数字" % path)
    if width <= 0 or lane_count <= 0 or pitch <= 0:
        raise RuntimeError("%s 的 width_cm / lane_count / lane_pitch_cm 必须是正数" % path)
    implied = lane_count * pitch
    dev = abs(implied - width) / width
    if dev > 0.10:
        raise RuntimeError(
            "%s 的泳道不变式不成立：lane_count × lane_pitch_cm = %d × %.3f = %.3f cm，"
            "但 width_cm = %.3f cm（偏差 %.1f%%，容差 10%%）"
            % (path, lane_count, pitch, implied, width, dev * 100.0))
    anchor = cfg.get("anchor") or "bottom"
    if anchor not in GEL_ANCHORS:
        raise RuntimeError("%s 的 anchor=%r 非法（只接受 %s）"
                           % (path, anchor, " / ".join(GEL_ANCHORS)))
    cfg["anchor"] = anchor
    return cfg
