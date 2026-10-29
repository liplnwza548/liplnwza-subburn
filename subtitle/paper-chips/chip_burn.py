#!/usr/bin/env python3
"""Paper-chip subtitle burner (template: paper-chips).

Extracted from LiplnwZa Motion Kit v0.2.1 renderer/kit.py captions()/paper()/text()/pop()
— Warodom torn-paper word chips, rendered as a transparent overlay strip and
composited onto existing footage with ffmpeg. Audio passthrough (-c:a copy).

Usage:
  chip_burn.py --video IN.mov --script NN.txt --stt NN.json --out OUT.mp4
Inputs:
  --script : AGY-corrected script, one phrase per line (source of truth for text)
  --stt    : JSON with word-level timings (MFA-aligned; source of truth for timing)
Output chips json is written next to --out as <out>.chips.json
"""
import argparse, bisect, difflib, json, math, random, subprocess, sys
from functools import lru_cache
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, features

W, H = 1080, 1920
STRIP_Y, STRIP_H = 1470, 340          # chip zone: kit cy=1635, chip h=140 + pop/rotate margin
CHIP_CY = 1635 - STRIP_Y              # chip center inside strip
INK = '#181b26'; WHITE = '#fffdf6'
RED = '#f44336'; BLUE = '#2264d8'; YELLOW = '#ffd648'; GREEN = '#38b577'
PALETTE = [YELLOW, BLUE, RED, GREEN]
# --- Portable font resolution (handover patch; ONLY change vs original) ---
# Order: 1) ../fonts/Prompt-Bold.ttf relative to this script
#           (package layout: subtitle/paper-chips/chip_burn.py
#                            + subtitle/fonts/Prompt-Bold.ttf)
#        2) env var CHIP_FONT_PATH
#        3) original absolute path on Bean's machine (last fallback)
import os as _os
_FONT_CANDIDATES = [Path(__file__).resolve().parent.parent / 'fonts' / 'Prompt-Bold.ttf']
if _os.environ.get('CHIP_FONT_PATH'):
    _FONT_CANDIDATES.append(Path(_os.environ['CHIP_FONT_PATH']))
_FONT_CANDIDATES.append(Path.home() / 'workspace/liplnwza-motion-kit/v021/kit/LiplnwZa_Motion_Kit_v0.2.1/assets/fonts/Prompt-Bold.ttf')
FONT_PATH = next((p for p in _FONT_CANDIDATES if p.is_file()), _FONT_CANDIDATES[0])
# --- end portable patch ---

# ---------- kit.py verbatim pieces (pop / font / text / paper / layer) ----------
def pop(u):
    if u <= 0: return 0
    u = min(1, u); return 1 + 2.70158 * (u - 1) ** 3 + 1.70158 * (u - 1) ** 2

@lru_cache(maxsize=100)
def font(n):
    return ImageFont.truetype(str(FONT_PATH), n, layout_engine=ImageFont.Layout.RAQM)

def text(draw, box, value, size=70, color=INK):
    x, y, w, h = box; lines = str(value).split('\n')
    for size in range(size, 17, -1):
        f = font(size); bb = [f.getbbox(s) for s in lines]; hs = [b[3] - b[1] for b in bb]
        total = sum(hs) + (len(lines) - 1) * size * .2
        if max(b[2] - b[0] for b in bb) <= w and total <= h: break
    yy = y + (h - total) / 2
    for s, b, hh in zip(lines, bb, hs):
        draw_x = x + (w - (b[2] - b[0])) / 2
        draw.text((draw_x - b[0], yy - b[1]), s, font=f, fill=color)
        yy += hh + size * .2
    return size

