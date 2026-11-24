"""Run the full EPCraft regression suite from a checkout."""

from . import selftest, selftest_cli, selftest_workbench_io, selftest_workflow


def main():
    selftest.selftest()
    selftest_cli.selftest()
    selftest_workbench_io.selftest()
    selftest_workflow.selftest()


if __name__ == "__main__":
    main()
