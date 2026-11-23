"""
Where a SaaS keeps what seoprobe produces: OAuth tokens per tenant and audits.

seoprobe stores nothing itself. It asks for a token store (any object with three async methods)
and hands back results you can save as JSON. Two token stores are shown: Postgres, which this
example uses, and Redis, which you can swap in.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg
import redis.asyncio as redis
from cryptography.fernet import Fernet

import seoprobe

# --------------------------------------------------------------------------- token stores


class PostgresTokenStore:
    """
    One encrypted row per tenant (the OAuth "account" is the tenant id). seoprobe calls these
    methods when a tenant connects Google, when an access token is refreshed and when a revoked
    token is dropped.
    """

    def __init__(self, pool: asyncpg.Pool, fernet: Fernet) -> None:
        self.pool, self.fernet = pool, fernet

    async def get(self, account: str) -> dict[str, Any] | None:
        raw = await self.pool.fetchval("SELECT record FROM google_tokens WHERE account = $1", account)
        return json.loads(self.fernet.decrypt(raw)) if raw is not None else None

    async def set(self, account: str, record: dict[str, Any]) -> None:
        await self.pool.execute(
            """
            INSERT INTO google_tokens (account, record) VALUES ($1, $2)
            ON CONFLICT (account) DO UPDATE SET record = EXCLUDED.record, updated_at = now()
            """,
            account,
            self.fernet.encrypt(json.dumps(record).encode()),
        )

    async def delete(self, account: str) -> None:
        await self.pool.execute("DELETE FROM google_tokens WHERE account = $1", account)


class RedisTokenStore:
    """The same contract on Redis, for teams that keep session-like data there."""

    def __init__(self, client: redis.Redis, fernet: Fernet, prefix: str = "seoprobe:google-token:") -> None:
        self.client, self.fernet, self.prefix = client, fernet, prefix

    async def get(self, account: str) -> dict[str, Any] | None:
        raw = await self.client.get(self.prefix + account)
        return json.loads(self.fernet.decrypt(raw)) if raw is not None else None

    async def set(self, account: str, record: dict[str, Any]) -> None:
        await self.client.set(self.prefix + account, self.fernet.encrypt(json.dumps(record).encode()))

    async def delete(self, account: str) -> None:
        await self.client.delete(self.prefix + account)


# --------------------------------------------------------------------------- pending OAuth requests


class PendingAuthRequests:
    """
    The OAuth flow is stateless on seoprobe's side: ``gsc.auth_url`` returns an ``AuthRequest``
    that you keep until Google redirects back. Redis with a TTL is a good home: the callback may
    land on any API instance, and an abandoned sign-in cleans itself up.
    """

    def __init__(self, client: redis.Redis, ttl_s: int = 600) -> None:
        self.client, self.ttl_s = client, ttl_s

    async def save(self, request: seoprobe.AuthRequest) -> None:
        await self.client.set(f"seoprobe:oauth:{request.state}", request.model_dump_json(), ex=self.ttl_s)

    async def take(self, state: str) -> seoprobe.AuthRequest | None:
        raw = await self.client.getdel(f"seoprobe:oauth:{state}")  # single use
        return seoprobe.AuthRequest.model_validate_json(raw) if raw else None


# --------------------------------------------------------------------------- audits


@dataclass
class AuditRow:
    id: str
    tenant: str
    url: str
    mode: str
    max_pages: int | None
    status: str
    stage: str | None
    error_code: str | None
    result: dict[str, Any] | None


class Audits:
    """The audits table: a job row the API creates and the worker fills in."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def create(self, tenant: str, url: str, mode: str, max_pages: int | None) -> str:
        audit_id = str(uuid.uuid4())
        await self.pool.execute(
            "INSERT INTO audits (id, tenant, url, mode, max_pages) VALUES ($1, $2, $3, $4, $5)",
            uuid.UUID(audit_id),
            tenant,
            url,
            mode,
            max_pages,
        )
        return audit_id

    async def get(self, audit_id: str) -> AuditRow | None:
        row = await self.pool.fetchrow("SELECT * FROM audits WHERE id = $1", uuid.UUID(audit_id))
        return _row(row) if row else None

    async def previous(self, row: AuditRow) -> AuditRow | None:
        """The tenant's last finished audit of the same URL before this one: the report compares with it."""
        found = await self.pool.fetchrow(
            """
            SELECT * FROM audits WHERE tenant = $1 AND url = $2 AND status = 'done' AND id <> $3
              AND created_at < (SELECT created_at FROM audits WHERE id = $3)
            ORDER BY created_at DESC LIMIT 1
            """,
            row.tenant,
            row.url,
            uuid.UUID(row.id),
        )
        return _row(found) if found else None

    async def running(self, audit_id: str) -> None:
        await self.pool.execute("UPDATE audits SET status = 'running' WHERE id = $1", uuid.UUID(audit_id))

    async def stage(self, audit_id: str, stage: str) -> None:
        await self.pool.execute("UPDATE audits SET stage = $2 WHERE id = $1", uuid.UUID(audit_id), stage)

    async def finish(self, audit_id: str, result: seoprobe.Result) -> None:  # type: ignore[type-arg]
        await self.pool.execute(
            """
            UPDATE audits SET status = $2, error_code = $3, result = $4::jsonb, finished_at = now()
            WHERE id = $1
            """,
            uuid.UUID(audit_id),
            "done" if result.ok else "failed",
            result.error.code.value if result.error else None,
            result.to_json(),
        )


def _row(r: asyncpg.Record) -> AuditRow:
    result = r["result"]
    return AuditRow(
        id=str(r["id"]),
        tenant=r["tenant"],
        url=r["url"],
        mode=r["mode"],
        max_pages=r["max_pages"],
        status=r["status"],
        stage=r["stage"],
        error_code=r["error_code"],
        result=json.loads(result) if result else None,
    )
