#!/usr/bin/env python3
"""camera-orchestrator — entry point.

Thin shim: all CLI logic lives in camera_orchestrator/cmd.py.

Usage:
    python main.py --config config.yaml batch <folder> [--annotate] [--mode fast|accurate]
    python main.py --config config.yaml grab [--out ./incoming] [--force] [--poll SECONDS]
    python main.py --config config.yaml capture [--iso 800 --shutter 2 --count 30 ...]
"""
from camera_orchestrator.cmd import main

if __name__ == "__main__":
    main()
