"""P2-005 evidence gates G1/G2/G3; no Production Auto Mode.

Gates are pure: they validate evidence the caller captured; they never run tools,
never read the clock, never touch the filesystem. Automated checks are never
human listening.
"""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import statistics

from pythainlp import word_tokenize

from .authorization import Decision
from .evidence import (
    CheckRecord,
    EvidenceError,
    EvidenceRecord,
    create_evidence_record,
    load_evidence_record,
)
from .job import JobSpec, validate_style_spec
from .state import JobCheckpoint, RENDER_NOT_PERMITTED
from subtitle.viralpop.render.script_lock import (
    CoverageError,
    diff_report,
    verify_coverage,
)

MFA_EVIDENCE_INVALID = 'MFA_EVIDENCE_INVALID'
SCRIPT_NEEDS_EVIDENCE = 'SCRIPT_NEEDS_EVIDENCE'
ALLOWED_MFA_TOOLS = frozenset({'thai_mfa'})
_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_TIMESTAMP = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
_METHODS = frozenset({'subprocess_execution', 'deterministic_check', 'synthetic_stub'})


@dataclass(frozen=True)
class GateDecision:
    passed: bool
    gate_name: str
    deny_code: str | None
    evidence: EvidenceRecord | None
    reason: str


def _validate_timestamp(value) -> bool:
    if type(value) is not str or not _TIMESTAMP.fullmatch(value):
        return False
    try:
        datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
        return True
    except ValueError:
        return False


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _human_check() -> CheckRecord:
    return CheckRecord(
        'human_audio_verification',
        'NOT_CHECKED',
        None,
        'audited_by_human',
        None,
        'Automated pipeline only; no human listening has been performed.',
    )


def _deny(gate_name: str, deny_code: str, reason: str) -> GateDecision:
    return GateDecision(False, gate_name, deny_code, None, reason)


