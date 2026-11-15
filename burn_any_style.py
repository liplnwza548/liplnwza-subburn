#!/usr/bin/env python3
"""Burn Thai subtitles in any of the 15 locked styles — one command.

    python3 burn_any_style.py --style karaoke --video IN.mp4 --script NN.txt \
        --words NN_words.json --mfa NN_mfa.json --out OUT.mp4

    python3 burn_any_style.py --list-styles        # show all 15 styles
    python3 burn_any_style.py --style viralpop ... --dry-run   # print only

Style set (15, all already approved — no new styles here):
  - viralpop (default): main style, sentence subtitles + yellow keywords.
  - paper-chips: torn-paper word chips.
  - 13 standalone styles via burn_styles.py: karaoke, editorial,
    highlighter, reveal, underline, minimal, frosted, dualtone,
    comic, luxury, terminal, clay, wordpop.

House rules enforced by this router (never bypassed):
  - Each style goes to its own approved burner only. The two workflow
    styles (viralpop / paper-chips) stay separate from the 13 standalone
    styles; nothing is merged and no default is changed implicitly.
  - Never uses `ffmpeg -vf subtitles=` directly. The router only runs
    the three approved burner scripts as subprocesses.
  - Fail-closed on existing output: refuses if --out (or its sidecar
    .proto.json / .chips.json) already exists, unless --overwrite.
  - Timing always comes from the caller's files (MFA words / MFA json).
    The router never invents timing and never substitutes STT timing
    for MFA timing.
  - The paper-chips burner needs pythainlp, which a plain python may
    not have. The router resolves a working interpreter for it
    (see _resolve_python) instead of failing; the other two burners
    run under the calling python.

This router is for the manual burn flow (Philip sends a clip, Bean
burns it). The Phase 2 automatic pipeline keeps its own locked
allowlist (viralpop / paper-chips only, overrides must be {}) and is
not touched by this file.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# This file lives at the repo root, so the repo root is its own folder.
# All three burners and the fonts live inside this repo — the router
# never reaches outside it for a burner.
REPO = HERE

VIRALPOP_BURNER = REPO / "subtitle" / "viralpop" / "scripts" / "burn_script_aligned.py"
CHIP_BURNER = REPO / "subtitle" / "paper-chips" / "chip_burn.py"
STYLES_BURNER = REPO / "subtitle" / "styles" / "burn_styles.py"
DEFAULT_FONTS = REPO / "subtitle" / "viralpop" / "fonts"

STYLES_13 = [
    ("karaoke", "คาราโอเกะไล่เติมคำ (คำที่พูดแล้วเป็นสีมิ้นต์)"),
    ("editorial", "สารคดีนิ่ง (แผ่นดำโปร่งแสง ข้อความนิ่ง)"),
    ("highlighter", "แถบปากกาไฮไลต์ (แถบเขียวมะนาวใต้คำที่กำลังพูด)"),
    ("reveal", "เผยคำทีละคำ (คำที่ยังไม่ถึงสีเทาจาง)"),
    ("underline", "ขีดเส้นใต้วิ่งตามคำ (แถบเขียวใต้คำที่กำลังพูด)"),
    ("minimal", "ตัวบางมินิมอล (ตัวขาวเล็ก เงาอ่อน ไม่มีแผ่นรอง)"),
    ("frosted", "แคปซูลกระจกฝ้า (ตัวดำบนแคปซูลขาวโปร่งแสง)"),
    ("dualtone", "บล็อกสลับสี (คำสลับกล่องดำ/ขาว มีขอบบาง)"),
    ("comic", "บับเบิลการ์ตูน (กล่องขาวขอบดำ ติ่งชี้ขึ้นกลางจอ)"),
    ("luxury", "ลักชูรี (ตัวเซอริฟขาว มีเงาอ่อน)"),
    ("terminal", "คอนโซล (ตัวเขียวบนกล่องดำขอบเขียว + เคอร์เซอร์)"),
    ("clay", "ดินน้ำมัน (คำละสีพาสเทล ขอบเข้ม + เงา)"),
    ("wordpop", "เวิร์ดป๊อป (คำที่กำลังพูดเด้งตัวใหญ่สีเหลือง)"),
]

ALL_STYLES = (
    [("viralpop", "สไตล์หลัก (ประโยค + คำสำคัญสีเหลือง)"),
     ("paper-chips", "ชิปกระดาษฉีก (ชิปคำเด้งทีละชิป)")]
    + STYLES_13
)
STYLE_NAMES = [name for name, _ in ALL_STYLES]
STYLES_13_NAMES = [name for name, _ in STYLES_13]


def _resolve_python(needs_pythainlp: bool) -> str:
    """Pick a python that can run the burner.

    Plain python is fine for viralpop and the 13 styles. paper-chips
    needs pythainlp + PIL, so probe the calling python first, then
    sibling venvs next to the repo if any exist (Bean's VM keeps
    .venv-whisper / .venv-subburn-test there). BURN_ANY_STYLE_PYTHON
    overrides everything.
    """
    override = os.environ.get("BURN_ANY_STYLE_PYTHON")
    if override:
        return override
    if not needs_pythainlp:
        return sys.executable
    candidates = [
        sys.executable,
        str(REPO.parent / ".venv-whisper" / "bin" / "python"),
        str(REPO.parent / ".venv-subburn-test" / "bin" / "python"),
    ]
    tried = []
    for cand in candidates:
        if not Path(cand).is_file():
            continue
        tried.append(cand)
        probe = subprocess.run(
            [cand, "-c", "import pythainlp, PIL"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if probe.returncode == 0:
            return cand
    raise _fail("no python with pythainlp+PIL found for paper-chips "
                f"(tried: {', '.join(tried) or 'none'}; install pythainlp "
                "into your python or set BURN_ANY_STYLE_PYTHON)")


def _fail(message: str) -> "SystemExit":
    print(f"burn_any_style: {message}", file=sys.stderr)
    return SystemExit(2)


def _need_file(path: str | None, what: str) -> Path:
    if not path:
        raise _fail(f"missing {what} (required for this style)")
    p = Path(path)
    if not p.is_file():
        raise _fail(f"{what} not found: {p}")
    return p


def _need_dir(path: str | None, what: str) -> Path:
    if not path:
        raise _fail(f"missing {what} (required for this style)")
    p = Path(path)
    if not p.is_dir():
        raise _fail(f"{what} not found: {p}")
    return p


def build_command(a: argparse.Namespace) -> list[str]:
    """Map one style choice to the exact burner command. No side effects."""
    video = _need_file(a.video, "--video")
    script = _need_file(a.script, "--script")
    out = Path(a.out)
    if out.parent and not out.parent.is_dir():
        raise _fail(f"output folder not found: {out.parent}")

    if not a.overwrite:
        taken = [p for p in (out, Path(str(out) + ".proto.json"),
                             Path(str(out) + ".chips.json")) if p.exists()]
        if taken:
            raise _fail(
                f"output already exists, refusing to overwrite: {taken[0]} "
                "(pass --overwrite to allow it)"
            )

    if a.style == "viralpop":
        words = _need_file(a.words, "--words (MFA words json)")
        fonts = _need_dir(a.fonts, "--fonts")
        cmd = [
            _resolve_python(False), str(VIRALPOP_BURNER),
            "--video", str(video),
            "--script", str(script),
            "--fonts", str(fonts),
            "--out", str(out),
            "--words-json", str(words),
            "--profile", "vo",
        ]
        if a.keywords:
            cmd += ["--keywords", a.keywords]
        return cmd

    if a.style == "paper-chips":
        words = _need_file(a.words, "--words (MFA words json, passed as --stt)")
        cmd = [
            _resolve_python(True), str(CHIP_BURNER),
            "--video", str(video),
            "--script", str(script),
            "--stt", str(words),
            "--out", str(out),
            "--fps", str(a.fps),
            "--crf", str(a.crf),
        ]
        return cmd

    # 13 standalone styles.
    mfa = _need_file(a.mfa, "--mfa (raw MFA json)")
    return [
        _resolve_python(False), str(STYLES_BURNER),
        "--style", a.style,
        "--video", str(video),
        "--script", str(script),
        "--mfa", str(mfa),
        "--out", str(out),
        "--syllable-chunks",
        "--fps", str(a.fps),
        "--crf", str(a.crf),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Burn Thai subtitles in any of the 15 locked styles.")
    ap.add_argument("--style", default="viralpop", choices=STYLE_NAMES,
                    help="subtitle style (default: viralpop)")
    ap.add_argument("--video", help="source clip")
    ap.add_argument("--script", help="approved script text (one phrase per line)")
    ap.add_argument("--words", help="MFA words json (STT format) — viralpop/paper-chips")
    ap.add_argument("--mfa", help="raw MFA json — the 13 standalone styles")
    ap.add_argument("--fonts", default=str(DEFAULT_FONTS),
                    help="fonts dir for viralpop (default: repo viralpop fonts)")
    ap.add_argument("--keywords", default=None,
                    help="comma-separated yellow keywords (viralpop only)")
    ap.add_argument("--out", help="output mp4")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--overwrite", action="store_true",
                    help="allow overwriting an existing output file")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the burner command and run nothing")
    ap.add_argument("--list-styles", action="store_true",
                    help="list all 15 styles and exit")
    a = ap.parse_args()

    if a.list_styles:
        for name, desc in ALL_STYLES:
            print(f"{name:12s} {desc}")
        return 0

    for burner in (VIRALPOP_BURNER, CHIP_BURNER, STYLES_BURNER):
        if not burner.is_file():
            print(f"burn_any_style: burner not found: {burner}", file=sys.stderr)
            return 2
    if not a.video or not a.script or not a.out:
        print("burn_any_style: --video, --script and --out are required",
              file=sys.stderr)
        return 2

    try:
        cmd = build_command(a)
    except SystemExit as exc:
        return int(exc.code or 2)

    print("style:", a.style)
    print("run:", " ".join(cmd), flush=True)
    if a.dry_run:
        return 0
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        print(f"burn_any_style: burner exited with code {rc}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
