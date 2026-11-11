"""Pure retry-policy engine for ThaiSubburn Phase 2 (P2-008).

Decisions and constraints:
- D1: 1 original + max 2 retries = max 3 attempts per failure key.
- D2: Re-verify grant before every retry.
- D3: Fresh QC after every successful retry render.
- D4: Orchestrator alone orders retries.
- D5: Bounded exponential backoff base 2.0s, cap 8.0s, jitter 1.0s.
- D6: P2-008 ships core library only; orchestrator/QC wiring goes to P2-009.
- C1: The ONLY quota formula is `attempt_counts.get(key, 0) < (1 + retry_allowance)`,
      checked BEFORE increment every time (counts=1 -> retry allowed, counts=2 -> allowed, counts=3 -> exhausted).
- C2: The attempt ledger lives in checkpoint.attempt_counts (checkpoint.json) + RetryEvent
      in events.jsonl via audit.py — NEVER a separate attempt_ledger.json file.
- C3: The _step_auto_mode_render fix (orchestrator.py:815) is DEFERRED to P2-009 per D6-A.
- C7: No new gate names (only G1/G2/G3 + pre-render/render evidence record + QC).
- C13: Function names must be exactly the §5 set: classify_failure, can_retry,
       decide_retry, create_retry_event (+ RetryDecision/RetryEvent).
- C14: Exhausted means attempt_counts[key] >= 1 + retry_allowance (retry_allowance 0-2 from grant, never decreases).

Architectural rules:
(a) This module is PURE — no file I/O, no subprocess, no sleeps; the orchestrator
    (P2-009 wiring) owns persistence via state.increment_attempt + audit.save_checkpoint
    (atomic) + audit.append_event BEFORE starting a retry attempt (Spec 158).
(b) Failure-key naming convention (snake_case <stage>_<action>): intake_staging,
    transcribe_processor, mfa_align, render_candidate, qc_probe — keys are free-form strings,
    NOT validated against a closed set.
(c) No /tmp, no SQLite, no STT timing as MFA evidence, human checks always NOT_CHECKED.
"""

from __future__ import annotations

from dataclasses import dataclass
import random
import re
from types import MappingProxyType

from subtitle.core.errors import (
    AttemptResult,
    FailureClassification,
    SubburnError,
    classify_code,
)
from subtitle.core.state import JobCheckpoint, KNOWN_STATES

# C3: The _step_auto_mode_render fix (orchestrator.py:815) is DEFERRED to P2-009 per D6-A.

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RETRY_ATTEMPTS: int = 2
DEFAULT_BASE_DELAY_SECONDS: float = 2.0
DEFAULT_MAX_DELAY_SECONDS: float = 8.0
DEFAULT_JITTER_SECONDS: float = 1.0

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_EVENT_ID = re.compile(r"^evt_retry_[a-z0-9_]+_\d{3}$")


# ---------------------------------------------------------------------------
# Data Contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryDecision:
    should_retry: bool
    classification: FailureClassification
    failure_key: str
    failure_code: str
    target_state: str
    current_attempt: int
    max_allowance: int
    exhausted: bool
    action: str
    reason: str
    backoff_seconds: float

    def __post_init__(self) -> None:
        if type(self.should_retry) is not bool:
            raise ValueError("AUTH_SCHEMA_INVALID")
        if type(self.exhausted) is not bool:
            raise ValueError("AUTH_SCHEMA_INVALID")
        if self.classification not in ("RETRYABLE", "NON_RETRYABLE"):
            raise ValueError("AUTH_SCHEMA_INVALID")
        for val in (
            self.failure_key,
            self.failure_code,
            self.target_state,
            self.action,
            self.reason,
        ):
            if type(val) is not str or not val:
                raise ValueError("AUTH_SCHEMA_INVALID")
        if type(self.current_attempt) is not int or self.current_attempt < 0:
            raise ValueError("AUTH_SCHEMA_INVALID")
        if type(self.max_allowance) is not int or not (0 <= self.max_allowance <= 2):
            raise ValueError("AUTH_SCHEMA_INVALID")
        if type(self.backoff_seconds) not in (int, float) or not (self.backoff_seconds >= 0):
            raise ValueError("AUTH_SCHEMA_INVALID")


