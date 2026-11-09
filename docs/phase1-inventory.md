# Baseline inventory and audit comparison

Evidence: commit `39a2ff685c6e73a5dc2f1918e494bd8263bee2d6` checked against GitHub main on 2026-11-09. The supplied report is a static audit; its reported historic visual inspection is not a new test result.

| Component | Evidence and status |
|---|---|
| Current workflow | `SKILL.md`, `Subtitle-Burn-Colab.ipynb`: approval, MFA timing and QC; notebook has 34 cells (20 code, 14 markdown). Colab paths and manual delivery remain. |
| Existing styles | `subtitle/paper-chips/chip_burn.py`, `subtitle/viralpop/scripts/burn_script_aligned.py`: current notebook paths; per-job approval and QC still required. |
| Standalone 13 | `subtitle/styles/burn_styles.py`: karaoke, editorial, highlighter, reveal, underline, minimal, frosted, dualtone, comic, luxury, terminal, clay, wordpop. No notebook references to this burner. Visual designs have a recorded Philip approval and clip 01 frame inspection in `subtitle/styles/README.md`; that is not proof of generic production readiness. |
| Syllables | `chunk_phrases` rejects unknown words when syllable chunking is used. This limitation is conditional on that path, not a claim that all rendering modes reject unknown words. |
| Tests | `subtitle/viralpop/tests/`; `subtitle/styles/test_styles.py` writes a strip and asks for human visual check. Its docstring says 6 styles but its actual assertion lists 13. No `.github/workflows` at baseline. |

F-01–F-09 match the inspected files, with the conditional syllable nuance above. F-05 README's 27-cell count is stale. F-06 the visual strip is not automatically assessed. F-07 path `/content/motion-kit-portable` and Colab facilities are embedded, so portable workflow is future work. F-10 Drive sharing permissions were not independently checked and were not changed. No whole-project safety or production certification follows from this inventory.

Risk: direct standalone renderer CLI is callable outside notebook's approval gate; direct use does not prove authorization. Current repo instructions forbid bypassing approval. Hash binding is not a cryptographic signature and file access must be protected. Conflicting historical QUICKSTART advice to use live Groq timing is superseded by root SKILL MFA requirement. No fresh validation of visual fidelity, audio sync, real Groq/MFA/Drive or all styles was performed.

## Offline checks

From repository root, with Python and Pillow installed (`python -m pip install -r subtitle/viralpop/requirements.txt -r subtitle/viralpop/requirements-test.txt`):

```sh
python -m unittest discover -s tests -v
cd subtitle/viralpop
python -m unittest discover -s tests -p 'test_script_align.py' -v
python -m unittest discover -s tests -p 'test_script_ass.py' -v
python -m unittest discover -s tests -p 'test_script_gate.py' -v
```

If the home directory is read-only, set `PYTHAINLP_DATA` to a writable data directory. The new tests extract notebook helper functions and also run three burn cells up to their missing-approval stop, using synthetic temporary data; they do not exercise successful full cells or render video. `test_styles.py` writes a tracked image, so do not run it in place without preserving the file; T3 requires human visual inspection. The documented commands do not cover live services, full notebook execution, render integrations, or audio/visual QC.
