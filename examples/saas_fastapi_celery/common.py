"""
Settings, database schema and token encryption shared by the API (app.py) and the worker
(worker.py). Everything comes from environment variables, so the same code runs on a laptop
(docker-compose.yml) and in production.
"""

from __future__ import annotations

import os

import asyncpg
from cryptography.fernet import Fernet

import seoprobe

DATABASE_URL = os.environ.get("SAAS_DATABASE_URL", "postgresql://seoprobe:seoprobe@localhost:15432/seoprobe")
REDIS_URL = os.environ.get("SAAS_REDIS_URL", "redis://localhost:16379/0")
# Encrypts every OAuth token at rest. Generate one with Fernet.generate_key() and keep it in your
# secret manager; losing it means every customer has to reconnect Google.
TOKEN_KEY = os.environ.get("SAAS_TOKEN_KEY", "")
PUBLIC_URL = os.environ.get("SAAS_PUBLIC_URL", "http://127.0.0.1:8000")  # where Google sends users back

# How long the work may take. The worker's hard limit sits above the audit's own deadline, so
# seoprobe always gets to finish and report (partially) before Celery kills the task.
AUDIT_TIMEOUT_S = 600.0
LINK_CHECK_BUDGET_S = 20.0  # background audits can afford to wait for the link checks
VISIBILITY_BUDGET_S = 15.0
QUICK_CHECK_TIMEOUT_S = 20.0  # the synchronous API endpoint must answer fast

SCHEMA = """
CREATE TABLE IF NOT EXISTS audits (
    id          uuid PRIMARY KEY,
    tenant      text NOT NULL,
    url         text NOT NULL,
    mode        text NOT NULL,
    max_pages   int,
    status      text NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
    stage       text,                             -- the last stage seoprobe finished
    error_code  text,
    result      jsonb,                            -- the whole seoprobe Result, as JSON
    created_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS audits_tenant_url ON audits (tenant, url, created_at DESC);
CREATE TABLE IF NOT EXISTS google_tokens (
    account    text PRIMARY KEY,                  -- one row per tenant
    record     bytea NOT NULL,                    -- Fernet-encrypted JSON, never plain text
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""


def seoprobe_config() -> seoprobe.Config:
    """API keys and limits from SEOPROBE_* variables (SEOPROBE_GOOGLE_API_KEY, ...)."""
    return seoprobe.Config.from_env()


def fernet() -> Fernet:
    if not TOKEN_KEY:
        raise RuntimeError("set SAAS_TOKEN_KEY (Fernet.generate_key()) before storing OAuth tokens")
    return Fernet(TOKEN_KEY.encode())


async def connect() -> asyncpg.Pool:
    """A connection pool, and the tables if they do not exist yet (use real migrations in production)."""
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
    return pool
