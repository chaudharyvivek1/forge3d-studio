"""The worker: claim a job, run forge3d, record the asset.

Deliberately a thread pool in the API process, not Celery and not a broker. The
queue is the `jobs` table, so a restart resumes whatever was queued and a crash
loses nothing. `claim_next_job` is a conditional UPDATE, which is the same
guarantee `SELECT ... FOR UPDATE` gives.

Studio never builds geometry itself: every handler below calls forge3d's public
API and stores what it returns.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from . import db, storage
from .config import Settings, get_settings

#: Kinds a client may submit, their credit cost, and whether they need a provider.
#:
#: Cost is charged **before** the work starts (see `db.spend_credits`), so a user
#: at zero credits cannot reach a paid provider by racing the check.
KINDS: dict[str, dict] = {
    "shape": {"cost": 0, "provider": "local",
              "summary": "a parametric shape from the equation library"},
    "planet": {"cost": 0, "provider": "local",
               "summary": "a procedural planet with baked PBR textures"},
    "provider": {"cost": 25, "provider": "cloud",
                 "summary": "text-to-3D through a hosted provider (needs a key)"},
}


class JobCancelled(RuntimeError):
    """Raised inside a handler when the client asked to stop."""


def _engine():
    """Import forge3d lazily so importing studio does not cost 13 s of trimesh."""
    from forge3d import server as engine_server

    return engine_server


def _copy_asset(job: dict, user_id: str, asset_id: str, produced: dict) -> dict:
    """Move whatever the engine produced into studio's own storage."""
    glb = storage.store(user_id, asset_id, produced["path"], "model") if produced.get("path") else None
    stl = storage.store(user_id, asset_id, produced["stl"], "model") if produced.get("stl") else None
    html = None
    if produced.get("viewer") and Path(produced["viewer"]).is_file():
        html = storage.store(user_id, asset_id, produced["viewer"], "viewer")
    return {"glb_path": glb, "stl_path": stl, "html_path": html}


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------
def _handle_shape(job: dict, params: dict, progress: Callable[[float, str], None]) -> dict:
    progress(0.1, "building")
    produced = _engine().create_shape(
        shape=params.get("shape", "supershape"),
        params=params.get("params") or {},
        resolution=int(params.get("resolution", 96)),
        name=params.get("name") or f"{params.get('shape', 'shape')}",
        purpose=params.get("purpose", "game"),
        render_previews=False,      # previews are a studio-side concern, later
        make_viewer=True,
    )
    if not produced.get("ok"):
        raise RuntimeError(produced.get("error") or "generation failed")
    progress(0.7, "validated")
    return produced


def _handle_planet(job: dict, params: dict, progress: Callable[[float, str], None]) -> dict:
    progress(0.1, "building")
    produced = _engine().create_planet(
        radius=float(params.get("radius", 100)),
        seed=int(params.get("seed", 0)),
        subdivisions=int(params.get("subdivisions", 5)),
        canyon=bool(params.get("canyon", False)),
        texture_resolution=int(params.get("texture_resolution", 256)),
        name=params.get("name") or "planet",
        render_previews=False,
        make_viewer=True,
    )
    if not produced.get("ok"):
        raise RuntimeError(produced.get("error") or "generation failed")

    # The engine reports a planet's face count under `planet`, not `card`. Studio
    # stores one shape for every asset, so normalise here rather than teaching the
    # database about each engine return shape.
    planet = produced.get("planet") or {}
    produced["card"] = {
        "triangles": int(planet.get("faces") or 0),
        "extents": planet.get("extents"),
    }
    progress(0.7, "validated")
    return produced


