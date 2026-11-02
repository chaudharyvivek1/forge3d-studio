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


class ProviderNotConfigured(RuntimeError):
    """No provider or key, so nothing was ever sent. A refund is safe here."""


class ProviderRejected(RuntimeError):
    """The provider refused the request outright, so nothing was produced."""


class UncertainSubmission(RuntimeError):
    """The provider may have accepted — and charged for — this job.

    Never refund automatically: the work may exist and have cost real money.
    Reconcile with the provider using `idempotency_key` first.
    """

    def __init__(self, idempotency_key: str | None, detail: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(detail)


def _require_provider() -> str:
    """Fail before the network if no provider is configured.

    Checking first is what makes the refund policy honest: we can only refund
    without risk when we know for certain nothing was submitted.
    """
    from forge3d.config import get_settings as engine_settings
    from forge3d.providers import registry as provider_registry

    selected = (engine_settings().provider or "auto").strip().lower()
    providers = provider_registry.list_providers()
    by_name = {p["name"]: p for p in providers}

    if selected in ("", "auto"):
        available = [p["name"] for p in providers if p["available"]]
        if available:
            return available[0]
        needed = ", ".join(f"{p['name']} ({p['key_env']})" for p in providers[:3])
        raise ProviderNotConfigured(
            "no text-to-3D provider is configured. Set FORGE3D_PROVIDER and its key "
            f"— for example {needed}. Nothing was sent, so no credits were spent."
        )

    provider = by_name.get(selected)
    if provider is None:
        raise ProviderNotConfigured(
            f"unknown provider {selected!r}; known: {', '.join(sorted(by_name))}"
        )
    if not provider["available"]:
        raise ProviderNotConfigured(
            f"provider {selected!r} is not configured: set {provider['key_env']}"
        )
    return selected


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
    provider_name = _require_provider()
    progress(0.05, f"submitting to {provider_name}")
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
        # The provider may have created the job and charged for it. This is not a
        # failure, and it is certainly not a refund.
        raise UncertainSubmission(
            produced.get("idempotency_key") or job.get("idempotency_key"),
            "submission uncertain: the provider may have accepted this job "
            f"(key {produced.get('idempotency_key') or job.get('idempotency_key')}). "
            "Check the provider dashboard before resubmitting.",
        )
    if not produced.get("ok"):
        # A clean rejection: the engine says the provider refused it, so nothing
        # was produced and the credits go back.
        raise ProviderRejected(produced.get("error") or "provider generation failed")
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
        """Record the outcome, and decide about money.

        The refund rule is the whole point of this method, so it is written out
        rather than inferred:

        | outcome                        | refund | why                                   |
        |--------------------------------|--------|---------------------------------------|
        | cancelled while queued         | yes    | done in `request_cancel`, not here    |
        | cancelled while running        | no     | the provider may already have charged |
        | not configured                 | yes    | nothing was sent                      |
        | provider rejected it           | yes    | nothing was produced                  |
        | submission uncertain           | **no** | it may exist and have been billed     |
        | anything else, paid            | **no** | we cannot prove nothing was charged   |

        The last two rows are the ones that matter: refunding an uncertain
        submission hands out provider work for free, and an unexpected exception
        tells us nothing about whether the remote job exists.
        """
        cost = int(job.get("cost") or 0)
        cancelled = isinstance(exc, JobCancelled)
        uncertain = isinstance(exc, UncertainSubmission)
        safe_to_refund = isinstance(exc, (ProviderNotConfigured, ProviderRejected))

        if cancelled:
            status, refund, review = "cancelled", False, False
            detail = "cancelled by the client"
        elif uncertain:
            status, refund, review = "uncertain", False, True
            detail = str(exc)
        elif safe_to_refund:
            status, refund, review = "failed", bool(cost), False
            detail = str(exc)
        else:
            # Unknown failure on a paid job: assume the money may be spent.
            status = "failed"
            refund = False
            review = bool(cost)
            detail = f"{type(exc).__name__}: {exc}"

        with db.connect() as conn:
            if refund and cost:
                db.refund_credits(conn, job["user_id"], cost)
            db.update_job(
                conn, job["id"], status=status, stage=status, error=detail[:2000],
                finished_at=time.time(), refunded=1 if refund else 0,
                needs_review=1 if review else 0,
            )
        if not cancelled and not uncertain:
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
