"""Platform capability adapters for P2-009 (offline only).

An adapter is a thin layer that translates one platform's outside world
(where files live, which programs are available, where a review copy may
go) into uniform answers the orchestrator can use. The adapter has NO
decision authority over authorization, content, or quality: those stay
with verify() / G1 / G2 / G3 / QC.

Rules (plan P2-009 section 4.1, A1-A8):

  A1  Identical core decisions for equivalent fixtures; only path values
      may differ between the VM and Colab adapters.
  A2  No automatic fallback. A missing or unreadable root is an
      AdapterError immediately. The caller specifies the root explicitly
      every time; no default root is ever guessed, created, or invented.
  A3  Capability checks are read-only observation (look at PATH, look at
      the simulated config). They never run ffmpeg/ffprobe/MFA, never
      write a file, never touch a network.
  A4  Same-origin delivery: before every write, the destination's
      provider + id + owner_id must match the authorized review
      destination exactly. Mismatch means deny before even one byte is
      written. The job must be READY_FOR_REVIEW.
  A5  Source and output stay strictly separated; the staging root must
      not overlap the trusted source root.
  A6  No secret in any report: capability names and booleans only.
      Test keys must be labelled ``test-key-...`` and are never real keys.
  A7  Colab is simulation only. A "fake Drive" is an ordinary local
      folder pretending to be a Drive parent. No Google library is ever
      imported, no HTTP call is ever made, nothing is uploaded.
  A8  Every demo artifact is labelled OFFLINE (``offline: True`` on the
      receipt, ``OFFLINE-`` filename prefix).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class AdapterError(ValueError):
    """Adapter-local failure. ``code`` is adapter scope only, e.g.
    ``ADAPTER_ROOT_MISSING`` / ``ADAPTER_DESTINATION_MISMATCH`` /
    ``ADAPTER_NOT_READY`` / ``ADAPTER_ROOT_UNSAFE``.

    These codes are NOT system deny codes: the adapter has no authority
    to decide authorization, so its codes never flow into a grant
    decision. The orchestrator reads the report and decides.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PlatformCapabilities:
    """One read-only observation of what a platform can do right now.

    ``drive_access`` is ``'available'`` | ``'unavailable'`` |
    ``'not_configured'``. ``LocalAdapter`` always reports
    ``'not_configured'``: a VM has no Drive concept, and that is not a
    defect. ``missing`` lists capability names only, never secret values.
    """

    ffmpeg_available: bool
    ffprobe_available: bool
    mfa_tool_available: bool
    drive_access: str
    drive_parent_id: str | None
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityReport:
    """Result of ``check_capabilities``.

    ``blocked`` is True only when something REQUIRED for this job is
    missing (ffmpeg / ffprobe / the MFA tool for the chosen style).
    A missing Drive credential does NOT set ``blocked=True`` here:
    rendering does not need it; the delivery step fails closed with
    AdapterError instead, and the orchestrator maps that to
    BLOCKED_NEEDS_REVIEW (owner-fixable, plan section 10, D6).
    """

    adapter_name: str
    checked_at: str
    capabilities: PlatformCapabilities
    blocked: bool
    blocking_reason: str | None
    notes: str


@dataclass(frozen=True)
class DeliveryReceipt:
    """Proof that a review copy was written, and where.

    All three destination parts must match the authorized review
    destination in the grant. ``artifact_relpath`` is a path relative
    to the destination, never absolute. ``offline`` is always True in
    Phase 2A.
    """

    job_id: str
    destination_provider: str
    destination_id: str
    destination_owner_id: str
    artifact_sha256: str
    artifact_relpath: str
    evidence_id: str
    offline: bool
    delivered_at: str


@runtime_checkable
class PlatformAdapter(Protocol):
    """The contract every platform adapter must satisfy.

    Every method takes time from the caller (``now``) and never reads
    the real clock, the same rule run_qc follows.

    Documented deviations from plan section 5.1 (see docs/adapters.md,
    "Deviations from plan", D-I..D-IV):

    - ``resolve_source`` takes ``source_path``: a source_ref has no
      filename, so the adapter cannot find the file from it alone.
    - ``staging_root`` accepts ``trusted_source_root`` so the adapter
      can enforce the source/output separation (rule A5).
    - ``deliver_review`` takes ``expected_destination`` (the authorized
      review destination from the grant); without it, rule A4 could not
      be checked at all.
    """

    name: str

    def check_capabilities(self, *, now: str) -> CapabilityReport:
        ...

    def resolve_source(
        self, *, source_ref: dict[str, Any], source_path: Path,
        trusted_source_root: Path,
    ) -> Path:
        ...

    def staging_root(
        self, *, base_staging_dir: Path,
        trusted_source_root: Path | None = None,
    ) -> Path:
        ...

    def deliver_review(
        self, *, job_id: str, artifact_path: Path, destination: dict[str, Any],
        expected_destination: dict[str, Any], target_state: str,
        evidence_id: str, offline: bool = True, now: str,
    ) -> DeliveryReceipt:
        ...
