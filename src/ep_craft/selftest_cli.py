#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_cli：公开 `ep-craft` 命令层自检 —— 不启动 VectorCraft、不读用户文件。

覆盖：新旧两套 parser 的形状、四个组共用 sync / edit、缺 action 的组报 usage、
launch 的 connect/open、figure export 的画板选择与输出保护、workbench export/import
的参数转交，以及 main 的一行错误边界（RuntimeError / OSError / ValueError → 2）。

运行：python3 -m ep_craft.selftest_cli（在 src/ 上或安装后）
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path


import ep_craft                    # noqa: E402
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

    # 顶层不再有旧的扁平命令，组内也不会混入别的组的命令
    assert set(ap._subparsers._group_actions[0].choices) == {
        "figure", "crop", "workbench", "profile", "launch", "workflow", "selftest", "doctor"}
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
    for group in ("figure", "crop", "workbench", "profile"):
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
    key = "ep_craft.workbench_io"
    old = sys.modules.get(key)
    old_attr = getattr(ep_craft, "workbench_io", None)
    sys.modules[key] = mod
    ep_craft.workbench_io = mod
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
                del ep_craft.workbench_io
            except AttributeError:
                pass
        else:
            ep_craft.workbench_io = old_attr

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


# ---------------------------------------------------------------- executable

def test_public_entry():
    """入口 = python3 -m ep_craft（包可导入）与安装后的 console script；scripts/ 目录已删除。"""
    import importlib.util
    from ep_craft import cli as package_cli
    package = Path(__file__).resolve().parent                    # …/src/ep_craft（签出）或 site-packages/ep_craft
    repo = package.parent.parent if package.parent.name == "src" else None
    if repo is not None and not (repo / "pyproject.toml").is_file():
        repo = None
    if repo is not None:
        assert not (repo / "skills" / "ep-craft" / "scripts").exists(),             "skills/ep-craft/scripts 应当已删除（入口改为 console script / python -m ep_craft）"

    assert callable(package_cli.main), package_cli.main
    assert importlib.util.find_spec("ep_craft.__main__") is not None, "缺少 python -m ep_craft 的 __main__"

    # 模块入口真的能跑（签出时用 repo/src，安装后用当前解释器里的包）
    env = dict(os.environ)
    if repo is not None:
        src = str(repo / "src")
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    r = subprocess.run([sys.executable, "-m", "ep_craft", "--help"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    for name in ("figure", "crop", "workbench", "profile", "launch", "doctor", "selftest"):
        assert name in r.stdout, (name, r.stdout, r.stderr)

    # 安装形态：发行版的 console script 指向 ep_craft.cli:main（签出未安装时跳过）
    from importlib.metadata import entry_points
    eps = entry_points()
    selected = [ep for ep in (eps.select(group="console_scripts", name="ep-craft")
                              if hasattr(eps, "select") else eps.get("console_scripts", []))
                if ep.name == "ep-craft"]
    if repo is None:
        assert any(ep.value == "ep_craft.cli:main" for ep in selected), selected

    help_text = gel_photo.build_parser(canvas=True).format_help()
    for name in ("figure", "crop", "workbench", "profile", "launch", "doctor", "selftest"):
        assert name in help_text, (name, help_text)


# ---------------------------------------------------------------- 入口

def selftest():
    tests = [test_parser_hierarchy, test_legacy_parser_unchanged, test_shared_sync_and_edit_dispatch,
             test_error_boundary, test_launch, test_figure_export, test_workbench_io_delegation,
             test_public_entry]
    for t in tests:
        t()
    print("selftest_cli %d/%d 通过" % (len(tests), len(tests)))
    return len(tests)


if __name__ == "__main__":
    selftest()
