"""HTTP API.

The rule this layer enforces: **a paid job is refused before it is submitted, not
after.** Credits are reserved with a conditional UPDATE, so two simultaneous
submits from a user with one job's worth of credit produce one job, not two.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db, storage
from .central import (
    CentralClient,
    CentralError,
    CentralUnavailable,
    InsufficientCredits,
    InvalidSession,
    NotEntitled,
    client_from_settings,
)
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
from .ratelimit import enforce_job_limit, enforce_login_limit
from .reconcile import reconcile_pending, recover_orphan_reservations
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

    # Rate-limit counters start empty on every boot, and a fresh app gets fresh
    # counters rather than inheriting whatever a previous one counted.
    application.state.limiters = {}

    # The central platform is the authority for identity and money. If studio
    # cannot reach it at boot it still starts — assets, scenes and history stay
    # readable — but every paid path below fails closed until it comes back.
    central: CentralClient = getattr(application.state, "central", None) or client_from_settings(
        settings
    )
    application.state.central = central

    try:
        application.state.reconciled = reconcile_pending(central, settings)
        # Only after reconciliation: a reservation that reconciliation has just
        # attached to a job must not be mistaken for an orphan.
        application.state.orphans = recover_orphan_reservations(central, settings)
    except CentralUnavailable:
        # Starting without reconciliation is safe: nothing is retried, and the
        # next boot will find the same pending reservations.
        application.state.reconciled = {"skipped": "central unavailable"}

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
    version="0.2.2",
    description=(
        "The product surface around forge3d: accounts, a job queue, an asset "
        "library and shareable scene pages. Geometry and validation are done by "
        "the pinned forge3d engine, never reimplemented here."
    ),
)


@app.middleware("http")
async def limit_payload(request: Request, call_next):
    """Refuse an oversized body before it is parsed.

    FastAPI would happily read a 500 MB JSON document into memory first. The
    declared length is checked up front; a body without a length is left to the
    server's own limits.
    """
    settings = get_settings()
    declared = request.headers.get("content-length")
    if declared:
        try:
            too_big = int(declared) > settings.max_payload_bytes
        except ValueError:
            too_big = False
        if too_big:
            return JSONResponse(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                content={"detail": "request body is too large"},
            )
    return await call_next(request)


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
        "central_configured": settings.central_configured,
        "reconciled": getattr(app.state, "reconciled", None),
    }


@app.get("/kinds", response_model=list[KindOut])
def list_kinds(request: Request) -> list[KindOut]:
    """What can be generated, and what the platform charges for it.

    The price is read from the platform because that is the only authority. If
    it cannot be reached the configured figures are shown with
    `authoritative: false` rather than pretending to be current.
    """
    prices: dict[str, int] = {}
    try:
        prices = central_client(request).prices()
    except CentralError:
        prices = {}

    out: list[KindOut] = []
    for name, info in KINDS.items():
        priced = name in prices
        out.append(
            KindOut(
                kind=name,
                cost=prices[name] if priced else int(info["cost"]),
                authoritative=priced,
                provider=info["provider"],
                summary=info["summary"],
            )
        )
    return out


# ---------------------------------------------------------------------------
# auth
#
# Studio no longer authenticates anyone. It forwards to the central platform,
# keeps the platform's access token in its own cookie, and asks the platform
# who a token belongs to. There is no local password, no local session table
# that grants access, and no local credit balance.
# ---------------------------------------------------------------------------
def central_client(request: Request) -> CentralClient:
    client = getattr(request.app.state, "central", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the central platform is not configured",
        )
    return client


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="authentication is temporarily unavailable, try again shortly",
    )


def current_user(request: Request) -> dict:
    """Resolve the caller against the central platform, then to a local profile.

    Fails closed: if the platform cannot confirm the session, the request is
    refused rather than assumed good.
    """
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="not signed in"
        )

    central = central_client(request)
    try:
        account = central.verify(token)
    except CentralUnavailable:
        raise _unavailable() from None
    except InvalidSession:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired"
        ) from None

    if account is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired"
        )

    with db.connect() as conn:
        profile = db.profile_for_central_account(
            conn, account["account_id"], account["email"]
        )
    return {
        "id": profile["id"],
        "email": profile["email"],
        "central_account_id": account["account_id"],
    }


@app.post("/auth/request", response_model=LoginResponse)
def request_login(payload: LoginRequest, request: Request) -> LoginResponse:
    """Ask the platform to email a magic link.

    The link lands on this application's own callback, which is registered with
    the platform — studio cannot redirect it, and neither can a caller.
    """
    settings = get_settings()
    enforce_login_limit(request, settings.login_rate_limit, settings.rate_window_seconds)

    central = central_client(request)
    try:
        result = central.request_login(payload.email)
    except CentralUnavailable:
        raise _unavailable() from None
    except CentralError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"could not start sign-in: {exc.detail}",
        ) from None

    return LoginResponse(
        sent=bool(result.get("sent")),
        dev_token=result.get("dev_token"),
        note=str(result.get("note") or "check your email for the sign-in link"),
    )


@app.get("/auth/callback")
def login_callback(token: str, request: Request, response: Response) -> dict:
    """Trade the platform's single-use link token for a session."""
    settings = get_settings()
    central = central_client(request)
    try:
        session = central.exchange(token)
    except CentralUnavailable:
        raise _unavailable() from None
    except InvalidSession:
        raise HTTPException(
            status_code=400, detail="invalid, expired or already used sign-in link"
        ) from None
    except CentralError as exc:
        raise HTTPException(status_code=502, detail=f"sign-in failed: {exc.detail}") from None

    with db.connect() as conn:
        profile = db.profile_for_central_account(
            conn, session.account_id, session.email
        )

    response.set_cookie(
        settings.session_cookie,
        session.access_token,
        httponly=True,
        samesite="lax",
        max_age=settings.session_ttl_seconds,
        secure=not settings.dev_mode,
    )
    return {"ok": True, "user_id": profile["id"], "email": profile["email"]}


