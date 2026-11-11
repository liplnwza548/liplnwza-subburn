# Platform adapters (P2-009) — offline only

## What an adapter is

An adapter is a thin layer that translates one platform's outside world
(where files live, which programs are available, where a review copy
may go) into uniform answers the orchestrator can use. The adapter has
**no decision authority** over authorization, content, or quality: those
stay with `verify()` / G1 / G2 / G3 / QC.

Two adapters exist in Phase 2A:

| Adapter | Name | Where files live |
|---|---|---|
| `LocalAdapter` | `local` | The VM's staging directory |
| `ColabAdapter` | `colab` | A local folder **simulating** a Drive parent ("fake Drive") |

## The 8 contract rules (§4.1)

**A1 — Identical core decisions.** On equivalent fixtures, VM and
Colab decide identically. Only path values may differ.

**A2 — No automatic fallback.** A missing or unreadable root is an
`AdapterError` immediately. The caller specifies the root explicitly
every time; no default root is ever guessed, created, or invented. A
missing root is never "fixed" by trying another folder or another
Drive parent.

**A3 — Capability checks are read-only observation.** They look at
`PATH` and at the simulated config. They never run ffmpeg/ffprobe/MFA,
never write a file, never touch a network. The result is a
`CapabilityReport` with booleans and missing names only — no secrets.

**A4 — Same-origin delivery, checked before every write.** The
destination's `provider` + `id` + `owner_id` must all match the
authorized review destination in the grant exactly. Mismatch means
deny before even one byte is written. The job must be
`READY_FOR_REVIEW`.

**A5 — Source and output strictly separated.** The trusted source
root and the staging root must not overlap or be parent/child of each
other. The candidate lives only in `job_output_dir`; a delivery copy
goes only to the review destination, never back into staging/source.

**A6 — No secret in logs or reports.** Reports carry capability names
and booleans only. Test keys must be labelled `test-key-...` and are
never real keys.

**A7 — Colab is simulation only.** A "fake Drive" is an ordinary
local folder pretending to be a Drive parent. No Google library is
ever imported, no HTTP call is ever made, nothing is uploaded. Every
Drive-related name in code, docs, and reports says
`fake` / `simulated` / `offline`.

**A8 — Offline label.** Every demo artifact carries `offline: true`
on the receipt and an `OFFLINE-` filename prefix. An unlabeled demo
fails.

## Interfaces

```python
# subtitle/adapters/base.py
class AdapterError(ValueError): ...   # code: ADAPTER_ROOT_MISSING /
                                      # ADAPTER_DESTINATION_MISMATCH /
                                      # ADAPTER_NOT_READY / ADAPTER_ROOT_UNSAFE
@dataclass(frozen=True) class PlatformCapabilities:
    ffmpeg_available: bool
    ffprobe_available: bool
    mfa_tool_available: bool        # informational only, never blocks
    drive_access: str               # 'available' | 'unavailable' | 'not_configured'
    drive_parent_id: str | None
    missing: tuple[str, ...]        # names only, no secrets

@dataclass(frozen=True) class CapabilityReport:
    adapter_name: str               # 'local' | 'colab'
    checked_at: str                 # ISO-8601 UTC, caller-supplied
    capabilities: PlatformCapabilities
    blocked: bool                   # True only when a REQUIRED tool is missing
    blocking_reason: str | None     # 'SYSTEM_DEPENDENCY_MISSING' or None
    notes: str                      # human-readable, no secrets

@dataclass(frozen=True) class DeliveryReceipt:
    job_id, destination_provider, destination_id, destination_owner_id,
    artifact_sha256, artifact_relpath, evidence_id,
    offline: bool, delivered_at: str

class PlatformAdapter(Protocol):
    name: str
    check_capabilities(*, now: str) -> CapabilityReport
    resolve_source(*, source_ref, source_path, trusted_source_root) -> Path
    staging_root(*, base_staging_dir, trusted_source_root=None) -> Path
    deliver_review(*, job_id, artifact_path, destination,
                   expected_destination, target_state, evidence_id,
                   offline=True, now) -> DeliveryReceipt
```

`LocalAdapter(base_staging_dir=...)`: VM staging; `drive_access` is
always `'not_configured'`; delivery writes to
`<base_staging_dir>/delivery/<job_id>/OFFLINE-<filename>` (still inside
staging — no external destination on the VM in Phase 2A).

`ColabAdapter(fake_drive_root=..., fake_drive_parent_id=...,
fake_drive_credential=...)`: fake Drive only. Delivery writes to
`<fake_drive_root>/<fake_drive_parent_id>/OFFLINE-<filename>`.
The credential is a test key (`test-key-...`) or `None`; only its
presence matters. If it is missing, `deliver_review` raises
`AdapterError('ADAPTER_NOT_READY')` — the owner must fix it, and the
orchestrator maps that failure to `BLOCKED_NEEDS_REVIEW` (plan §10,
decision D6).

## How the orchestrator uses adapters

