#!/usr/bin/env python3
"""camera-orchestrator — entry point.

Thin shim; all CLI logic lives in camera_orchestrator/interfaces/cli.py.
Equivalent ways to run (venv active, or use .venv/bin/python):

    python main.py <command> ...
    ./main.py <command> ...                 # executable
    python -m camera_orchestrator <command> ...

Commands: batch <folder>, grab, capture. Use `<cmd> --help` for options.
"""
from camera_orchestrator.interfaces.cli import main

if __name__ == "__main__":
    main()