@dataclass(frozen=True)
class RetryEvent:
    schema_version: str
    event_id: str
    job_id: str
    failure_code: str
    failure_scope: str
    classification: FailureClassification
    attempt_count: int
    max_allowance: int
    exact_action: str
    before_artifact_digests: MappingProxyType
    after_artifact_digests: MappingProxyType
    result: AttemptResult
    timestamp: str
    reason: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != "1":
            raise ValueError("AUTH_SCHEMA_INVALID")
        if type(self.event_id) is not str or not _EVENT_ID.match(self.event_id):
            raise ValueError("AUTH_SCHEMA_INVALID")
        for val in (
            self.job_id,
            self.failure_code,
            self.failure_scope,
            self.exact_action,
            self.reason,
        ):
            if type(val) is not str or not val:
                raise ValueError("AUTH_SCHEMA_INVALID")
        if self.classification not in ("RETRYABLE", "NON_RETRYABLE"):
            raise ValueError("AUTH_SCHEMA_INVALID")
        if type(self.attempt_count) is not int or self.attempt_count < 1:
            raise ValueError("AUTH_SCHEMA_INVALID")
        if type(self.max_allowance) is not int or not (0 <= self.max_allowance <= 2):
            raise ValueError("AUTH_SCHEMA_INVALID")
        if self.result not in ("SUCCESS", "FAILED", "EXHAUSTED"):
            raise ValueError("AUTH_SCHEMA_INVALID")
        if type(self.timestamp) is not str or not _TIMESTAMP.match(self.timestamp):
            raise ValueError("AUTH_SCHEMA_INVALID")
        for mapping in (self.before_artifact_digests, self.after_artifact_digests):
            if type(mapping) is not MappingProxyType:
                raise ValueError("AUTH_SCHEMA_INVALID")
            for k, v in mapping.items():
                if type(k) is not str or not k:
                    raise ValueError("AUTH_SCHEMA_INVALID")
                if type(v) is not str or not _DIGEST.match(v):
                    raise ValueError("AUTH_SCHEMA_INVALID")


def create_retry_event(
    *,
    job_id: str,
    failure_key: str,
    failure_code: str,
    classification: FailureClassification,
    attempt_count: int,
    max_allowance: int,
    exact_action: str,
    before_artifact_digests: dict,
    after_artifact_digests: dict,
    result: AttemptResult,
    now_iso: str,
    reason: str,
) -> RetryEvent:
    """Construct and validate a frozen RetryEvent.

    The event ID format is evt_retry_{failure_key}_{attempt_count:03d}.
    This function is pure and does NOT write to disk (persistence is the
    orchestrator's responsibility via audit.append_event).
    """
    if type(job_id) is not str or not job_id:
        raise ValueError("AUTH_SCHEMA_INVALID")
    if type(failure_key) is not str or not failure_key:
        raise ValueError("AUTH_SCHEMA_INVALID")
    if type(failure_code) is not str or not failure_code:
        raise ValueError("AUTH_SCHEMA_INVALID")
    if classification not in ("RETRYABLE", "NON_RETRYABLE"):
        raise ValueError("AUTH_SCHEMA_INVALID")
    if type(attempt_count) is not int or attempt_count < 1 or attempt_count > 999:
        raise ValueError("AUTH_SCHEMA_INVALID")
    if type(max_allowance) is not int or not (0 <= max_allowance <= 2):
        raise ValueError("AUTH_SCHEMA_INVALID")
    if type(exact_action) is not str or not exact_action:
        raise ValueError("AUTH_SCHEMA_INVALID")
    if type(before_artifact_digests) not in (dict, MappingProxyType):
        raise ValueError("AUTH_SCHEMA_INVALID")
    if type(after_artifact_digests) not in (dict, MappingProxyType):
        raise ValueError("AUTH_SCHEMA_INVALID")
    for mapping in (before_artifact_digests, after_artifact_digests):
        for k, v in mapping.items():
            if type(k) is not str or not k:
                raise ValueError("AUTH_SCHEMA_INVALID")
            if type(v) is not str or not _DIGEST.match(v):
                raise ValueError("AUTH_SCHEMA_INVALID")
    if result not in ("SUCCESS", "FAILED", "EXHAUSTED"):
        raise ValueError("AUTH_SCHEMA_INVALID")
    if type(now_iso) is not str or not _TIMESTAMP.match(now_iso):
        raise ValueError("AUTH_SCHEMA_INVALID")
    if type(reason) is not str or not reason:
        raise ValueError("AUTH_SCHEMA_INVALID")

    event_id = f"evt_retry_{failure_key}_{attempt_count:03d}"
    if not _EVENT_ID.match(event_id):
        raise ValueError("AUTH_SCHEMA_INVALID")

    frozen_before = MappingProxyType(dict(before_artifact_digests))
    frozen_after = MappingProxyType(dict(after_artifact_digests))

    return RetryEvent(
        schema_version="1",
        event_id=event_id,
        job_id=job_id,
        failure_code=failure_code,
        failure_scope=failure_key,
        classification=classification,
        attempt_count=attempt_count,
        max_allowance=max_allowance,
        exact_action=exact_action,
        before_artifact_digests=frozen_before,
        after_artifact_digests=frozen_after,
        result=result,
        timestamp=now_iso,
        reason=reason,
    )


