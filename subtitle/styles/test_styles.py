#!/usr/bin/env python3
"""Lock tests for the production burners (burn_styles.py).

T1 - Position lock: STRIP_Y/STRIP_H must be 1100/340 (safe zone 1100-1440).
T2 - No ffmpeg subtitles filter anywhere in the production burner.
T3 - Thai glyph test: render a strip with tricky Thai (leading vowels,
     above/below marks, tone marks) and save it for a human eye check -
     no tofu boxes, no floating vowels.
T4 - Style list lock: 13 standalone style names are offered.
"""
import re, sys
from pathlib import Path

SRC = Path(__file__).with_name('burn_styles.py').read_text(encoding='utf-8')
fails = []

if 'STRIP_Y, STRIP_H = 1100, 340' not in SRC:
    fails.append('T1 position lock: STRIP_Y/STRIP_H is not 1100/340')
if 'subtitles=' in SRC or 'subtitles,' in SRC:
    fails.append('T2: ffmpeg subtitles filter found in production burner')
m = re.search(r"choices=\[([^\]]+)\]", SRC)
styles = sorted(s.strip().strip("'\"") for s in m.group(1).split(',')) if m else []
if styles != ['clay', 'comic', 'dualtone', 'editorial', 'frosted', 'highlighter',
              'karaoke', 'luxury', 'minimal', 'reveal', 'terminal', 'underline',
              'wordpop']:
    fails.append(f'T4 style list lock: got {styles}')

# T3: Thai strip render through the real Renderer code path.
sys.path.insert(0, str(Path(__file__).parent))
import importlib.util
spec = importlib.util.spec_from_file_location('burn_styles', Path(__file__).with_name('burn_styles.py'))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
words = [
    {'word': 'แต่', 'start': 0.0, 'end': 0.3, 'interpolated': False},
    {'word': 'เก้าอี้', 'start': 0.3, 'end': 0.8, 'interpolated': False},
    {'word': 'ตัวนี้', 'start': 0.8, 'end': 1.2, 'interpolated': False},
    {'word': 'ใช้ได้', 'start': 1.2, 'end': 1.6, 'interpolated': False},
    {'word': 'ดีมาก', 'start': 1.6, 'end': 2.0, 'interpolated': False},
]
phrase = {'text': 'แต่เก้าอี้ตัวนี้ใช้ได้ดีมาก', 'words': words,
          'start': 0.0, 'end': 2.0, 'disp_end': 2.35}
r = mod.Renderer('karaoke', [phrase])
img = r.frame(1.0)
img.save(Path(__file__).with_name('thai_test_strip.png'))
print('T3 thai strip saved: thai_test_strip.png (check by eye: no tofu, vowels attached)')

if fails:
    print('FAIL:')
    for f in fails:
        print(' -', f)
    sys.exit(1)
print('PASS: T1 position lock, T2 no subtitles filter, T4 style list lock')
