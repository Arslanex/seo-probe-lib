"""
The Celery worker: runs the long audits away from the API.

    celery -A worker worker --loglevel=info

Why a Session per task: a seoprobe Session belongs to the event loop that opened it, and Celery
runs each task in its own ``asyncio.run``. Opening the session inside the task is therefore the
correct (and simple) choice; an audit lasts tens of seconds, so the cost of a fresh connection
pool is negligible. Temporal works the same way: put the body of ``_run`` in an activity.
"""

from __future__ import annotations

import asyncio
from typing import Any

from celery import Celery
from common import (
    AUDIT_TIMEOUT_S,
    LINK_CHECK_BUDGET_S,
    REDIS_URL,
    TOKEN_KEY,
    VISIBILITY_BUDGET_S,
    connect,
    fernet,
    seoprobe_config,
)
from stores import Audits, PostgresTokenStore

import seoprobe

celery_app = Celery("seoprobe_saas", broker=REDIS_URL)
celery_app.conf.update(
    task_acks_late=True,  # a crashed worker hands the job to another one ...
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # ... and one worker does not hoard long audits
    task_soft_time_limit=AUDIT_TIMEOUT_S + 30,  # above seoprobe's own deadline, so it can finish
    task_time_limit=AUDIT_TIMEOUT_S + 60,
)


@celery_app.task(name="seoprobe.run_audit")
def run_audit(audit_id: str) -> str:
    return asyncio.run(_run(audit_id))


async def _run(audit_id: str) -> str:
    pool = await connect()
    try:
        audits = Audits(pool)
        job = await audits.get(audit_id)
        if job is None or job.status in ("done", "failed"):  # redelivered after it finished: nothing to do
            return "skipped"
        await audits.running(audit_id)

        async def progress(event: seoprobe.Event) -> None:  # what the API's status endpoint shows
            if event.kind == "progress" and event.data.get("status") == "done" and event.data.get("stage"):
                await audits.stage(audit_id, event.data["stage"])

        store = PostgresTokenStore(pool, fernet()) if TOKEN_KEY else None
        async with seoprobe.Session(seoprobe_config(), token_store=store, on_event=progress) as session:
            options: dict[str, Any] = {
                "session": session,
                "timeout_s": AUDIT_TIMEOUT_S,
                "link_check_budget_s": LINK_CHECK_BUDGET_S,
                "visibility_budget_s": VISIBILITY_BUDGET_S,
                "account": job.tenant,  # the audit reads Search Console through the tenant's own connection
            }
            if job.mode == "site":
                result = await seoprobe.audit_site(job.url, max_pages=job.max_pages, **options)
            else:
                result = await seoprobe.audit_page(job.url, **options)
        await audits.finish(audit_id, result)  # the whole Result, as JSON: the report is built from it later
        return result.status.value
    finally:
        await pool.close()