def classify_failure(
    *,
    deny_code: str | None = None,
    target_state: str | None = None,
    exception: Exception | None = None,
) -> tuple[FailureClassification, str, str]:
    """Classify a failure into retryable/non-retryable with canonical failure code.

    Precedence: deny_code first, then exception, then target_state.
    Never raises on bad input — always returns a fail-closed tuple.
    """
    try:
        if deny_code is not None:
            if isinstance(deny_code, str) and deny_code:
                classification, target = classify_code(deny_code)
                resolved_target = "BLOCKED_NEEDS_REVIEW" if target is None else target
                return (classification, resolved_target, deny_code)
            return ("NON_RETRYABLE", "BLOCKED_NEEDS_REVIEW", "UNKNOWN_FAILURE")

        if exception is not None:
            if isinstance(exception, SubburnError):
                code = getattr(exception, "code", None)
                if isinstance(code, str) and code:
                    classification, target = classify_code(code)
                    resolved_target = "BLOCKED_NEEDS_REVIEW" if target is None else target
                    return (classification, resolved_target, code)
                return ("NON_RETRYABLE", "BLOCKED_NEEDS_REVIEW", "UNKNOWN_FAILURE")
            if isinstance(exception, TimeoutError):
                return ("RETRYABLE", "FAILED_JOB", "STORAGE_READ_TIMEOUT")
            if isinstance(exception, ConnectionError):
                return ("RETRYABLE", "FAILED_JOB", "DOWNLOAD_INTERRUPTED")
            if isinstance(exception, FileNotFoundError):
                return ("NON_RETRYABLE", "BLOCKED_AUTHORIZATION", "SOURCE_NOT_FOUND")
            return ("NON_RETRYABLE", "BLOCKED_NEEDS_REVIEW", "UNKNOWN_EXCEPTION")

        if isinstance(target_state, str):
            if target_state == "READY_FOR_REVIEW":
                return ("NON_RETRYABLE", "READY_FOR_REVIEW", "NO_FAILURE")
            if target_state in KNOWN_STATES:
                return ("NON_RETRYABLE", target_state, "UNKNOWN_FAILURE")

        return ("NON_RETRYABLE", "BLOCKED_NEEDS_REVIEW", "UNKNOWN_FAILURE")
    except Exception:
        return ("NON_RETRYABLE", "BLOCKED_NEEDS_REVIEW", "UNKNOWN_FAILURE")


