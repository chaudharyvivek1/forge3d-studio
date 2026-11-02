"""Request and response schemas.

Kept separate from the routes so the wire format is reviewable in one place, and
so a future client SDK can be generated from it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field

JobKind = Literal["shape", "planet", "provider"]


class LoginRequest(BaseModel):
    email: EmailStr


class LoginResponse(BaseModel):
    sent: bool = True
    #: Only present in dev mode, where there is no mail provider to deliver it.
    dev_token: str | None = None
    note: str = (
        "dev mode: the token is returned here instead of being emailed. "
        "Set STUDIO_DEV_MODE=false to disable."
    )


class UserOut(BaseModel):
    id: str
    email: str
    credits: int


class JobSubmit(BaseModel):
    kind: JobKind
    params: dict[str, Any] = Field(default_factory=dict)
    #: Supply your own to make a retried submit idempotent. A repeat with the
    #: same key returns the original job instead of starting a second one.
    idempotency_key: str | None = None


class JobOut(BaseModel):
    id: str
    kind: str
    status: str
    provider: str
    cost: int
    progress: float
    stage: str
    error: str | None = None
    asset_id: str | None = None
    #: True when the credits were returned; False when they were kept, either
    #: because the work was delivered or because it may have been billed.
    refunded: bool = False
    #: A human should reconcile this job with the provider before anyone retries.
    needs_review: bool = False
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None


class AssetOut(BaseModel):
    id: str
    job_id: str | None
    name: str
    triangles: int
    size_bytes: int
    validation: dict[str, Any] = Field(default_factory=dict)
    created_at: float
    has_glb: bool = False
    has_stl: bool = False
    has_viewer: bool = False


class SceneItem(BaseModel):
    asset_id: str
    position: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    scale: float = 1.0
    rotation: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])


class SceneIn(BaseModel):
    name: str
    items: list[SceneItem]


class SceneOut(BaseModel):
    id: str
    name: str
    items: list[SceneItem]
    created_at: float
    updated_at: float


class KindOut(BaseModel):
    kind: str
    cost: int
    provider: str
    summary: str


class ExportOut(BaseModel):
    ok: bool
    asset_id: str | None = None
    html: str | None = None
    size_bytes: int = 0
    error: str | None = None
