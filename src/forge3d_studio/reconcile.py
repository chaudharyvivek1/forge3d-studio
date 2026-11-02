"""What a restart has to settle.

A job holds a reservation on the central platform from the moment it is queued.
If the process dies while that job is `running`, the money is still reserved and
nobody knows whether the provider was charged.

The rule this module applies, and the only rule that is safe:

    **never resubmit paid work that may already have been accepted.**

So a job found `running` after a restart is not requeued. Its reservation is
flagged for reconciliation and the job is marked `uncertain`. A job still
`queued` is left alone — nothing was ever sent for it, so the worker can simply
pick it up.

Every decision is written to the log with the job id and reservation id, because
this is the code someone will read at 2am after a crash.
"""

from __future__ import annotations

import logging
import time

from . import db
from .central import CentralClient, CentralError, CentralUnavailable

logger = logging.getLogger("forge3d_studio.reconcile")


def reconcile_pending(central: CentralClient, settings=None) -> dict[str, int]:
    """Classify every job that still holds a reservation.

    Returns counts per outcome. Stops early if the platform is unreachable:
    leaving rows untouched is always safe, and the next boot will try again.
    """
    counts = {
        "checked": 0,
        "queued": 0,
        "flagged": 0,
        "released": 0,
        "settled": 0,
        "committed": 0,
        "unreachable": 0,
    }

    with db.connect() as conn:
        jobs = db.jobs_with_pending_reservations(conn)

    for job in jobs:
        counts["checked"] += 1
        reservation_id = job["reservation_id"]

        try:
            reservation = central.get_reservation(reservation_id)
        except CentralUnavailable:
            counts["unreachable"] += 1
            logger.warning(
                "reconcile stopped: central platform unreachable, %s job(s) left as they were",
                len(jobs) - counts["checked"] + 1,
            )
            return counts
        except CentralError as exc:
            counts["unreachable"] += 1
            logger.warning(
                "reconcile: could not read reservation %s for job %s (%s)",
                reservation_id,
                job["id"],
                exc,
            )
            continue

        if reservation.state == "refunded":
            # The money is already back. Make the job say so.
            with db.connect() as conn:
                db.update_job(
                    conn,
                    job["id"],
                    status="failed",
                    stage="reconciled",
                    error="reservation was released while the process was down",
                    finished_at=time.time(),
                    refunded=1,
                    needs_review=0,
                )
            counts["released"] += 1
            logger.info(
                "reconcile: job %s reservation %s was refunded, marked failed",
                job["id"],
                reservation_id,
            )

        elif job["status"] == "succeeded":
            # The asset is delivered. What is missing is the commit, so retry
            # it — the platform's commit is idempotent. Credits are never
            # released for work the user already has.
            if reservation.state == "settled":
                with db.connect() as conn:
                    db.update_job(conn, job["id"], needs_review=0)
                counts["settled"] += 1
                logger.info(
                    "reconcile: job %s was already committed upstream", job["id"]
                )
                continue
            try:
                state, changed = central.commit(reservation_id)
            except CentralError as exc:
                counts["unreachable"] += 1
                logger.warning(
                    "reconcile: commit retry failed for job %s (%s); it stays flagged",
                    job["id"],
                    exc,
                )
                continue
            with db.connect() as conn:
                db.update_job(conn, job["id"], needs_review=0)
            counts["committed"] += 1
            logger.info(
                "reconcile: committed reservation %s for delivered job %s (%s)",
                reservation_id,
                job["id"],
                state,
            )

        elif reservation.state == "settled":
            # Credits are spent but no result reached us. Do not guess.
            with db.connect() as conn:
                db.update_job(
                    conn,
                    job["id"],
                    status="uncertain",
                    stage="reconciled",
                    error="reservation settled but no local result; check the provider",
                    needs_review=1,
                )
            counts["settled"] += 1
            logger.warning(
                "reconcile: job %s reservation %s settled without a local result",
                job["id"],
                reservation_id,
            )

        elif reservation.state == "needs_review":
            with db.connect() as conn:
                db.update_job(conn, job["id"], needs_review=1)
            counts["flagged"] += 1
            logger.info(
                "reconcile: job %s already awaiting review", job["id"]
            )

        else:  # reserved
            if job["status"] == "queued":
                # Nothing was ever submitted for it. The worker may pick it up.
                counts["queued"] += 1
                continue

            # It was running when the process died: the provider may have been
            # called and may have been paid. Flag, never retry.
            try:
                central.flag(reservation_id, reason="worker_died_while_running")
            except CentralError as exc:
                logger.warning(
                    "reconcile: could not flag reservation %s (%s)", reservation_id, exc
                )
            with db.connect() as conn:
                db.update_job(
                    conn,
                    job["id"],
                    status="uncertain",
                    stage="reconciled",
                    error=(
                        "the worker stopped while this job was running; the provider "
                        "may have accepted it, so it was not retried"
                    ),
                    needs_review=1,
                )
            counts["flagged"] += 1
            logger.warning(
                "reconcile: job %s was running at shutdown, reservation %s flagged",
                job["id"],
                reservation_id,
            )

    logger.info("reconcile finished: %s", counts)
    return counts


