"""HTTP API.

The rule this layer enforces: **a paid job is refused before it is submitted, not
after.** Credits are reserved with a conditional UPDATE, so two simultaneous
submits from a user with one job's worth of credit produce one job, not two.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db, storage
from .config import get_settings
from .models import (
    AssetOut,
    ExportOut,
    JobOut,
    JobSubmit,
    KindOut,
    LoginRequest,
    LoginResponse,
    SceneIn,
    SceneOut,
    UserOut,
)
from .worker import KINDS

# Create the schema at import as well as at startup: the statements are
# `IF NOT EXISTS`, and without this any code path that touches the API before the
# lifespan runs (a test client used without `with`, an ASGI bridge, a script)
# fails with a raw "no such table" 500 instead of working.
get_settings().ensure_dirs()
db.init_db()


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Start the DB and the worker pool with the app, and stop them with it."""
    settings = get_settings()
    db.init_db()
    worker = None
    if getattr(application.state, "start_worker", True):
        from .worker import Worker

        worker = Worker(settings)
        worker.start()
        application.state.worker = worker
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()
            application.state.worker = None


app = FastAPI(
    lifespan=lifespan,
    title="forge3d-studio",
    version="0.1.2",
    description=(
        "The product surface around forge3d: accounts, a job queue, an asset "
        "library and shareable scene pages. Geometry and validation are done by "
        "the pinned forge3d engine, never reimplemented here."
    ),
)


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------
@app.get("/healthz")
def healthz() -> dict:
    settings = get_settings()
    with db.connect() as conn:
        queued = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status = 'queued'"
        ).fetchone()["n"]
    return {
        "ok": True,
        "service": "forge3d-studio",
        "version": app.version,
        "dev_mode": settings.dev_mode,
        "queued_jobs": queued,
    }


@app.get("/kinds", response_model=list[KindOut])
def list_kinds() -> list[KindOut]:
    return [KindOut(kind=name, **info) for name, info in KINDS.items()]


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
def current_user(request: Request) -> dict:
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="not signed in")
    with db.connect() as conn:
        row = db.user_for_session(conn, token)
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="session expired")
    return dict(row)


@app.post("/auth/request", response_model=LoginResponse)
def request_login(payload: LoginRequest) -> LoginResponse:
    """Start a magic-link login.

    In dev mode the token comes back in the response, because there is no mail
    provider. That is a deliberate, documented hole — `STUDIO_DEV_MODE=false`
    closes it.
    """
    settings = get_settings()
    with db.connect() as conn:
        token = db.create_login_token(conn, payload.email, settings.login_token_ttl_seconds)
    if settings.dev_mode:
        return LoginResponse(sent=True, dev_token=token)
    # A real deployment sends this by email and returns nothing sensitive.
    return LoginResponse(sent=True, dev_token=None,
                         note="check your email for the sign-in link")


@app.get("/auth/callback")
def login_callback(token: str, response: Response) -> dict:
    settings = get_settings()
    with db.connect() as conn:
        email = db.consume_login_token(conn, token)
        if email is None:
            raise HTTPException(status_code=400, detail="invalid or expired token")
        user = db.ensure_user(conn, email, settings.starting_credits)
        session = db.create_session(conn, user["id"], settings.session_ttl_seconds)
    response.set_cookie(
        settings.session_cookie, session,
        httponly=True, samesite="lax", max_age=settings.session_ttl_seconds,
        secure=not settings.dev_mode,
    )
    return {"ok": True, "user_id": user["id"], "email": user["email"]}


@app.post("/auth/logout")
def logout(request: Request, response: Response) -> dict:
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie)
    if token:
        with db.connect() as conn:
            db.delete_session(conn, token)
    response.delete_cookie(settings.session_cookie)
    return {"ok": True}


@app.get("/me", response_model=UserOut)
def me(user: dict = Depends(current_user)) -> UserOut:
    return UserOut(id=user["id"], email=user["email"], credits=user["credits"])


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------
def _job_out(row: Any) -> JobOut:
    return JobOut(
        id=row["id"], kind=row["kind"], status=row["status"], provider=row["provider"],
        cost=row["cost"], progress=row["progress"], stage=row["stage"],
        error=row["error"], asset_id=row["asset_id"], created_at=row["created_at"],
        started_at=row["started_at"], finished_at=row["finished_at"],
        refunded=bool(row["refunded"]), needs_review=bool(row["needs_review"]),
    )


