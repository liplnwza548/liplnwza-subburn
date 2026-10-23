#!/usr/bin/env python3
"""COS/Muse entrypoint: script-aligned burn (v1 default).

Thin wrapper over render/script_pipeline.py so dispatch needs only
video + script + fonts::

    python scripts/burn_script_aligned.py \\
        --video <clip>.mp4 --script <script>.txt \\
        --fonts <fontsdir> --out <out>.mp4 [--frames-dir <qa/>]

Add --words-json <asr>.json to skip live Groq STT (offline/tests).
Exit codes: 0 = burned + QA passed; 2 = mismatch gate blocked
(report printed, nothing written); 1 = STT/burn/QA error.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "render"))

from script_pipeline import _cli  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(_cli())