def reclaim_stale_jobs(central: CentralClient, settings=None) -> dict[str, int]:
    """Reclaim jobs whose worker stopped heartbeating mid-flight.

    This is the running-process case, not the restart case: another worker is
    still alive, a lease expired, and the job it was running is now in limbo.

    The job is **never** put back in the queue. For a paid kind the provider may
    already have been called, so it becomes `uncertain` and its reservation is
    flagged for review. A free kind failed safely, so it is simply marked failed.
    """
    counts = {"stale": 0, "flagged": 0, "failed": 0}
    with db.connect() as conn:
        stale = db.stale_running_jobs(conn)

    for job in stale:
        counts["stale"] += 1
        cost = int(job["cost"] or 0)
        reservation_id = job["reservation_id"]

        if reservation_id and cost:
            try:
                central.flag(reservation_id, reason="worker_lease_expired")
            except CentralError as exc:
                logger.warning(
                    "reclaim: could not flag reservation %s for job %s (%s)",
                    reservation_id,
                    job["id"],
                    exc,
                )
            status_value, review = "uncertain", 1
            detail = (
                "the worker stopped heartbeating while this job was running; the "
                "provider may have accepted it, so it was not retried"
            )
        else:
            status_value, review = "failed", 0
            detail = "the worker stopped heartbeating while this job was running"

        with db.connect() as conn:
            db.update_job(
                conn,
                job["id"],
                status=status_value,
                stage="reclaimed",
                error=detail,
                finished_at=time.time(),
                needs_review=review,
            )
        counts["flagged" if review else "failed"] += 1
        logger.warning(
            "reclaim: job %s lease expired on worker %s", job["id"], job["worker_id"]
        )

    return counts


def recover_orphan_reservations(central: CentralClient, settings) -> dict[str, int]:
    """Release reservations that were created but never attached to a job.

    The window this closes is small and real: studio reserves, the platform
    succeeds, and then the process dies before the job row is inserted. The
    credits are held against work that will never exist.

    Two guards keep this from becoming a way to lose money:

    * **a grace period.** A reservation younger than ``orphan_grace_seconds`` is
      left alone, because it may belong to a submit that is still in flight.
    * **a local job is decisive.** If any job row references the reservation,
      this sweep does not touch it — the normal reconciliation owns it, and a
      reservation with a job may already have reached a provider.

    A reservation with no job row cannot have reached a provider, because only a
    claimed job runs a handler. That is what makes releasing it safe.
    """
    grace = float(getattr(settings, "orphan_grace_seconds", 300.0))
    cutoff = time.time() - grace
    counts = {"candidates": 0, "released": 0, "skipped": 0, "failed": 0}

    try:
        orphans = central.reservations_for_app(state="reserved", older_than=cutoff)
    except CentralUnavailable:
        logger.warning("orphan sweep skipped: central platform unreachable")
        return counts
    except CentralError as exc:
        logger.warning("orphan sweep skipped: %s", exc)
        return counts

    for reservation in orphans:
        counts["candidates"] += 1

        # Defence in depth: the query asked for `reserved`, but a page could
        # have been produced by a platform that ignores the filter, and a
        # settled or flagged reservation must never be released here.
        if reservation.state != "reserved":
            counts["skipped"] += 1
            continue

        with db.connect() as conn:
            job = db.job_for_reservation(conn, reservation.id)
        if job is not None:
            # Attached after all. The job path decides what happens to it.
            counts["skipped"] += 1
            continue
        try:
            central.release(reservation.id, reason="orphan_no_job")
        except CentralUnavailable:
            # The platform went away mid-sweep. Stop: the rest of the list is
            # still valid and a later run will pick it up.
            counts["failed"] += 1
            logger.warning(
                "orphan sweep stopped early: platform unreachable after %s of %s",
                counts["candidates"],
                len(orphans),
            )
            return counts
        except CentralError as exc:
            counts["failed"] += 1
            logger.warning(
                "orphan sweep: could not release %s (%s)", reservation.id, exc
            )
            continue
        counts["released"] += 1
        logger.warning(
            "orphan sweep: released reservation %s (%s, %s credits) — no job was "
            "ever attached to it",
            reservation.id,
            reservation.kind,
            reservation.cost,
        )

    if counts["candidates"]:
        logger.info(
            "orphan sweep finished: %d processed, %d released, %d skipped, %d failed",
            counts["candidates"],
            counts["released"],
            counts["skipped"],
            counts["failed"],
        )
    return counts


__all__ = ["reclaim_stale_jobs", "reconcile_pending", "recover_orphan_reservations"]
