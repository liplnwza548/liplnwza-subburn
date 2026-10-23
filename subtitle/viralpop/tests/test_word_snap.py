#!/usr/bin/env python3
"""Word-snap tests (2026-09-24): internal sub-cue boundaries must stop
landing mid-word or mid-pause from width-proportional subdivision."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "render"))

from script_split import (  # noqa: E402
    _char_snap_target,
    _snap_bounds_to_words,
    _token_spans,
    _word_char_ranges,
    _word_edges,
    split_matched,
)


def _words():
    # word timeline: word A [1.0, 2.0], word B [2.0, 3.0]
    return [
        {"word": "ก", "start": 1.0, "end": 2.0},
        {"word": "ข", "start": 2.0, "end": 3.0},
    ]


def _char_words():
    # two multi-char words: 'กก' [1.0, 2.0] (chars 0-1), 'ขขข' [2.0, 3.0]
    # (chars 2-4); ranges share the aligner's compacted coordinates.
    return [
        {"word": "กก", "start": 1.0, "end": 2.0},
        {"word": "ขขข", "start": 2.0, "end": 3.0},
    ]


def test_word_edges_collects_starts_and_ends():
    assert _word_edges(_words()) == [1.0, 2.0, 3.0]


def test_token_spans_skips_whitespace():
    ws = [
        {"word": "ก", "start": 1.0, "end": 2.0},
        {"word": " ", "start": 2.0, "end": 2.5},
        {"word": "ข", "start": 2.5, "end": 3.0},
    ]
    assert _token_spans(ws) == [(1.0, 2.0), (2.5, 3.0)]


def test_snap_inside_token_goes_to_nearer_edge_of_same_token():
    # boundary at 2.2 sits inside B [2.0, 3.0] -> nearer edge is 2.0.
    # The word is never split across cues.
    out = _snap_bounds_to_words([1.0, 2.2, 3.0], _words())
    assert out == [1.0, 2.0, 3.0], out


def test_snap_inside_token_near_end():
    # boundary at 2.8 sits inside B [2.0, 3.0] -> nearer edge is 3.0,
    # but 3.0 is the parent end -> snap rejected, proportional kept.
    out = _snap_bounds_to_words([1.0, 2.8, 3.0], _words())
    assert out == [1.0, 2.8, 3.0], out


def test_snap_silence_gap_goes_to_middle():
    # words A [1.0, 1.5], B [1.7, 3.0]; boundary 1.55 sits in the
    # 0.2s gap -> middle 1.6 (both sides share the error).
    ws = [
        {"word": "ก", "start": 1.0, "end": 1.5},
        {"word": "ข", "start": 1.7, "end": 3.0},
    ]
    out = _snap_bounds_to_words([1.0, 1.55, 3.0], ws)
    assert out == [1.0, 1.6, 3.0], out


def test_snap_big_gap_keeps_proportional():
    # words A [1.0, 1.5], B [2.5, 3.0]; gap is 1.0s wide -> guessing
    # the middle trades a small error for a big one -> keep.
    ws = [
        {"word": "ก", "start": 1.0, "end": 1.5},
        {"word": "ข", "start": 2.5, "end": 3.0},
    ]
    out = _snap_bounds_to_words([1.0, 1.6, 3.0], ws)
    assert out == [1.0, 1.6, 3.0], out


def test_snap_refuses_when_neighbour_would_go_short():
    # boundary at 1.2 inside A [1.0, 2.0] -> nearer edge 1.0, but the
    # first cue would collapse to 0.0s -> keep proportional.
    out = _snap_bounds_to_words([1.0, 1.2, 3.0], _words())
    assert out == [1.0, 1.2, 3.0], out


def test_snap_on_edge_is_noop():
    out = _snap_bounds_to_words([1.0, 2.0, 3.0], _words())
    assert out == [1.0, 2.0, 3.0], out


def test_char_ranges_share_aligner_coordinates():
    ranges = _word_char_ranges(_char_words())
    assert ranges == [(0, 2, 1.0, 2.0), (2, 5, 2.0, 3.0)], ranges


def test_char_snap_first_half_goes_to_onset():
    # char 2 is the first char of 'ขขข' -> piece B starts with it.
    ranges = _word_char_ranges(_char_words())
    assert _char_snap_target(2, ranges) == 2.0


def test_char_snap_second_half_goes_to_end():
    # char 4 is the last char of 'ขขข' -> piece A ends with it.
    ranges = _word_char_ranges(_char_words())
    assert _char_snap_target(4, ranges) == 3.0


def test_char_snap_word_never_split():
    # boundary char inside 'ขขข' resolves to one of ITS edges only.
    ranges = _word_char_ranges(_char_words())
    assert _char_snap_target(3, ranges) in (2.0, 3.0)


def test_snap_with_char_bounds_uses_word_identity():
    # parent span [1.0, 3.0], boundary char 2 = onset of 'ขขข' [2.0,3.0].
    # Time-only fallback would look near t=2.2; the char path knows.
    ranges = _word_char_ranges(_char_words())
    out = _snap_bounds_to_words([1.0, 2.2, 3.0], _char_words(),
                                char_bounds=[0, 2, 5], ranges=ranges)
    assert out == [1.0, 2.0, 3.0], out


def test_char_snap_word_end_facing_pause_goes_middle():
    # A 'กก' [1.0, 1.5] (chars 0-1), B 'ขขข' [1.7, 3.0] (chars 2-4).
    # Boundary char 1 = last char of A -> word end 1.5 faces a 0.2s
    # pause -> minimax middle 1.6 (not 1.5, not 1.0).
    ws = [
        {"word": "กก", "start": 1.0, "end": 1.5},
        {"word": "ขขข", "start": 1.7, "end": 3.0},
    ]
    ranges = _word_char_ranges(ws)
    assert _char_snap_target(1, ranges) == 1.6


def test_snap_no_words_is_noop():
    bounds = [1.0, 2.4, 3.0]
    assert _snap_bounds_to_words(bounds, None) == bounds
    assert _snap_bounds_to_words(bounds, []) == bounds


def test_comparative_tee_clause_never_severs():
    import thai_line_split as tls
    toks = tls._word_tokens("จุของได้เยอะกว่าที่คิดอีกนะ")
    # cut between กว่า|ที่ must be forbidden even though ที่ is preferred
    for tok, a, b in toks:
        if tok == "ที่":
            assert tls._phrase_forbidden(a, toks), (toks, a)
            break
    else:
        raise AssertionError(f"expected ที่ token in {toks}")
    # ...while the good cut after เยอะ stays allowed
    for tok, a, b in toks:
        if tok == "กว่า":
            assert not tls._phrase_forbidden(a, toks), (toks, a)
            break


def test_split_matched_without_words_keeps_legacy():
    entry = {
        "line_index": 0, "script_line": "กขคงจฉชซฌญฎฏฐฑฒณดต",
        "text": "กขคงจฉชซฌญฎฏฐฑฒณดต",
        "display_text": "กขคงจฉชซฌญฎฏฐฑฒณดต",
        "start": 1.0, "end": 3.0,
        "source_char_start": 0, "source_char_end": 20,
    }
    legacy = split_matched([dict(entry)])
    snapped = split_matched([dict(entry)], words=_words())
    assert legacy and snapped
    assert all("script_line" in m for m in legacy + snapped)
    for m in legacy + snapped:
        assert m["end"] > m["start"]


def test_norm_numerals_maps_thai_words_to_digits():
    from script_align import _norm_numerals
    assert _norm_numerals("สี่โมงเย็น") == "4โมงเย็น"
    assert _norm_numerals("แสงสวยเลย") == "แสงสวยเลย"


def test_keyword_spans_mark_every_occurrence():
    from script_split import _keyword_spans
    spans = _keyword_spans("จุของได้เยอะกว่าที่คิด",
                           ["จุของได้เยอะ", " ", "ไม่มี"])
    assert (0, 12) in spans
    assert _keyword_spans("abc", [""]) == []
    assert _keyword_spans("abc", None) == []


def test_vo_profile_passes_phrase_cue_general_refuses():
    from script_marks import validate_final_cues
    cues = [{"script_line": "กระเป๋าคู่ใจใบนี้ค่ะ",
             "start": 8.02, "end": 9.37}]
    ok = validate_final_cues([dict(c) for c in cues], profile="vo")
    assert ok["cues"] == 1
    try:
        validate_final_cues([dict(c) for c in cues], profile="general")
    except ValueError:
        pass
    else:
        raise AssertionError("general profile should refuse 7-syl cue")