@app.post("/auth/logout")
def logout(request: Request, response: Response) -> dict:
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie)
    if token:
        # A logout that cannot reach the platform still clears the cookie; the
        # platform's own expiry is the backstop.
        with suppress(CentralError):
            central_client(request).revoke(token)
    response.delete_cookie(settings.session_cookie)
    return {"ok": True}


@app.get("/me", response_model=UserOut)
def me(request: Request, user: dict = Depends(current_user)) -> UserOut:
    central = central_client(request)
    try:
        credits = central.balance(user["central_account_id"])
        features = central.entitlements(user["central_account_id"])
    except CentralUnavailable:
        # A zero balance and an unreachable billing service look identical to a
        # user, and are opposite problems. Say which one this is.
        raise _unavailable() from None
    except CentralError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"billing is not answering correctly: {exc.detail}",
        ) from None

    # Studio only knows about itself. Listing the other applications a person
    # can reach is the platform's job, not ours, and is deliberately not guessed
    # at here.
    return UserOut(
        id=user["id"],
        email=user["email"],
        credits=credits,
        apps=[get_settings().auth_billing_app_id],
        entitlements=features,
    )


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
        reservation_id=row["reservation_id"],
    )


@app.post("/jobs", response_model=JobOut, status_code=status.HTTP_201_CREATED)
def submit_job(
    payload: JobSubmit, request: Request, user: dict = Depends(current_user)
) -> JobOut:
    """Reserve on the platform, then queue locally.

    Order matters and is not negotiable: entitlement, then reservation, then the
    job row. A job is never created that is not backed by a reservation, and a
    provider is never called for a job that is not queued.
    """
    info = KINDS.get(payload.kind)
    if info is None:
        raise HTTPException(status_code=400, detail=f"unknown kind {payload.kind!r}")
    cost = int(info["cost"])

    settings = get_settings()
    enforce_job_limit(
        request, user["id"], settings.job_rate_limit, settings.rate_window_seconds
    )

    # Bounded before anything is reserved: a huge or deeply nested params object
    # is rejected here rather than after the user has been charged for it.
    raw_params = json.dumps(payload.params or {})
    if len(raw_params) > settings.max_params_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"generation parameters are too large ({len(raw_params)} bytes)",
        )

    central = central_client(request)
    account_id = user["central_account_id"]

    try:
        if settings.require_entitlement and not central.is_entitled(
            account_id, payload.kind
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"your account is not entitled to {payload.kind} generation",
            )
        key = payload.idempotency_key or db.new_id()
        reservation = central.create_reservation(
            account_id=account_id,
            kind=payload.kind,
            cost=cost,
            idempotency_key=key,
            metadata={"params": payload.params},
        )
    except HTTPException:
        raise
    except InsufficientCredits as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=exc.detail
        ) from None
    except NotEntitled as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=exc.detail
        ) from None
    except InvalidSession:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired"
        ) from None
    except CentralUnavailable:
        raise _unavailable() from None
    except CentralError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"could not reserve credits: {exc.detail}",
        ) from None

    with db.connect() as conn:
        # A retried submit resolves to the job this reservation already made.
        existing = db.job_for_reservation(conn, reservation.id)
        if existing is not None:
            return _job_out(existing)

        try:
            job = db.create_job(
                conn,
                user_id=user["id"],
                kind=payload.kind,
                params=payload.params,
                provider=info["provider"],
                # The platform's price, not studio's configured figure.
                cost=reservation.cost,
                idempotency_key=key,
                reservation_id=reservation.id,
            )
        except Exception:
            # The reservation exists but no job will run against it. Give the
            # credits back rather than stranding them.
            with suppress(CentralError):
                central.release(reservation.id, reason="job_insert_failed")
            raise
    return _job_out(job)


