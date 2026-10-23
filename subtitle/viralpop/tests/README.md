# tests/ — ticket 01: script-to-timeline alignment

## How to call `align_script_to_timeline`

```python
import sys
sys.path.insert(0, "render")  # from the skill root
from script_align import align_script_to_timeline

script_lines = ["สวยแพงมาก", "ทรงสวย"]  # Lip's script, one cue per line
words = [  # Groq verbose_json words[] — timing only, never display text
    {"word": "สวยแพง", "start": 0.0, "end": 0.5},
    {"word": "มาก", "start": 0.5, "end": 0.8},
    {"word": "ทรงสวย", "start": 0.9, "end": 1.4},
]

result = align_script_to_timeline(script_lines, words)
# result["matched"]:    [{script_line, text, display_text, start, end, ...}]
# result["mismatches"]: [{line_index, script_line, time_hint,
#                         heard_snippet, reason}]
#
# RULE: text / display_text ALWAYS equal script_line exactly.
# STT strings feed timing only. If mismatches is non-empty, stop and
# ask Lip before any burn (gate_before_burn, ticket 03).
```

You may also pass the full Groq `verbose_json` dict instead of the
`words[]` list — the function reads its `"words"` key.

## Run the tests (stdlib only, no pytest needed)

From the skill root (`animated-subtitle-renderer-v2/`):

    python3 tests/test_script_align.py
    # or
    python3 -m unittest tests.test_script_align -v
```

## Tickets 02 + 03 — ASS build (no repack) + mismatch gate

```python
import sys
sys.path.insert(0, "render")  # from the skill root
from script_align import align_script_to_timeline
from script_gate import gate_before_burn   # ticket 03: refuses on mismatches
from script_ass import build_ass           # ticket 02: one Dialogue per line

alignment = align_script_to_timeline(script_lines, words)
gate = gate_before_burn(alignment)
if not gate["ok"]:
    print(gate["report"])   # ALL mismatches at once; no ASS, no burn
else:
    ass_text = build_ass(alignment, "out/sub.ass")  # text == script lines
```

RULES: `build_ass` raises `MismatchGateError` (writes no file) when
`mismatches` is non-empty; Dialogue text always equals the script line
verbatim (no cross-line packing, no mid-word split, no loanword/ASR
rewrite); style is Viral Pop + FontName `Prompt Bold`, 1080x1920.

Run the tests (stdlib only, no pytest needed):

    python3 tests/test_script_ass.py
    python3 tests/test_script_gate.py
    # or
    python3 -m unittest tests.test_script_ass tests.test_script_gate -v

## Tickets 05 + 06 — pipeline entrypoint + primary_bag_demo regression

Single entrypoint wiring align -> gate -> ASS -> burn -> QA
(`render/script_pipeline.py::run_script_pipeline`, CLI
`scripts/burn_script_aligned.py --video --script --fonts --out`):

```python
import sys
sys.path.insert(0, "render")  # from the skill root
from script_pipeline import run_script_pipeline

result = run_script_pipeline(
    "clips/<clip>.mp4",           # finished video (audio: -c:a copy)
    ["สวยแพงมาก", "ทรงสวย"],        # ...or a script .txt path (one cue/line)
    "fonts", "output/<name>_subtitled.mp4",
    words_json="asr_test/<name>.json",  # cached Groq timing; omit for live STT
)
if not result["ok"]:
    print(result["report"])  # ALL mismatches at once; no ASS, no burn
else:
    print(result["report"])  # burn report: filter, geometry, duration, frames
```

Smoke command for Muse/COS (full suite, stdlib only, no pytest):

    python3 tests/test_script_align.py
    python3 tests/test_script_ass.py
    python3 tests/test_script_gate.py
    python3 tests/test_script_burn.py
    python3 tests/test_primary_bag_demo.py   # SPEC acceptance table

`test_primary_bag_demo.py` uses synthetic timeline fixtures (no real
bag clip needed) and FAILS if primary-case errors return: มาก leaking
to the next cue text, คลาสสิก splitting to orphan ก, or ASR อย่าบ
becoming display text when the script says อย่างในภาพ. Its burn smoke
builds a tiny synthetic 1080x1920 clip (needs ffmpeg/ffprobe).

## Source of truth

- Implementation: `render/script_align.py`
- Ticket: SkillsProject `.scratch/burn-subtitle-v1/issues/01-align-script-timeline.md`
- Matching mirrors `render/align.py::align_cues` (same window, same
  length penalty, same 0.42 threshold, same min/max span timing) so
  cue timing stays consistent with the existing pipeline. Failures go
  to `mismatches[]` instead of raising.