def check_transcript_evidence(
    *,
    spec,
    transcript_data,
    script_text,
    script_lines,
    tool_metadata,
    artifact_relpath,
    evidence_id,
    now,
) -> GateDecision:
    if not isinstance(spec, JobSpec):
        raise ValueError('INVALID_SPEC')
    if type(script_text) is not str:
        raise ValueError('INVALID_SCRIPT_TEXT')
    if type(script_lines) is not list or not all(type(line) is str for line in script_lines):
        raise ValueError('INVALID_SCRIPT_LINES')
    if type(evidence_id) is not str or not evidence_id:
        raise ValueError('INVALID_EVIDENCE_ID')
    if (
        type(artifact_relpath) is not str
        or not artifact_relpath
        or artifact_relpath.startswith('/')
        or Path(artifact_relpath).is_absolute()
        or '..' in Path(artifact_relpath).parts
        or any(part == '..' for part in artifact_relpath.split('/'))
    ):
        raise ValueError('INVALID_ARTIFACT_RELPATH')
    if not _validate_timestamp(now):
        raise ValueError('INVALID_TIMESTAMP')

    if type(tool_metadata) is not dict:
        return _deny('G1', SCRIPT_NEEDS_EVIDENCE, "tool_metadata must be a dict")

    if 'processor' not in tool_metadata or type(tool_metadata['processor']) is not str or not tool_metadata['processor']:
        return _deny('G1', SCRIPT_NEEDS_EVIDENCE, "tool_metadata missing or invalid key 'processor'")
    if 'model' not in tool_metadata or type(tool_metadata['model']) is not str or not tool_metadata['model']:
        return _deny('G1', SCRIPT_NEEDS_EVIDENCE, "tool_metadata missing or invalid key 'model'")
    if 'model_version' not in tool_metadata or type(tool_metadata['model_version']) is not str or not tool_metadata['model_version']:
        return _deny('G1', SCRIPT_NEEDS_EVIDENCE, "tool_metadata missing or invalid key 'model_version'")
    if 'method' not in tool_metadata or tool_metadata['method'] not in _METHODS:
        return _deny('G1', SCRIPT_NEEDS_EVIDENCE, "tool_metadata missing or invalid key 'method'")

    if 'command' in tool_metadata:
        cmd = tool_metadata['command']
        if type(cmd) not in (list, tuple) or not all(type(c) is str and bool(c) for c in cmd):
            return _deny('G1', SCRIPT_NEEDS_EVIDENCE, "tool_metadata invalid key 'command'")

    if 'exit_code' in tool_metadata:
        ec = tool_metadata['exit_code']
        if type(ec) is bool or type(ec) is not int:
            return _deny('G1', SCRIPT_NEEDS_EVIDENCE, "tool_metadata invalid key 'exit_code'")
        if ec != 0:
            return _deny('G1', SCRIPT_NEEDS_EVIDENCE, f"processor exited with non-zero exit code {ec}")

    if 'captured_at' in tool_metadata:
        ca = tool_metadata['captured_at']
        if not _validate_timestamp(ca):
            return _deny('G1', SCRIPT_NEEDS_EVIDENCE, "tool_metadata invalid key 'captured_at'")
        if ca < spec.created_at or ca > now:
            return _deny('G1', SCRIPT_NEEDS_EVIDENCE, f"captured_at {ca!r} out of bounds [{spec.created_at}, {now}]")

    if tool_metadata['processor'] not in spec.allowed_processors:
        return _deny('G1', SCRIPT_NEEDS_EVIDENCE, f"processor {tool_metadata['processor']!r} not in allowed_processors")

    if (
        type(transcript_data) is not dict
        or 'text' not in transcript_data
        or type(transcript_data['text']) is not str
    ):
        return _deny('G1', SCRIPT_NEEDS_EVIDENCE, "transcript_data must be a dict containing 'text' of type str")

    mismatches = diff_report(script_lines, transcript_data['text'])
    if mismatches:
        preview = [
            f"{m.get('type')} at line {m.get('line_index')}"
            for m in mismatches[:3]
        ]
        return _deny(
            'G1',
            SCRIPT_NEEDS_EVIDENCE,
            f"Transcript mismatches approved script: {len(mismatches)} mismatch(es) ({'; '.join(preview)})",
        )

    record = create_evidence_record(
        evidence_id=evidence_id,
        job_id=spec.job_id,
        stage='SCRIPT_EVIDENCE_CHECKED',
        tool=tool_metadata['processor'],
        tool_version=tool_metadata['model_version'],
        method=tool_metadata['method'],
        command=tuple(tool_metadata.get('command', ())),
        input_digest_refs={
            'source_media_sha256': spec.source.sha256,
            'script_sha256': _sha256_text(script_text),
        },
        artifact_sha256=_sha256_text(transcript_data['text']),
        artifact_relpath=artifact_relpath,
        captured_at=tool_metadata.get('captured_at', now),
        exit_code=tool_metadata.get('exit_code'),
        media_duration_seconds=None,
        checks=(
            CheckRecord('transcript_processor_allowed', 'PASSED', tool_metadata['processor'], ','.join(sorted(spec.allowed_processors)), None, ''),
            CheckRecord('transcript_script_match', 'PASSED', len(script_lines), len(script_lines), None, ''),
            _human_check(),
        ),
        notes='',
    )
    return GateDecision(True, 'G1', None, record, 'SCRIPT_EVIDENCE_CHECKED')


def _reject_constant(value):
    raise ValueError('INVALID_JSON_CONSTANT')


def _tokenize_script(script_text: str) -> list[str]:
    toks: list[str] = []
    for ln in script_text.split('\n'):
        ln = ln.strip()
        if ln:
            toks.extend(
                t for t in word_tokenize(ln, engine='newmm', keep_whitespace=False)
                if t.strip()
            )
    fx: list[str] = []
    for t in toks:
        if t == 'ๆ' and fx:
            fx[-1] += t
        else:
            fx.append(t)
    return fx


