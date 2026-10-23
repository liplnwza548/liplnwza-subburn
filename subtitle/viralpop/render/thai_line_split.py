#!/usr/bin/env python3
"""Width-aware Thai phrase splitting.

Measures each authored subtitle phrase's REAL rendered pixel width (shaped
with the actual Prompt-Bold font via HarfBuzz, not guessed from character
count) and, if it would overflow the video's safe width at the real style's
fontsize, splits it into shorter phrases at a safe Thai syllable boundary so
the line no longer runs off the frame edges.

This module only decides WHERE to cut the text. It does not invent timing:
each resulting sub-phrase is fed through the existing, unmodified
Whisper-character-timestamp alignment (see build_v5_candidate.py, which
reuses build_v4.py's proven `align_maps`/phrase-span search verbatim) so
every sub-cue's start/end still comes from real per-character ASR timing.

Syllable layer (V6): `count_syllables()` counts spoken syllables for the
3-5-syllable cue target using `pythainlp syllable_tokenize(engine="dict")`,
with two corrections:
- known loanwords (see ../loanword_dict.json) use their true spoken count
  and are never cut through (protected spans);
- digit runs are counted by Thai pronunciation (6->"หก"=1, 10->"สิบ"=1).

Segmentation backends (C6 2026-09-25): WITH pythainlp the newmm
word tokenizer gives real word boundaries and cuts land between words.
WITHOUT it (offline boxes) there is NO Thai word segmentation, so the
only provable word boundaries are whitespace gaps: cuts are restricted
to space-adjacent positions, and a spaceless run that overflows the
budget raises NoSafeSplitError (fail loud — install pythainlp, reword,
or widen the budget) instead of severing a word mid-grapheme.
Limits: the fallback cannot find phrase/word edges inside spaceless
text, so long spaceless lines REQUIRE the dictionary backend to split.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from fontTools.ttLib import TTFont

try:
    import uharfbuzz as hb
    from pythainlp.corpus.common import thai_words
    from pythainlp.tokenize import Tokenizer, syllable_tokenize
    from pythainlp.util import Trie
    _FULL_NLP = True
    BACKEND = "harfbuzz+pythainlp"
except ImportError:  # Meta AI / offline boxes without net: graceful fallback
    hb = None  # type: ignore[assignment]
    thai_words = None  # type: ignore[assignment]
    Tokenizer = None  # type: ignore[assignment]
    syllable_tokenize = None  # type: ignore[assignment]
    Trie = None  # type: ignore[assignment]
    _FULL_NLP = False
    BACKEND = "fonttools+regex-fallback"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loanwords as _loanwords  # noqa: E402

# Marks that attach to the previous base character (zero advance width) --
# never split immediately before one of these (it would strand it without
# its base).
COMBINING_MARKS = set("ัิีึืุูำ็่้๊๋์ํฺ")
# Leading vowels that are written before their consonant but read after it --
# never split immediately after one of these (it would strand it without the
# consonant it belongs to).
LEADING_VOWELS = set("เแโใไ")


class NoSafeSplitError(ValueError):
    """No word-boundary-safe cut exists for an over-budget phrase.

    Raised only when no Thai word dictionary is available (offline box
    without pythainlp) and the overflowing run has no whitespace gap to
    cut on. Callers translate this to their own fail-loud error
    (script_split -> WidthOverflowError). It is NEVER correct to catch
    this and cut mid-run: Thai has no inter-word spaces, so any cut
    inside a spaceless run severs a word mid-grapheme.
    """


def _build_tokenizer() -> Tokenizer:
    """newmm tokenizer whose dictionary also knows every loanword canonical
    form and Thai variant, so custom terms (e.g. "โฮลเดอร์") segment as one
    token instead of being severed mid-word ("โฮ" + "ลเดอร์"). The standard
    thai_words() set is kept (union, not replacement) so ordinary Thai still
    segments as before."""
    extra: set[str] = set()
    for e in _loanwords.entries():
        extra.add(e["canonical"])
        for v in e.get("variants", []):
            if not re.search(r"[A-Za-z]", v):
                extra.add(v)
    return Tokenizer(custom_dict=Trie(set(thai_words()) | extra), engine="newmm")


_tokenizer = _build_tokenizer() if _FULL_NLP else None


# --- Fallback segmentation (no pythainlp): regex Thai syllables ---------
# Over-segments slightly on ambiguous finals (safe direction: shorter lines,
# never overflow). Combining-mark / leading-vowel safety is still enforced
# by _safe_split_points, and loanword spans stay protected via loanwords.py
# (pure stdlib, works in both backends).
_THAI_CONS = set("กขฃคฅฆงจฉชซฌญฎฏฐฑฒณดตถทธนบปผฝพฟภมยรลวศษสหฬอฮ")
_CLUSTER_SECOND = set("รลว")
_CLUSTER_FIRST = set("กขคตทปผพ")
# Consonants that can close a Thai syllable: a lone one almost always
# belongs to the previous syllable (ถึง ส่ง มาก เป็น).
_FINAL_MERGE = set("กดบนมงยวญณฬลรจพทบปค")


def _fallback_syllable_tokens(text: str) -> list[str]:
    toks: list[str] = []
    cur = ""
    for ch in text:
        if ch in _THAI_CONS and cur:
            prev = cur[-1]
            # Keep true clusters (คร ปล กว ...) inside one syllable.
            if not (ch in _CLUSTER_SECOND and prev in _CLUSTER_FIRST):
                toks.append(cur)
                cur = ""
        elif ch.isspace() and cur:
            toks.append(cur)
            cur = ""
            continue
        cur += ch
    if cur:
        toks.append(cur)
    toks = [t for t in toks if t.strip()] or ([text] if text.strip() else [])
    # Merge lone final consonants back (ถึ|ง -> ถึง, ส่|ง -> ส่ง):
    # a single-consonant token that can close a syllable belongs to the
    # previous one far more often than it starts a new syllable.
    merged: list[str] = []
    for t in toks:
        if len(t) == 1 and t in _FINAL_MERGE and merged:
            merged[-1] += t
        else:
            merged.append(t)
    return merged


def _syllable_tokens(text: str) -> list[str]:
    if _FULL_NLP:
        return [t for t in syllable_tokenize(text, engine="dict")]
    return _fallback_syllable_tokens(text)


def tokenize_words(text: str) -> list[str]:
    """Word tokens with loanwords kept whole (used by the syllable packer)."""
    if _tokenizer is not None:
        return [t for t in _tokenizer.word_tokenize(text) if t.strip()]
    # Fallback: loanword spans whole + whitespace split for the rest.
    out: list[str] = []
    spans = sorted(_loanwords.protected_spans(text) + _loanwords.override_spans(text))
    cursor = 0
    for a, b in spans:
        if a < cursor:  # overlapping span already covered
            continue
        for part in text[cursor:a].split():
            out.extend(_fallback_syllable_tokens(part))
        out.append(text[a:b])
        cursor = b
    for part in text[cursor:].split():
        out.extend(_fallback_syllable_tokens(part))
    return [t for t in out if t.strip()]


def _word_boundaries(text: str) -> list[int]:
    """Character offsets that fall BETWEEN word tokens (i.e. safe to cut on
    without breaking a word, e.g. "ถึง" or "ลง" apart). Character classes
    alone (combining marks / leading vowels) are not enough for Thai:
    a plain final consonant like the "ง" in "ถึง" is not a mark and not a
    leading vowel, so a class-only rule would happily sever it -- real word
    segmentation is required. Fallback backend (no pythainlp): no word
    boundary is claimed, callers relax to syllable/character-class cuts."""
    if _tokenizer is None:
        return []
    tokens = _tokenizer.word_tokenize(text)
    offsets = []
    pos = 0
    for tok in tokens:
        pos += len(tok)
        if 0 < pos < len(text):
            offsets.append(pos)
    return offsets


#: Phrase-integrity rules for cue splitting (Thai readability, VO 2026-09-24).
#: A cut AFTER one of these must stick to the following word (prefixes,
#: conjunctions, relativizers, prepositions, auxiliaries) ...
_NO_SPLIT_AFTER = frozenset([
    "กำลัง", "ความ", "การ", "นัก", "ชาว", "ผู้",
    "ที่", "ซึ่ง", "และ", "แต่", "เพราะ", "ว่า",
    "ของ", "ใน", "ไป", "มา", "จะ", "ได้", "ให้", "กับ", "โดย",
])
#: ... and a cut BEFORE one of these must stick to the preceding word
#: (sentence-final particles, intensifiers, complements).
_NO_SPLIT_BEFORE = frozenset([
    "เลย", "แล้ว", "มาก", "ที่สุด", "นะ", "คะ", "ค่ะ", "ครับ",
    "สิ", "หน่อย", "เยอะ", "ไว้", "จัง", "บ้าง",
])
#: Bonus (not a ban): cuts right BEFORE one of these start a new phrase
#: (clause linkers first, then VO-proven phrase heads).
_PREFER_BEFORE = frozenset([
    "ที่", "ซึ่ง", "และ", "แต่", "เพราะ", "ว่า", "คือ",
    "ขอ", "วัน", "แสง", "กระเป๋า", "ช่อง", "หยิบ", "แถม",
    "แนะนำ", "สั่งซื้อ", "ราคา", "เปิด", "จุ",
])
#: Exception to the ที่-bonus: after a comparative head the ที่-clause is
#: inseparable (กว่าที่คิด, เท่าที่เห็น, เหมือนที่บอก). Cutting there
#: severs the comparison, so it is FORBIDDEN, not preferred.
_COMPARATIVE_HEADS = ("กว่า", "เท่า", "เหมือน", "คล้าย", "เท่ากับ")
#: Bonus: cuts right AFTER a time expression (verb phrase starts next).
_PREFER_AFTER_TIME_TAILS = ("โมงเย็น", "โมงเช้า", "ตอนเช้า", "ตอนเย็น",
                            "ตอนกลางคืน", "เมื่อวาน", "เมื่อเช้า", "วันนี้")
#: Score bonus (px) for a preferred cut, as a fraction of total width.
_PREFER_BONUS_FRAC = 0.30


def _word_tokens(text: str) -> list[tuple[str, int, int]]:
    """Non-space word tokens with char spans [(tok, start, end)]."""
    if _tokenizer is None:
        return []
    out: list[tuple[str, int, int]] = []
    pos = 0
    for tok in _tokenizer.word_tokenize(text):
        start = pos
        pos += len(tok)
        if tok.strip():
            out.append((tok, start, pos))
    return out


def _phrase_forbidden(i: int, toks: list[tuple[str, int, int]]) -> bool:
    """True when cutting at char i would sever a Thai phrase."""
    prev = None
    nxt = None
    for tok, a, b in toks:
        if b <= i:
            prev = tok
        if a >= i and nxt is None:
            nxt = tok
    if prev in _NO_SPLIT_AFTER:
        return True
    if nxt in _NO_SPLIT_BEFORE:
        return True
    if nxt == "ที่" and prev is not None and prev.endswith(_COMPARATIVE_HEADS):
        return True
    return False


def _phrase_preferred(i: int, toks: list[tuple[str, int, int]]) -> bool:
    """True when char i is a natural phrase edge (bonus, never a ban)."""
    prev = None
    nxt = None
    for tok, a, b in toks:
        if b <= i:
            prev = tok
        if a >= i and nxt is None:
            nxt = tok
    if nxt in _PREFER_BEFORE:
        return True
    if prev is not None and prev.endswith(_PREFER_AFTER_TIME_TAILS):
        return True
    return False


def _dict_syllable_count(chunk: str) -> int:
    chunk = chunk.strip()
    if not chunk:
        return 0
    return sum(1 for t in _syllable_tokens(chunk) if t.strip())


def _digit_syllable_count(run: str) -> int:
    try:
        n = int(run)
    except ValueError:
        return len(run)
    if 0 <= n <= 999:
        return _dict_syllable_count(_loanwords.thai_number_reading(n))
    raise ValueError(
        f"digit run '{run}' exceeds supported range (0-999); "
        f"cannot count syllables by pronunciation"
    )


def count_syllables(text: str) -> int:
    """Spoken-syllable count for the 3-5-syllable cue target.

    Loanword spans use their table override (never the raw dict-engine
    count, which is wrong both ways: "สแตนเลส" undercounts, "โฮลเดอร์"
    overcounts with a junk fragment). Digit runs count by pronunciation.
    Everything else uses syllable_tokenize(engine="dict").

    ๆ (mai yamok) repeats the preceding unit ("รีบๆ" = รีบ-รีบ = 2): each ๆ
    adds one more count of the unit before it (table canonical preferred,
    else the last word token), then ๆ itself counts 0.
    """
    extra = 0
    canonicals = sorted(
        (e["canonical"] for e in _loanwords.entries()), key=len, reverse=True
    )
    for m in re.finditer(r"ๆ", text):
        pre = text[: m.start()]
        unit = next((c for c in canonicals if pre.endswith(c)), None)
        if unit is None:
            if _tokenizer is not None:
                toks = [t for t in _tokenizer.word_tokenize(pre) if t.strip()]
            else:
                toks = tokenize_words(pre)
            unit = toks[-1] if toks else None
        if unit:
            extra += count_syllables(unit)
    text = text.replace("ๆ", "")
    spans = _loanwords.override_spans(text)
    # Non-overlapping count segments: (start, end, kind).
    segments: list[tuple[int, int, str]] = []
    for a, b in spans:
        ov = _loanwords.syllable_override(text[a:b])
        segments.append((a, b, "override" if ov is not None else "plain"))
    for m in re.finditer(r"\d+", text):
        a, b = m.start(), m.end()
        if not any(x < b and a < y for x, y, _ in segments):
            segments.append((a, b, "digits"))
    segments.sort()
    count = 0
    cursor = 0
    for a, b, kind in segments:
        if a > cursor:
            count += _dict_syllable_count(text[cursor:a])
        if kind == "override":
            count += _loanwords.syllable_override(text[a:b]) or 0
        elif kind == "digits":
            count += _digit_syllable_count(text[a:b])
        else:
            count += _dict_syllable_count(text[a:b])
        cursor = b
    if cursor < len(text):
        count += _dict_syllable_count(text[cursor:])
    return count + extra


def _syllable_boundaries(text: str) -> list[int]:
    """Character offsets between dict-engine syllables (whitespace ignored),
    usable as a last-resort cut grid inside a single over-budget span."""
    bounds = []
    pos = 0
    for tok in _syllable_tokens(text):
        pos += len(tok)
        if 0 < pos < len(text) and tok.strip():
            bounds.append(pos)
    return bounds


class _Shaper:
    def __init__(self, font_path: str, fontsize: float):
        ttf = TTFont(font_path)
        self.upem = ttf["head"].unitsPerEm
        self.fontsize = fontsize
        self.font = None
        self._adv: dict[str, float] | None = None
        if hb is not None:
            blob = hb.Blob.from_file_path(font_path)
            face = hb.Face(blob)
            self.font = hb.Font(face)
            self.font.scale = (self.upem, self.upem)
        else:
            # Fallback: raw hmtx advances (no kern/mark positioning).
            # Thai combining marks ship with 0 advance in Prompt, so the
            # sum matches HarfBuzz cluster widths closely enough for the
            # split decision; final burn is still ffmpeg/libass shaped.
            cmap = ttf.getBestCmap()
            hmtx = ttf["hmtx"]
            self._adv = {}
            for cp, glyph in cmap.items():
                try:
                    self._adv[chr(cp)] = float(hmtx[glyph][0])
                except KeyError:
                    continue
            self._space_w = float(self._adv.get(" ", self.upem // 2))

    def cluster_widths_px(self, text: str) -> list[float]:
        """Per-character advance width in pixels, in string order (0 for
        zero-advance combining marks, matching the character they attach to)."""
        if self.font is not None:
            buf = hb.Buffer()
            buf.add_str(text)
            buf.guess_segment_properties()
            hb.shape(self.font, buf, {"kern": True, "mark": True, "mkmk": True, "ccmp": True})
            # HarfBuzz may reorder/merge for shaping; since Thai has no reordering
            # and every mark keeps its own cluster == its own char index here,
            # map back to per-character advances by cluster index.
            widths = [0.0] * len(text)
            for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
                widths[info.cluster] += pos.x_advance / self.upem * self.fontsize
            return widths
        assert self._adv is not None
        return [self._adv.get(ch, self._space_w) / self.upem * self.fontsize for ch in text]

    def width_px(self, text: str) -> float:
        return sum(self.cluster_widths_px(text))


_shaper: _Shaper | None = None


def init(font_path: str, fontsize: float) -> None:
    global _shaper
    _shaper = _Shaper(font_path, fontsize)


def width_px(text: str) -> float:
    assert _shaper is not None, "call init() first"
    return _shaper.width_px(text)


def _prev_base(text: str, i: int) -> str:
    """Nearest preceding character that is not a combining mark (the base
    the cut would separate from)."""
    j = i - 1
    while j >= 0 and text[j] in COMBINING_MARKS:
        j -= 1
    return text[j] if j >= 0 else ""


def _safe_split_points(
    text: str, protected_spans: list[tuple[int, int]], require_word_boundary: bool
) -> list[int]:
    """Character indices i (0 < i < len(text)) where text[:i] | text[i:] is a
    safe cut: not inside a combining-mark cluster, not between a leading
    vowel and its consonant, not inside a protected (keyword/loanword) span,
    and (when `require_word_boundary`) only on a real word-token boundary --
    character classes alone cannot tell a syllable-final consonant (e.g. the
    "ง" in "ถึง") from the start of the next syllable, so word segmentation
    is required to avoid splitting a word in half.

    No-dictionary backend (offline box without pythainlp): there is no word
    segmentation at all, so the ONLY provable word boundaries are the
    whitespace gaps — cuts are restricted to space-adjacent positions.
    Never cut inside a spaceless run (C6 2026-09-25: เซ็ต -> เซ็ | ต)."""
    def inside_protected(i: int) -> bool:
        return any(a < i < b for a, b in protected_spans)

    space_only = require_word_boundary and _tokenizer is None
    word_ok = (set(_word_boundaries(text))
               if (require_word_boundary and not space_only) else None)
    toks = (_word_tokens(text)
            if (require_word_boundary and not space_only) else [])
    points = []
    for i in range(1, len(text)):
        if space_only and not (text[i].isspace() or text[i - 1].isspace()):
            continue
        if word_ok is not None and i not in word_ok:
            continue
        if toks and _phrase_forbidden(i, toks):
            # Phrase-integrity (VO 2026-09-24): never sever a prefix
            # (กำลัง|สวย), a suffix (|เลย), or a linker from the word it
            # belongs to. Removed on the strict path only; the relaxed
            # fallback below keeps them.
            continue
        if text[i] in COMBINING_MARKS:
            continue
        # Fallback backend has no dictionary: a lone syllable-final
        # consonant (ง in ทรง/ส่ง) belongs to the syllable on its LEFT.
        # Cutting before it strands a bare onset (ทร|ง) -- forbid it the
        # same way we forbid stranding combining marks.
        if (
            _tokenizer is None
            and text[i] in _FINAL_MERGE
            and _prev_base(text, i) in _THAI_CONS
        ):
            continue
        # Never strand ๆ (mai yamok) without the word it repeats.
        if text[i] == "ๆ":
            continue
        if text[i - 1] in LEADING_VOWELS:
            continue
        if inside_protected(i):
            continue
        points.append(i)
    return points


def _merge_loanword_spans(
    text: str, protected_spans: list[tuple[int, int]] | None
) -> list[tuple[int, int]]:
    merged = list(protected_spans or [])
    for span in _loanwords.protected_spans(text):
        if not any(a < span[1] and span[0] < b for a, b in merged):
            merged.append(span)
    return merged


def split_phrase_tracked(
    text: str,
    max_width_px: float,
    protected_spans: list[tuple[int, int]] | None = None,
) -> tuple[list[str], bool]:
    """Like split_phrase, but also returns forced=True when the only way to
    fit the width was cutting inside a protected loanword span at a syllable
    boundary (single over-budget span covering the whole phrase). Callers
    must surface the flag for QA review instead of auto-passing."""
    assert _shaper is not None, "call init() first"
    protected_spans = _merge_loanword_spans(text, protected_spans)
    if _shaper.width_px(text) <= max_width_px or len(text) < 2:
        return [text], False

    widths = _shaper.cluster_widths_px(text)
    cum = []
    total = 0.0
    for w in widths:
        total += w
        cum.append(total)
    target = total / 2.0

    forced = False
    has_dict = _tokenizer is not None
    candidates = _safe_split_points(text, protected_spans, require_word_boundary=True)
    if not candidates and has_dict:
        # No word-token boundary available (rare: the whole overflowing
        # phrase tokenized as one run) -- relax to a syllable-safe cut
        # (still never breaks a combining mark or a leading vowel off its
        # consonant) rather than leaving the line to overflow the frame.
        # Dictionary-only: without word segmentation there is no safe
        # syllable grid, so the fallback backend never relaxes (it already
        # cuts on the whitespace grid above).
        candidates = _safe_split_points(text, protected_spans, require_word_boundary=False)
    if not candidates and has_dict:
        # A single protected span covers the whole over-budget phrase, so no
        # safe point exists. Exception path: cut at a syllable boundary
        # inside it (still mark/leading-vowel safe) and flag forced=True.
        for a, b in protected_spans:
            if a <= 0 and b >= len(text):
                syl = [
                    i for i in _syllable_boundaries(text)
                    if not (text[i] in COMBINING_MARKS or text[i - 1] in LEADING_VOWELS)
                ]
                if syl:
                    candidates = syl
                    forced = True
                break
    if not candidates:
        # No word-safe cut exists. Cutting inside a spaceless run without
        # word segmentation severs words mid-grapheme (C6 2026-09-25:
        # เซ็ต -> เซ็ | ต) — refuse loudly instead of guessing. Remedy:
        # install pythainlp (newmm dictionary), reword the line shorter,
        # or widen the width budget.
        raise NoSafeSplitError(
            f"no word-boundary-safe cut for over-budget phrase {text!r} "
            f"(width {total:.1f}px over budget {max_width_px:.1f}px, "
            f"dictionary={'available' if has_dict else 'MISSING — install pythainlp'})"
        )

    phrase_toks = _word_tokens(text)
    best = min(
        candidates,
        key=lambda i: abs(cum[i - 1] - target)
        - (_PREFER_BONUS_FRAC * total
           if _phrase_preferred(i, phrase_toks) else 0.0),
    )
    left, right = text[:best], text[best:]

    def shift_spans(spans, offset, lo, hi):
        out = []
        for a, b in spans:
            a2, b2 = max(a, lo) - offset, min(b, hi) - offset
            if a2 < b2:
                out.append((a2, b2))
        return out

    left_spans = shift_spans(protected_spans, 0, 0, best)
    right_spans = shift_spans(protected_spans, best, best, len(text))

    left_pieces, left_forced = split_phrase_tracked(left, max_width_px, left_spans)
    right_pieces, right_forced = split_phrase_tracked(right, max_width_px, right_spans)
    return left_pieces + right_pieces, (forced or left_forced or right_forced)


def split_phrase(
    text: str,
    max_width_px: float,
    protected_spans: list[tuple[int, int]] | None = None,
) -> list[str]:
    """Recursively split `text` into pieces that each fit under
    `max_width_px`, cutting only at safe boundaries. protected_spans are
    (start,end) character ranges (e.g. keyword terms) that must not be cut
    through. Loanword spans are always protected automatically."""
    pieces, _ = split_phrase_tracked(text, max_width_px, protected_spans)
    return pieces
