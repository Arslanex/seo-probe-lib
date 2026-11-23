"""
11 - Embedding seoprobe in your system: your token store, threads, processes, queues.

    python examples/11_embedding.py [url]

seoprobe is a component: it keeps no state between calls, owns no scheduler or database, and
works the same inside an async app, a thread pool or a process pool.

* Your own ``TokenStore``: three async methods; back them with your database (encrypt tokens).
* Threads: each thread runs its own event loop, with its own (or a temporary) session.
* Processes: ``Result`` and ``SeoprobeError`` pickle, so a process pool or a task queue can
  carry them; open the ``Session`` inside the worker, never before forking.
* JSON: ``to_json()`` / ``model_validate_json()`` for queues and databases that store text.
* Events: an ``on_event`` hook sees every operation start, progress and finish.
* Budgets and deadlines are per call: the caller decides how long to wait.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any

import seoprobe

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.python.org/"


class DictTokenStore:
    """
    Stands in for a database table: account -> token record. Any object with these three
    async methods is a TokenStore; nothing else is required.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def get(self, account: str) -> dict[str, Any] | None:
        return self.rows.get(account)  # SELECT record FROM google_tokens WHERE account = $1

    async def set(self, account: str, record: dict[str, Any]) -> None:
        self.rows[account] = record  # INSERT ... ON CONFLICT (account) DO UPDATE

    async def delete(self, account: str) -> None:
        self.rows.pop(account, None)


def in_a_thread(url: str) -> str:
    """A worker thread: its own event loop, a temporary session (no session= passed)."""
    res = asyncio.run(seoprobe.onpage(url))
    return f"{url} -> {res.status.value}, {len(res.findings)} findings"


def in_a_process(url: str) -> seoprobe.Result:  # type: ignore[type-arg]
    """A worker process (spawned): the Result travels back to the parent by pickle."""
    return asyncio.run(seoprobe.fetch(url))


def fails_in_a_process(url: str) -> Any:
    return asyncio.run(seoprobe.fetch(url)).unwrap()  # raises SeoprobeError in the worker


async def main() -> None:
    # Your store, your events, your budgets.
    events: list[str] = []

    async def on_event(ev: seoprobe.Event) -> None:
        if ev.kind != "progress":
            events.append(f"{ev.kind} {ev.operation} {ev.status.value if ev.status else ''}".strip())

    store = DictTokenStore()
    async with seoprobe.Session(seoprobe.Config(), token_store=store, on_event=on_event) as s:
        conn = (await seoprobe.gsc.connection(account="tenant-42", session=s)).unwrap()
        print(f"token store in use; tenant-42 connected={conn.connected}")

        t0 = time.monotonic()
        res = await seoprobe.audit_page(URL, link_check_budget_s=4, visibility_budget_s=4, timeout_s=120, session=s)
        print(
            f"audit with 4 s budgets: {res.status.value} in {time.monotonic() - t0:.0f} s, score {res.unwrap().scores.overall}"
        )
        print(f"events: {len(events)}, e.g. {events[:3]}")

        # Store it as text, load it back later (another process, another day).
        text = res.to_json()
        back = seoprobe.Result.model_validate_json(text)  # data comes back as a dict here;
        audit = seoprobe.Audit.model_validate(back.data)  # validate it into its model when you need it
        print(
            f"JSON round trip: {len(text) // 1024} KB, same: {back.to_dict() == res.to_dict()}, audit score {audit.scores.overall if audit.scores else None}"
        )
        print(
            f"pickle round trip: {len(pickle.dumps(res)) // 1024} KB, same: {pickle.loads(pickle.dumps(res)).to_dict() == res.to_dict()}"
        )

    # A session belongs to the event loop that opened it: another loop gets a clear error.
    shared = seoprobe.Session()
    await shared.__aenter__()
    try:
        await asyncio.to_thread(lambda: asyncio.run(seoprobe.fetch(URL, session=shared)))
    except RuntimeError as exc:
        print(f"session used from another thread's loop -> RuntimeError: {str(exc)[:70]}...")
    finally:
        await shared.__aexit__(None, None, None)

    # Threads, each with its own loop.
    with ThreadPoolExecutor(max_workers=3) as pool:
        for out in pool.map(in_a_thread, [URL, "https://example.com/", "https://www.ruby-lang.org/en/"]):
            print(f"thread: {out}")

    # Processes: results and errors cross the boundary.
    with ProcessPoolExecutor(max_workers=2, mp_context=mp.get_context("spawn")) as pool:
        for r in pool.map(in_a_process, [URL, "https://example.com/"]):
            page = r.unwrap()
            print(f"process: {page.final_url} HTTP {page.status}, title {page.parsed.title if page.parsed else None!r}")
        try:
            pool.submit(fails_in_a_process, "ftp://example.com/").result()
        except seoprobe.SeoprobeError as exc:
            print(f"process: SeoprobeError arrived in the parent, code={exc.code.value}")


if __name__ == "__main__":
    asyncio.run(main())
