#!/usr/bin/env python3
"""Production subtitle burners (Philip-approved styles, 2026-11-04).

Approved styles (all Prompt-Bold, Thai word-level timing from MFA, safe zone):
  karaoke     - phrase stays, spoken words turn mint green, rest white
  editorial   - phrase stays static on a dark translucent plate, no word anim
  highlighter - phrase stays, current word gets a neon marker bar behind it
  reveal      - phrase stays, upcoming words stay faded grey until spoken
  underline   - phrase stays, lime bar tracks under the current word
  minimal     - small white text, soft shadow only, no plate
  frosted     - dark text on a frosted white capsule
  dualtone    - words alternate black/white blocks
  comic       - white bubble with black outline, tail pointing up (default)
  luxury      - serif white text with soft shadow (Noto Serif Thai exception)
  terminal    - green text on black console box with cursor
  clay        - pastel clay letters per word, dark outline + shadow
  wordpop     - current word pops large yellow (unlike viralpop keywords)

Safe zone (locked 2026-11-04): strip y 1100-1440 in a 1080x1920 frame,
bottom margin 520px, right margin 140px. Text is drawn per WORD (never per
character) so Thai vowels/tone marks are never split from their word.
Chunks are 3-5 syllables (same rule as the approved samples). Outputs are
refused when they already exist unless --overwrite is given. No ffmpeg
subtitles filter is used anywhere - subtitles are PIL strips overlaid with ffmpeg.

Usage:
  burn_styles.py --style karaoke|editorial|highlighter|reveal|underline|minimal \
      --video IN.mov --script NN.txt --mfa NN_mfa.json --out OUT.mp4
"""
import argparse, bisect, hashlib, json, math, subprocess, sys, tempfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
STRIP_Y, STRIP_H = 1100, 340
STRIP_CY = 170
MAX_W = 900
WHITE = '#fffdf6'
MINT = '#35e0a1'
LIME = '#c8ff3d'
BLACK = '#000000'
FONT_CANDIDATES = [
    # Repo copy first (identical file to the verified production run),
    # so other agents get pixel-identical frames without Bean's local paths.
    Path(__file__).with_name('fonts') / 'Prompt-Bold.ttf',
    Path.home() / 'workspace/liplnwza-motion-kit-repo/assets/fonts/Prompt-Bold.ttf',
    Path.home() / 'workspace/liplnwza-motion-kit/v021/kit/LiplnwZa_Motion_Kit_v0.2.1/assets/fonts/Prompt-Bold.ttf',
]
FONT_PATH = next((p for p in FONT_CANDIDATES if p.exists()), FONT_CANDIDATES[0])

from functools import lru_cache
@lru_cache(maxsize=64)
def font(n):
    return ImageFont.truetype(str(FONT_PATH), n, layout_engine=ImageFont.Layout.RAQM)

SERIF_PATH = Path('/usr/share/fonts/truetype/noto/NotoSerifThai-Bold.ttf')
@lru_cache(maxsize=64)
def font_serif(n):
    p = SERIF_PATH if SERIF_PATH.exists() else FONT_PATH
    return ImageFont.truetype(str(p), n, layout_engine=ImageFont.Layout.RAQM)

CLAY_PALETTE = [('#ffb6c9', '#d97a9c'), ('#b5e8c8', '#6fbf8f'),
                ('#a8ccff', '#6a9ae0'), ('#ffe9a8', '#e0b93e')]

# Syllable counts for the clip-01 vocabulary, counted by hand (Thai ear check).
# Every MFA word in 01_mfa.json must be in this dict; unknown words raise
# instead of being guessed, so a wrong count can never slip in silently.
SYLLABLES = {
    'เป็น': 1, 'เป้': 1, 'อีก': 1, 'ใบ': 1,
    'ที่': 1, 'หยิบ': 1, 'ใช้': 1, 'บ่อย': 1, 'กว่า': 1, 'กระเป๋า': 2,
    'ทรง': 1, 'อื่น': 1, 'ไป': 1, 'แล้ว': 1,
    'ไม่': 1, 'เทอะ': 1, 'ทะ': 1,
    'แต่': 1, 'งาน': 1, 'ได้': 1, 'ครบ': 1,
    'เบา': 1, 'สบาย': 2,
    'เหมาะ': 1, 'กับ': 1, 'ชีวิต': 2, 'ประจำ': 2, 'วัน': 1, 'มาก': 1,
    'อยู่': 1, 'สวย': 1, 'เสีย': 1, 'ง่าย': 1,
    'ใส่': 1, 'ของ': 1, 'จุ': 1, 'ใจ': 1,
    'ต้อง': 1, 'กลัว': 1, 'ขาด': 1,
    'เปิด': 1, 'ควาน': 1, 'หา': 1, 'นาน': 1,
    'ใคร': 1, 'ชอบ': 1, 'แนว': 1, 'นี้': 1,
    'ดู': 1, 'ใน': 1, 'ตะกร้า': 2, 'เลย': 1, 'นะ': 1,
}