@lru_cache(maxsize=400)
def paper(w, h, color, seed):
    r = random.Random(seed); pts = []; pad = 9
    for x in range(pad, w - pad, max(10, w // 20)): pts.append((x, pad + r.randint(-5, 5)))
    for y in range(pad, h - pad, max(10, h // 20)): pts.append((w - pad + r.randint(-5, 5), y))
    for x in range(w - pad, pad, -max(10, w // 20)): pts.append((x, h - pad + r.randint(-5, 5)))
    for y in range(h - pad, pad, -max(10, h // 20)): pts.append((pad + r.randint(-5, 5), y))
    im = Image.new('RGBA', (w, h)); dr = ImageDraw.Draw(im)
    dr.polygon([(x + 6, y + 6) for x, y in pts], fill=INK)
    dr.line(pts + [pts[0]], fill=WHITE, width=11, joint='curve')
    dr.polygon(pts, fill=color)
    dr.line(pts + [pts[0]], fill=INK, width=4, joint='curve')
    return im

def layer(im, a, cx, cy, scale=1, angle=0):
    if scale <= 0: return
    a = a.resize((max(1, round(a.width * scale)), max(1, round(a.height * scale))), Image.Resampling.BICUBIC)
    if angle: a = a.rotate(angle, Image.Resampling.BICUBIC, expand=True)
    im.alpha_composite(a, (round(cx - a.width / 2), round(cy - a.height / 2)))

# ---------- chip building: script tokens timed by STT char alignment ----------
def build_chips(script_text, stt):
    from pythainlp import word_tokenize
    tokens = []
    for line in script_text.split('\n'):
        line = line.strip()
        if line:
            tokens.extend(t for t in word_tokenize(line, engine='newmm', keep_whitespace=False) if t.strip())
    # FIX 2026-10-26 (plan PLAN-chip-merge-fix.md): attach a bare repetition mark
    # to the preceding token BEFORE alignment, so "สุดๆ" is one token and "ๆ"
    # can never survive as its own chip for the next word to glue onto.
    fixed = []
    for t in tokens:
        if t == 'ๆ' and fixed:
            fixed[-1] += t
        else:
            fixed.append(t)
    tokens = fixed
    stt_words = [w for w in stt.get('words', []) if w['word'].strip()]
    # char streams
    s_chars, s_tok = [], []           # script chars + token index per char
    for ti, tok in enumerate(tokens):
        for c in tok:
            s_chars.append(c); s_tok.append(ti)
    t_chars, t_start, t_end = [], [], []   # stt chars + interpolated char times
    for w in stt_words:
        ws = w['word'].replace(' ', '')
        if not ws: continue
        for k, c in enumerate(ws):
            t_chars.append(c)
            t_start.append(w['start'] + (w['end'] - w['start']) * k / len(ws))
            t_end.append(w['start'] + (w['end'] - w['start']) * (k + 1) / len(ws))
    sm = difflib.SequenceMatcher(None, s_chars, t_chars, autojunk=False)
    tok_start = [None] * len(tokens); tok_end = [None] * len(tokens)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ('equal', 'replace'):
            n = min(i2 - i1, j2 - j1)
            for k in range(n):
                ti = s_tok[i1 + k]
                if tok_start[ti] is None: tok_start[ti] = t_start[j1 + k]
                tok_end[ti] = t_end[j1 + k]
    missing = [(i, tok) for i, tok in enumerate(tokens) if tok_start[i] is None]
    if missing:
        details = ', '.join(f'index {i} (token #{i + 1}): {tok!r}' for i, tok in missing)
        raise ValueError(f'alignment failed: unmatched script tokens: {details}')
    chips = []
    for i, tok in enumerate(tokens):
        s, e = float(tok_start[i]), float(max(tok_end[i], tok_start[i] + 0.12))
        # FIX 2026-10-26: the artifact rule must also apply only to
        # single-character fragments. Unrestricted, it fused whole words
        # whose interpolated starts collided ("สุดๆ"+"ช่อง" -> "สุดๆช่อง",
        # "ใบ"+"อื่น" -> "ใบอื่น") on character-level STT.
        if chips and len(tok) == 1 and s - chips[-1]['start'] < 0.06:      # artifact sliver: merge back
            chips[-1]['word'] += tok; chips[-1]['end'] = max(chips[-1]['end'], e); continue
        chips.append({'word': tok, 'start': round(s, 3), 'end': round(e, 3)})
    for i in range(len(chips) - 1):
        chips[i]['end'] = min(chips[i]['end'], chips[i + 1]['start'])
        if chips[i]['end'] <= chips[i]['start']: chips[i]['end'] = chips[i]['start'] + 0.1
    # FIX 2026-10-26 (revised after regression test): merge a short chip
    # (<0.15s) back ONLY when it is a single-character fragment of the
    # previous word (tokenizer split, e.g. "เนี๊ย"+"บ"). A multi-character
    # short chip is a real word spoken fast (e.g. "ซิบ", "ช่อง") — keep it
    # as its own chip, stretching its display to 0.15s out of the following
    # gap without moving any other chip. The earlier "previous chip also
    # short" clause is REMOVED: it chained fragments into the next word
    # ("บ"+"ซิบ" -> "บซิบ", "สุดๆ"+"ช่อง" -> "สุดๆช่อง").
    merged = []
    for ci, c in enumerate(chips):
        if merged and c['end'] - c['start'] < 0.15 and len(c['word']) == 1:
            merged[-1]['word'] += c['word']; merged[-1]['end'] = max(merged[-1]['end'], c['end'])
        else:
            if merged and c['end'] - c['start'] < 0.15 and ci + 1 < len(chips):
                c['end'] = min(c['start'] + 0.15, chips[ci + 1]['start'])
                if c['end'] <= c['start']:
                    c['end'] = c['start'] + 0.1
            merged.append(c)
    # a chip must not end with the negation ไม่ (incomplete phrase) — pull next chip in
    out = []
    for c in merged:
        if out and out[-1]['word'].endswith('ไม่'):
            out[-1]['word'] += c['word']; out[-1]['end'] = max(out[-1]['end'], c['end'])
        else:
            out.append(c)
    return out

# ---------- rendering ----------
class ChipRenderer:
    def __init__(self, chips):
        self.words = chips
        self.wt = [w['start'] for w in chips]
        self._base = {}    # (i, ids_tuple) -> (img_unrotated, width)
        self._still = {}   # (i, ids_tuple) -> rotated img
        self._layout_cache = {}

    def _layout(self, active):
        if active in self._layout_cache:
            return self._layout_cache[active]
        ids = list(range(active // 3 * 3, active + 1))
        ww = [max(112, font(80).getbbox(self.words[i]['word'])[2] + 68) for i in ids]
        while len(ids) > 1 and sum(ww) + 18 * (len(ids) - 1) > 960:
            ids.pop(0); ww.pop(0)
        total = sum(ww) + 18 * (len(ids) - 1)
        scale = min(1, 960 / total)
        self._layout_cache[active] = (ids, ww, scale)
        return ids, ww, scale

    def _base_img(self, i, ids_key, width, scale):
        key = (i, ids_key)
        if key not in self._base:
            a = paper(width, 140, PALETTE[i % 4], 800 + i).copy()
            text(ImageDraw.Draw(a), (16, 16, width - 32, 108), self.words[i]['word'],
                 round(80 * scale), INK if i % 4 == 0 else WHITE)
            self._base[key] = a
        return self._base[key]

    def frame(self, t):
        im = Image.new('RGBA', (W, STRIP_H))
        active = bisect.bisect_right(self.wt, t) - 1
        if active < 0 or t > self.words[active]['end'] + .5:
            return im
        ids, ww, scale = self._layout(active)
        ids_key = tuple(ids)
        x = (W - (sum(ww) + 18 * (len(ids) - 1)) * scale) / 2
        for i, w0 in zip(ids, ww):
            width = round(w0 * scale)
            a = self._base_img(i, ids_key, width, scale)
            angle = 2 if i % 2 else -2
            if i == active:
                layer(im, a.copy(), x + width / 2, CHIP_CY, pop((t - self.words[i]['start']) / .14), angle)
            else:
                key = (i, ids_key)
                if key not in self._still:
                    self._still[key] = a.copy().rotate(angle, Image.Resampling.BICUBIC, expand=True)
                r = self._still[key]
                im.alpha_composite(r, (round(x + width / 2 - r.width / 2), round(CHIP_CY - r.height / 2)))
            x += width + 18 * scale
        return im

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--video', required=True)
    ap.add_argument('--script', required=True)
    ap.add_argument('--stt', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--fps', type=int, default=30)
    ap.add_argument('--crf', type=int, default=18)
    a = ap.parse_args()

    script = Path(a.script).read_text(encoding='utf-8')
    stt = json.loads(Path(a.stt).read_text(encoding='utf-8'))
    chips = build_chips(script, stt)
    chips_path = a.out + '.chips.json'
    Path(chips_path).write_text(json.dumps(chips, ensure_ascii=False, indent=1), encoding='utf-8')
    dur = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                          '-of', 'csv=p=0', a.video]).decode().strip())
    print(f'chips: {len(chips)} ({len(chips)/dur:.2f}/s), speech {chips[0]["start"]:.2f}-{chips[-1]["end"]:.2f}s of {dur:.1f}s')
    print(f'chips json: {chips_path}')

    r = ChipRenderer(chips)
    n = math.ceil(dur * a.fps)
    # Render strip frames with run-length dedup: most frames repeat the previous
    # state (chips change only on pop/change events) — save one PNG per run and
    # feed ffmpeg via the concat demuxer with per-frame durations.
    import hashlib, tempfile
    tmp = Path(tempfile.mkdtemp(prefix='chipstrip-', dir='/tmp'))
    runs = []            # (png_name, frame_count)
    prev_hash = None
    for f in range(n):
        img = r.frame(f / a.fps)
        h = hashlib.md5(img.tobytes()).hexdigest()
        if h == prev_hash:
            runs[-1][1] += 1
        else:
            name = f's{len(runs):04d}.png'
            img.save(tmp / name)
            runs.append([name, 1])
            prev_hash = h
        if f % 300 == 0: print(f'  frame {f}/{n}', flush=True)
    concat = tmp / 'strip.txt'
    lines = []
    for name, cnt in runs:
        lines.append(f"file '{name}'")
        lines.append(f'duration {cnt / a.fps:.4f}')
    lines.append(f"file '{runs[-1][0]}'")   # concat demuxer needs last file repeated
    concat.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'strip: {len(runs)} unique frames of {n}')

    cmd = ['ffmpeg', '-y', '-v', 'error', '-i', a.video,
           '-f', 'concat', '-safe', '0', '-i', str(concat),
           '-filter_complex', f'[0:v][1:v]overlay=0:{STRIP_Y}:format=auto',
           '-c:a', 'copy', '-c:v', 'libx264', '-crf', str(a.crf), '-preset', 'veryfast',
           '-pix_fmt', 'yuv420p', a.out]
    rc = subprocess.run(cmd).returncode
    if rc != 0: raise SystemExit(f'ffmpeg failed rc={rc}')
    print(f'DONE: {a.out}')

if __name__ == '__main__':
    main()
