# Phase 1: Audit + Foundation — specification

Baseline: `39a2ff685c6e73a5dc2f1918e494bd8263bee2d6`. Phase 1 is documentation, development guardrails and offline tests only. Production renderer, notebook and its approval semantics remain unchanged. No private media, secrets, push, PR or merge.

## Current operation

`SKILL.md` and the Colab notebook require actual Philip approval and `approval.json`, then MFA timing with hash binding before either `paper-chips` or `viralpop` is burned. The 13 standalone styles in `subtitle/styles/burn_styles.py` are not wired into this workflow. Standalone CLI is not itself the notebook approval gate. Do not treat an offline fixture as a real authorization. A visual or audio check requires an actual reviewer; program success alone is not visual or sync approval.

## Decisions for future phases, not current behavior

Philip's Q1–Q15 describe: portable agents with capability checks; shared Linux/Colab core; auto-produce to review without intermediate approval and no publishing permission; at most two retryable retries; 15 evidence-qualified styles; explicit style selection; validated allowed overrides; Drive/chat/local intake and origin delivery by default; automated QC and honest NOT_CHECKED listening status; sequential per-clip isolation with systemic stop; durable MP4/QC/manifest/checkpoint; local VM and Drive Colab resume; safe cleanup only of system-owned temporary files after delivery and resume checks. These are target requirements, not implemented claims. Changing current approval demands a separately designed and tested authorization policy in a later phase. The current default paper-chips behavior remains in effect until then.

## Acceptance

Inventory with evidence and risks, clear current vs target, accurate style status, short AGENTS.md, runnable offline regression tests without live services or media, transparent test results, two-axis review and 3–5 actionable next-phase slices. Human visual review, real Groq/MFA/Drive and end-to-end output are outside offline evidence. Preserve attribution/license when considering the Matt Pocock source pack; workflow terms here describe the approach, not an installed copy of those tools.
