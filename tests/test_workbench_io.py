#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_workbench_io：Workbench export/import 的纯数据回归测试（不开 VectorCraft、不读用户文件）。

覆盖：导出字段/活标签名、schema 与有限数字校验、胶面尺寸绑定、泳道计数变化、window/band/trim/
sample band 的真实几何 roundtrip、lane 名重定、低置信标记跨文档传播，以及旧文档缺 peak_auto 时
拒绝无法保证的 sample band 导入。导入计划用 figure.apply_ops_json（与活文档执行器一一对应）
落到内存文档上，再用 workbench.from_document 读回，因此测的是真正的几何改动。
"""
import copy
import json
import os
import struct
import sys
import tempfile
import zlib


from epcraft import doc_edit                                                                         # noqa: E402
from epcraft import figure                                                                           # noqa: E402
from epcraft import vc_file                                                                          # noqa: E402
from epcraft import workbench                                                                        # noqa: E402
from epcraft import workbench_io                                                                     # noqa: E402


def _png(w, h, rgb=(128, 128, 128)):
    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))
    rows = b"".join(b"\0" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _build(lanes=None, bands=None, marker_lane=1, window=None, names=None, low_confidence=None,
           peaks=(), labels=None, meta_extra=None, gel=None):
    """内存 .vectorcraft 文档 + 模型：Workbench 画板、全部图层、gel 图、ep-meta、label lane n。"""
    lanes = lanes or [{"left_cm": 0.1, "right_cm": 1.2, "trimmed": False},
                      {"left_cm": 1.2, "right_cm": 2.4, "trimmed": False},
                      {"left_cm": 2.4, "right_cm": 3.6, "trimmed": False}]
    bands = bands or [{"mw": 250.0, "y_cm": 0.8}, {"mw": 100.0, "y_cm": 1.5},
                      {"mw": 50.0, "y_cm": 2.4}]
    names = names or ["M", "1", "2"]
    gel = gel or {"width_cm": 5.0, "height_cm": 3.5}
    window = window or {"top_cm": 0.15, "bottom_cm": 3.3}
    centres = [round((ln["left_cm"] + ln["right_cm"]) / 2.0, 4) for ln in lanes]
    meta = {"lane_names": list(names), "prev_centres_cm": centres,
            "bands": [[float(b["mw"]), float(b["y_cm"])] for b in bands], "dpi": 127}
    if low_confidence:
        meta["marker_confidence"] = {"ok": False, "picked": 3, "need": 3, "gate": "断崖不够", "notes": []}
    if meta_extra:
        meta.update(meta_extra)
    model = {"gel": dict(gel), "window": dict(window),
             "lanes": [dict(ln) for ln in lanes], "bands": [dict(b) for b in bands],
             "marker_lane": int(marker_lane), "meta": meta}
    assert not workbench.validate(model), workbench.validate(model)

    doc = vc_file.new_doc("selftest-io")
    png = _png(60, 42)                                  # 5.0 : 3.5 = 60 : 42
    key, w, h = vc_file.add_image(doc, png)
    layers = workbench.make_layers(doc)
    workbench.add_workbench(doc, layers, model, key, w, h, x0=14.0, peaks=peaks)
    texts = labels if labels is not None else [("" if n == "" else n) for n in names]
    for j, text in enumerate(texts, 1):
        vc_file.lay_children(layers["labels"]).append(
            vc_file.text(doc, "label lane %d" % j, 20.0 + 20.0 * j, 4.0, text, size=6))
    return doc, doc["top"]["document"], model


def _export(document, model=None):
    data = workbench_io.build_export(document, model)
    return json.loads(json.dumps(data))                 # 走一遍 JSON，确保可序列化


def _apply(document, ops):
    top = {"document": copy.deepcopy(document)}
    figure.apply_ops_json(top, ops)
    return top["document"]


def _expect(fn, frag):
    try:
        fn()
    except (RuntimeError, ValueError) as e:
        assert frag in str(e), "%r 不在错误里：%s" % (frag, e)
    else:
        raise AssertionError("应当报错（含 %r）" % frag)


def _tick_sets(document, model):
    return sorted((lane, y) for lane, y, _x in workbench_io._valid_ticks(document, model))


# ---------------------------------------------------------------- 用例

def test_export_shape():
    doc, document, model = _build(labels=["WT", "?", "2"],
                                  peaks=[(2, 1.6), (2, 2.7), (3, 2.0)])
    data = _export(document, model)
    assert data["schema"] == workbench_io.SCHEMA and data["version"] == workbench_io.VERSION
    assert data["gel"] == {"width_cm": 5.0, "height_cm": 3.5}
    assert data["window"] == {"top_cm": 0.15, "bottom_cm": 3.3}
    assert data["marker_lane"] == 1 and len(data["bands"]) == 3
    assert [ln["name"] for ln in data["lanes"]] == ["WT", None, "2"]   # 活标签（未存）+ "?" → null
    assert data["lanes"][2]["trimmed"] is False
    assert data["sample_bands"] == [{"lane": 2, "y_cm": 1.6}, {"lane": 2, "y_cm": 2.7},
                                    {"lane": 3, "y_cm": 2.0}]
    assert data["low_confidence"] is False
    # 没有 sample bands 图层的文档：键应当缺席（import 不碰刻度）
    lay = vc_file.layer_by_name(document, "sample bands")
    document["layers"] = [l for l in document["layers"] if l["id"] != lay["id"]]
    data2 = _export(document, workbench.from_document(document)[0])
    assert "sample_bands" not in data2


def test_validate_rules():
    _doc, document, model = _build()
    good = workbench_io.validate_export(_export(document, model), model)
    assert len(good["lanes"]) == 3 and len(good["bands"]) == 3

    bad = copy.deepcopy(good)
    bad["version"] = 2
    _expect(lambda: workbench_io.validate_export(bad, model), "version 1")

    bad = copy.deepcopy(good)
    bad["window"]["top_cm"] = float("nan")
    _expect(lambda: workbench_io.validate_export(bad, model), "有限数字")

    bad = copy.deepcopy(good)
    bad["gel"]["width_cm"] = 6.0
    _expect(lambda: workbench_io.validate_export(bad, model), "与目标文档")

    bad = copy.deepcopy(good)
    bad["lanes"][1]["left_cm"] = 1.3
    _expect(lambda: workbench_io.validate_export(bad, model), "不连续")

    bad = copy.deepcopy(good)
    bad["bands"][1]["mw"] = 300.0
    _expect(lambda: workbench_io.validate_export(bad, model), "分子量")

    bad = copy.deepcopy(good)
    bad["marker_lane"] = 9
    _expect(lambda: workbench_io.validate_export(bad, model), "超出")

    bad = copy.deepcopy(good)
    bad["sample_bands"] = [{"lane": 1, "y_cm": 2.0}]
    _expect(lambda: workbench_io.validate_export(bad, model), "marker 泳道")

    bad = copy.deepcopy(good)
    del bad["lanes"]
    _expect(lambda: workbench_io.validate_export(bad, model), "缺少 'lanes'")


def test_unmodified_roundtrip_is_noop():
    _doc, document, model = _build(peaks=[(2, 1.6), (2, 2.7)])
    data = workbench_io.validate_export(_export(document, model), model)
    ops, _meta, _lines, _warns = workbench_io.plan_import(document, model, data)
    assert ops == [], ops
    after = _apply(document, ops)
    model2, _notes = workbench.from_document(after)
    assert _export(after, model2) == _export(document, model)


def test_geometry_and_names_roundtrip():
    _doc, document, model = _build(peaks=[(2, 1.6), (2, 2.7), (3, 2.0)],
                                  meta_extra={"peak_auto": {"2": [1.6, 2.7], "3": [2.0]}})
    data = _export(document, model)
    data["lanes"][0]["right_cm"] = 1.4
    data["lanes"][1]["left_cm"] = 1.4
    data["window"]["bottom_cm"] = 3.2
    data["bands"][1] = {"mw": 90.0, "y_cm": 1.7}
    data["marker_lane"] = 2
    data["lanes"][2]["trimmed"] = True
    data["lanes"][1]["name"] = "A"
    data["sample_bands"] = [{"lane": 1, "y_cm": 1.9}, {"lane": 3, "y_cm": 2.0}]
    clean = workbench_io.validate_export(data, model)
    ops, merged_meta, lines, _warns = workbench_io.plan_import(document, model, clean)
    assert ops and any(op["op"] in ("reshape", "create", "delete") for op in ops), [o["op"] for o in ops]
    after = _apply(document, ops)
    m2, _notes = workbench.from_document(after)

    assert m2["window"] == {"top_cm": 0.15, "bottom_cm": 3.2}, m2["window"]
    assert [round(ln["right_cm"] - ln["left_cm"], 3) for ln in m2["lanes"]] == [1.3, 1.0, 1.2], m2["lanes"]
    assert m2["lanes"][2]["trimmed"] is True
    assert m2["marker_lane"] == 2
    assert [b["mw"] for b in m2["bands"]] == [250.0, 90.0, 50.0]
    assert [round(b["y_cm"], 3) for b in m2["bands"]] == [0.8, 1.7, 2.4]
    assert doc_edit.current_names(after, m2) == ["M", "A", "2"], doc_edit.current_names(after, m2)
    assert merged_meta["lane_names"] == ["M", "A", "2"]
    assert merged_meta["prev_centres_cm"] == workbench.centres(m2)
    assert set(i for i, ln in enumerate(m2["lanes"], 1) if ln["trimmed"]) == {3}
    assert _tick_sets(after, m2) == [(1, 1.9), (3, 2.0)]
    # 缺 peak_auto 的旧文档会拒绝；这份有 peak_auto → override 保证 sync 复现导入刻度
    m2_meta = doc_edit._meta(after)[1]
    assert m2_meta["peak_overrides"]["1"] == {"add": [1.9]}, m2_meta["peak_overrides"]
    assert "marker_confidence" not in m2_meta
    # 再导一次应当完全等值
    data2 = _export(after, m2)
    assert data2["window"] == data["window"] and data2["marker_lane"] == data["marker_lane"]
    assert data2["lanes"] == data["lanes"] and data2["bands"] == data["bands"]
    assert data2["sample_bands"] == data["sample_bands"]


def test_lane_count_change_rebases_names():
    _doc, document, model = _build(peaks=[(2, 1.6), (2, 2.7)],
                                   meta_extra={"peak_auto": {"2": [1.6, 2.7]}})
    data = _export(document, model)
    data["lanes"] = [{"left_cm": 0.1, "right_cm": 1.0, "trimmed": False, "name": "M"},
                     {"left_cm": 1.0, "right_cm": 1.9, "trimmed": False, "name": "A"},
                     {"left_cm": 1.9, "right_cm": 2.8, "trimmed": False, "name": "B"},
                     {"left_cm": 2.8, "right_cm": 3.6, "trimmed": False, "name": "C"}]
    data["marker_lane"] = 2
    data["sample_bands"] = []
    clean = workbench_io.validate_export(data, model)
    ops, merged_meta, lines, _warns = workbench_io.plan_import(document, model, clean)
    assert any(op["op"] == "create" and op["layer"] == "lanes" for op in ops), ops
    after = _apply(document, ops)
    m2, _notes = workbench.from_document(after)
    assert len(m2["lanes"]) == 4 and m2["marker_lane"] == 2
    assert doc_edit.current_names(after, m2) == ["M", "A", "B", "C"], doc_edit.current_names(after, m2)
    assert merged_meta["lane_names"] == ["M", "A", "B", "C"]
    assert merged_meta["prev_centres_cm"] == workbench.centres(m2)
    # 再导一次：不应再增删线 / 改名
    clean2 = workbench_io.validate_export(_export(after, m2), m2)
    ops2, _m2, _l2, _w2 = workbench_io.plan_import(after, m2, clean2)
    assert ops2 == [], ops2


def test_low_confidence_flag_propagation():
    _sdoc, sdoc, smodel = _build(low_confidence=True)
    source = _export(sdoc, smodel)
    assert source["low_confidence"] is True

    _tdoc, tdoc, tmodel = _build(low_confidence=False)
    clean = workbench_io.validate_export(source, tmodel)
    ops, merged, lines, _w = workbench_io.plan_import(tdoc, tmodel, clean)
    assert merged["marker_confidence"]["ok"] is False        # 源低置信 → 目标也是低置信
    assert merged["bands"] == [[b["mw"], b["y_cm"]] for b in clean["bands"]]
    after = _apply(tdoc, ops)
    assert doc_edit._meta(after)[1]["marker_confidence"]["ok"] is False

    # 反过来：导入非低置信永远不“证明”已有低置信标记（false 不清除）
    _tdoc2, tdoc2, tmodel2 = _build(low_confidence=True)
    clean2 = workbench_io.validate_export(source, tmodel2)
    clean2["low_confidence"] = False
    ops2, merged2, _l, _w = workbench_io.plan_import(tdoc2, tmodel2, clean2)
    assert merged2["marker_confidence"]["ok"] is False
    assert doc_edit._meta(_apply(tdoc2, ops2))[1]["marker_confidence"]["ok"] is False



def test_legacy_without_peak_auto_refuses_sample_edits():
    _doc, document, model = _build(peaks=[(2, 1.6), (2, 2.7)], meta_extra={"peak_auto": None})
    # add_workbench 写出的 meta 里 peak_auto 为 None；删掉键模拟旧版 render
    m = dict(model)
    m["meta"] = {k: v for k, v in model["meta"].items() if k != "peak_auto"}
    data = _export(document, m)
    clean = workbench_io.validate_export(data, m)
    ops, _meta, _lines, _w = workbench_io.plan_import(document, m, clean)
    assert ops == [] or not any(o["op"] == "create" and o["layer"] == "sample bands" for o in ops), ops

    data2 = copy.deepcopy(clean)
    data2["sample_bands"] = [{"lane": 2, "y_cm": 1.9}]
    _expect(lambda: workbench_io.plan_import(document, m, workbench_io.validate_export(data2, m)), "peak_auto")


def test_import_file_is_valid_json_only():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "bad.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        _expect(lambda: workbench_io._load_json(path), "合法 JSON")
        _expect(lambda: workbench_io._load_json(os.path.join(td, "missing.json")), "找不到")


# ---------------------------------------------------------------- 入口

TESTS = (test_export_shape, test_validate_rules, test_unmodified_roundtrip_is_noop,
         test_geometry_and_names_roundtrip, test_lane_count_change_rebases_names,
         test_low_confidence_flag_propagation, test_legacy_without_peak_auto_refuses_sample_edits,
         test_import_file_is_valid_json_only)


def run():
    """→ 通过的用例数；任何断言失败都抛出去（tests 聚合入口靠异常判失败）。"""
    passed = 0
    for fn in TESTS:
        fn()
        passed += 1
    print("Workbench IO tests: %d/%d passed" % (passed, len(TESTS)))
    return passed


if __name__ == "__main__":
    run()
