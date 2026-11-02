"""Settings, all env-overridable.

One rule this file enforces: **studio never generates geometry itself.** It pins a
forge3d version and points that library at its own work directory, so a studio
install cannot corrupt a developer's local forge3d state.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    # Everything studio writes lives under one directory, so a dev install is
    # removable with `rm -rf .studio`.
    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("STUDIO_DATA_DIR", "./.studio")).resolve()
    )
    # SQLite for a single-process MVP. The switch point to Postgres is documented
    # in docs/architecture.md: the moment more than one API process writes.
    db_path: Path | None = None
    storage_dir: Path | None = None
    forge3d_workdir: Path | None = None

    # Auth
    session_cookie: str = "studio_session"
    session_ttl_seconds: int = field(
        default_factory=lambda: _env_int("STUDIO_SESSION_TTL", 7 * 24 * 3600)
    )
    login_token_ttl_seconds: int = field(
        default_factory=lambda: _env_int("STUDIO_LOGIN_TTL", 900)
    )
    # Dev mode returns the magic-link token in the API response instead of
    # emailing it. It must be false anywhere a stranger can reach the API.
    dev_mode: bool = field(default_factory=lambda: _env_bool("STUDIO_DEV_MODE", True))

    # Central platform. Studio does not own identity or credits: both live in
    # the private auth/billing service, and these four values are the whole of
    # studio's side of that contract.
    auth_billing_api_url: str = field(
        default_factory=lambda: os.environ.get(
            "AUTH_BILLING_API_URL", "http://127.0.0.1:8080"
        ).rstrip("/")
    )
    auth_billing_app_id: str = field(
        default_factory=lambda: os.environ.get("AUTH_BILLING_APP_ID", "forge3d-studio")
    )
    #: Never sent to a browser, never logged, never echoed in an error.
    auth_billing_service_token: str = field(
        default_factory=lambda: os.environ.get("AUTH_BILLING_SERVICE_TOKEN", "")
    )
    auth_billing_timeout_seconds: float = field(
        default_factory=lambda: _env_float("AUTH_BILLING_TIMEOUT_SECONDS", 5.0)
    )
    #: How long a verified session is trusted without asking the platform again.
    #: Revocation is only as prompt as this window.
    session_cache_ttl_seconds: float = field(
        default_factory=lambda: _env_float("STUDIO_SESSION_CACHE_TTL", 15.0)
    )
    #: How long a reservation may exist with no job attached before it is
    #: treated as orphaned and released. Long enough for a slow submit to land.
    orphan_grace_seconds: float = field(
        default_factory=lambda: _env_float("STUDIO_ORPHAN_GRACE_SECONDS", 300.0)
    )
    #: Refuse a job whose kind is not in the account's entitlements.
    require_entitlement: bool = field(
        default_factory=lambda: _env_bool("STUDIO_REQUIRE_ENTITLEMENT", True)
    )

    #: Legacy: kept so an old database still opens. Studio no longer reads or
    #: writes this balance — the platform owns it.
    starting_credits: int = field(
        default_factory=lambda: _env_int("STUDIO_STARTING_CREDITS", 100)
    )

    # Workers
    worker_threads: int = field(default_factory=lambda: _env_int("STUDIO_WORKERS", 2))
    #: How long a claim is valid without a heartbeat. A worker that stops
    #: breathing loses its lease, which is how a dead worker is noticed while
    #: the rest of the process keeps running.
    worker_lease_seconds: float = field(
        default_factory=lambda: _env_float("STUDIO_WORKER_LEASE_SECONDS", 120.0)
    )
    #: How often the pool looks for expired leases.
    stale_sweep_seconds: float = field(
        default_factory=lambda: _env_float("STUDIO_STALE_SWEEP_SECONDS", 60.0)
    )

    # Server-sent events
    sse_heartbeat_seconds: float = field(
        default_factory=lambda: _env_float("STUDIO_SSE_HEARTBEAT_SECONDS", 15.0)
    )
    sse_poll_seconds: float = field(
        default_factory=lambda: _env_float("STUDIO_SSE_POLL_SECONDS", 0.4)
    )

    # Limits
    login_rate_limit: int = field(
        default_factory=lambda: _env_int("STUDIO_LOGIN_RATE_LIMIT", 10)
    )
    job_rate_limit: int = field(
        default_factory=lambda: _env_int("STUDIO_JOB_RATE_LIMIT", 60)
    )
    rate_window_seconds: float = field(
        default_factory=lambda: _env_float("STUDIO_RATE_WINDOW", 60.0)
    )
    max_payload_bytes: int = field(
        default_factory=lambda: _env_int("STUDIO_MAX_PAYLOAD_BYTES", 64 * 1024)
    )
    max_params_bytes: int = field(
        default_factory=lambda: _env_int("STUDIO_MAX_PARAMS_BYTES", 8 * 1024)
    )

    _secret: str = field(default_factory=lambda: os.environ.get("STUDIO_SECRET", ""))

    def __post_init__(self) -> None:
        self.db_path = self.db_path or self.data_dir / "studio.db"
        self.storage_dir = self.storage_dir or self.data_dir / "assets"
        self.forge3d_workdir = self.forge3d_workdir or self.data_dir / "forge3d"

    @property
    def central_configured(self) -> bool:
        """Whether studio has everything it needs to reach the platform."""
        return bool(self.auth_billing_api_url and self.auth_billing_service_token)

    def require_central(self) -> None:
        if not self.central_configured:
            raise RuntimeError(
                "studio needs AUTH_BILLING_API_URL and AUTH_BILLING_SERVICE_TOKEN; "
                "it no longer authenticates users or holds credits itself"
            )

    @property
    def secret(self) -> str:
        """Signing/derivation secret. Generated per-process in dev."""
        if self._secret:
            return self._secret
        if not hasattr(self, "_generated_secret"):
            object.__setattr__(self, "_generated_secret", secrets.token_hex(32))
        return self._generated_secret  # type: ignore[attr-defined]

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.storage_dir, self.forge3d_workdir):
            Path(path).mkdir(parents=True, exist_ok=True)

    def apply_to_engine(self) -> None:
        """Point forge3d at studio's own workdir before any engine call.

        Without this, a studio job would write into the developer's `./.forge3d`
        and two systems would share job ids and model directories.
        """
        os.environ["FORGE3D_WORKDIR"] = str(self.forge3d_workdir)
        from forge3d import config as engine_config

        engine_config._settings = None


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
        _settings.ensure_dirs()
        _settings.apply_to_engine()
    return _settings
