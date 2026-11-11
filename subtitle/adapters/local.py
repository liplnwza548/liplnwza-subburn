"""Local VM adapter (P2-009, offline only).

The VM has no Drive concept: ``drive_access`` is always
``'not_configured'`` and ``drive_parent_id`` is always None. That is
not a defect.

Delivery on the VM stays inside staging:
``<base_staging_dir>/delivery/<job_id>/OFFLINE-<filename>`` (plan
section 10, D3). There is no external destination on the VM in Phase 2A.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .base import (
    AdapterError,
    CapabilityReport,
    DeliveryReceipt,
    PlatformCapabilities,
)


def _mfa_tool_present() -> bool:
    """Whether an MFA tool binary is on PATH right now.

    Observation only: never run MFA to check. The pipeline in Phase 2A
    receives the MFA artifact from the caller, so this is informational
    and never blocks a job (see docs/adapters.md, deviation D-V).
    """
    return shutil.which("mfa") is not None


def _check_source_root_overlap(
    candidate: Path, trusted_source_root: Path,
) -> bool:
    """True when ``candidate`` and ``trusted_source_root`` overlap
    (same path, or one is the parent of the other)."""
    try:
        candidate = candidate.resolve()
        trusted = trusted_source_root.resolve()
    except OSError:
        return True
    return candidate == trusted or trusted in candidate.parents or candidate in trusted.parents


class LocalAdapter:
    """Platform adapter for the local VM. ``name`` is ``'local'``."""

    name = "local"

    def __init__(self, *, base_staging_dir: Path) -> None:
        self._base_staging_dir = Path(base_staging_dir)

    def check_capabilities(self, *, now: str) -> CapabilityReport:
        ffmpeg = shutil.which("ffmpeg") is not None
        ffprobe = shutil.which("ffprobe") is not None
        mfa = _mfa_tool_present()
        missing = tuple(
            name
            for name, ok in (
                ("ffmpeg", ffmpeg),
                ("ffprobe", ffprobe),
            )
            if not ok
        )
        caps = PlatformCapabilities(
            ffmpeg_available=ffmpeg,
            ffprobe_available=ffprobe,
            mfa_tool_available=mfa,
            drive_access="not_configured",
            drive_parent_id=None,
            missing=missing,
        )
        blocked = bool(missing)
        return CapabilityReport(
            adapter_name=self.name,
            checked_at=now,
            capabilities=caps,
            blocked=blocked,
            blocking_reason="SYSTEM_DEPENDENCY_MISSING" if blocked else None,
            notes=(
                "missing tool(s): " + ", ".join(missing)
                if blocked
                else "ffmpeg + ffprobe available; VM has no Drive concept"
            ),
        )

    def resolve_source(
        self, *, source_ref: dict[str, Any], source_path: Path,
        trusted_source_root: Path,
    ) -> Path:
        """Return the real source path, which must sit under
        ``trusted_source_root``. No file is created or copied; intake
        re-checks and hashes the file itself."""
        from subtitle.core.intake import _Deny, safe_resolve_path

        try:
            resolved = safe_resolve_path(source_path, trusted_source_root)
        except _Deny as exc:
            raise AdapterError(
                'ADAPTER_ROOT_MISSING',
                f"source is not under the trusted source root: {exc}",
            ) from exc
        if not resolved.is_file():
            raise AdapterError(
                "ADAPTER_ROOT_MISSING",
                f"source is not a readable file: {resolved.name}",
            )
        return resolved

    def staging_root(
        self, *, base_staging_dir: Path,
        trusted_source_root: Path | None = None,
    ) -> Path:
        """Return the staging root the caller asked for. Never create a
        root, never fall back to another folder."""
        if not base_staging_dir.exists() or not base_staging_dir.is_dir():
            raise AdapterError(
                "ADAPTER_ROOT_MISSING",
                f"staging root does not exist or is not a directory: {base_staging_dir}",
            )
        if trusted_source_root is not None and _check_source_root_overlap(
            base_staging_dir, trusted_source_root
        ):
            raise AdapterError(
                "ADAPTER_ROOT_UNSAFE",
                "staging root overlaps the trusted source root; "
                "source and output must stay separated",
            )
        return base_staging_dir

    def deliver_review(
        self, *, job_id: str, artifact_path: Path, destination: dict[str, Any],
        expected_destination: dict[str, Any], target_state: str,
        evidence_id: str, offline: bool = True, now: str,
    ) -> DeliveryReceipt:
        """Copy the artifact into staging delivery and return a receipt.

        Same-origin check (rule A4) happens BEFORE any write: the
        destination's provider + id + owner_id must all match
        ``expected_destination``; otherwise deny and write nothing.
        """
        if target_state != "READY_FOR_REVIEW":
            raise AdapterError(
                "ADAPTER_NOT_READY",
                f"delivery requires READY_FOR_REVIEW, job is {target_state}",
            )
        if not offline:
            raise AdapterError(
                "ADAPTER_NOT_READY",
                "Phase 2A delivery is offline only (offline must be True)",
            )
        for field in ("provider", "id", "owner_id"):
            if destination.get(field) != expected_destination.get(field):
                raise AdapterError(
                    "ADAPTER_DESTINATION_MISMATCH",
                    f"destination {field} does not match the authorized "
                    "review destination; nothing was written",
                )
        if not artifact_path.is_file():
            raise AdapterError(
                "ADAPTER_ROOT_MISSING",
                f"artifact is not a readable file: {artifact_path.name}",
            )
        import hashlib

        delivery_dir = (
            self._base_staging_dir / "delivery" / job_id
        )
        delivery_dir.mkdir(parents=True, exist_ok=True)
        out_path = delivery_dir / f"OFFLINE-{artifact_path.name}"
        data = artifact_path.read_bytes()
        out_path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        return DeliveryReceipt(
            job_id=job_id,
            destination_provider=str(destination["provider"]),
            destination_id=str(destination["id"]),
            destination_owner_id=str(destination["owner_id"]),
            artifact_sha256=digest,
            artifact_relpath=f"delivery/{job_id}/OFFLINE-{artifact_path.name}",
            evidence_id=evidence_id,
            offline=True,
            delivered_at=now,
        )
