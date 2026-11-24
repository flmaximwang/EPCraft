#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_cli：公开 `epcraft` 命令层自检 —— 不启动 VectorCraft、不读用户文件。

覆盖：新旧两套 parser 的形状、四个组共用 sync / edit、缺 action 的组报 usage、
launch 的 connect/open、figure export 的画板选择与输出保护、workbench export/import
的参数转交，以及 main 的一行错误边界（RuntimeError / OSError / ValueError → 2）。

运行：python3 -m epcraft.selftest_cli（在 src/ 上或安装后）
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path


import epcraft                    # noqa: E402
from . import canvas_cli          # noqa: E402
from . import gel_photo           # noqa: E402
from . import vc_control          # noqa: E402


# ---------------------------------------------------------------- 小工具

class _FakeClient:
    def __init__(self, artboards=None):
        self.artboards = artboards if artboards is not None else [
            {"name": "Workbench"}, {"name": "Figure"}, {"name": "Profiles"}]
        self.exported = []
        self.opened = []
        self.activated = []

    def inspect(self, depth=0):
        return {"artboards": self.artboards}

    def execute(self, command, params=None):
        if command == "document.activate":
            self.activated.append(params["index"])
        return {}

    def open(self, path):
        self.opened.append(path)
        return {}

    def export_png(self, path, artboard, dpi, background="white"):
        self.exported.append((path, artboard, dpi, background))
        with open(path, "wb") as f:
            f.write(b"\x89PNG selftest")
        return 10, 20, dpi


