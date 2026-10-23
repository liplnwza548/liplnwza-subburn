#!/usr/bin/env python3
"""Single place that loads the Groq API key.

Lookup order (first hit wins, real environment always wins over files):
1. ``GROQ_API_KEY`` already in the process environment (system env / CI secret).
2. ``.env`` file found by searching upward from this skill folder
   (so ``subtitled/.env`` is picked up automatically when running from
   anywhere inside the work tree). Loaded via ``python-dotenv`` with
   ``override=False`` so it can never clobber a real env var.

The key value itself is never printed or logged.
"""
from __future__ import annotations

import os
from pathlib import Path

MISSING_MSG = (
    "ERROR: GROQ_API_KEY_MISSING — set the GROQ_API_KEY environment variable "
    "or put GROQ_API_KEY=... in a .env file at the work root "
    "(see .env.example). The key is never stored inside the skill files."
)


def _find_dotenv() -> Path | None:
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate
        # Stop at filesystem root or drive root.
        if parent == parent.parent:
            break
    # Fall back to the current working directory.
    cwd_candidate = Path.cwd() / ".env"
    return cwd_candidate if cwd_candidate.is_file() else None


def load_groq_key() -> str:
    """Return the Groq API key or raise SystemExit with a clear message."""
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        return key
    dotenv_path = _find_dotenv()
    if dotenv_path is not None:
        try:
            from dotenv import load_dotenv

            load_dotenv(dotenv_path, override=False)
        except ImportError:
            raise SystemExit(
                "ERROR: python-dotenv is not installed, so the .env file at "
                f"{dotenv_path} was not loaded. Run setup first "
                "(pip install -r requirements.txt) or export GROQ_API_KEY directly."
            )
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if key:
            return key
    raise SystemExit(MISSING_MSG)
