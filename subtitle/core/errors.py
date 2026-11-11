"""Central error taxonomy and exception hierarchy for ThaiSubburn Phase 2 (P2-008).

Architectural rules and invariants:
(a) The quota rule `attempt_counts.get(key, 0) < (1 + retry_allowance)` is enforced
    by retry.py, not here.
(b) No new gate names exist — only G1/G2/G3 + pre-render/render evidence record + QC (C7).
(c) Human checks are always NOT_CHECKED in Phase 2A, automated checks never count
    as human listening.
(d) No /tmp usage, no SQLite, no STT timing as MFA evidence.
(e) Binary-missing rule (C11): qc.py keeps mapping any system_fault to FAILED_JOB;
    FAILED_SYSTEM is used ONLY when the failure is detected outside QC (e.g. an
    orchestrator pre-flight finding ffmpeg/ffprobe missing from PATH, or the same
    failure hitting every clip) — that detection is not wired in this ticket.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

FailureClassification = Literal["RETRYABLE", "NON_RETRYABLE"]
AttemptResult = Literal["SUCCESS", "FAILED", "EXHAUSTED"]


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class SubburnError(Exception):
    """Base exception for all ThaiSubburn Phase 2 errors."""

    def __init__(self, code: str, message: str) -> None:
        if type(code) is not str or not code:
            raise ValueError("AUTH_SCHEMA_INVALID")
        if type(message) is not str:
            raise ValueError("AUTH_SCHEMA_INVALID")
        super().__init__(f"{code}: {message}")
        self.code: str = code
        self.message: str = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class NonRetryableError(SubburnError):
    """Security, authorization, or content faults; never retried."""


class RetryableError(SubburnError):
    """Transient technical faults; retryable under unchanged inputs + quota."""


class SystemFailureError(SubburnError):
    """Infrastructure or environment faults; runner must halt."""


# ---------------------------------------------------------------------------
# Taxonomy table
# ---------------------------------------------------------------------------

_FAILURE_TAXONOMY: dict[str, tuple[str, str | None]] = {
    # NON_RETRYABLE -> CANCELLED (1)
    "AUTH_REVOKED": ("NON_RETRYABLE", "CANCELLED"),
    # NON_RETRYABLE -> BLOCKED_AUTHORIZATION (18)
    "AUTH_EXPIRED_OR_NOT_YET_VALID": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "AUTH_SCHEMA_INVALID": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "AUTH_INVALID_OR_UNTRUSTED": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "AUTH_TIME_INVALID": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "STYLE_NOT_AUTHORIZED": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "ACTION_NOT_AUTHORIZED": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "REVOCATION_INVALID": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "REVOCATION_STALE_OR_MISMATCH": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "GRANT_REPLAY": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "SOURCE_MISMATCH": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "DESTINATION_NOT_AUTHORIZED": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "JOB_STATE_CONFLICT": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "SOURCE_DIGEST_MISMATCH": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "SOURCE_MUTATED_ERROR": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "UNSAFE_SOURCE_SYMLINK": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "UNSAFE_PATH_TRAVERSAL": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "UNSAFE_OUTPUT_ALIAS": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    "SOURCE_NOT_FOUND": ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION"),
    # NON_RETRYABLE -> BLOCKED_NEEDS_REVIEW (7)
    "MFA_EVIDENCE_INVALID": ("NON_RETRYABLE", "BLOCKED_NEEDS_REVIEW"),
    "SCRIPT_NEEDS_EVIDENCE": ("NON_RETRYABLE", "BLOCKED_NEEDS_REVIEW"),
    "RENDER_NOT_PERMITTED": ("NON_RETRYABLE", "BLOCKED_NEEDS_REVIEW"),
    "SCRIPT_EVIDENCE_UNCHECKED": ("NON_RETRYABLE", "BLOCKED_NEEDS_REVIEW"),
    "CONTENT_EVIDENCE_AMBIGUOUS": ("NON_RETRYABLE", "BLOCKED_NEEDS_REVIEW"),
    "CORRECTION_EVIDENCE_REQUIRED": ("NON_RETRYABLE", "BLOCKED_NEEDS_REVIEW"),
    "AMBIGUOUS_SPEECH_DETECTED": ("NON_RETRYABLE", "BLOCKED_NEEDS_REVIEW"),
    # NON_RETRYABLE -> FAILED_JOB (3)
    "STATE_SKIP_FORBIDDEN": ("NON_RETRYABLE", "FAILED_JOB"),
    "INVALID_STATE_REVISION": ("NON_RETRYABLE", "FAILED_JOB"),
    "EXHAUSTED_RETRIES": ("NON_RETRYABLE", "FAILED_JOB"),
    # NON_RETRYABLE -> None (no transition) (3)
    "TERMINAL_STATE_LOCKED": ("NON_RETRYABLE", None),
    "REVOKED_OR_CANCELLED_JOB": ("NON_RETRYABLE", None),
    "IDEMPOTENT_NOOP": ("NON_RETRYABLE", None),
    # NON_RETRYABLE -> RENDER_PERMITTED (1)
    "AUTO_MODE_DISABLED": ("NON_RETRYABLE", "RENDER_PERMITTED"),
    # NON_RETRYABLE -> READY_FOR_REVIEW (1)
    "WARN_LOUDNESS_OUT_OF_TARGET": ("NON_RETRYABLE", "READY_FOR_REVIEW"),
    # NON_RETRYABLE -> FAILED_SYSTEM (2)
    "SYSTEM_DEPENDENCY_MISSING": ("NON_RETRYABLE", "FAILED_SYSTEM"),
    "SYSTEM_OUT_OF_DISK": ("NON_RETRYABLE", "FAILED_SYSTEM"),
    # RETRYABLE -> FAILED_JOB (when quota exhausted) (5)
    "RENDER_EXECUTION_FAILED": ("RETRYABLE", "FAILED_JOB"),
    "STORAGE_READ_TIMEOUT": ("RETRYABLE", "FAILED_JOB"),
    "DOWNLOAD_INTERRUPTED": ("RETRYABLE", "FAILED_JOB"),
    "OUTPUT_STAGING_BUSY": ("RETRYABLE", "FAILED_JOB"),
    "EXTERNAL_SERVICE_503": ("RETRYABLE", "FAILED_JOB"),
}

FAILURE_TAXONOMY: MappingProxyType[str, tuple[str, str | None]] = MappingProxyType(
    _FAILURE_TAXONOMY
)

RETRYABLE_CODES: frozenset[str] = frozenset(
    code
    for code, (classification, _) in FAILURE_TAXONOMY.items()
    if classification == "RETRYABLE"
)

NON_RETRYABLE_CODES: frozenset[str] = frozenset(
    code
    for code, (classification, _) in FAILURE_TAXONOMY.items()
    if classification == "NON_RETRYABLE"
)

SYSTEM_FAILURE_CODES: frozenset[str] = frozenset({
    "SYSTEM_DEPENDENCY_MISSING",
    "SYSTEM_OUT_OF_DISK",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def classify_code(code: str) -> tuple[FailureClassification, str | None]:
    """Return taxonomy entry for code; fail-closed if unknown or invalid."""
    if type(code) is not str or not code or code not in FAILURE_TAXONOMY:
        return ("NON_RETRYABLE", "BLOCKED_NEEDS_REVIEW")
    classification, target_state = FAILURE_TAXONOMY[code]
    if classification == "RETRYABLE":
        return ("RETRYABLE", target_state)
    return ("NON_RETRYABLE", target_state)


def target_state_for(code: str, *, current_state: str | None = None) -> str | None:
    """Return taxonomy target_state; if entry target is None, return current_state."""
    if type(code) is not str or not code or code not in FAILURE_TAXONOMY:
        return "BLOCKED_NEEDS_REVIEW"
    _, target = FAILURE_TAXONOMY[code]
    if target is None:
        return current_state
    return target