def check_mfa_provenance(
    *,
    spec,
    mfa_artifact_bytes,
    expected_script_sha256,
    expected_audio_sha256,
    media_duration_seconds,
    script_text,
    tool_metadata,
    artifact_relpath,
    evidence_id,
    now,
) -> GateDecision:
    """Gate G2: Validate MFA provenance and word timing integrity.

    Pure function: no subprocess, no clock reads, no filesystem access, no /tmp,
    no SQLite, no verify() call.

    Rule: caller must pass script_text in lock_script canonical form: ''.join of stripped non-empty lines.
    """
    if not isinstance(spec, JobSpec):
        raise ValueError('INVALID_SPEC')
    if type(mfa_artifact_bytes) is not bytes:
        raise ValueError('INVALID_MFA_ARTIFACT_BYTES')
    if type(expected_script_sha256) is not str or not _DIGEST.fullmatch(expected_script_sha256):
        raise ValueError('INVALID_EXPECTED_SCRIPT_SHA256')
    if type(expected_audio_sha256) is not str or not _DIGEST.fullmatch(expected_audio_sha256):
        raise ValueError('INVALID_EXPECTED_AUDIO_SHA256')
    if (
        type(media_duration_seconds) is bool
        or type(media_duration_seconds) not in (int, float)
        or not math.isfinite(media_duration_seconds)
        or media_duration_seconds <= 0
    ):
        raise ValueError('INVALID_MEDIA_DURATION_SECONDS')
    if type(script_text) is not str:
        raise ValueError('INVALID_SCRIPT_TEXT')
    if type(evidence_id) is not str or not evidence_id:
        raise ValueError('INVALID_EVIDENCE_ID')
    if (
        type(artifact_relpath) is not str
        or not artifact_relpath
        or artifact_relpath.startswith('/')
        or Path(artifact_relpath).is_absolute()
        or '..' in Path(artifact_relpath).parts
        or any(part == '..' for part in artifact_relpath.split('/'))
    ):
        raise ValueError('INVALID_ARTIFACT_RELPATH')
    if not _validate_timestamp(now):
        raise ValueError('INVALID_TIMESTAMP')

    if type(tool_metadata) is not dict:
        return _deny('G2', MFA_EVIDENCE_INVALID, "tool_metadata must be a dict")

    if 'tool' not in tool_metadata or type(tool_metadata['tool']) is not str or not tool_metadata['tool']:
        return _deny('G2', MFA_EVIDENCE_INVALID, "tool_metadata missing or invalid key 'tool'")
    if 'tool_version' not in tool_metadata or type(tool_metadata['tool_version']) is not str or not tool_metadata['tool_version']:
        return _deny('G2', MFA_EVIDENCE_INVALID, "tool_metadata missing or invalid key 'tool_version'")
    if 'method' not in tool_metadata or tool_metadata['method'] not in ('subprocess_execution', 'synthetic_stub'):
        return _deny('G2', MFA_EVIDENCE_INVALID, "tool_metadata missing or invalid key 'method'")
    if (
        'command' not in tool_metadata
        or type(tool_metadata['command']) not in (list, tuple)
        or len(tool_metadata['command']) == 0
        or not all(type(c) is str and bool(c) for c in tool_metadata['command'])
    ):
        return _deny('G2', MFA_EVIDENCE_INVALID, "tool_metadata missing or invalid key 'command'")
    if (
        'exit_code' not in tool_metadata
        or type(tool_metadata['exit_code']) is bool
        or type(tool_metadata['exit_code']) is not int
    ):
        return _deny('G2', MFA_EVIDENCE_INVALID, "tool_metadata missing or invalid key 'exit_code'")
    if (
        'captured_at' not in tool_metadata
        or not _validate_timestamp(tool_metadata['captured_at'])
    ):
        return _deny('G2', MFA_EVIDENCE_INVALID, "tool_metadata missing or invalid key 'captured_at'")

    if tool_metadata['tool'] not in ALLOWED_MFA_TOOLS:
        return _deny('G2', MFA_EVIDENCE_INVALID, f"tool {tool_metadata['tool']!r} not in ALLOWED_MFA_TOOLS")

    if tool_metadata['exit_code'] != 0:
        return _deny('G2', MFA_EVIDENCE_INVALID, f"exit_code is {tool_metadata['exit_code']} (must be 0)")

    ca = tool_metadata['captured_at']
    if ca < spec.created_at or ca > now:
        return _deny('G2', MFA_EVIDENCE_INVALID, f"captured_at {ca!r} out of bounds [{spec.created_at}, {now}]")

    try:
        artifact = json.loads(
            mfa_artifact_bytes.decode('utf-8'),
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeError):
        return _deny('G2', MFA_EVIDENCE_INVALID, 'artifact is not valid JSON')

    if type(artifact) is not dict:
        return _deny('G2', MFA_EVIDENCE_INVALID, 'artifact must be a JSON object')

    if 'segments' in artifact:
        return _deny('G2', MFA_EVIDENCE_INVALID, "STT forgery detected: 'segments' key present")

    if 'tiers' not in artifact or type(artifact['tiers']) is not dict:
        return _deny('G2', MFA_EVIDENCE_INVALID, "missing 'tiers'")

    tiers = artifact['tiers']
    if 'words' not in tiers or type(tiers['words']) is not dict:
        return _deny('G2', MFA_EVIDENCE_INVALID, "missing 'tiers.words'")

    words_tier = tiers['words']
    if 'entries' not in words_tier or type(words_tier['entries']) is not list:
        return _deny('G2', MFA_EVIDENCE_INVALID, "missing 'tiers.words.entries'")

    raw_entries = words_tier['entries']
    for entry in raw_entries:
        if type(entry) is dict and any(k in entry for k in ('probability', 'avg_logprob', 'temperature')):
            return _deny('G2', MFA_EVIDENCE_INVALID, 'STT forgery detected: entry contains probability/avg_logprob/temperature')

    kept_entries = []
    for entry in raw_entries:
        if type(entry) not in (list, tuple) or len(entry) != 3:
            return _deny('G2', MFA_EVIDENCE_INVALID, f"entry must be list or tuple of 3 items: {entry!r}")
        start, end, word = entry
        if type(word) is not str:
            return _deny('G2', MFA_EVIDENCE_INVALID, f"entry word must be str: {word!r}")
        if word in ('<eps>', ''):
            continue
        if (
            type(start) is bool
            or type(start) not in (int, float)
            or not math.isfinite(start)
            or type(end) is bool
            or type(end) not in (int, float)
            or not math.isfinite(end)
        ):
            return _deny('G2', MFA_EVIDENCE_INVALID, f"entry timing must be finite int/float: {entry!r}")
        kept_entries.append((float(start), float(end), word))

    script_tokens = _tokenize_script(script_text)
    if len(kept_entries) != len(script_tokens):
        return _deny(
            'G2',
            MFA_EVIDENCE_INVALID,
            f"count mismatch: mfa entries={len(kept_entries)} != script tokens={len(script_tokens)}",
        )

    for i, ((start, end, word), token) in enumerate(zip(kept_entries, script_tokens)):
        if word != token and word != '<unk>':
            return _deny(
                'G2',
                MFA_EVIDENCE_INVALID,
                f"word mismatch at token {i}: mfa={word!r} != script={token!r}",
            )

    if len(kept_entries) == 0:
        return _deny('G2', MFA_EVIDENCE_INVALID, "empty entries: no words to verify")

    for i, (start, end, word) in enumerate(kept_entries):
        if start < 0.0:
            return _deny('G2', MFA_EVIDENCE_INVALID, f"timing invariant failed: word {i} start {start} < 0.0")
        if end < 0.0:
            return _deny('G2', MFA_EVIDENCE_INVALID, f"timing invariant failed: word {i} end {end} < 0.0")
        if (end - start) < 0.010 - 1e-9:
            return _deny('G2', MFA_EVIDENCE_INVALID, f"timing invariant failed: word {i} duration {end - start:.4f} < 0.010")
        if i > 0 and start < kept_entries[i - 1][1]:
            return _deny('G2', MFA_EVIDENCE_INVALID, f"timing invariant failed: word {i} start {start} < previous word end {kept_entries[i - 1][1]}")

    if kept_entries[-1][1] > media_duration_seconds:
        return _deny(
            'G2',
            MFA_EVIDENCE_INVALID,
            f"timing invariant failed: last word end {kept_entries[-1][1]} > media duration {media_duration_seconds}",
        )

    durations = [end - start for start, end, _ in kept_entries]
    pstdev = statistics.pstdev(durations)
    if len(kept_entries) >= 5 and pstdev < 0.005:
        return _deny(
            'G2',
            MFA_EVIDENCE_INVALID,
            f"averaged-timing heuristic (best-effort): word durations pstdev {pstdev:.6f} < 0.005",
        )

    actual_script_sha = _sha256_text(script_text)
    if actual_script_sha != expected_script_sha256:
        return _deny(
            'G2',
            MFA_EVIDENCE_INVALID,
            f"script hash mismatch: computed {actual_script_sha} != expected {expected_script_sha256}",
        )

    unk_positions = [
        (i, script_tokens[i])
        for i, ((start, end, word), token) in enumerate(zip(kept_entries, script_tokens))
        if word == '<unk>'
    ]

    artifact_sha = hashlib.sha256(mfa_artifact_bytes).hexdigest()
    common_checks = (
        CheckRecord('executor_process_exit', 'PASSED', 0, 0, None, ''),
        CheckRecord('artifact_sha256_match', 'PASSED', artifact_sha, artifact_sha, None, ''),
        CheckRecord('timing_monotonic_finite', 'PASSED', True, True, None, ''),
        CheckRecord('script_word_count_match', 'PASSED', len(kept_entries), len(kept_entries), None, ''),
        CheckRecord('averaged_timing_heuristic', 'PASSED', round(pstdev, 6), 'pstdev>=0.005', None, 'best-effort heuristic'),
    )

    if unk_positions:
        unk_desc = '; '.join(f"{idx}->{tok}" for idx, tok in unk_positions)
        unk_check = CheckRecord(
            'unk_words',
            'FAILED',
            len(unk_positions),
            0,
            None,
            f"positions: {unk_desc}",
        )
        record = create_evidence_record(
            evidence_id=evidence_id,
            job_id=spec.job_id,
            stage='ALIGNED',
            tool=tool_metadata['tool'],
            tool_version=tool_metadata['tool_version'],
            method=tool_metadata['method'],
            command=tuple(tool_metadata['command']),
            input_digest_refs={
                'source_media_sha256': spec.source.sha256,
                'extracted_audio_sha256': expected_audio_sha256,
                'script_sha256': expected_script_sha256,
            },
            artifact_sha256=artifact_sha,
            artifact_relpath=artifact_relpath,
            captured_at=tool_metadata['captured_at'],
            exit_code=0,
            media_duration_seconds=float(media_duration_seconds),
            checks=(*common_checks, unk_check, _human_check()),
            notes='',
        )
        return GateDecision(
            False,
            'G2',
            SCRIPT_NEEDS_EVIDENCE,
            record,
            f"script needs evidence: {len(unk_positions)} unk word(s) ({unk_desc})",
        )

    unk_check = CheckRecord('unk_words', 'PASSED', 0, 0, None, '')
    record = create_evidence_record(
        evidence_id=evidence_id,
        job_id=spec.job_id,
        stage='ALIGNED',
        tool=tool_metadata['tool'],
        tool_version=tool_metadata['tool_version'],
        method=tool_metadata['method'],
        command=tuple(tool_metadata['command']),
        input_digest_refs={
            'source_media_sha256': spec.source.sha256,
            'extracted_audio_sha256': expected_audio_sha256,
            'script_sha256': expected_script_sha256,
        },
        artifact_sha256=artifact_sha,
        artifact_relpath=artifact_relpath,
        captured_at=tool_metadata['captured_at'],
        exit_code=0,
        media_duration_seconds=float(media_duration_seconds),
        checks=(*common_checks, unk_check, _human_check()),
        notes='',
    )
    return GateDecision(True, 'G2', None, record, 'ALIGNED')


