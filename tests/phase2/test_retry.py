"""Offline tests for P2-008 (Bounded Retry and Failure Taxonomy).

Baseline note:
- Baseline before this ticket: 133 phase2 + 6 foundation = 139 tests before this ticket.
- This test module adds 26 tests; total suite becomes 159 phase2 + 6 foundation = 165 tests.
- All tests in this module use synthetic data fixtures exclusively (no real media,
  no private data, no network calls, and no secret keys).
- tempfile.TemporaryDirectory is allowed in TESTS ONLY for checkpoint/event-log
  durability tests (production code never writes to /tmp).
- Invariants: Automated checks never count as human listening; loudness warning
  is never a retry trigger.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
from types import MappingProxyType
import unittest

from subtitle.core.audit import (
    append_event,
    load_checkpoint,
    load_event_log,
    save_checkpoint,
)
from subtitle.core.errors import (
    FailureClassification,
    SystemFailureError,
    classify_code,
)
from subtitle.core.retry import (
    RetryDecision,
    RetryEvent,
    can_retry,
    classify_failure,
    create_retry_event,
    decide_retry,
)
from subtitle.core.state import JobCheckpoint, JobEvent, increment_attempt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOW: str = "2026-11-10T12:00:00Z"
KEY: str = "render_candidate"


# ---------------------------------------------------------------------------
# Test Fixture Helpers
# ---------------------------------------------------------------------------

def make_checkpoint(
    counts: dict | None = None,
    *,
    state: str = "RENDER_PERMITTED",
) -> JobCheckpoint:
    """Construct a frozen JobCheckpoint fixture for retry policy tests."""
    return JobCheckpoint(
        schema_version="1",
        job_id="job-1",
        grant_id="grant-1",
        current_state=state,
        state_revision=2,
        last_event_id="evt-1",
        attempt_counts=MappingProxyType(dict(counts or {})),
        updated_at=NOW,
        blocked_from_state=None,
    )


def fresh_decision(**kwargs) -> RetryDecision:
    """Thin wrapper calling decide_retry with default test parameters."""
    params = {
        "checkpoint": make_checkpoint(),
        "failure_key": KEY,
        "retry_allowance": 2,
    }
    params.update(kwargs)
    return decide_retry(**params)


# ---------------------------------------------------------------------------
# Category 1: Happy Path (3 tests)
# ---------------------------------------------------------------------------


class TestRetryHappyPath(unittest.TestCase):
    """Category 1: Happy path retry decisions under quota."""

    def test_single_retry_then_success_path(self) -> None:
        """C1-01: First retry under quota is permitted with exponential backoff."""
        checkpoint = make_checkpoint(counts={KEY: 1})
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key=KEY,
            deny_code="RENDER_EXECUTION_FAILED",
            retry_allowance=2,
        )
        self.assertTrue(decision.should_retry)
        self.assertFalse(decision.exhausted)
        self.assertEqual(decision.classification, "RETRYABLE")
        self.assertEqual(decision.target_state, "RENDER_PERMITTED")
        self.assertEqual(decision.action, f"retry:{KEY}")
        self.assertEqual(decision.failure_code, "RENDER_EXECUTION_FAILED")
        self.assertEqual(decision.current_attempt, 1)
        self.assertGreaterEqual(decision.backoff_seconds, 2.0)
        self.assertLessEqual(decision.backoff_seconds, 3.0)

    def test_second_retry_then_success_path(self) -> None:
        """C1-02: Second retry under quota is permitted with doubled base backoff."""
        checkpoint = make_checkpoint(counts={KEY: 2})
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key=KEY,
            deny_code="RENDER_EXECUTION_FAILED",
            retry_allowance=2,
        )
        self.assertTrue(decision.should_retry)
        self.assertFalse(decision.exhausted)
        self.assertEqual(decision.classification, "RETRYABLE")
        self.assertEqual(decision.target_state, "RENDER_PERMITTED")
        self.assertEqual(decision.action, f"retry:{KEY}")
        self.assertEqual(decision.failure_code, "RENDER_EXECUTION_FAILED")
        self.assertEqual(decision.current_attempt, 2)
        self.assertGreaterEqual(decision.backoff_seconds, 4.0)
        self.assertLessEqual(decision.backoff_seconds, 5.0)

    def test_backoff_exact_with_zero_jitter(self) -> None:
        """C1-03: Backoff calculation is deterministic when jitter is zero and bounded by cap+jitter."""
        cp1 = make_checkpoint(counts={KEY: 1})
        d1 = decide_retry(
            checkpoint=cp1,
            failure_key=KEY,
            deny_code="RENDER_EXECUTION_FAILED",
            jitter_seconds=0.0,
        )
        self.assertEqual(d1.backoff_seconds, 2.0)

        cp2 = make_checkpoint(counts={KEY: 2})
        d2 = decide_retry(
            checkpoint=cp2,
            failure_key=KEY,
            deny_code="RENDER_EXECUTION_FAILED",
            jitter_seconds=0.0,
        )
        self.assertEqual(d2.backoff_seconds, 4.0)

        for c in (0, 1, 2):
            cp = make_checkpoint(counts={KEY: c})
            for _ in range(10):
                d = decide_retry(
                    checkpoint=cp,
                    failure_key=KEY,
                    deny_code="RENDER_EXECUTION_FAILED",
                )
                self.assertLessEqual(d.backoff_seconds, 9.0)
                self.assertGreaterEqual(d.backoff_seconds, 2.0)


# ---------------------------------------------------------------------------
# Category 2: Deny / Exhausted (3 tests)
# ---------------------------------------------------------------------------


class TestRetryDenyPath(unittest.TestCase):
    """Category 2: Deny, exhausted, and fail-closed retry decisions."""

    def test_exhausted_retries_go_to_failed_job(self) -> None:
        """C2-01: Exhausted retries transition target to FAILED_JOB with action exhausted."""
        checkpoint = make_checkpoint(counts={KEY: 3})
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key=KEY,
            deny_code="RENDER_EXECUTION_FAILED",
            retry_allowance=2,
        )
        self.assertFalse(decision.should_retry)
        self.assertTrue(decision.exhausted)
        self.assertEqual(decision.classification, "RETRYABLE")
        self.assertEqual(decision.target_state, "FAILED_JOB")
        self.assertEqual(decision.action, "exhausted")
        self.assertEqual(decision.backoff_seconds, 0.0)

    def test_unknown_deny_code_fails_closed(self) -> None:
        """C2-02: Unknown deny code fails closed to BLOCKED_NEEDS_REVIEW as non-retryable."""
        checkpoint = make_checkpoint()
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key=KEY,
            deny_code="SOMETHING_MADE_UP",
            retry_allowance=2,
        )
        self.assertFalse(decision.should_retry)
        self.assertFalse(decision.exhausted)
        self.assertEqual(decision.classification, "NON_RETRYABLE")
        self.assertEqual(decision.target_state, "BLOCKED_NEEDS_REVIEW")
        self.assertEqual(decision.action, "no_retry")
        self.assertEqual(decision.backoff_seconds, 0.0)

    def test_no_inputs_fails_closed(self) -> None:
        """C2-03: No failure inputs provided fails closed to BLOCKED_NEEDS_REVIEW."""
        checkpoint = make_checkpoint()
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key=KEY,
            retry_allowance=2,
        )
        self.assertFalse(decision.should_retry)
        self.assertFalse(decision.exhausted)
        self.assertEqual(decision.classification, "NON_RETRYABLE")
        self.assertEqual(decision.target_state, "BLOCKED_NEEDS_REVIEW")
        self.assertEqual(decision.action, "no_retry")
        self.assertEqual(decision.backoff_seconds, 0.0)


# ---------------------------------------------------------------------------
# Category 3: Non-Retryable (5 tests)
# ---------------------------------------------------------------------------


class TestRetryNonRetryable(unittest.TestCase):
    """Category 3: Non-retryable security, authorization, and content faults."""

    def test_revoked_grant_goes_to_cancelled(self) -> None:
        """C3-01: Revoked grant transitions to CANCELLED and is non-retryable."""
        checkpoint = make_checkpoint(counts={})
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key=KEY,
            deny_code="AUTH_REVOKED",
            retry_allowance=2,
        )
        self.assertFalse(decision.should_retry)
        self.assertEqual(decision.target_state, "CANCELLED")
        self.assertFalse(decision.exhausted)
        self.assertEqual(decision.classification, "NON_RETRYABLE")
        self.assertEqual(decision.action, "no_retry")
        self.assertEqual(decision.backoff_seconds, 0.0)

    def test_expired_grant_blocked_authorization(self) -> None:
        """C3-02: Expired grant transitions to BLOCKED_AUTHORIZATION."""
        checkpoint = make_checkpoint()
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key=KEY,
            deny_code="AUTH_EXPIRED_OR_NOT_YET_VALID",
            retry_allowance=2,
        )
        self.assertFalse(decision.should_retry)
        self.assertEqual(decision.target_state, "BLOCKED_AUTHORIZATION")
        self.assertFalse(decision.exhausted)
        self.assertEqual(decision.classification, "NON_RETRYABLE")
        self.assertEqual(decision.action, "no_retry")
        self.assertEqual(decision.backoff_seconds, 0.0)

    def test_hash_mismatch_blocked_authorization(self) -> None:
        """C3-03: Source hash mismatch transitions to BLOCKED_AUTHORIZATION."""
        checkpoint = make_checkpoint()
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key=KEY,
            deny_code="SOURCE_DIGEST_MISMATCH",
            retry_allowance=2,
        )
        self.assertFalse(decision.should_retry)
        self.assertEqual(decision.target_state, "BLOCKED_AUTHORIZATION")
        self.assertFalse(decision.exhausted)
        self.assertEqual(decision.classification, "NON_RETRYABLE")
        self.assertEqual(decision.action, "no_retry")
        self.assertEqual(decision.backoff_seconds, 0.0)

    def test_missing_mfa_and_ambiguous_speech_blocked_needs_review(self) -> None:
        """C3-04: Missing MFA evidence and ambiguous speech transition to BLOCKED_NEEDS_REVIEW."""
        checkpoint = make_checkpoint()
        d_mfa = decide_retry(
            checkpoint=checkpoint,
            failure_key=KEY,
            deny_code="MFA_EVIDENCE_INVALID",
            retry_allowance=2,
        )
        self.assertFalse(d_mfa.should_retry)
        self.assertEqual(d_mfa.target_state, "BLOCKED_NEEDS_REVIEW")
        self.assertFalse(d_mfa.exhausted)
        self.assertEqual(d_mfa.classification, "NON_RETRYABLE")
        self.assertEqual(d_mfa.action, "no_retry")
        self.assertEqual(d_mfa.backoff_seconds, 0.0)

        d_speech = decide_retry(
            checkpoint=checkpoint,
            failure_key=KEY,
            deny_code="AMBIGUOUS_SPEECH_DETECTED",
            retry_allowance=2,
        )
        self.assertFalse(d_speech.should_retry)
        self.assertEqual(d_speech.target_state, "BLOCKED_NEEDS_REVIEW")
        self.assertFalse(d_speech.exhausted)
        self.assertEqual(d_speech.classification, "NON_RETRYABLE")
        self.assertEqual(d_speech.action, "no_retry")
        self.assertEqual(d_speech.backoff_seconds, 0.0)

    def test_loudness_warning_never_triggers_retry(self) -> None:
        """C3-05: Loudness warning transitions to READY_FOR_REVIEW and never triggers retry."""
        checkpoint = make_checkpoint()
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key=KEY,
            deny_code="WARN_LOUDNESS_OUT_OF_TARGET",
            retry_allowance=2,
        )
        self.assertFalse(decision.should_retry)
        self.assertEqual(decision.target_state, "READY_FOR_REVIEW")
        self.assertEqual(decision.backoff_seconds, 0.0)
        self.assertFalse(decision.exhausted)
        self.assertEqual(decision.classification, "NON_RETRYABLE")
        self.assertEqual(decision.action, "no_retry")


# ---------------------------------------------------------------------------
# Category 4: Repeated Failures (3 tests)
# ---------------------------------------------------------------------------


class TestRetryRepeatedFailures(unittest.TestCase):
    """Category 4: Repeated failures and per-failure-key quota enforcement."""

    def test_same_key_exhausted_after_three_attempts(self) -> None:
        """C4-01: Same key is exhausted after three completed attempts under retry_allowance=2."""
        cp1 = make_checkpoint(counts={"render_candidate": 1})
        d1 = decide_retry(
            checkpoint=cp1,
            failure_key="render_candidate",
            deny_code="RENDER_EXECUTION_FAILED",
            retry_allowance=2,
        )
        self.assertTrue(d1.should_retry)
        self.assertFalse(d1.exhausted)

        cp2 = make_checkpoint(counts={"render_candidate": 2})
        d2 = decide_retry(
            checkpoint=cp2,
            failure_key="render_candidate",
            deny_code="RENDER_EXECUTION_FAILED",
            retry_allowance=2,
        )
        self.assertTrue(d2.should_retry)
        self.assertFalse(d2.exhausted)

        cp3 = make_checkpoint(counts={"render_candidate": 3})
        d3 = decide_retry(
            checkpoint=cp3,
            failure_key="render_candidate",
            deny_code="RENDER_EXECUTION_FAILED",
            retry_allowance=2,
        )
        self.assertFalse(d3.should_retry)
        self.assertTrue(d3.exhausted)
        self.assertEqual(d3.target_state, "FAILED_JOB")
        self.assertEqual(d3.action, "exhausted")

    def test_quota_is_per_failure_key(self) -> None:
        """C4-02: Quota is tracked independently per failure key."""
        checkpoint = make_checkpoint(counts={"render_candidate": 3, "qc_probe": 0})
        d_render = decide_retry(
            checkpoint=checkpoint,
            failure_key="render_candidate",
            deny_code="RENDER_EXECUTION_FAILED",
            retry_allowance=2,
        )
        self.assertFalse(d_render.should_retry)
        self.assertTrue(d_render.exhausted)
        self.assertEqual(d_render.target_state, "FAILED_JOB")

        d_qc = decide_retry(
            checkpoint=checkpoint,
            failure_key="qc_probe",
            deny_code="RENDER_EXECUTION_FAILED",
            retry_allowance=2,
        )
        self.assertTrue(d_qc.should_retry)
        self.assertFalse(d_qc.exhausted)

    def test_zero_and_one_allowance_boundaries(self) -> None:
        """C4-03: Boundary checks for retry_allowance 0 and 1."""
        cp1 = make_checkpoint(counts={KEY: 1})
        d_zero = decide_retry(
            checkpoint=cp1,
            failure_key=KEY,
            deny_code="RENDER_EXECUTION_FAILED",
            retry_allowance=0,
        )
        self.assertFalse(d_zero.should_retry)
        self.assertTrue(d_zero.exhausted)
        self.assertEqual(d_zero.target_state, "FAILED_JOB")

        d_one_ok = decide_retry(
            checkpoint=cp1,
            failure_key=KEY,
            deny_code="RENDER_EXECUTION_FAILED",
            retry_allowance=1,
        )
        self.assertTrue(d_one_ok.should_retry)
        self.assertFalse(d_one_ok.exhausted)

        cp2 = make_checkpoint(counts={KEY: 2})
        d_one_exhausted = decide_retry(
            checkpoint=cp2,
            failure_key=KEY,
            deny_code="RENDER_EXECUTION_FAILED",
            retry_allowance=1,
        )
        self.assertFalse(d_one_exhausted.should_retry)
        self.assertTrue(d_one_exhausted.exhausted)
        self.assertEqual(d_one_exhausted.target_state, "FAILED_JOB")


# ---------------------------------------------------------------------------
# Category 5: Fault Injection Per Stage (4 tests)
# ---------------------------------------------------------------------------


class TestRetryFaultInjection(unittest.TestCase):
    """Category 5: Transient fault injection per stage under retry quota."""

    def test_render_failure_is_retryable(self) -> None:
        """C5-01: Render candidate execution failure is retryable under quota."""
        checkpoint = make_checkpoint(counts={"render_candidate": 1})
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key="render_candidate",
            deny_code="RENDER_EXECUTION_FAILED",
            retry_allowance=2,
        )
        self.assertTrue(decision.should_retry)
        self.assertEqual(decision.action, "retry:render_candidate")
        self.assertGreaterEqual(decision.backoff_seconds, 2.0)
        self.assertLessEqual(decision.backoff_seconds, 3.0)

    def test_intake_staging_failure_is_retryable(self) -> None:
        """C5-02: Intake staging storage read timeout is retryable under quota."""
        checkpoint = make_checkpoint(counts={"intake_staging": 1})
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key="intake_staging",
            deny_code="STORAGE_READ_TIMEOUT",
            retry_allowance=2,
        )
        self.assertTrue(decision.should_retry)
        self.assertEqual(decision.action, "retry:intake_staging")

    def test_transcribe_external_service_failure_is_retryable(self) -> None:
        """C5-03: Transcribe processor external service 503 is retryable with doubled backoff."""
        checkpoint = make_checkpoint(counts={"transcribe_processor": 2})
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key="transcribe_processor",
            deny_code="EXTERNAL_SERVICE_503",
            retry_allowance=2,
        )
        self.assertTrue(decision.should_retry)
        self.assertGreaterEqual(decision.backoff_seconds, 4.0)
        self.assertLessEqual(decision.backoff_seconds, 5.0)

    def test_mfa_timeout_exception_is_retryable(self) -> None:
        """C5-04: MFA alignment TimeoutError maps to STORAGE_READ_TIMEOUT and is retryable."""
        checkpoint = make_checkpoint(counts={"mfa_align": 1})
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key="mfa_align",
            exception=TimeoutError("mfa align timed out"),
            retry_allowance=2,
        )
        self.assertTrue(decision.should_retry)
        self.assertEqual(decision.failure_code, "STORAGE_READ_TIMEOUT")


# ---------------------------------------------------------------------------
# Category 6: Restart Durability (3 tests)
# ---------------------------------------------------------------------------


class TestRetryRestartDurability(unittest.TestCase):
    """Category 6: Attempt counts and retry audit durability across restarts."""

    def test_attempt_counts_survive_checkpoint_round_trip(self) -> None:
        """C6-01: Checkpoint attempt counts survive disk serialization and round-trip."""
        with tempfile.TemporaryDirectory() as d:
            ckpt_path = Path(d) / "checkpoint.json"
            cp = make_checkpoint(counts={"render_candidate": 2})
            save_checkpoint(ckpt_path, cp)
            loaded = load_checkpoint(ckpt_path)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.attempt_counts, {"render_candidate": 2})
            decision = decide_retry(
                checkpoint=loaded,
                failure_key="render_candidate",
                deny_code="RENDER_EXECUTION_FAILED",
                retry_allowance=2,
            )
            self.assertTrue(decision.should_retry)

    def test_attempt_counts_survive_restart_simulation(self) -> None:
        """C6-02: Attempt counts survive simulated process restart without count reset."""
        with tempfile.TemporaryDirectory() as d:
            ckpt_path = Path(d) / "checkpoint.json"
            cp = make_checkpoint(counts={"render_candidate": 2})
            save_checkpoint(ckpt_path, cp)

            loaded_cp = load_checkpoint(ckpt_path)
            self.assertIsNotNone(loaded_cp)

            updated_cp = increment_attempt(loaded_cp, "render_candidate")
            self.assertEqual(updated_cp.attempt_counts["render_candidate"], 3)
            save_checkpoint(ckpt_path, updated_cp)
            reloaded_cp = load_checkpoint(ckpt_path)
            self.assertIsNotNone(reloaded_cp)

            decision = decide_retry(
                checkpoint=reloaded_cp,
                failure_key="render_candidate",
                deny_code="RENDER_EXECUTION_FAILED",
                retry_allowance=2,
            )
            self.assertFalse(decision.should_retry)
            self.assertTrue(decision.exhausted)
            self.assertEqual(decision.target_state, "FAILED_JOB")

    def test_retry_event_survives_in_event_log(self) -> None:
        """C6-03: Retry event persisted to event log survives audit load."""
        with tempfile.TemporaryDirectory() as d:
            events_path = Path(d) / "events.jsonl"
            ev = create_retry_event(
                job_id="job-1",
                failure_key="render_candidate",
                failure_code="RENDER_EXECUTION_FAILED",
                classification="RETRYABLE",
                attempt_count=2,
                max_allowance=2,
                exact_action="reexecute_render",
                before_artifact_digests={},
                after_artifact_digests={},
                result="FAILED",
                now_iso=NOW,
                reason="renderer exit 1",
            )
            prior_event = JobEvent(
                event_id="evt-1",
                job_id="job-1",
                prior_state="RENDER_PERMITTED",
                next_state="RENDER_PERMITTED",
                state_revision=1,
                evidence_refs=("evidence-1",),
                actor="orchestrator",
                decision="PERMIT",
                timestamp=NOW,
                reason="initial render permitted",
            )
            job_event = JobEvent(
                event_id=ev.event_id,
                job_id="job-1",
                prior_state="RENDER_PERMITTED",
                next_state="RENDER_PERMITTED",
                state_revision=2,
                evidence_refs=("retry:" + ev.event_id,),
                actor="orchestrator",
                decision="RETRY",
                timestamp=NOW,
                reason="retry scheduled",
            )
            append_event(events_path, prior_event)
            append_event(events_path, job_event)
            events = load_event_log(events_path)
            self.assertTrue(any(e.event_id == ev.event_id for e in events))


# ---------------------------------------------------------------------------
# Category 7: Revocation (2 tests)
# ---------------------------------------------------------------------------


class TestRetryRevocation(unittest.TestCase):
    """Category 7: Revocation handling and checkpoint state preservation."""

    def test_revocation_on_first_attempt_cancels(self) -> None:
        """C7-01: Revocation on first attempt transitions to CANCELLED and disallows retry."""
        checkpoint = make_checkpoint(counts={})
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key="render_candidate",
            deny_code="AUTH_REVOKED",
            retry_allowance=2,
        )
        self.assertFalse(decision.should_retry)
        self.assertEqual(decision.target_state, "CANCELLED")

    def test_revocation_between_retries_cancels_and_preserves_counts(self) -> None:
        """C7-02: Revocation between retries cancels and preserves attempt counts without mutation."""
        cp = make_checkpoint(counts={"render_candidate": 1})
        decision = decide_retry(
            checkpoint=cp,
            failure_key="render_candidate",
            deny_code="AUTH_REVOKED",
            retry_allowance=2,
        )
        self.assertFalse(decision.should_retry)
        self.assertEqual(decision.target_state, "CANCELLED")
        self.assertEqual(cp.attempt_counts, {"render_candidate": 1})


# ---------------------------------------------------------------------------
# Category 8: System Propagation (3 tests)
# ---------------------------------------------------------------------------


class TestRetrySystemPropagation(unittest.TestCase):
    """Category 8: Infrastructure and system error propagation."""

    def test_system_dependency_missing_propagates_failed_system(self) -> None:
        """C8-01: Missing system dependency propagates NON_RETRYABLE to FAILED_SYSTEM."""
        checkpoint = make_checkpoint(counts={})
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key="render_candidate",
            deny_code="SYSTEM_DEPENDENCY_MISSING",
            retry_allowance=2,
        )
        self.assertFalse(decision.should_retry)
        self.assertEqual(decision.classification, "NON_RETRYABLE")
        self.assertEqual(decision.target_state, "FAILED_SYSTEM")

    def test_system_out_of_disk_propagates_failed_system(self) -> None:
        """C8-02: Out of disk system fault propagates to FAILED_SYSTEM via code and exception."""
        checkpoint = make_checkpoint(counts={})
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key="render_candidate",
            deny_code="SYSTEM_OUT_OF_DISK",
            retry_allowance=2,
        )
        self.assertFalse(decision.should_retry)
        self.assertEqual(decision.classification, "NON_RETRYABLE")
        self.assertEqual(decision.target_state, "FAILED_SYSTEM")

        result = classify_failure(
            exception=SystemFailureError("SYSTEM_OUT_OF_DISK", "disk full")
        )
        self.assertEqual(result, ("NON_RETRYABLE", "FAILED_SYSTEM", "SYSTEM_OUT_OF_DISK"))

    def test_qc_system_fault_stays_failed_job_not_failed_system(self) -> None:
        """C8-03: QC system fault stays FAILED_JOB and does not propagate to FAILED_SYSTEM (C11)."""
        result = classify_failure(target_state="FAILED_JOB")
        self.assertEqual(result, ("NON_RETRYABLE", "FAILED_JOB", "UNKNOWN_FAILURE"))

