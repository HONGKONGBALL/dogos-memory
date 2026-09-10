"""Build, validate, apply, and roll back Vbot identity bundles."""

from .bundle import IdentityError, IdentityManifest, build_bundle, validate_bundle
from .importer import ApplyResult, apply_bundle, mark_restart_complete, rollback_bundle
from .staging import pack_staging_envelope, unpack_staging_envelope

__all__ = [
    "ApplyResult",
    "IdentityError",
    "IdentityManifest",
    "apply_bundle",
    "build_bundle",
    "mark_restart_complete",
    "pack_staging_envelope",
    "rollback_bundle",
    "unpack_staging_envelope",
    "validate_bundle",
]