def check_pre_render_gate(
    *,
    spec,
    checkpoint,
    mfa_evidence,
    script_text,
    cues,
    authorization_decision,
    evidence_id,
    now,
) -> GateDecision:
    """Gate G3: Pre-render Gate verifying coverage, authorization, and evidence provenance.

    Pure function: no subprocess, no clock reads, no filesystem access, no /tmp,
    no SQLite, no verify() call.
    """
    if not isinstance(spec, JobSpec):
        raise ValueError('INVALID_SPEC')
    if not isinstance(checkpoint, JobCheckpoint):
        raise ValueError('INVALID_CHECKPOINT')
    if not isinstance(mfa_evidence, EvidenceRecord):
        raise ValueError('INVALID_MFA_EVIDENCE')
    if type(script_text) is not str:
        raise ValueError('INVALID_SCRIPT_TEXT')
    if type(cues) is not list:
        raise ValueError('INVALID_CUES')
    if not isinstance(authorization_decision, Decision):
        raise ValueError('INVALID_AUTHORIZATION_DECISION')
    if type(evidence_id) is not str or not evidence_id:
        raise ValueError('INVALID_EVIDENCE_ID')
    if not _validate_timestamp(now):
        raise ValueError('INVALID_TIMESTAMP')

    if not authorization_decision.allowed:
        return _deny(
            'G3',
            authorization_decision.reason,
            f"fresh authorization decision denied: {authorization_decision.reason}",
        )

    if checkpoint.current_state != 'ALIGNED':
        return _deny(
            'G3',
            RENDER_NOT_PERMITTED,
            f"checkpoint state is {checkpoint.current_state}, expected ALIGNED",
        )

    if mfa_evidence.job_id != spec.job_id:
        return _deny(
            'G3',
            MFA_EVIDENCE_INVALID,
            f"mfa evidence job_id {mfa_evidence.job_id!r} != spec job_id {spec.job_id!r}",
        )
    if mfa_evidence.stage != 'ALIGNED':
        return _deny(
            'G3',
            MFA_EVIDENCE_INVALID,
            f"mfa evidence stage is {mfa_evidence.stage!r}, expected ALIGNED",
        )

    mfa_digests = mfa_evidence.input_digest_refs if hasattr(mfa_evidence.input_digest_refs, 'get') else {}
    if mfa_digests.get('source_media_sha256') != spec.source.sha256:
        return _deny(
            'G3',
            MFA_EVIDENCE_INVALID,
            "mfa evidence source_media_sha256 mismatch",
        )
    if mfa_digests.get('script_sha256') != _sha256_text(script_text):
        return _deny(
            'G3',
            MFA_EVIDENCE_INVALID,
            "mfa evidence script_sha256 mismatch",
        )

    try:
        validate_style_spec(spec.style_id, dict(spec.style_overrides))
    except ValueError as exc:
        return _deny('G3', str(exc), f"style spec invalid: {exc}")

    try:
        verify_coverage(script_text, cues)
    except CoverageError as exc:
        return _deny('G3', MFA_EVIDENCE_INVALID, f"cue coverage failed: {exc}")

    record = create_evidence_record(
        evidence_id=evidence_id,
        job_id=spec.job_id,
        stage='RENDER_PERMITTED',
        tool='gate',
        tool_version='1',
        method='deterministic_check',
        command=(),
        input_digest_refs={
            'source_media_sha256': spec.source.sha256,
            'script_sha256': _sha256_text(script_text),
            'mfa_artifact_sha256': mfa_evidence.artifact_sha256,
        },
        artifact_sha256=mfa_evidence.artifact_sha256,
        artifact_relpath=mfa_evidence.artifact_relpath,
        captured_at=now,
        exit_code=None,
        media_duration_seconds=mfa_evidence.media_duration_seconds,
        checks=(
            CheckRecord('authorization_fresh', 'PASSED', True, True, None, ''),
            CheckRecord('coverage_exact', 'PASSED', len(cues), len(cues), None, ''),
            CheckRecord('hash_chain_match', 'PASSED', True, True, None, ''),
            _human_check(),
        ),
        notes='',
    )
    return GateDecision(True, 'G3', None, record, 'RENDER_PERMITTED')


def load_gate_evidence(job_dir, evidence_id, *, expected_job_id=None) -> EvidenceRecord | None:
    """Load an evidence file directly from <job_dir>/evidence/<evidence_id>.json via load_evidence_record.

    The gates module loads evidence files itself; it does NOT extend load_job_record from audit.py (appendix C6).
    """
    return load_evidence_record(job_dir, evidence_id, expected_job_id=expected_job_id)