def chunk_phrases(phrases):
    """Split each script-line phrase into 3-5 syllable chunks.

    Rules: never split a word; a chunk never ends with 'ไม่' (extend it by
    one word); a trailing chunk under 3 syllables merges back into the
    previous chunk; chunks never cross a script-line boundary.
    """
    out = []
    for p in phrases:
        words = p['words']
        for w in words:
            if w['word'] not in SYLLABLES:
                raise ValueError(f"no syllable count for word {w['word']!r}")
        # Optimal partition by dynamic programming: every chunk 3-5 syllables
        # and never ending with 'ไม่'. A plain greedy cut fails here (it
        # strands a 1-syllable tail that then has to be merged back, pushing
        # the previous chunk over 5). If no valid partition exists, fall
        # back to the whole phrase as one chunk so nothing is silently cut.
        n = len(words)
        syl = [SYLLABLES[w['word']] for w in words]
        pref = [0]
        for s in syl:
            pref.append(pref[-1] + s)
        feasible = [False] * (n + 1)
        feasible[n] = True
        for i in range(n - 1, -1, -1):
            for j in range(i + 1, n + 1):
                s = pref[j] - pref[i]
                if s > 5:
                    break
                if s >= 3 and words[j - 1]['word'] != 'ไม่' and feasible[j]:
                    feasible[i] = True
                    break
        chunks, i = [], 0
        while i < n:
            cands = []
            for j in range(i + 1, n + 1):
                s = pref[j] - pref[i]
                if s > 5:
                    break
                if s >= 3 and words[j - 1]['word'] != 'ไม่' and feasible[j]:
                    cands.append((abs(s - 4), -s, j))
            if not cands:
                chunks = [words]
                break
            cands.sort()
            j = cands[0][2]
            chunks.append(words[i:j])
            i = j
        for ch in chunks:
            out.append({'text': ''.join(w['word'] for w in ch), 'words': ch,
                        'start': ch[0]['start'], 'end': ch[-1]['end'],
                        'syllables': sum(SYLLABLES[w['word']] for w in ch)})
    for j, p in enumerate(out):
        nxt = out[j + 1]['start'] if j + 1 < len(out) else None
        p['disp_end'] = p['end'] + 0.35
        if nxt is not None:
            p['disp_end'] = min(p['disp_end'], nxt - 0.05)
        if p['disp_end'] <= p['start'] + 0.2:
            p['disp_end'] = p['start'] + 0.2
    return out

# MFA tokens that bundle several dictionary words into one timed token.
# Splitting them is required for syllable chunking (chunk borders can only
# fall between timed words). Sub-word times are INTERPOLATED proportionally
# to character count inside the token's measured interval - the same
# technique chip_burn.py already uses for STT char alignment. They are not
# directly measured, so karaoke/highlighter switches inside these tokens
# must be ear-checked, and the proto json marks them.
EXPAND = {
    'ชีวิตประจำวัน': ['ชีวิต', 'ประจำ', 'วัน'],
    'กระเป๋าเบา': ['กระเป๋า', 'เบา'],
    'ไม่ต้อง': ['ไม่', 'ต้อง'],
    'เสียทรง': ['เสีย', 'ทรง'],
    'ใช้งาน': ['ใช้', 'งาน'],
    'เหมาะกับ': ['เหมาะ', 'กับ'],
    'เทอะทะ': ['เทอะ', 'ทะ'],
    'จุใจ': ['จุ', 'ใจ'],
    'ควานหา': ['ควาน', 'หา'],
}