@app.get("/jobs", response_model=list[JobOut])
def list_jobs(user: dict = Depends(current_user), limit: int = 50,
              needs_review: bool = False,
              status: str | None = None) -> list[JobOut]:
    """Recent jobs. `needs_review=true` is the reconciliation queue.

    A job lands there when the platform kept the credits because the provider
    may have been charged — an uncertain submission, or a failure we could not
    classify. Someone has to check the provider dashboard and settle it.
    """
    clauses = ["user_id = ?"]
    params: list = [user["id"]]
    if needs_review:
        clauses.append("needs_review = 1")
    if status:
        clauses.append("status = ?")
        params.append(status)
    params.append(max(1, min(200, limit)))

    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at DESC LIMIT ?",
            params,
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
def cancel_job(
    job_id: str, request: Request, user: dict = Depends(current_user)
) -> JobOut:
    """Cancel before the provider is contacted, and release the reservation.

    A **queued** job is cancelled outright and its reservation released: nothing
    was sent, so holding the credits would be taking money for nothing. A
    **running** job is only flagged — the provider may already have charged, so
    the reservation stays put and the worker decides at the end.
    """
    central = central_client(request)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user["id"])
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no such job")

    if row["status"] == "queued":
        with db.connect() as conn:
            claimed = conn.execute(
                "UPDATE jobs SET status = 'cancelled', finished_at = ?, stage = 'cancelled' "
                "WHERE id = ? AND status = 'queued'",
                (time.time(), job_id),
            ).rowcount
        if claimed == 1 and row["reservation_id"]:
            try:
                central.release(row["reservation_id"], reason="cancelled_before_start")
            except CentralError:
                # The money stays reserved. Flag it so a human settles it rather
                # than leaving a silent charge behind.
                with db.connect() as conn:
                    db.update_job(conn, job_id, needs_review=1)
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job_out(row)

    if row["status"] == "running":
        with db.connect() as conn:
            db.update_job(conn, job_id, cancel_requested=1)
        return _job_out(row)

    raise HTTPException(
        status_code=409, detail="job is not cancellable (already finished?)"
    )


def event_frames(
    job_id: str,
    user_id: str,
    settings,
    last_event_id: str | None = None,
) -> Iterator[str]:
    """The bytes of the job event stream, as a plain generator.

    Split out from the route so it can be read a frame at a time in a test
    without holding a socket open, and so the framing rules live in one place:

    * **an event is only sent when something changed** — an idle job costs one
      database read per poll instead of a stream of identical frames;
    * **a heartbeat comment goes out during quiet periods**, because a proxy
      drops a connection that has been silent for a minute and the browser then
      reconnects forever;
    * **every frame carries an id**, and ``Last-Event-ID`` is honoured, so a
      reconnecting client resumes instead of restarting the display.
    """
    poll_seconds = max(0.005, settings.sse_poll_seconds)
    heartbeat_seconds = max(0.005, settings.sse_heartbeat_seconds)
    terminal = {"succeeded", "failed", "cancelled", "uncertain"}

    try:
        sequence = int(last_event_id or 0)
    except ValueError:
        sequence = 0

    # Ask the browser to come back, rather than letting it pick its own rate.
    yield "retry: 5000\n\n"

    last_payload: dict | None = None
    last_sent = time.monotonic()

    while True:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)
            ).fetchone()

        if row is None:
            # A job that is not yours looks exactly like one that does not
            # exist. Both end the stream; neither leaks the difference.
            yield 'data: {"error": "no such job"}\n\n'
            return

        payload = {
            "status": row["status"],
            "progress": row["progress"],
            "stage": row["stage"],
            "error": row["error"],
            "asset_id": row["asset_id"],
            "needs_review": bool(row["needs_review"]),
        }

        now = time.monotonic()
        if payload != last_payload:
            sequence += 1
            yield f"id: {sequence}\ndata: {json.dumps(payload)}\n\n"
            last_payload = payload
            last_sent = now
        elif now - last_sent >= heartbeat_seconds:
            # A comment frame: valid SSE, ignored by EventSource, enough to keep
            # the connection alive through a proxy that counts idle seconds.
            yield ": heartbeat\n\n"
            last_sent = now

        if row["status"] in terminal:
            return
        time.sleep(poll_seconds)


@app.get("/jobs/{job_id}/events")
def job_events(
    job_id: str,
    user: dict = Depends(current_user),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Server-sent progress, one direction, resumable, with heartbeats.

    `uncertain` is terminal. The provider may have accepted the job, so the
    stream must end and a human must reconcile it — a client that kept waiting
    would sit there forever.
    """
    return StreamingResponse(
        event_frames(job_id, user["id"], get_settings(), last_event_id),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            "connection": "keep-alive",
            # Stops nginx buffering the stream into uselessness.
            "x-accel-buffering": "no",
        },
    )


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