@contextlib.contextmanager
def _patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def _run(argv, canvas=True):
    """跑 main，返回 (rc, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = gel_photo.main(argv, canvas=canvas)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------- parser

def test_parser_hierarchy():
    ap = gel_photo.build_parser(canvas=True)
    cases = [
        (["launch"], "launch", None, "cmd_launch"),
        (["launch", "fig.vectorcraft", "--port", "7800"], "launch", None, "cmd_launch"),
        (["figure", "render", "x.jpg", "--out", "f.vectorcraft"], "figure", "render", "cmd_render"),
        (["figure", "edit", "f.vectorcraft"], "figure", "edit", "cmd_edit"),
        (["figure", "sync", "f.vectorcraft"], "figure", "sync", "cmd_sync"),
        (["figure", "export", "f.vectorcraft"], "figure", "export", "cmd_figure_export"),
        (["crop", "probe", "x.jpg"], "crop", "probe", "cmd_probe"),
        (["crop", "sync", "f.vectorcraft"], "crop", "sync", "cmd_sync"),
        (["workbench", "edit", "f.vectorcraft"], "workbench", "edit", "cmd_edit"),
        (["workbench", "sync", "f.vectorcraft"], "workbench", "sync", "cmd_sync"),
        (["workbench", "export", "f.vectorcraft"], "workbench", "export", "cmd_workbench_export"),
        (["workbench", "import", "f.vectorcraft", "--input", "d.json"],
         "workbench", "import", "cmd_workbench_import"),
        (["profile", "sync", "f.vectorcraft"], "profile", "sync", "cmd_sync"),
        (["densitometry", "measure", "fig.mw.json"], "densitometry", "measure", "cmd_measure"),
        (["dens", "measure", "fig.mw.json", "--lanes", "2-4"], "dens", "measure", "cmd_measure"),
        (["doctor"], "doctor", None, "cmd_doctor"),
        (["selftest"], "selftest", None, "cmd_selftest_all"),
    ]
    for argv, cmd, action, func in cases:
        a = ap.parse_args(argv)
        assert a.cmd == cmd, (argv, a.cmd)
        assert getattr(a, "action", None) == action, (argv, getattr(a, "action", None))
        assert a.func.__name__ == func, (argv, a.func)

    # 默认值：port 7797、figure export 固定 Figure 画板 / 300 dpi / out=None
    a = ap.parse_args(["launch"])
    assert a.port == vc_control.DEFAULT_PORT and a.doc is None
    a = ap.parse_args(["figure", "export", "f.vectorcraft"])
    assert a.dpi == 300 and a.out is None and a.background == "white"
    assert not hasattr(a, "artboard"), "figure export 不应暴露任意画板选择"
    a = ap.parse_args(["workbench", "export", "f.vectorcraft"])
    assert a.out is None and a.port == vc_control.DEFAULT_PORT
    a = ap.parse_args(["workbench", "import", "f.vectorcraft", "--input", "d.json"])
    assert a.input == "d.json" and a.no_sync is False and a.dpi == 300
    a = ap.parse_args(["figure", "sync", "f.vectorcraft"])
    assert a.reset_peaks is None and a.dry_run is False and a.dpi == 300

    # densitometry measure 的默认值（＝§4.1 的常量）与旗标
    a = ap.parse_args(["dens", "measure", "fig.mw.json"])
    assert a.flank_cm == 0.15 and a.min_ink == 0.5 and a.out is None and a.lanes is None
    assert a.include_marker is False and a.include_trimmed is False
    assert ap.parse_args(["dens", "measure", "fig.mw.json", "--include-marker"]).include_marker is True

    # 顶层不再有旧的扁平命令，组内也不会混入别的组的命令
    assert set(ap._subparsers._group_actions[0].choices) == {
        "figure", "crop", "workbench", "profile", "launch", "workflow", "densitometry", "dens",
        "selftest", "doctor"}
    for argv in (["probe", "x.jpg"], ["render", "x.jpg", "--out", "f"],
                 ["figure", "probe", "x.jpg"], ["crop", "render", "x.jpg", "--out", "f"],
                 ["figure", "export", "f.vectorcraft", "--artboard", "Workbench"]):
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                ap.parse_args(argv)
            except SystemExit:
                pass
            else:
                raise AssertionError("该被拒绝的命令通过了：%r" % (argv,))

    # 组必须带 action：每个组单独出现时 argparse 打 usage 并以 2 退出
    for group in ("figure", "crop", "workbench", "profile", "densitometry", "dens"):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                ap.parse_args([group])
            except SystemExit as e:
                assert e.code == 2, (group, e.code)
            else:
                raise AssertionError("组 %s 不带 action 却通过了" % group)
        assert "usage:" in err.getvalue() and group in err.getvalue(), (group, err.getvalue())


def test_legacy_parser_unchanged():
    ap = gel_photo.build_parser(canvas=False)
    for argv, func in ((["probe", "x.jpg"], "cmd_probe"),
                       (["render", "x.jpg", "--out", "f"], "cmd_render"),
                       (["sync", "f.vectorcraft"], "cmd_sync"),
                       (["edit", "f.vectorcraft"], "cmd_edit"),
                       (["export", "old.scene.json"], "_removed_export"),
                       (["doctor"], "cmd_doctor"),
                       (["selftest"], "cmd_selftest_legacy")):
        a = ap.parse_args(argv)
        assert a.func.__name__ == func, (argv, a.func)
    # 自动识别：旧扁平入口也接受新层级，但老命令仍走扁平 parser
    with _patched(gel_photo, "cmd_sync", lambda a: 0):
        rc, _out, _err = _run(["sync", "f.vectorcraft"], canvas=None)
        assert rc == 0
        rc, out, _err = _run(["figure", "sync", "f.vectorcraft"], canvas=None)
        assert rc == 0 and "usage:" not in out


# ---------------------------------------------------------------- dispatch

def test_shared_sync_and_edit_dispatch():
    seen = []
    with _patched(gel_photo, "cmd_sync", lambda a: (seen.append(("sync", a.doc)) or 0)), \
            _patched(gel_photo, "cmd_edit", lambda a: (seen.append(("edit", a.doc)) or 0)):
        for group in ("figure", "crop", "workbench", "profile"):
            rc, _o, err = _run([group, "sync", "f.vectorcraft"])
            assert rc == 0, (group, err)
        for group in ("figure", "workbench"):
            rc, _o, err = _run([group, "edit", "f.vectorcraft", "--label", "3=WT"])
            assert rc == 0, (group, err)
    assert seen == [("sync", "f.vectorcraft")] * 4 + [("edit", "f.vectorcraft")] * 2, seen


def test_error_boundary():
    for exc in (RuntimeError("boom"), OSError("no such file"), ValueError("bad value")):
        def boom(_a, exc=exc):
            raise exc
        with _patched(gel_photo, "cmd_doctor", boom):
            rc, out, err = _run(["doctor"])
        assert rc == 2, (exc, rc)
        assert out == "" and err.startswith("[!] ") and "\n" not in err.rstrip("\n"), (exc, out, err)
        assert str(exc) in err


# ---------------------------------------------------------------- launch

def test_launch():
    fake = _FakeClient()
    calls = {}

    def connect(port, path=None, launch=True):
        calls.update(port=port, path=path, launch=launch)
        return fake, True

    with _patched(vc_control, "connect", connect), \
            _patched(canvas_cli, "_ensure_open", lambda client, path: 0):
        rc, out, err = _run(["launch", "--port", "7801"])
        assert rc == 0 and calls == {"port": 7801, "path": None, "launch": True}, (rc, calls, err)
        assert "7801" in out

        with tempfile.TemporaryDirectory() as td:
            doc = os.path.join(td, "fig.vectorcraft")
            open(doc, "wb").close()
            rc, out, err = _run(["launch", doc])
            assert rc == 0, err
            assert calls["path"] == os.path.abspath(doc), calls
            assert os.path.abspath(doc) in out

            # 文档不存在：先失败，绝不启动 app（connect 不被调用，path 还停在上一份文档）
            rc, _o, err = _run(["launch", os.path.join(td, "missing.vectorcraft")])
            assert rc == 2 and "missing.vectorcraft" in err, err
            assert calls["path"] == os.path.abspath(doc), "缺文档时不应调用 connect"


# ---------------------------------------------------------------- figure export

def test_figure_export():
    fake = _FakeClient()
    with tempfile.TemporaryDirectory() as td:
        doc = os.path.join(td, "fig.vectorcraft")
        with open(doc, "wb") as f:
            f.write(b"document bytes")
        calls = {"connect": 0}

        def connect(port, path=None, launch=True):
            calls["connect"] += 1
            return fake, False

        with _patched(vc_control, "connect", connect), \
                _patched(canvas_cli, "_ensure_open", lambda client, path: 0):
            # 默认输出路径 + Figure 画板（按名字找到下标 1）+ 300 dpi
            rc, out, err = _run(["figure", "export", doc])
            assert rc == 0, err
            assert fake.exported[-1] == (os.path.join(td, "fig.png"), 1, 300, "white"), fake.exported[-1]
            assert os.path.exists(os.path.join(td, "fig.png"))

            # 指定 dpi / 背景 / 输出路径（画板仍固定 Figure）
            out2 = os.path.join(td, "fig-hi.png")
            rc, _o, err = _run(["figure", "export", doc, "--out", out2,
                                "--dpi", "150", "--background", "black"])
            assert rc == 0, err
            assert fake.exported[-1] == (out2, 1, 150, "black"), fake.exported[-1]

            # 文档里没有 Figure 画板：报错并列出已有画板
            nofig = _FakeClient(artboards=[{"name": "Workbench"}, {"name": "Profiles"}])
            with _patched(vc_control, "connect", lambda *a, **k: (nofig, False)):
                rc, _o, err = _run(["figure", "export", doc])
            assert rc == 2 and "Figure" in err and "Workbench" in err, err

            # 输出保护：只允许 .png；失败如果发生在连接 app 之前，app 不应被启动。
            before = calls["connect"]
            for bad in (doc, os.path.join(td, "other.vectorcraft"),
                        os.path.join(td, "lane.json"), os.path.join(td, "no-suffix")):
                rc, _o, err = _run(["figure", "export", doc, "--out", bad])
                assert rc == 2, (bad, rc)
                assert ".png" in err.lower() or "vectorcraft" in err.lower(), (bad, err)
            assert calls["connect"] == before, "输出保护应在连接 app 之前失败"

            # dpi 必须是正数，同样在连接 app 之前失败
            for bad_dpi in ("0", "-1"):
                rc, _o, err = _run(["figure", "export", doc, "--dpi", bad_dpi])
                assert rc == 2 and "dpi" in err.lower(), (bad_dpi, err)
            assert calls["connect"] == before, "dpi 校验应在连接 app 之前失败"

            with open(doc, "rb") as f:
                assert f.read() == b"document bytes"


# ---------------------------------------------------------------- workbench IO

def test_workbench_io_delegation():
    mod = types.ModuleType("workbench_io")
    seen = {}

    def cmd_export(args):
        seen["export"] = vars(args)
        return 0

    def cmd_import(args):
        seen["import"] = vars(args)
        return 0

    mod.cmd_export, mod.cmd_import = cmd_export, cmd_import
    key = "epcraft.workbench_io"
    old = sys.modules.get(key)
    old_attr = getattr(epcraft, "workbench_io", None)
    sys.modules[key] = mod
    epcraft.workbench_io = mod
    try:
        rc, _o, err = _run(["workbench", "export", "f.vectorcraft", "--port", "7802"])
        assert rc == 0, err
        assert seen["export"]["doc"] == "f.vectorcraft"
        assert seen["export"]["out"] is None and seen["export"]["port"] == 7802

        rc, _o, err = _run(["workbench", "export", "f.vectorcraft", "--out", "lane.json"])
        assert rc == 0, err
        assert seen["export"]["out"] == "lane.json"

        rc, _o, err = _run(["workbench", "import", "f.vectorcraft", "--input", "lane.json",
                            "--no-sync", "--dpi", "200", "--port", "7803"])
        assert rc == 0, err
        assert seen["import"]["doc"] == "f.vectorcraft" and seen["import"]["input"] == "lane.json"
        assert seen["import"]["no_sync"] is True and seen["import"]["dpi"] == 200
        assert seen["import"]["port"] == 7803
    finally:
        if old is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = old
        if old_attr is None:
            try:
                del epcraft.workbench_io
            except AttributeError:
                pass
        else:
            epcraft.workbench_io = old_attr

    # 缺 --input 是 argparse 用法错误（退出 2）
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            gel_photo.main(["workbench", "import", "f.vectorcraft"], canvas=True)
        except SystemExit as e:
            assert e.code == 2, e.code
        else:
            raise AssertionError("workbench import 缺 --input 却通过了")
    assert "--input" in err.getvalue(), err.getvalue()


# ---------------------------------------------------------------- densitometry chain (1/5: measure)

def test_densitometry_cli():
    """`densitometry measure` end to end on a synthetic report: the printed table, the written
    dens.json, both aliases, and every refusal path (stale report, --out onto an input, missing
    output dir, bad --lanes) as one `[!] …` stderr line with exit 2."""
    from . import selftest_densitometry

    with tempfile.TemporaryDirectory() as td:
        rep = selftest_densitometry._synthetic_report([
            {"label": "M", "marker": True, "bands": [(1.0, 20.0, 0.03)]},
            {"label": "S1", "bands": [(3.0, 30.0, 0.03), (4.0, 25.0, 0.05)]},
            {"label": None, "bands": []},
        ])
        report = os.path.join(td, "fig.mw.json")
        selftest_densitometry.densitometry.write_json(rep, report)
        out = os.path.join(td, "fig.dens.json")

        rc, stdout, stderr = _run(["densitometry", "measure", report])
        assert rc == 0, stderr
        assert stderr == "", "成功路径不该写 stderr：%r" % (stderr,)
        lines = stdout.strip().splitlines()
        assert lines[0].startswith("[i] 报告 ") and "2 条泳道" in lines[0], lines[0]
        assert lines[1] == "泳道 2 S1：2 带，面积 5.3 / 3.7 gray·cm", lines[1]
        assert lines[2] == "泳道 3：0 带，面积 0.0 / 0.0 gray·cm", lines[2]
        assert lines[-1] == "[i] 写出 %s" % out, lines[-1]
        assert os.path.isfile(out), out
        dens = selftest_densitometry.densitometry.load_json(out, "ep-craft-densitometry")
        assert dens["source"]["report"] == report, dens["source"]
        assert [ln["lane"] for ln in dens["lanes"]] == [2, 3], dens["lanes"]
        assert dens["params"]["lanes"] == "all sample lanes, trimmed and marker excluded", dens["params"]["lanes"]

        # 别名 dens + 两个 include 旗标 + --lanes 覆盖默认选择
        rc, stdout, stderr = _run(["dens", "measure", report, "--include-marker"])
        assert rc == 0 and stderr == "", stderr
        assert "3 条泳道" in stdout and "泳道 1 M：1 带" in stdout, stdout
        rc, stdout, stderr = _run(["dens", "measure", report, "--lanes", "1"])
        assert rc == 0 and stderr == "", stderr
        assert "泳道 1 M：1 带，面积 1.5 / 1.1 gray·cm" in stdout, stdout

        # .vectorcraft 参数解析到它的 <stem>.mw.json；报告比文档旧 → 先 sync
        doc = os.path.join(td, "fig.vectorcraft")
        with open(doc, "w", encoding="utf-8") as f:
            f.write("document")
        os.utime(report, (2000, 2000))
        os.utime(doc, (3000, 3000))
        rc, _o, err = _run(["dens", "measure", doc])
        assert rc == 2 and "先 sync" in err and err.startswith("[!] "), err
        assert "\n" not in err.rstrip("\n"), err

        # 报告不存在 / 不是报告 v2 都是同一行错误
        rc, _o, err = _run(["dens", "measure", os.path.join(td, "nope.mw.json")])
        assert rc == 2 and err.startswith("[!] ") and "nope.mw.json" in err, err
        broken = os.path.join(td, "broken.mw.json")
        with open(broken, "w", encoding="utf-8") as f:
            f.write("{}")
        rc, _o, err = _run(["dens", "measure", broken])
        assert rc == 2 and "v2" in err, err

        # --out 指向命令读的文件 → 拒绝；目录不存在 / 目录目标 → 拒绝；--lanes 越界 → 拒绝
        rc, _o, err = _run(["dens", "measure", report, "--out", report])
        assert rc == 2 and "--out" in err and "输入文件" in err, err
        rc, _o, err = _run(["dens", "measure", report, "--out", os.path.join(td, "no", "x.json")])
        assert rc == 2 and "输出目录" in err, err
        rc, _o, err = _run(["dens", "measure", report, "--lanes", "9"])
        assert rc == 2 and "--lanes" in err, err
        rc, _o, err = _run(["dens", "measure", report, "--out", td])
        assert rc == 2 and "目录" in err, err

        # 文档参数（报告已更新）：默认输出仍是 <report stem>.dens.json，不是 <doc stem>.dens.json；
        # 文档本身也是本命令读的东西，--out 指向它同样被拒
        os.utime(report, (4000, 4000))
        rc, stdout, stderr = _run(["dens", "measure", doc])
        assert rc == 0 and stderr == "", stderr
        assert stdout.strip().splitlines()[-1] == "[i] 写出 %s" % out, stdout
        rc, _o, err = _run(["dens", "measure", doc, "--out", doc])
        assert rc == 2 and "--out" in err and "输入文件" in err, err


# ---------------------------------------------------------------- densitometry chain (2/5: curve)

def test_densitometry_curve_cli():
    """`densitometry curve` end to end: the parser (defaults, repeatable --drop), the printed §5
    table, the curve.json written to the default `fig.curve.json` (chain suffix swapped, ruling
    R12), both aliases, and every refusal path as one `[!] …` stderr line with exit 2."""
    from . import selftest_densitometry

    ap = gel_photo.build_parser(canvas=True)
    a = ap.parse_args(["dens", "curve", "--dens", "fig.dens.json", "--std", "2=20"])
    assert a.cmd == "dens" and a.action == "curve" and a.func.__name__ == "cmd_curve"
    assert a.baseline == "flank" and a.fit == "linear" and a.unit == "ug" and a.out is None
    assert a.drop == [], "不给 --drop 时 drop 应是空列表（不是 None）"
    a = ap.parse_args(["densitometry", "curve", "--dens", "d.json", "--std", "2=20",
                       "--drop", "4", "--drop", "5", "--baseline", "global", "--fit", "power",
                       "--unit", "ng", "--out", "c.json"])
    assert a.drop == [4, 5], a.drop
    assert (a.baseline, a.fit, a.unit, a.out) == ("global", "power", "ng", "c.json")

    with tempfile.TemporaryDirectory() as td:
        rep = selftest_densitometry._synthetic_report([
            {"label": "M", "marker": True, "bands": [(1.0, 20.0, 0.03)]},
            {"label": "S1", "bands": [(3.0, 30.0, 0.04)]},
            {"label": "S2", "bands": [(3.0, 60.0, 0.04), (4.5, 20.0, 0.04)]},
            {"label": "S3", "bands": [(3.0, 120.0, 0.04)]},
        ])
        report = os.path.join(td, "fig.mw.json")
        selftest_densitometry.densitometry.write_json(rep, report)
        rc, stdout, stderr = _run(["dens", "measure", report])
        assert rc == 0 and stderr == "", stderr
        dens = os.path.join(td, "fig.dens.json")
        out = os.path.join(td, "fig.curve.json")
        assert os.path.isfile(dens) and not os.path.exists(out)

        # 成功路径：表头 + 每个标准点一行 + 一行 [i]，默认输出 fig.curve.json（链后缀替换，R12）
        rc, stdout, stderr = _run(["dens", "curve", "--dens", dens, "--std", "2=24,3=48,4=96"])
        assert rc == 0, stderr
        assert stderr == "", "成功路径不该写 stderr：%r" % (stderr,)
        lines = stdout.strip().splitlines()
        # 方程按 §5 印 2 dp（去尾零），r² 固定 4 dp
        assert lines[0] == ("[标曲] 基线 flank，拟合 linear（ug）：mass = 0.07 + 7.98·area，"
                            "r² 1.0000，3 点"), lines[0]
        assert lines[1] == "  96 ug   面积 12（n=1）  残差 0.0%", lines[1]
        assert lines[2] == "  48 ug   面积  6（n=1）  残差 0.0%", lines[2]
        assert lines[3] == "  24 ug   面积  3（n=1）  残差 0.0%", lines[3]
        assert lines[4] == "[i] 写出 %s" % out, lines[4]
        assert len(lines) == 5, lines
        assert os.path.isfile(out), out
        curve = selftest_densitometry.densitometry.load_json(out, "ep-craft-densitometry-curve")
        assert curve["version"] == 1 and curve["kind"] == "ep-craft-densitometry-curve"
        assert curve["unit"] == "ug" and curve["baseline"] == "flank" and curve["fit"] == "linear"
        assert curve["source"] == {"dens": dens, "std": "2=24,3=48,4=96", "drop": []}, curve["source"]
        assert [p["mass"] for p in curve["points"]] == [24.0, 48.0, 96.0]
        assert [p["lanes"] for p in curve["points"]] == [[2], [3], [4]], curve["points"]
        assert curve["fit_params"]["n"] == 3 and curve["notes"] == [], curve
        # 同一个 dens 再跑一次：字节一致（没有时间戳、没有随机内容）
        rc, _o, _e = _run(["densitometry", "curve", "--dens", dens, "--std", "2=24,3=48,4=96",
                           "--out", os.path.join(td, "again.curve.json")])
        assert rc == 0, _e
        with open(out, "rb") as f1, open(os.path.join(td, "again.curve.json"), "rb") as f2:
            assert f1.read() == f2.read(), "两次 curve 的 JSON 不一致"

        # 默认输出名（R12）：fig.dens.json → fig.curve.json；不是 .dens.json 的退回落扩展名
        from . import densitometry_cli
        calls = ((os.path.join(td, "fig.dens.json"), os.path.join(td, "fig.curve.json")),
                 (os.path.join(td, "FIG.DENS.JSON"), os.path.join(td, "FIG.curve.json")),
                 (os.path.join(td, "mydens.json"), os.path.join(td, "mydens.curve.json")),
                 (os.path.join(td, "mydens"), os.path.join(td, "mydens.curve.json")))
        for typed, want in calls:
            got = densitometry_cli._curve_out_path(typed, None)
            assert got == os.path.abspath(want), (typed, got, want)
        # 相对路径按输入所在目录（= 当前目录）落在旁边，显式 --out 照用（abspath）
        assert densitometry_cli._curve_out_path("fig.dens.json", None) == \
            os.path.abspath("fig.curve.json")
        # 端到端：非 .dens.json 的 dens 走退落名，文件真的落在那儿
        flat = os.path.join(td, "mydens.json")
        with open(dens, encoding="utf-8") as src, open(flat, "w", encoding="utf-8") as dst:
            dst.write(src.read())
        rc, stdout, stderr = _run(["dens", "curve", "--dens", flat, "--std", "2=24,3=48,4=96"])
        assert rc == 0 and stderr == "", stderr
        fallback = os.path.join(td, "mydens.curve.json")
        assert stdout.strip().splitlines()[-1] == "[i] 写出 %s" % fallback, stdout
        assert os.path.isfile(fallback) and not os.path.exists(
            os.path.join(td, "mydens.dens.curve.json")), "退落名不该保留 .dens"

        # 顶部标准点误标了质量 → 递减，标准点不单调（提示里带 plot-bands）
        rc, _o, err = _run(["dens", "curve", "--dens", dens, "--std", "2=96,3=48,4=24"])
        assert rc == 2 and err.startswith("[!] ") and "标准点不单调" in err, err
        assert "plot-bands" in err and "\n" not in err.rstrip("\n"), err

        # :1 选最高带、:* 取整条泳道；S2 是两带泳道
        rc, stdout, stderr = _run(["dens", "curve", "--dens", dens, "--std", "2:1=24,3:1=48,4:1=96"])
        assert rc == 0 and stderr == "", stderr
        assert stdout.strip().splitlines()[0] == lines[0], stdout
        rc, stdout, stderr = _run(["dens", "curve", "--dens", dens, "--std", "2:*=20,3:*=40,4:*=80",
                                   "--out", os.path.join(td, "whole.curve.json")])
        assert rc == 0 and stderr == "", stderr
        whole = selftest_densitometry.densitometry.load_json(os.path.join(td, "whole.curve.json"),
                                                            "ep-craft-densitometry-curve")
        assert abs(whole["points"][1]["volume"] - 8.0) < 0.01, whole["points"][1]

        # --baseline global / --fit power / --unit ng 都进曲线，power 用 mass = scale·area^exponent
        rc, stdout, stderr = _run(["dens", "curve", "--dens", dens, "--std", "2=24,3=48,4=96",
                                   "--baseline", "global", "--out", os.path.join(td, "g.json")])
        assert rc == 0 and stderr == "", stderr
        assert "基线 global" in stdout, stdout
        global_curve = selftest_densitometry.densitometry.load_json(os.path.join(td, "g.json"),
                                                                    "ep-craft-densitometry-curve")
        assert global_curve["baseline"] == "global", global_curve
        assert abs(global_curve["fit_params"]["slope"] - 11.544) < 1e-3, global_curve["fit_params"]
        rc, stdout, stderr = _run(["dens", "curve", "--dens", dens, "--std", "2=24,3=48,4=96",
                                   "--fit", "power", "--unit", "ng", "--out", os.path.join(td, "p.json")])
        assert rc == 0 and stderr == "", stderr
        assert stdout.splitlines()[0].startswith("[标曲] 基线 flank，拟合 power（ng）：mass = "), stdout
        assert stdout.splitlines()[0].endswith("·area^1，r² 1.0000，3 点"), stdout   # 2 dp 方程
        power = selftest_densitometry.densitometry.load_json(os.path.join(td, "p.json"),
                                                             "ep-craft-densitometry-curve")
        assert power["fit_params"]["space"] == "log10" and power["unit"] == "ng", power

        # --drop：点保留但弃用，表里写（已弃用），没有残差；2 点时会给出成点数提示
        rc, stdout, stderr = _run(["dens", "curve", "--dens", dens, "--std", "2=24,3=48,4=96",
                                   "--drop", "4", "--out", os.path.join(td, "d.json")])
        assert rc == 0 and stderr == "", stderr
        dlines = stdout.strip().splitlines()
        assert dlines[0].endswith("2 点"), dlines[0]      # 3 个标准点里弃用了 1 个
        assert dlines[1] == "  96 ug   面积 12（n=1）  （已弃用）", dlines[1]
        assert dlines[-2] == "  注：只有 2 个标准点，r² 不足以说明形状", dlines
        assert dlines[-1].startswith("[i] 写出 "), dlines[-1]
        dropped = selftest_densitometry.densitometry.load_json(os.path.join(td, "d.json"),
                                                               "ep-craft-densitometry-curve")
        assert dropped["source"]["drop"] == [4] and dropped["points"][-1]["used"] is False
        assert dropped["fit_params"]["n"] == 2

        # 各种拒绝：marker 泳道、不在 dens 里、带序号越界、弃用全部、--out 指向输入 / 目录 / 缺目录
        marker_dens = os.path.join(td, "all.dens.json")     # 含 marker 泳道的那份 dens
        rc, _o, _e = _run(["dens", "measure", report, "--include-marker", "--out", marker_dens])
        assert rc == 0, _e
        rc, _o, err = _run(["dens", "curve", "--dens", marker_dens, "--std", "1=20,2=24,3=48"])
        assert rc == 2 and err.startswith("[!] ") and "marker 泳道" in err, err
        assert "\n" not in err.rstrip("\n"), err
        rc, _o, err = _run(["dens", "curve", "--dens", dens, "--std", "2=24,9=48"])
        assert rc == 2 and "不在 dens 里" in err, err
        rc, _o, err = _run(["dens", "curve", "--dens", dens, "--std", "2=24,3:3=48"])
        assert rc == 2 and "只有 2 个带" in err, err
        rc, _o, err = _run(["dens", "curve", "--dens", dens, "--std", "2=24,3=48,4=96",
                            "--drop", "2", "--drop", "3", "--drop", "4"])
        assert rc == 2 and "所有标准点" in err, err
        rc, _o, err = _run(["dens", "curve", "--dens", dens, "--std", "2=24,3=48", "--drop", "9"])
        assert rc == 2 and "不在 --std 里" in err, err
        # --baseline / --fit 的非法取值由 argparse 的 choices 挡掉（usage + 2，不进命令体）
        for argv, flag in ((("--baseline", "local"), "--baseline"), (("--fit", "cubic"), "--fit")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                try:
                    ap.parse_args(["dens", "curve", "--dens", "d.json", "--std", "2=24"] + list(argv))
                except SystemExit as e:
                    assert e.code == 2, (argv, e.code)
                else:
                    raise AssertionError("%r 该被 argparse 拒绝" % (argv,))
            assert flag in err.getvalue(), (argv, err.getvalue())
        rc, _o, err = _run(["dens", "curve", "--dens", dens, "--std", "2=24,3=48,4=96",
                            "--out", dens])
        assert rc == 2 and "--out" in err and "输入文件" in err, err
        rc, _o, err = _run(["dens", "curve", "--dens", dens, "--std", "2=24,3=48,4=96",
                            "--out", os.path.join(td, "no", "x.json")])
        assert rc == 2 and "输出目录" in err, err
        rc, _o, err = _run(["dens", "curve", "--dens", dens, "--std", "2=24,3=48,4=96",
                            "--out", td])
        assert rc == 2 and "目录" in err, err
        # dens 不存在 / --std 为空：同一行错误
        rc, _o, err = _run(["dens", "curve", "--dens", os.path.join(td, "nope.dens.json"),
                            "--std", "2=24"])
        assert rc == 2 and err.startswith("[!] ") and "nope.dens.json" in err, err
        rc, _o, err = _run(["dens", "curve", "--dens", dens, "--std", " "])
        assert rc == 2 and "--std" in err and "空" in err, err


# ---------------------------------------------------------------- executable

def test_public_entry():
    """入口 = python3 -m epcraft（包可导入）与安装后的 console script。"""
    import importlib.util
    from epcraft import cli as package_cli
    package = Path(__file__).resolve().parent                    # …/src/epcraft（签出）或 site-packages/epcraft
    repo = package.parent.parent if package.parent.name == "src" else None
    if repo is not None and not (repo / "pyproject.toml").is_file():
        repo = None
    assert callable(package_cli.main), package_cli.main
    assert importlib.util.find_spec("epcraft.__main__") is not None, "缺少 python -m epcraft 的 __main__"

    # 模块入口真的能跑（签出时用 repo/src，安装后用当前解释器里的包）
    env = dict(os.environ)
    if repo is not None:
        src = str(repo / "src")
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    r = subprocess.run([sys.executable, "-m", "epcraft", "--help"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    for name in ("figure", "crop", "workbench", "profile", "launch", "doctor", "selftest"):
        assert name in r.stdout, (name, r.stdout, r.stderr)

    # 安装形态：发行版的 console script 指向 epcraft.cli:main（签出未安装时跳过）
    from importlib.metadata import entry_points
    eps = entry_points()
    selected = [ep for ep in (eps.select(group="console_scripts", name="epcraft")
                              if hasattr(eps, "select") else eps.get("console_scripts", []))
                if ep.name == "epcraft"]
    if repo is None:
        assert any(ep.value == "epcraft.cli:main" for ep in selected), selected

    help_text = gel_photo.build_parser(canvas=True).format_help()
    for name in ("figure", "crop", "workbench", "profile", "launch", "doctor", "selftest"):
        assert name in help_text, (name, help_text)


# ---------------------------------------------------------------- 入口

def selftest():
    tests = [test_parser_hierarchy, test_legacy_parser_unchanged, test_shared_sync_and_edit_dispatch,
             test_error_boundary, test_launch, test_figure_export, test_workbench_io_delegation,
             test_densitometry_cli, test_densitometry_curve_cli, test_public_entry]
    for t in tests:
        t()
    print("selftest_cli %d/%d 通过" % (len(tests), len(tests)))
    return len(tests)


if __name__ == "__main__":
    selftest()
