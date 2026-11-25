"""Offline checks for the staged workflow command and canvas isolation."""
import contextlib
import io
import os
import tempfile

from epcraft import gel_photo
from .test_cli import _FakeClient, _patched, _run
from epcraft import canvas_cli


STAGES = ("init-canvas", "img-to-crop", "crop-to-workbench",
          "workbench-to-profile", "workbench-to-figure", "export")


def test_workflow_parser():
    parser = gel_photo.build_parser(canvas=True)
    for stage in STAGES:
        argv = ["workflow", stage]
        argv += ["photo.jpg", "--doc", "fig.vectorcraft"] if stage == "img-to-crop" else ["fig.vectorcraft"]
        args = parser.parse_args(argv)
        assert args.cmd == "workflow" and args.action == stage
        assert args.doc == "fig.vectorcraft" and args.dpi == 300 and args.port == 7797
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            parser.parse_args(["workflow"])
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError("workflow requires a stage")
    # The module's automatic public/legacy parser selection recognizes workflow.
    from epcraft import workflow_cli
    with _patched(workflow_cli, "cmd_init_canvas", lambda args: 0):
        assert _run(["workflow", "init-canvas", "x.vectorcraft"], canvas=None)[0] == 0


def test_workflow_export():
    with tempfile.TemporaryDirectory() as tmp:
        doc = os.path.join(tmp, "fig.vectorcraft")
        with open(doc, "wb") as stream:
            stream.write(b"original")
        client = _FakeClient(artboards=[{"name": "Crop"}, {"name": "Profiles"}, {"name": "Figure"}])
        with _patched(canvas_cli.vc_control, "connect", lambda *a, **k: (client, False)), \
                _patched(canvas_cli, "_ensure_open", lambda *a: 0):
            rc, out, err = _run(["workflow", "export", doc, "--dpi", "600"])
            assert rc == 0, (out, err)
            assert client.exported == [(os.path.splitext(doc)[0] + ".png", 2, 600, "white")]
            for flags in (["--out", doc], ["--dpi", "0"]):
                rc, _, err = _run(["workflow", "export", doc] + flags)
                assert rc == 2 and "Traceback" not in err
            assert len(client.exported) == 1
        with open(doc, "rb") as stream:
            assert stream.read() == b"original"


def test_workflow_init():
    from epcraft import workflow_start, vc_file, workbench
    with tempfile.TemporaryDirectory() as tmp:
        doc = os.path.join(tmp, "new.vectorcraft")
        client = _FakeClient()
        with _patched(workflow_start.vc_control, "connect", lambda *a, **k: (client, False)):
            rc, out, err = _run(["workflow", "init-canvas", doc])
            assert rc == 0, (out, err)
            top, blobs = vc_file.read(doc)
            document = top["document"]
            assert [a["name"] for a in document["artboards"]] == list(workflow_start.BOARDS)
            assert not blobs
            assert [layer["name"] for layer in document["layers"]] == [row[0] for row in workbench.LAYERS]
            for layer in document["layers"]:
                if layer["name"] != "ep-meta":
                    assert vc_file.lay_children(layer) == []
            assert workflow_start.load_meta(document)["stage"] == "init-canvas"
            with open(doc, "rb") as stream:
                original = stream.read()
            rc, _, err = _run(["workflow", "init-canvas", doc])
            assert rc == 2 and "Traceback" not in err
            with open(doc, "rb") as stream:
                assert stream.read() == original
            assert not os.path.exists(os.path.join(tmp, "new.png"))
            invalid = os.path.join(tmp, "invalid.vectorcraft")
            rc, _, err = _run(["workflow", "init-canvas", invalid, "--dpi", "0"])
            assert rc == 2 and "Traceback" not in err
            assert not os.path.exists(invalid)
            rc, _, err = _run(["workflow", "img-to-crop", "missing.jpg", "--doc", doc])
            assert rc == 2 and "Traceback" not in err
            with open(doc, "rb") as stream:
                assert stream.read() == original


def test_workflow_stage_isolation():
    from epcraft import workflow_stages as stages
    document = {"artboards": [{"name": name} for name in ("Crop", "Profiles", "Workbench", "Figure")],
                "layers": [{"name": name, "kind": {"children": [{"id": i, "kind": {}}]}}
                           for i, name in enumerate(("gel", "labels", "profiles", "lanes", "sample bands", "ep-meta"), 10)]}
    planned = [{"op": "artboard", "index": 0}, {"op": "artboard", "index": 2},
               {"op": "create", "layer": "gel"}, {"op": "move", "id": 11},
               {"op": "delete", "id": 12}, {"op": "create", "layer": "profiles"},
               {"op": "stroke", "id": 13}, {"op": "delete", "id": 14},
               {"op": "set_text", "id": 15, "text": "{}"}]

    def plan(canonical, model, report, layout=None):
        assert [a["name"] for a in canonical["artboards"]] == list(stages._CANON_BOARDS)
        return planned, {}

    with _patched(stages.figure, "plan_sync", plan):
        ops, _ = stages._plan(None, document, {}, {}, "stack")
    assert [op["index"] for op in ops if op["op"] == "artboard"] == [3, 1]
    profile = stages._stage_filter(ops, document, "profile")
    figure = stages._stage_filter(ops, document, "figure")
    assert profile == [ops[1], ops[4], ops[5], ops[8]]
    assert figure == [ops[0], ops[2], ops[3], ops[8]]
    assert [a["name"] for a in document["artboards"]] == ["Crop", "Profiles", "Workbench", "Figure"]
    # No Workbench stroke/delete reaches either derived canvas transition.
    assert all(op.get("id") not in (13, 14) for op in profile + figure)


def test_workflow_refresh_figure_pixels():
    from epcraft import workflow_stages as stages
    image_kind = {"type": "image", "key": "current", "width": 200, "height": 150,
                  "xf": [0.5, 0, 0, 0.5, 20, 30]}
    stale_kind = dict(image_kind, key="previous")
    document = {"artboards": [{"name": name} for name in stages._CANON_BOARDS],
                "layers": [{"name": "gel", "kind": {"children": [
                    {"id": 1, "name": "gel", "kind": image_kind},
                    {"id": 2, "name": "gel seg 1", "kind": {"children": [
                        {"id": 3, "kind": stale_kind}]}}]}}]}

    def plan(canonical, model, report, layout=None):
        children = canonical["layers"][0]["kind"]["children"]
        assert [n["id"] for n in children] == [1], "stale clip must be absent when generating replacements"
        return [{"op": "create", "layer": "gel", "spec": {"key": "current"}}], {}

    with _patched(stages.figure, "plan_sync", plan):
        ops, _ = stages._plan(None, document, {}, {}, "stack", rebuild_stale_gel=True)
    assert ops[0] == {"op": "delete", "id": 2}
    assert ops[1]["spec"]["key"] == "current"
    assert len(document["layers"][0]["kind"]["children"]) == 2, "planning must preserve source document"
    stale_kind["key"] = "current"
    assert stages._stale_gel_segs(document, {}) == [], "fresh segments should be kept"


def run():
    tests = [test_workflow_parser, test_workflow_export, test_workflow_init,
             test_workflow_stage_isolation, test_workflow_refresh_figure_pixels]
    for test in tests:
        test()
    print("test_workflow %d/%d passed" % (len(tests), len(tests)))
    return len(tests)


if __name__ == "__main__":
    run()
