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

    # Money
    starting_credits: int = field(
        default_factory=lambda: _env_int("STUDIO_STARTING_CREDITS", 100)
    )

    # Workers
    worker_threads: int = field(default_factory=lambda: _env_int("STUDIO_WORKERS", 2))

    _secret: str = field(default_factory=lambda: os.environ.get("STUDIO_SECRET", ""))

    def __post_init__(self) -> None:
        self.db_path = self.db_path or self.data_dir / "studio.db"
        self.storage_dir = self.storage_dir or self.data_dir / "assets"
        self.forge3d_workdir = self.forge3d_workdir or self.data_dir / "forge3d"

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