def expand_mfa_words(mfa_words):
    out = []
    for w in mfa_words:
        subs = EXPAND.get(w['word'])
        if not subs:
            out.append({'word': w['word'], 'start': float(w['start']),
                        'end': float(w['end']), 'interpolated': False})
            continue
        total_chars = sum(len(s) for s in subs)
        t0, dur = float(w['start']), float(w['end']) - float(w['start'])
        t = t0
        for si, s in enumerate(subs):
            share = dur * len(s) / total_chars
            end = t0 + dur if si == len(subs) - 1 else t + share
            out.append({'word': s, 'start': t, 'end': end, 'interpolated': True})
            t = end
    return out

def load_phrases(script_text, mfa_words):
    """Group MFA words into the script lines by exact char consumption."""
    mfa_words = expand_mfa_words(mfa_words)
    lines = [l.strip() for l in script_text.split('\n') if l.strip()]
    phrases, i = [], 0
    for line in lines:
        target = line.replace(' ', '')
        words, acc = [], ''
        while i < len(mfa_words) and len(acc) < len(target):
            w = mfa_words[i]
            words.append({'word': w['word'], 'start': float(w['start']), 'end': float(w['end']),
                          'interpolated': bool(w.get('interpolated', False))})
            acc += w['word']; i += 1
        if acc != target:
            raise ValueError(f'phrase/MFA mismatch on line {line!r}: got {acc!r}')
        phrases.append({'text': line, 'words': words,
                        'start': words[0]['start'], 'end': words[-1]['end']})
    if i != len(mfa_words):
        raise ValueError(f'{len(mfa_words) - i} MFA words left unconsumed')
    for j, p in enumerate(phrases):
        nxt = phrases[j + 1]['start'] if j + 1 < len(phrases) else None
        p['disp_end'] = p['end'] + 0.35
        if nxt is not None:
            p['disp_end'] = min(p['disp_end'], nxt - 0.05)
        if p['disp_end'] <= p['start'] + 0.2:
            p['disp_end'] = p['start'] + 0.2
    return phrases

def layout_phrase(phrase, per_word):
    """Return (size, lines) where lines = list of word-lists fitting MAX_W."""
    for size in range(58, 35, -2):
        f = font(size)
        ascent, descent = f.getmetrics()
        line_h = ascent + descent + 14
        lines, cur, cur_w = [], [], 0.0
        gap = 12 if per_word else 0
        for w in phrase['words']:
            ww = f.getlength(w['word']) if per_word else None
            if per_word:
                add = ww if not cur else gap + ww
                if cur and cur_w + add > MAX_W:
                    lines.append(cur); cur, cur_w = [w], ww
                else:
                    cur.append(w); cur_w += add
            else:
                cur.append(w)
        if cur:
            lines.append(cur)
        if not per_word:
            # re-split whole-phrase lines by measured line-string width
            lines2, cur, cur_w = [], [], 0.0
            for w in phrase['words']:
                trial = ''.join(x['word'] for x in cur) + w['word']
                tw = f.getlength(trial)
                if cur and tw > MAX_W:
                    lines2.append(cur); cur = [w]
                else:
                    cur.append(w)
            if cur:
                lines2.append(cur)
            lines = lines2
        if len(lines) <= 2:
            widths = []
            for ln in lines:
                if per_word:
                    widths.append(sum(f.getlength(x['word']) for x in ln) + gap * (len(ln) - 1))
                else:
                    widths.append(f.getlength(''.join(x['word'] for x in ln)))
            if max(widths) <= MAX_W:
                return size, lines, line_h, widths
    raise ValueError(f"phrase does not fit: {phrase['text']!r}")

def layout_serif(phrase):
    """Like layout_phrase(per_word=False) but with the serif font."""
    for size in range(56, 35, -2):
        f = font_serif(size)
        ascent, descent = f.getmetrics()
        line_h = ascent + descent + 14
        lines, cur = [], []
        for w in phrase['words']:
            trial = ''.join(x['word'] for x in cur) + w['word']
            if cur and f.getlength(trial) > MAX_W:
                lines.append(cur); cur = [w]
            else:
                cur.append(w)
        if cur:
            lines.append(cur)
        if len(lines) <= 2:
            widths = [f.getlength(''.join(x['word'] for x in ln)) for ln in lines]
            if max(widths) <= MAX_W:
                return size, lines, line_h, widths
    raise ValueError(f"phrase does not fit (serif): {phrase['text']!r}")

