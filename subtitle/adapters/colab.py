"""Colab adapter with a FAKE (simulated, offline) Drive (P2-009).

A "fake Drive" is an ordinary local folder pretending to be a Drive
parent. There is NO real Google Drive here, and there never will be in
Phase 2A:

  - no Google client library is imported (a test greps the adapter
    files for those library names and requires zero matches), no HTTP
    call is ever made, nothing is uploaded;
  - no HTTP call, no upload, no network at all;
  - the simulated credential is a test key labelled ``test-key-...``,
    never a real key; only its presence or absence is reported, never
    its value;
  - every Drive-related name in code, docs, and reports says
    ``fake`` / ``simulated`` / ``offline``.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from .base import (
    AdapterError,
    CapabilityReport,
    DeliveryReceipt,
    PlatformCapabilities,
)
from .local import _check_source_root_overlap, _mfa_tool_present


class ColabAdapter:
    """Platform adapter for Colab, simulated offline against a local
    folder acting as the Drive parent. ``name`` is ``'colab'``.

    The caller supplies both the fake Drive root and the fake parent
    id; neither is ever hardcoded or guessed. ``fake_drive_credential``
    is a test key (``test-key-...``) or None; only its presence matters.
    """

    name = "colab"

    def __init__(
        self, *, fake_drive_root: Path, fake_drive_parent_id: str,
        fake_drive_credential: str | None = None,
    ) -> None:
        self._fake_drive_root = Path(fake_drive_root)
        self._fake_drive_parent_id = fake_drive_parent_id
        self._fake_drive_credential = fake_drive_credential

    def _drive_is_present(self) -> bool:
        return (
            self._fake_drive_root.exists()
            and self._fake_drive_root.is_dir()
        )

    def check_capabilities(self, *, now: str) -> CapabilityReport:
        ffmpeg = shutil.which("ffmpeg") is not None
        ffprobe = shutil.which("ffprobe") is not None
        mfa = _mfa_tool_present()
        missing: list[str] = [
            name
            for name, ok in (
                ("ffmpeg", ffmpeg),
                ("ffprobe", ffprobe),
            )
            if not ok
        ]
        drive_ok = self._drive_is_present()
        credential_ok = self._fake_drive_credential is not None
        if not drive_ok:
            missing.append("fake_drive_root")
        if not credential_ok:
            missing.append("drive_credential")
        drive_access = (
            "available" if drive_ok and credential_ok else "unavailable"
        )
        caps = PlatformCapabilities(
            ffmpeg_available=ffmpeg,
            ffprobe_available=ffprobe,
            mfa_tool_available=mfa,
            drive_access=drive_access,
            drive_parent_id=self._fake_drive_parent_id,
            missing=tuple(missing),
        )
        # A missing Drive credential/root does NOT block the pre-flight
        # render path (plan section 5.1.2): the job can still render and
        # be QC'd; only the simulated delivery step fails closed, and the
        # orchestrator maps that to BLOCKED_NEEDS_REVIEW (D6).
        tools_missing = [m for m in missing if m in ("ffmpeg", "ffprobe")]
        blocked = bool(tools_missing)
        notes = (
            "missing tool(s): " + ", ".join(tools_missing)
            if blocked
            else "ffmpeg + ffprobe available; "
            f"simulated Drive {drive_access}"
            + (
                " (credential missing)"
                if not credential_ok
                else ""
            )
        )
        return CapabilityReport(
            adapter_name=self.name,
            checked_at=now,
            capabilities=caps,
            blocked=blocked,
            blocking_reason="SYSTEM_DEPENDENCY_MISSING" if blocked else None,
            notes=notes,
        )

    def resolve_source(
        self, *, source_ref: dict[str, Any], source_path: Path,
        trusted_source_root: Path,
    ) -> Path:
        """Return the real source path. On Colab the source must sit
        under the fake Drive root OR under the caller-specified trusted
        source root; whichever fixture specifies explicitly. No file is
        created or copied; intake re-checks and hashes it itself."""
        from subtitle.core.intake import _Deny, safe_resolve_path

        for root in (self._fake_drive_root, trusted_source_root):
            try:
                resolved = safe_resolve_path(source_path, root)
            except (_Deny, ValueError, OSError):
                continue
            if resolved.is_file():
                return resolved
        raise AdapterError(
            "ADAPTER_ROOT_MISSING",
            f"source is not under the fake Drive root or the trusted "
            f"source root: {source_path.name}",
        )

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
        """Copy the artifact into the SIMULATED Drive parent folder and
        return a receipt.

        Same-origin check (rule A4) happens BEFORE any write: the
        destination's provider + id + owner_id must all match
        ``expected_destination``, and the destination id must be this
        adapter's fake parent id. Mismatch means deny and write nothing.
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
        if not self._drive_is_present():
            raise AdapterError(
                "ADAPTER_ROOT_MISSING",
                "simulated Drive root does not exist; "
                "no fallback to another parent is allowed",
            )
        if self._fake_drive_credential is None:
            raise AdapterError(
                "ADAPTER_NOT_READY",
                "simulated Drive credential missing; owner must fix it "
                "(BLOCKED_NEEDS_REVIEW), delivery refused",
            )
        if not self._fake_drive_parent_id:
            raise AdapterError(
                "ADAPTER_ROOT_MISSING",
                "no simulated Drive parent id was given; "
                "delivery refused before writing",
            )
        for field in ("provider", "id", "owner_id"):
            if destination.get(field) != expected_destination.get(field):
                raise AdapterError(
                    "ADAPTER_DESTINATION_MISMATCH",
                    f"destination {field} does not match the authorized "
                    "review destination; nothing was written",
                )
        if destination.get("id") != self._fake_drive_parent_id:
            raise AdapterError(
                "ADAPTER_DESTINATION_MISMATCH",
                "destination id is not this adapter's simulated Drive "
                "parent id; nothing was written",
            )
        if not artifact_path.is_file():
            raise AdapterError(
                "ADAPTER_ROOT_MISSING",
                f"artifact is not a readable file: {artifact_path.name}",
            )
        parent_dir = self._fake_drive_root / self._fake_drive_parent_id
        parent_dir.mkdir(parents=True, exist_ok=True)
        out_path = parent_dir / f"OFFLINE-{artifact_path.name}"
        data = artifact_path.read_bytes()
        out_path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        return DeliveryReceipt(
            job_id=job_id,
            destination_provider=str(destination["provider"]),
            destination_id=str(destination["id"]),
            destination_owner_id=str(destination["owner_id"]),
            artifact_sha256=digest,
            artifact_relpath=(
                f"{self._fake_drive_parent_id}/OFFLINE-{artifact_path.name}"
            ),
            evidence_id=evidence_id,
            offline=True,
            delivered_at=now,
        )