def can_retry(
    *,
    checkpoint: JobCheckpoint,
    failure_key: str,
    retry_allowance: int,
) -> bool:
    """Check whether a retry attempt is permitted under quota.

    counts record COMPLETED attempts (original attempt counts as 1), so with allowance 2: counts 0/1/2 -> True, counts 3 -> False.
    """
    if not isinstance(checkpoint, JobCheckpoint):
        raise ValueError("AUTH_SCHEMA_INVALID")
    if type(failure_key) is not str or not failure_key:
        raise ValueError("AUTH_SCHEMA_INVALID")
    if type(retry_allowance) is not int or not (0 <= retry_allowance <= 2):
        raise ValueError("AUTH_SCHEMA_INVALID")

    return checkpoint.attempt_counts.get(failure_key, 0) < (1 + retry_allowance)


def decide_retry(
    *,
    checkpoint: JobCheckpoint,
    failure_key: str,
    deny_code: str | None = None,
    target_state: str | None = None,
    exception: Exception | None = None,
    retry_allowance: int = 2,
    base_delay_seconds: float = 2.0,
    max_delay_seconds: float = 8.0,
    jitter_seconds: float = 1.0,
) -> RetryDecision:
    """Evaluate failure and checkpoint to produce a pure retry decision.

    Pure function: no file I/O, no subprocess, no sleep.
    """
    if not isinstance(checkpoint, JobCheckpoint):
        raise ValueError("AUTH_SCHEMA_INVALID")
    if type(failure_key) is not str or not failure_key:
        raise ValueError("AUTH_SCHEMA_INVALID")
    if type(retry_allowance) is not int or not (0 <= retry_allowance <= 2):
        raise ValueError("AUTH_SCHEMA_INVALID")
    if (
        type(base_delay_seconds) not in (int, float)
        or type(base_delay_seconds) is bool
        or not (base_delay_seconds > 0)
    ):
        raise ValueError("AUTH_SCHEMA_INVALID")
    if (
        type(max_delay_seconds) not in (int, float)
        or type(max_delay_seconds) is bool
        or not (max_delay_seconds >= base_delay_seconds)
    ):
        raise ValueError("AUTH_SCHEMA_INVALID")
    if (
        type(jitter_seconds) not in (int, float)
        or type(jitter_seconds) is bool
        or not (jitter_seconds >= 0)
    ):
        raise ValueError("AUTH_SCHEMA_INVALID")

    classification, resolved_target, canonical = classify_failure(
        deny_code=deny_code,
        target_state=target_state,
        exception=exception,
    )
    current_attempt = checkpoint.attempt_counts.get(failure_key, 0)

    if classification == "NON_RETRYABLE":
        return RetryDecision(
            should_retry=False,
            classification="NON_RETRYABLE",
            failure_key=failure_key,
            failure_code=canonical,
            target_state=resolved_target,
            current_attempt=current_attempt,
            max_allowance=retry_allowance,
            exhausted=False,
            action="no_retry",
            reason=f"Failure {canonical} is non-retryable; target state {resolved_target}",
            backoff_seconds=0.0,
        )

    if not can_retry(
        checkpoint=checkpoint,
        failure_key=failure_key,
        retry_allowance=retry_allowance,
    ):
        return RetryDecision(
            should_retry=False,
            classification="RETRYABLE",
            failure_key=failure_key,
            failure_code=canonical,
            target_state="FAILED_JOB",
            current_attempt=current_attempt,
            max_allowance=retry_allowance,
            exhausted=True,
            action="exhausted",
            reason=(
                f"Retry quota exhausted for {failure_key} with {canonical}: "
                f"attempt counts {current_attempt} reached limit (1 + {retry_allowance})"
            ),
            backoff_seconds=0.0,
        )

    retry_index = max(0, current_attempt - 1)
    backoff = min(max_delay_seconds, base_delay_seconds * (2 ** retry_index)) + random.uniform(
        0.0, jitter_seconds
    )

    return RetryDecision(
        should_retry=True,
        classification="RETRYABLE",
        failure_key=failure_key,
        failure_code=canonical,
        target_state=checkpoint.current_state,
        current_attempt=current_attempt,
        max_allowance=retry_allowance,
        exhausted=False,
        action=f"retry:{failure_key}",
        reason=(
            f"Permitting retry for {canonical} on {failure_key}: "
            f"current_attempt {current_attempt} within max_allowance {retry_allowance}"
        ),
        backoff_seconds=backoff,
    )

