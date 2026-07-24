"""
Entry point for running zai_python_helper as a module.

Usage: python -m zai_python_helper ...
"""

from zai_python_helper.cli import main

if __name__ == "__main__":
    import sys

    sys.exit(main())
