#!/usr/bin/env bash
#
# grab-solve.sh — TEMPORARY convenience wrapper.
#
# Pulls the latest frame off the camera, then plate-solves everything in the
# grab folder that isn't solved yet, using the local Docker solver. Annotated
# overlays and sidecar JSON land alongside the images.
#
# This is a stopgap until ApiSolver lands (see docs/20260712-api-solver-integration.md).
#
# Usage:
#   ./grab-solve.sh                 # grab + solve into ./incoming
#   OUT=~/Pictures ./grab-solve.sh  # override the folder
#   FAST=1 ./grab-solve.sh          # faster, lower-accuracy solve
#
set -uo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
OUT="${OUT:-./incoming}"
MODE_FLAG=""
[ "${FAST:-0}" = "1" ] && MODE_FLAG="--mode fast"

echo "==> Grabbing latest frame from camera into ${OUT} ..."
if ! "$PY" main.py grab --out "$OUT"; then
    echo "!! grab failed (camera disconnected?) — solving what's already in ${OUT}"
fi

echo "==> Solving unsolved frames in ${OUT} (local Docker solver) ..."
# batch skips images that already have a *_solved.json sidecar, so re-running
# after each shot only solves the new frame(s).
"$PY" main.py batch "$OUT" --annotate ${MODE_FLAG}

echo "==> Done. Results: ${OUT}/solve_results.json  |  overlays: ${OUT}/annotated/"