1. **Pre-flight** (before intake): `check_capabilities()`. If a
   required binary (ffmpeg/ffprobe) is missing, the whole run stops as
   `FAILED_SYSTEM` with deny code `SYSTEM_DEPENDENCY_MISSING` — a
   missing binary would hit every clip, not one clip. No job folder is
   created for a pre-flight failure. If the review destination is a
   Drive destination and the simulated Drive credential is missing, the
   run stops as `BLOCKED_NEEDS_REVIEW` (`ADAPTER_NOT_READY`): the
   owner can fix it.
2. **Retry loop** (`_step_auto_mode_render`): one failure key,
   `'render_candidate'`. Quota is `attempt_counts['render_candidate']
   < (1 + retry_allowance)`, checked with the single correct formula
   after `increment_attempt`. Re-verify with `action='render_candidate'
   before every retry; `AUTH_REVOKED` cancels the job, any other deny
   blocks it. Backoff comes from `decide_retry` (2–3 s after attempt
   1, 4–5 s after attempt 2, cap 8 s) and the orchestrator sleeps via
   an injectable `sleep_fn` (default `time.sleep`).
3. **Fresh QC after every successful render**: `run_qc` is called
   fresh (never reuse an old `QCResult`). If QC reports a system
   fault, the orchestrator does **not** transition to `FAILED_JOB`
   yet — `decide_retry` decides first; only when retry is not
   allowed does it transition `RENDERED → QC_EVALUATED → FAILED_JOB`.
4. **Attempt recording** (§5.3, correction C1): the durable ledger is
   `checkpoint.attempt_counts` (atomic save). Each attempt also gets
   one evidence file
   `<job_dir>/evidence/retry_render_candidate_{n:03d}.json`. The
   outcome transition carries refs
   `retry:render_candidate:attempts={n}` (plus `:exhausted` when the
   quota ran out). **No `RetryEvent` is ever written into
   `events.jsonl`**: `append_event` accepts `JobEvent` only, and
   `audit.py` / `state.py` were not changed for this ticket.

## Running the offline demo (1 clip, ColabAdapter)

The demo is offline end-to-end: synthetic media (ffmpeg `testsrc2` +
sine tone), a synthetic grant/status pair, the `ColabAdapter` against a
local folder acting as the fake Drive parent.

```
# from the repo root, with the test venv
~/workspace/.venv-subburn-test/bin/python \
    ~/workspace/subburn-jobpack/phase2-plan/P2-009_offline_demo.py
```

Expected outcome: the job ends at `READY_FOR_REVIEW` and a delivery
receipt is issued with `offline: True` and the delivered file named
`OFFLINE-...` inside the fake Drive parent. The run log is kept at
`~/workspace/subburn-jobpack/phase2-plan/P2-009_offline_demo_log.md`.

## Limitations — what this does NOT claim

- **No real Google Drive or chat integration.** Everything labelled
  Drive is a local folder simulation. Nothing is uploaded anywhere.
- **No Production Auto Mode.** `auto_mode` defaults to `False`;
  the demo and all tests run with an explicit opt-in.
- **No human listening/viewing.** `human_audio_verification` and
  `human_visual_verification` are always `NOT_CHECKED` in Phase 2A.
- **No cross-clip failure detection.** If a fault repeats on every
  clip, that detector is deferred to P2-010; in this ticket a fault
  that exhausts one clip's retries ends as `FAILED_JOB`.
- **Pre-flight is only a first gate.** Passing it does not promise
  the render will succeed; the retry loop handles real failures.
- **One `now` per run.** A grant expiring mid-loop is an accepted
  limitation of this ticket (plan §3.3, assumption 2).

## chip_burn `/tmp` peak (plan §4.5, decision D5)

`chip_burn.py` line 246 hardcodes `tempfile.mkdtemp(prefix='chipstrip-',
dir='/tmp')`; passing `TMPDIR` does not help, because Python ignores
`TMPDIR` when `dir=` is explicit. `/tmp` on this VM is a 512 MB tmpfs.
P2-009 measured the real peak on a synthetic clip of 30 s (the trial
bound is clips ≤ 60 s). Full log:
`~/workspace/subburn-jobpack/phase2-plan/P2-009_tmp_peak_trial.md`.

| Measured value | Result |
|---|---|
| Peak size of the `/tmp/chipstrip-*` folder | **10.67 MB** (187 unique strip frames) |
| Time the folder stayed in /tmp | **269.8 s** of a 283.1 s burn |
| Leftovers after finishing | **Yes — 1 dir, 10.67 MB** (the burner never deletes it) |

**Verdict:** peak 10.67 MB is far below the 100 MB threshold, so the
`/tmp` risk is acceptable with evidence for clips ≤ 60 s. A 60 s clip
would roughly double the strip count (~21 MB), still far below the
512 MB tmpfs. **Open item for P2-010:** every burn leaves its whole
`chipstrip-*` folder in /tmp (no cleanup code in the burner), so
repeated runs accumulate ~10 MB each. A fix (e.g. a wrapper that
removes the temp dir after a successful burn) touches the legacy
burner flow and needs separate approval.
