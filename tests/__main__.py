"""Run the full EPCraft regression suite from a checkout."""

from . import (
    test_cli,
    test_core,
    test_ep4xa_acceptance,
    test_workbench_io,
    test_workflow,
)


def main():
    test_core.run()
    test_cli.run()
    test_workbench_io.run()
    test_workflow.run()
    test_ep4xa_acceptance.run()


if __name__ == "__main__":
    main()