@app.post("/jobs", response_model=JobOut, status_code=status.HTTP_201_CREATED)
def submit_job(payload: JobSubmit, user: dict = Depends(current_user)) -> JobOut:
    info = KINDS.get(payload.kind)
    if info is None:
        raise HTTPException(status_code=400, detail=f"unknown kind {payload.kind!r}")
    cost = int(info["cost"])

    with db.connect() as conn:
        if payload.idempotency_key:
            existing = conn.execute(
                "SELECT * FROM jobs WHERE user_id = ? AND idempotency_key = ?",
                (user["id"], payload.idempotency_key),
            ).fetchone()
            if existing:
                return _job_out(existing)

        # Reserve before creating the job. If this fails, nothing was queued and
        # no provider was contacted.
        if not db.spend_credits(conn, user["id"], cost):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(f"not enough credits: {payload.kind} costs {cost}, "
                        f"you have {user['credits']}"),
            )

        try:
            job = db.create_job(
                conn, user_id=user["id"], kind=payload.kind, params=payload.params,
                provider=info["provider"], cost=cost,
                idempotency_key=payload.idempotency_key,
            )
        except Exception:
            db.refund_credits(conn, user["id"], cost)
            raise
    return _job_out(job)


@app.get("/jobs", response_model=list[JobOut])
def list_jobs(user: dict = Depends(current_user), limit: int = 50) -> list[JobOut]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user["id"], max(1, min(200, limit))),
        ).fetchall()
    return [_job_out(row) for row in rows]


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, user: dict = Depends(current_user)) -> JobOut:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user["id"])
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no such job")
    return _job_out(row)


@app.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: str, user: dict = Depends(current_user)) -> JobOut:
    with db.connect() as conn:
        if not db.request_cancel(conn, job_id, user["id"]):
            raise HTTPException(status_code=409,
                                detail="job is not cancellable (already finished?)")
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _job_out(row)


@app.get("/jobs/{job_id}/events")
def job_events(job_id: str, user: dict = Depends(current_user)) -> StreamingResponse:
    """Server-sent progress. One direction, no reconnect logic, survives proxies."""

    def stream() -> Iterator[str]:
        terminal = {"succeeded", "failed", "cancelled"}
        while True:
            with db.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user["id"])
                ).fetchone()
            if row is None:
                yield 'data: {"error": "no such job"}\n\n'
                return
            payload = {
                "status": row["status"], "progress": row["progress"],
                "stage": row["stage"], "error": row["error"],
                "asset_id": row["asset_id"],
            }
            yield f"data: {json.dumps(payload)}\n\n"
            if row["status"] in terminal:
                return
            time.sleep(0.4)

    return StreamingResponse(stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# assets
# ---------------------------------------------------------------------------
def _asset_out(row: Any) -> AssetOut:
    return AssetOut(
        id=row["id"], job_id=row["job_id"], name=row["name"],
        triangles=row["triangles"], size_bytes=row["size_bytes"],
        validation=json.loads(row["validation"] or "{}"), created_at=row["created_at"],
        has_glb=bool(row["glb_path"]), has_stl=bool(row["stl_path"]),
        has_viewer=bool(row["html_path"]),
    )


@app.get("/assets", response_model=list[AssetOut])
def list_assets(user: dict = Depends(current_user), limit: int = 50) -> list[AssetOut]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM assets WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user["id"], max(1, min(200, limit))),
        ).fetchall()
    return [_asset_out(row) for row in rows]


@app.get("/assets/{asset_id}", response_model=AssetOut)
def get_asset(asset_id: str, user: dict = Depends(current_user)) -> AssetOut:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM assets WHERE id = ? AND user_id = ?", (asset_id, user["id"])
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no such asset")
    return _asset_out(row)


