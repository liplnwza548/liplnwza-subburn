# Non-regression summary — Phase 2 offline (P2-010)

Spec section 11, items S-01–S-09, checked one by one against real
evidence. Every item points at the test or command that proves it.
Nothing here is claimed from memory: the numbers come from the logs
in `docs/phase2/evidence/`.

Branch: `phase2/p2-010-adversarial-review` (from `origin/main 650bccf`).
Commit checked: see `docs/phase2/review.md` section 1 (filled at commit time).
Date: 2026-11-11.

| # | Spec 11 item | Verdict | Evidence |
|---|---|---|---|
| S-01 | Legacy approval + tests still intact; unattended mode still OFF by default | PASS | Foundation 6/6 (`run-log-foundation.txt`); `test_authorization.py` 9/9; `auto_mode: bool = False` at `orchestrator.py:1758`; `git diff --stat 650bccf..HEAD -- Subtitle-Burn-Colab.ipynb` is empty (notebook untouched) |
| S-02 | Every wrong grant is denied BEFORE any external processing or render | PASS | Threat-matrix group A, all 20 rows PASS (`threat-matrix.csv`); `run-log-phase2.txt` 189/189 |
| S-03 | No real MFA provenance = never reaches RENDER_PERMITTED; STT timestamps never substitute for MFA | PASS | Threat-matrix group B rows B-01, B-05, B-06, B-12 PASS; `grep -rn groq subtitle/core/gates.py subtitle/core/orchestrator.py subtitle/core/qc.py` = 0 hits |
| S-04 | Changing transcript / source / style / MFA / grant invalidates dependent evidence; skipped states are denied | PASS | B-08 + C-05 + C-06 + C-10 PASS in `threat-matrix.csv` |
| S-05 | READY_FOR_REVIEW needs a QC record measured for real; not listened / not watched = NOT_CHECKED, never PASSED | PASS | B-13 + E-06 + E-07 PASS; `grep -n "passed: bool" subtitle/core/qc.py` = 0 hits; no global PASSED flag in `qc.py` |
| S-06 | At most 2 additional retries; every attempt auditable; revoked / unsafe / ambiguous are never retried | PASS | C-14–C-18 PASS in `threat-matrix.csv`; single quota formula at `retry.py:295` (`attempt_counts.get(key, 0) < (1 + retry_allowance)`) |
| S-07 | Source is never overwritten; nothing is shared / published / exported outside the approved destination | PASS | D-06 + D-07 + D-10 + E-10 PASS in `threat-matrix.csv` |
| S-08 | Synthetic offline tests show reject + happy path; old regressions still pass; NOT_RUN list is complete | PASS | Full suite 189/189 + 6/6 (`run-log-phase2.txt`, `run-log-foundation.txt`); viralpop baseline 132 pass / 0 fail / 10 error (all 10 = missing external fixture files, see review.md section 4); styles script run exit 0 (`run-log-styles.txt`); NOT_RUN NR-01–NR-12 listed in `review.md` section 8 |
| S-09 | Independent two-axis review; no merge of main until Philip approves + evidence is checked | PENDING | **Not yet done.** The independent review (Axis 1: Codex, Axis 2: AGY) is a separate step after this implementation. `review.md` section 13 is unsigned until then. The Draft PR must stay a draft and must not be merged until Philip approves the merge separately. |

## Result

8 of 9 items PASS. S-09 is PENDING (independent review not yet run).
The preliminary GO recommendation in `review.md` is therefore
explicitly conditional on S-09 completing and being signed.

## Notes

- Test totals: phase2 189/189 (182 baseline + 7 new in
  `test_adversarial.py`) + Foundation 6/6 = 195/195. The ticket's
  single command `python -m unittest discover -s tests -v` was NOT
  used alone: re-verified during P2-010, it runs only the 6
  Foundation tests and reports OK while the 182 phase2 tests never
  run (false-pass trap, plan C2). The full suite was run as two
  commands: `discover -s tests/phase2 -p 'test_*.py'` +
  `discover -s tests -p 'test_offline_foundation.py'`.
- The styles script run re-saves the tracked file
  `subtitle/styles/thai_test_strip.png`; the run produced
  byte-identical content (`git diff` empty), so no tracked reference
  asset changed.
- The viralpop baseline measured in P2-010 (132 pass / 0 fail /
  10 error) differs from the P2-002 Codex baseline (118 pass /
  3 fail / 21 error) measured with a different invocation and an
  older environment (fonttools/uharfbuzz were installed after
  P2-002). The 3 fails did not reproduce under this invocation.
  Recorded as P2 observation O-02 in `review.md` for the
  independent reviewers to re-check; it is not a regression found
  by P2-010.
