"""Register the explicit, staged canvas workflow."""
from ..vc import control as vc_control


def cmd_init_canvas(args):
    from ..workflow.start import cmd_init_canvas as run
    return run(args)


def cmd_img_to_crop(args):
    from ..workflow.start import cmd_img_to_crop as run
    return run(args)


def cmd_crop_to_workbench(args):
    from ..workflow.stages import cmd_crop_to_workbench as run
    return run(args)


def cmd_workbench_to_profile(args):
    from ..workflow.stages import cmd_workbench_to_profile as run
    return run(args)


def cmd_workbench_to_figure(args):
    from ..workflow.stages import cmd_workbench_to_figure as run
    return run(args)


def add_commands(sub, figure_export=None):
    """figure_export = canvas_cli.cmd_figure_export（由上层注入，本模块不反向 import canvas_cli）。"""
    from ..gel import session as gel_session
    from ..figure import figure

    group = sub.add_parser("workflow", help="Advance photo → Crop → Workbench → Profiles/Figure → PNG")
    stages = group.add_subparsers(dest="action", required=True)

    def document_stage(name, help_text, func):
        parser = stages.add_parser(name, help=help_text)
        parser.add_argument("doc", help="VectorCraft document path")
        parser.add_argument("--port", type=int, default=vc_control.DEFAULT_PORT)
        parser.add_argument("--dpi", type=int, default=300)
        parser.set_defaults(func=func)
        return parser

    document_stage("init-canvas", "Create a new document with four empty canvases", cmd_init_canvas)
    image = stages.add_parser("img-to-crop", help="Place a photo and editable crop frame on Crop")
    image.add_argument("image", help="Source gel photo")
    image.add_argument("--doc", required=True, help="Document created by init-canvas")
    image.add_argument("--port", type=int, default=vc_control.DEFAULT_PORT)
    image.add_argument("--dpi", type=int, default=300)
    gel_session.add_detect_args(image)
    image.set_defaults(func=cmd_img_to_crop)

    wb = document_stage("crop-to-workbench", "Seed Workbench from the current Crop frame", cmd_crop_to_workbench)
    wb.add_argument("--markers", help="Marker configuration or preset library JSON")
    wb.add_argument("--preset", help="Marker preset number or keyword")
    wb.add_argument("--lanes", help="Lane labels and marker lane configuration JSON")
    wb.add_argument("--trim-lanes", help="Lane numbers omitted from Figure, e.g. 1,14-15")
    wb.add_argument("--profiles", choices=("stack", "strips"), default=figure.PROFILE_LAYOUT)
    wb.add_argument("--label-angle", type=float, default=0.0)
    wb.add_argument("--marker-label-angle", type=float, default=0.0)

    for name, func, help_text in (
            ("workbench-to-profile", cmd_workbench_to_profile, "Rebuild Profiles from the live Workbench"),
            ("workbench-to-figure", cmd_workbench_to_figure, "Rebuild Figure from the live Workbench")):
        parser = document_stage(name, help_text, func)
        parser.add_argument("--dry-run", action="store_true", help="Print planned changes without saving")
        parser.add_argument("--reset-peaks", nargs="*", type=int, default=None, metavar="N")

    if figure_export is not None:
        export = document_stage("export", "Export the Figure canvas as PNG", figure_export)
        export.add_argument("--out", help="PNG path (default: document stem + .png)")
        export.add_argument("--background", default="white")
