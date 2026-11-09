# Development guardrails

- Read root `SKILL.md` and current code before changing the workflow; distinguish current behavior from future requirements.
- Preserve the real approval gate and hash binding. Never manufacture approval or use STT timing in place of MFA for production.
- Do not overwrite or delete source media; keep private media, credentials and job data out of Git. Use synthetic data for tests.
- Keep the two existing workflow styles separate from the 13 standalone styles; do not claim untested production readiness or change defaults implicitly.
- Run relevant offline tests and state what was skipped, including human visual/audio checks. Do not claim results not observed.
- No push, public PR, merge or deployment without user authorization.
