# CUE DECISION SPEC (Codex review 2026-09-24, Lip-approved plan).

A cue plan is burnable ONLY when every cue passes ALL hard gates.
Readability floors pass EITHER by satisfying the number OR by carrying
a recorded Lip exception id. Anything else is UNRESOLVABLE: emit the
verdict, burn nothing, ask Lip — never silently pass, never force.

HARD GATES (fail-loud, no exception possible):
  H1 wording-lock — concat of cue texts == approved script EXACTLY
     (char-for-char, in order). Proof: script_lock.verify_coverage().
  H2 width — every cue <= budget_px (926.84). No overflow ever.
  H3 timing-sanity — 0 < start < end; spans inside audio; post-trim
     monotonic (next.start >= prev.end - eps); ASS centisecond-distinct.
  H4 no-mid-word cut — cuts only on word/syllable boundaries, never
     before a combining mark or after a stranded leading vowel.
  H5 position-lock — POS (540,1400), Lip 2026-11-04 (was 1580;
     TikTok safe-zone fix): never move subs to dodge the product.
     No \N (one line per cue).

READABILITY FLOORS (pass by number OR by Lip exception id):
  R1 duration >= 0.6s per cue.
  R2 syllables <= ~6 per cue (count_syllables, loanword/digit/ๆ aware).
  R3 speech rate <= 10 syl/s (refuse); warn > 7 syl/s.
  R4 negation integrity — a keyword's negation particle (ไม่/ไม่ได้/
     ไม่ต้อง/อย่า/ห้าม) must sit in the SAME cue as the keyword.
     (Splitter must not strand them across a cut; colorize's whole-
     phrase yellow is the last line of defence, not the plan.)

VO PROFILE ("vo": full-auto voiceover burning, Lip-updated 2026-10-10):
  same hard gates H1-H5; readability floors become R1 >= 0.2s,
  R2 <= 10 syllables, R3 refuse > 20 syl/s. Rationale (Lip 2026-10-10):
  speech sync is primary — subtitles can appear and disappear quickly
  matching spoken audio. Phrase-complete spoken cues and natural fast-spoken
  segments (0.2s–0.4s) pass automatically without per-cue hand exceptions.
  Select with --profile vo / profile="vo"; default stays "general" (strict)
  for music/showcase clips.

ESTIMATED TIMINGS:
  E1 syllable-proportional subdivision is an ESTIMATE (syllables are
     not equal length), allowed only inside an approved slot, flagged
     is_estimated=True + method on the cue. Word-timestamp boundaries
     win whenever they exist.

UNRESOLVABLE VERDICT (the answer when constraints cannot all hold,
e.g. 7 syllables in a 0.78s slot, or หยิบของ 0.56s with both merges
over budget): report {cue, failed gates, tried options} and STOP.
Proceed only with a Lip exception record {id, reason, approved-by}.
Byte-identical-to-previous-file is NOT proof; wording-correct with
every diff explained is.
