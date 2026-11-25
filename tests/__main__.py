"""Run the full EPCraft regression suite from a checkout."""

from .cli import test_cli, test_workflow
from .core import test_core
from .densitometry import test_ep4xa_acceptance
from .gel import test_marker_redetect
from .vc import test_workbench_io


def main():
    test_core.run()
    test_cli.run()
    test_workbench_io.run()
    test_workflow.run()
    test_ep4xa_acceptance.run()
    test_marker_redetect.selftest()


if __name__ == "__main__":
    main()