def _handle_provider(job: dict, params: dict, progress: Callable[[float, str], None]) -> dict:
    """Paid path. Credits were already reserved by the caller."""
    progress(0.05, "submitting")
    produced = _engine().create_model(
        prompt=params.get("prompt", ""),
        name=params.get("name") or "provider-model",
        backend="hosted",
        mode="cloud",
        references=params.get("references") or [],
        idempotency_key=job.get("idempotency_key"),
        render_previews=False,
        make_viewer=True,
    )
    if produced.get("uncertain"):
        # The provider may have created the job. Never retry automatically.
        raise RuntimeError(
            f"submission uncertain (key {produced.get('idempotency_key')}); "
            "check the provider before resubmitting"
        )
    if not produced.get("ok"):
        raise RuntimeError(produced.get("error") or "provider generation failed")
    progress(0.7, "downloaded")
    return produced


HANDLERS: dict[str, Callable[[dict, dict, Callable[[float, str], None]], dict]] = {
    "shape": _handle_shape,
    "planet": _handle_planet,
    "provider": _handle_provider,
}


# ---------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------
class Worker:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    # -- one job ---------------------------------------------------------
    def process_one(self) -> bool:
        """Claim and run a single job. Returns False when the queue is empty."""
        with db.connect() as conn:
            row = db.claim_next_job(conn)
            if row is None:
                return False
            job = dict(row)

        try:
            self._run(conn_job=job)
        except Exception as exc:  # noqa: BLE001 - a job failure is data, not a crash
            self._fail(job, exc)
        return True

    def _run(self, conn_job: dict) -> None:
        job_id = conn_job["id"]
        user_id = conn_job["user_id"]
        params = json.loads(conn_job["params"] or "{}")
        kind = conn_job["kind"]

        handler = HANDLERS.get(kind)
        if handler is None:
            raise RuntimeError(f"unknown job kind {kind!r}")

        def progress(value: float, stage: str) -> None:
            with db.connect() as conn:
                if db.job_is_cancelled(conn, job_id):
                    raise JobCancelled("cancelled by the client")
                db.update_job(conn, job_id, progress=round(value, 3), stage=stage)

        progress(0.01, "starting")
        produced = handler(conn_job, params, progress)

        progress(0.9, "storing")
        asset_id = db.new_id()
        paths = _copy_asset(conn_job, user_id, asset_id, produced)
        card = produced.get("card") or {}
        validation = produced.get("validation") or {}

        with db.connect() as conn:
            asset = db.create_asset(
                conn,
                user_id=user_id,
                job_id=job_id,
                name=params.get("name") or kind,
                glb_path=paths["glb_path"],
                stl_path=paths["stl_path"],
                html_path=paths["html_path"],
                triangles=int(card.get("triangles") or 0),
                size_bytes=storage.size_of(paths["glb_path"]) if paths["glb_path"] else 0,
                validation=validation if isinstance(validation, dict) else {"summary": str(validation)},
            )
            db.update_job(conn, job_id, status="succeeded", progress=1.0,
                          stage="done", asset_id=asset["id"], finished_at=time.time())

    def _fail(self, job: dict, exc: Exception) -> None:
        cancelled = isinstance(exc, JobCancelled)
        status = "cancelled" if cancelled else "failed"
        detail = str(exc) if not isinstance(exc, JobCancelled) else "cancelled by the client"
        with db.connect() as conn:
            # A paid job that produced nothing is refunded: charging for a failure
            # the user cannot see the cause of is a support ticket, not a business.
            if not cancelled and job.get("cost"):
                db.refund_credits(conn, job["user_id"], int(job["cost"]))
            db.update_job(conn, job["id"], status=status, stage=status,
                          error=detail[:2000], finished_at=time.time())
        if not cancelled:
            traceback.print_exc()

    # -- the pool --------------------------------------------------------
    def run_forever(self, poll_seconds: float = 0.5) -> None:
        while not self._stop.is_set():
            if not self.process_one():
                self._stop.wait(poll_seconds)

    def start(self, threads: int | None = None) -> list[threading.Thread]:
        count = threads or self.settings.worker_threads
        for index in range(count):
            thread = threading.Thread(target=self.run_forever,
                                      name=f"studio-worker-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)
        return self._threads

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads.clear()