def layout_fixed(phrase, size):
    """Single fixed-size layout (whole phrase, split by measured width)."""
    f = font(size)
    ascent, descent = f.getmetrics()
    line_h = ascent + descent + 14
    lines, cur = [], []
    for w in phrase['words']:
        trial = ''.join(x['word'] for x in cur) + w['word']
        if cur and f.getlength(trial) > MAX_W:
            lines.append(cur); cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(cur)
    widths = [f.getlength(''.join(x['word'] for x in ln)) for ln in lines]
    return size, lines, line_h, widths

class Renderer:
    def __init__(self, style, phrases):
        self.style = style
        self.phrases = phrases
        self.starts = [p['start'] for p in phrases]
        self.layouts = {}
        for idx, p in enumerate(phrases):
            if style == 'luxury':
                self.layouts[idx] = layout_serif(p)
            elif style == 'minimal':
                self.layouts[idx] = layout_fixed(p, 40)
            else:
                self.layouts[idx] = layout_phrase(p, style in ('karaoke', 'highlighter', 'reveal', 'underline', 'dualtone', 'clay', 'wordpop'))

    def frame(self, t):
        im = Image.new('RGBA', (W, STRIP_H))
        idx = bisect.bisect_right(self.starts, t) - 1
        if idx < 0:
            return im
        p = self.phrases[idx]
        if t > p['disp_end']:
            return im
        size, lines, line_h, widths = self.layouts[idx]
        f = font_serif(size) if self.style == 'luxury' else font(size)
        ascent, descent = f.getmetrics()
        total_h = len(lines) * line_h - 14
        top = STRIP_CY - total_h / 2
        dr = ImageDraw.Draw(im)
        if self.style == 'editorial':
            pw = max(widths) + 56
            ph = total_h + 40
            dr.rounded_rectangle([540 - pw / 2, STRIP_CY - ph / 2,
                                  540 + pw / 2, STRIP_CY + ph / 2],
                                 radius=28, fill=(0, 0, 0, 190))
        for li, ln in enumerate(lines):
            baseline = top + li * line_h + ascent
            if self.style == 'minimal':
                txt = ''.join(x['word'] for x in ln)
                x = 540 - widths[li] / 2
                dr.text((x + 2, baseline + 3), txt, font=f, fill=(0, 0, 0, 150), anchor='ls')
                dr.text((x, baseline), txt, font=f, fill=WHITE, anchor='ls')
            elif self.style == 'luxury':
                txt = ''.join(x['word'] for x in ln)
                x = 540 - widths[li] / 2
                # Soft drop shadow so serif white stays readable on bright
                # footage (fix from the still-sample review).
                dr.text((x + 2, baseline + 3), txt, font=f, fill=(0, 0, 0, 170), anchor='ls')
                dr.text((x, baseline), txt, font=f, fill=WHITE, anchor='ls',
                        stroke_width=1, stroke_fill=BLACK)
            elif self.style == 'terminal':
                txt = ''.join(x['word'] for x in ln)
                tw = widths[li]
                dr.rounded_rectangle([540 - tw / 2 - 32, baseline - ascent - 18,
                                      540 + tw / 2 + 32, baseline + descent + 18],
                                     radius=16, fill=BLACK, outline='#33ff66', width=2)
                x = 540 - tw / 2
                dr.text((x, baseline), txt, font=f, fill='#33ff66', anchor='ls')
                dr.rectangle([x + tw + 12, baseline - ascent,
                              x + tw + 28, baseline + descent], fill='#33ff66')
            elif self.style == 'frosted':
                pw = widths[li] + 56
                dr.rounded_rectangle([540 - pw / 2, baseline - ascent - 14,
                                      540 + pw / 2, baseline + descent + 14],
                                     radius=30, fill=(255, 255, 255, 225))
                txt = ''.join(x['word'] for x in ln)
                x = 540 - widths[li] / 2
                dr.text((x, baseline), txt, font=f, fill='#1a1a1a', anchor='ls')
            elif self.style == 'comic':
                pw = widths[li] + 64
                top_y = baseline - ascent - 18
                bot_y = baseline + descent + 18
                dr.polygon([(540 - 20, top_y + 6), (540 + 20, top_y + 6), (540, top_y - 26)],
                           fill=(255, 255, 255, 255), outline=BLACK)
                dr.rounded_rectangle([540 - pw / 2, top_y,
                                      540 + pw / 2, bot_y],
                                     radius=28, fill=(255, 255, 255, 255), outline=BLACK, width=4)
                txt = ''.join(x['word'] for x in ln)
                x = 540 - widths[li] / 2
                dr.text((x, baseline), txt, font=f, fill='#1a1a1a', anchor='ls')
            elif self.style == 'editorial':
                txt = ''.join(x['word'] for x in ln)
                x = 540 - widths[li] / 2
                dr.text((x, baseline), txt, font=f, fill=WHITE,
                        anchor='ls', stroke_width=1, stroke_fill=BLACK)
            elif self.style == 'wordpop':
                # Current word pops large yellow; the rest stays white.
                # Drawn in two passes so the popped word sits on top of its
                # neighbours instead of being clipped by them.
                flat = [w for w in ln]
                cur = None
                for w in flat:
                    if w['start'] <= t:
                        cur = w
                x = 540 - widths[li] / 2
                slots = []
                for w in flat:
                    ww = f.getlength(w['word'])
                    slots.append((x, ww)); x += ww + 12
                for (sx, ww), w in zip(slots, flat):
                    if w is not cur:
                        dr.text((sx, baseline), w['word'], font=f, fill=WHITE,
                                anchor='ls', stroke_width=2, stroke_fill=BLACK)
                if cur is not None:
                    sx, ww = slots[flat.index(cur)]
                    fp = font(size + 18)
                    pw = fp.getlength(cur['word'])
                    dr.text((sx + ww / 2 - pw / 2, baseline), cur['word'], font=fp,
                            fill='#ffd60a', anchor='ls', stroke_width=2, stroke_fill=BLACK)
            else:
                x = 540 - widths[li] / 2
                cur_u = None
                if self.style == 'underline':
                    for w in ln:
                        if w['start'] <= t:
                            cur_u = w
                for wi, w in enumerate(ln):
                    ww = f.getlength(w['word'])
                    if self.style == 'reveal':
                        if w['start'] <= t:
                            dr.text((x, baseline), w['word'], font=f, fill=WHITE,
                                    anchor='ls', stroke_width=2, stroke_fill=BLACK)
                        else:
                            dr.text((x, baseline), w['word'], font=f,
                                    fill=(170, 170, 170, 255), anchor='ls')
                    elif self.style == 'underline':
                        dr.text((x, baseline), w['word'], font=f, fill=WHITE,
                                anchor='ls', stroke_width=2, stroke_fill=BLACK)
                        if w is cur_u:
                            dr.rounded_rectangle([x - 4, baseline + 12,
                                                  x + ww + 4, baseline + 26],
                                                 radius=7, fill=LIME)
                    elif self.style == 'clay':
                        fill, stroke = CLAY_PALETTE[wi % len(CLAY_PALETTE)]
                        dr.text((x + 2, baseline + 3), w['word'], font=f,
                                fill=(0, 0, 0, 90), anchor='ls')
                        dr.text((x, baseline), w['word'], font=f, fill=fill,
                                anchor='ls', stroke_width=2, stroke_fill=stroke)
                    elif self.style == 'dualtone':
                        # Alternate black/white blocks per word. Black blocks
                        # get a thin white outline so they stay visible on
                        # dark footage (fix from the still-sample review).
                        if wi % 2 == 0:
                            dr.rounded_rectangle([x - 10, baseline - ascent - 8,
                                                  x + ww + 10, baseline + descent + 8],
                                                 radius=10, fill=BLACK, outline=WHITE, width=2)
                            dr.text((x, baseline), w['word'], font=f, fill=WHITE, anchor='ls')
                        else:
                            dr.rounded_rectangle([x - 10, baseline - ascent - 8,
                                                  x + ww + 10, baseline + descent + 8],
                                                 radius=10, fill=WHITE, outline=BLACK, width=2)
                            dr.text((x, baseline), w['word'], font=f, fill=BLACK, anchor='ls')
                    elif self.style == 'karaoke':
                        col = MINT if w['start'] <= t else WHITE
                        dr.text((x, baseline), w['word'], font=f, fill=col,
                                anchor='ls', stroke_width=2, stroke_fill=BLACK)
                    else:  # highlighter
                        if w['start'] <= t <= w['end']:
                            dr.rounded_rectangle([x - 12, baseline - ascent - 10,
                                                  x + ww + 12, baseline + descent + 10],
                                                 radius=18, fill=LIME)
                            dr.text((x, baseline), w['word'], font=f, fill=BLACK, anchor='ls')
                        else:
                            dr.text((x, baseline), w['word'], font=f, fill=WHITE,
                                    anchor='ls', stroke_width=2, stroke_fill=BLACK)
                    x += ww + 12
        # fade in/out 0.12s at phrase edges
        fade = 1.0
        if t < p['start'] + 0.12:
            fade = max(0.0, (t - p['start']) / 0.12)
        elif t > p['disp_end'] - 0.12:
            fade = max(0.0, (p['disp_end'] - t) / 0.12)
        if fade < 1.0:
            a = im.split()[3].point(lambda v: int(v * fade))
            im.putalpha(a)
        return im

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--style', required=True, choices=['karaoke', 'editorial', 'highlighter',
                                                       'reveal', 'underline', 'minimal',
                                                       'frosted', 'dualtone', 'comic',
                                                       'luxury', 'terminal', 'clay', 'wordpop'])
    ap.add_argument('--video', required=True)
    ap.add_argument('--script', required=True)
    ap.add_argument('--mfa', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--overwrite', action='store_true',
                    help='allow replacing existing output files')
    ap.add_argument('--syllable-chunks', action='store_true',
                    help='re-chunk script lines into 3-5 syllable chunks')
    ap.add_argument('--fps', type=int, default=30)
    ap.add_argument('--crf', type=int, default=18)
    a = ap.parse_args()
    proto_path = Path(a.out + '.proto.json')
    if not a.overwrite:
        for output_path in (Path(a.out), proto_path):
            if output_path.exists() or output_path.is_symlink():
                raise SystemExit(
                    f'refusing to overwrite: {output_path} already exists '
                    '(pass --overwrite to replace it)'
                )
    script = Path(a.script).read_text(encoding='utf-8')
    mfa = json.loads(Path(a.mfa).read_text(encoding='utf-8'))
    phrases = load_phrases(script, mfa['words'])
    if a.syllable_chunks:
        phrases = chunk_phrases(phrases)
        print('syllable chunks:', len(phrases))
        for p in phrases:
            print(f"  {p['syllables']} syl {p['start']:.2f}-{p['end']:.2f}s {p['text']}", flush=True)
    proto_path.write_text(
        json.dumps({'style': a.style, 'phrases': phrases}, ensure_ascii=False, indent=1),
        encoding='utf-8')
    dur = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                                         'format=duration', '-of', 'csv=p=0', a.video]).decode().strip())
    print(f'style={a.style} phrases={len(phrases)} words={sum(len(p["words"]) for p in phrases)} '
          f'speech {phrases[0]["start"]:.2f}-{phrases[-1]["end"]:.2f}s of {dur:.1f}s', flush=True)
    r = Renderer(a.style, phrases)
    n = math.ceil(dur * a.fps)
    tmp = Path(tempfile.mkdtemp(prefix='protostrip-', dir='/tmp'))
    runs, prev = [], None
    for fi in range(n):
        img = r.frame(fi / a.fps)
        h = hashlib.md5(img.tobytes()).hexdigest()
        if h == prev:
            runs[-1][1] += 1
        else:
            name = f's{len(runs):04d}.png'
            img.save(tmp / name)
            runs.append([name, 1]); prev = h
        if fi % 300 == 0:
            print(f'  frame {fi}/{n}', flush=True)
    concat = tmp / 'strip.txt'
    lines = []
    for name, cnt in runs:
        lines.append(f"file '{name}'")
        lines.append(f'duration {cnt / a.fps:.4f}')
    lines.append(f"file '{runs[-1][0]}'")
    concat.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'strip: {len(runs)} unique frames of {n}', flush=True)
    cmd = ['ffmpeg', '-y', '-v', 'error', '-i', a.video,
           '-f', 'concat', '-safe', '0', '-i', str(concat),
           '-filter_complex', f'[0:v][1:v]overlay=0:{STRIP_Y}:format=auto',
           '-c:a', 'copy', '-c:v', 'libx264', '-crf', str(a.crf), '-preset', 'veryfast',
           '-pix_fmt', 'yuv420p', a.out]
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        raise SystemExit(f'ffmpeg failed rc={rc}')
    print(f'DONE: {a.out}')

if __name__ == '__main__':
    main()