@app.get("/assets/{asset_id}/file/{kind}")
def download_asset(asset_id: str, kind: str, user: dict = Depends(current_user)):
    column = {"glb": "glb_path", "stl": "stl_path", "viewer": "html_path"}.get(kind)
    if column is None:
        raise HTTPException(status_code=400, detail="kind must be glb, stl or viewer")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM assets WHERE id = ? AND user_id = ?", (asset_id, user["id"])
        ).fetchone()
    if row is None or not row[column]:
        raise HTTPException(status_code=404, detail="no such file")
    try:
        path = storage.resolve(row[column])
    except ValueError:
        raise HTTPException(status_code=500, detail="stored path is invalid") from None
    if not path.exists():
        raise HTTPException(status_code=410, detail="file is no longer on disk")
    media = {"glb": "model/gltf-binary", "stl": "model/stl",
             "viewer": "text/html"}[kind]
    return FileResponse(path, media_type=media, filename=f"{row['name']}.{path.suffix.lstrip('.')}")


# ---------------------------------------------------------------------------
# scenes
# ---------------------------------------------------------------------------
def _scene_out(row: Any) -> SceneOut:
    doc = json.loads(row["doc"] or "{}")
    return SceneOut(id=row["id"], name=row["name"], items=doc.get("items", []),
                    created_at=row["created_at"], updated_at=row["updated_at"])


@app.post("/scenes", response_model=SceneOut, status_code=201)
def create_scene(payload: SceneIn, user: dict = Depends(current_user)) -> SceneOut:
    with db.connect() as conn:
        known = {
            r["id"] for r in conn.execute(
                "SELECT id FROM assets WHERE user_id = ?", (user["id"],)
            ).fetchall()
        }
        missing = [item.asset_id for item in payload.items if item.asset_id not in known]
        if missing:
            raise HTTPException(status_code=400,
                                detail=f"unknown asset ids: {missing}")
        row = db.create_scene(
            conn, user_id=user["id"], name=payload.name,
            doc={"items": [item.model_dump() for item in payload.items]},
        )
    return _scene_out(row)


@app.get("/scenes/{scene_id}", response_model=SceneOut)
def get_scene(scene_id: str, user: dict = Depends(current_user)) -> SceneOut:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM scenes WHERE id = ? AND user_id = ?", (scene_id, user["id"])
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no such scene")
    return _scene_out(row)


@app.post("/scenes/{scene_id}/export", response_model=ExportOut)
def export_scene(scene_id: str, user: dict = Depends(current_user)) -> ExportOut:
    """Compose the scene into one self-contained HTML page, via forge3d's viewer."""
    with db.connect() as conn:
        scene = conn.execute(
            "SELECT * FROM scenes WHERE id = ? AND user_id = ?", (scene_id, user["id"])
        ).fetchone()
        if scene is None:
            raise HTTPException(status_code=404, detail="no such scene")
        doc = json.loads(scene["doc"] or "{}")
        items = doc.get("items", [])
        if not items:
            return ExportOut(ok=False, error="the scene has no items")

        resolved = []
        for item in items:
            row = conn.execute(
                "SELECT * FROM assets WHERE id = ? AND user_id = ?",
                (item["asset_id"], user["id"]),
            ).fetchone()
            if row is None or not row["glb_path"]:
                return ExportOut(ok=False, error=f"asset {item['asset_id']} has no mesh")
            resolved.append({
                "path": str(storage.resolve(row["glb_path"])),
                "name": row["name"],
                "position": item.get("position") or [0, 0, 0],
                "scale": item.get("scale", 1.0),
                "rotation": item.get("rotation") or [0, 0, 0],
            })

    from forge3d.viewer import build_scene_viewer

    settings = get_settings()
    out_dir = Path(settings.storage_dir) / user["id"] / db.new_id()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "scene.html"
    try:
        build_scene_viewer(resolved, target, title=scene["name"])
    except Exception as exc:  # noqa: BLE001 - reported to the client
        return ExportOut(ok=False, error=f"could not build the scene page: {exc}")

    asset_id = db.new_id()
    relative = storage.store(user["id"], asset_id, target, "scene")
    with db.connect() as conn:
        asset = db.create_asset(
            conn, user_id=user["id"], job_id=None, name=scene["name"],
            glb_path=None, stl_path=None, html_path=relative,
            triangles=0, size_bytes=storage.size_of(relative),
            validation={"scene": True, "items": len(resolved)},
        )
    return ExportOut(ok=True, asset_id=asset["id"], html=relative,
                     size_bytes=storage.size_of(relative))


# ---------------------------------------------------------------------------
# a tiny web page, so the API is not the only way in
# ---------------------------------------------------------------------------
_static = Path(__file__).parent / "static"
if _static.is_dir():
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")
