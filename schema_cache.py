# -*- coding: utf-8 -*-
"""
Redis-backed schema cache used by the schema extractor and SQL generator.

The JSON schema file is still useful as a human-readable validation artifact,
but Redis is the runtime source used by the LLM flow when a schema cache key is
provided.  Keeping the cache helpers in one module prevents the extractor and
generator from drifting into two incompatible Redis formats.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from session_store import DEFAULT_HISTORY_DATABASE_URL, DEFAULT_REDIS_URL, SessionStore


# Default schema cache lifetime: 20 minutes.
# After this the schema must be re-populated by re-running get_schema_details.py.
DEFAULT_SCHEMA_CACHE_TTL_SECONDS = 20 * 60


class SchemaCacheError(RuntimeError):
    """Raised when schema cache reads or writes cannot be completed."""


def normalize_schema_cache_key(cache_key: str) -> str:
    """
    Return the actual Redis key used for schema metadata.

    Callers pass a business-level key such as "snowflake:tpch:lineitem".
    This function adds a namespace so schema cache keys do not collide with
    session-memory keys or unrelated Redis data.
    """
    cleaned = cache_key.strip()
    if not cleaned:
        raise SchemaCacheError("Schema cache key cannot be empty.")

    if cleaned.startswith("sql_ai:schema:"):
        return cleaned

    return f"sql_ai:schema:{cleaned}"


def _redis_client(redis_url: str):
    """
    Create a Redis client for schema cache operations.

    The import is lazy so non-cache flows can still run in environments where
    Redis is not installed.  decode_responses=True keeps JSON values as str.
    """
    try:
        from redis import Redis
    except ImportError as error:
        raise SchemaCacheError(
            "Redis schema cache requires the redis package. "
            "Install dependencies with: pip install -r requirements.txt"
        ) from error

    return Redis.from_url(redis_url, decode_responses=True)


def save_schema_to_cache(
    schema: dict[str, Any] | list[dict[str, Any]],
    cache_key: str,
    redis_url: str = DEFAULT_REDIS_URL,
    ttl_seconds: int = DEFAULT_SCHEMA_CACHE_TTL_SECONDS,
    history_database_url: str = DEFAULT_HISTORY_DATABASE_URL,
) -> str:
    """
    Save schema metadata to Redis (hot cache) and PostgreSQL (durable backup).

    Redis stores the schema with a short TTL (default 20 minutes) for fast
    reads.  PostgreSQL stores it permanently so load_schema_from_cache() can
    restore Redis automatically when the TTL expires — no manual re-run of
    get_schema_details.py needed.

    Returns the full Redis key.
    """
    redis_key = normalize_schema_cache_key(cache_key)
    ttl = max(int(ttl_seconds), 60)
    now = datetime.now(timezone.utc).isoformat()
    envelope = {
        "schema": schema,
        "cached_at": now,
        "ttl_seconds": ttl,
    }

    # Write to Redis (hot cache with TTL)
    try:
        client = _redis_client(redis_url)
        client.setex(redis_key, ttl, json.dumps(envelope, default=str))
    except Exception as error:
        raise SchemaCacheError(f"Could not write schema to Redis: {error}") from error

    # Write to PostgreSQL (durable backup — survives Redis restarts and TTL expiry)
    try:
        store = SessionStore(
            redis_url=redis_url,
            history_database_url=history_database_url,
        )
        store.init_postgres()
        store.save_schema(cache_key, schema)
    except Exception as error:
        # Non-fatal: Redis write succeeded above; PostgreSQL backup is best-effort.
        # The schema is still usable for the current session — log and continue.
        print(f"Warning: schema saved to Redis but PostgreSQL backup failed: {error}")

    return redis_key


def load_schema_from_cache(
    cache_key: str,
    redis_url: str = DEFAULT_REDIS_URL,
    history_database_url: str = DEFAULT_HISTORY_DATABASE_URL,
) -> dict[str, Any] | list[dict[str, Any]]:
    """
    Load schema metadata for SQL generation.

    Read strategy (Redis-first, PostgreSQL fallback):
    1. Try Redis — sub-millisecond read for the common case.
    2. On Redis miss (TTL expired or Redis restarted), load from PostgreSQL
       and repopulate Redis so the next request is fast again.
    3. Raise SchemaCacheError only if both stores have no data (first-ever
       run, or get_schema_details.py was never run for this cache key).
    """
    redis_key = normalize_schema_cache_key(cache_key)

    try:
        client = _redis_client(redis_url)
        raw_payload = client.get(redis_key)
    except Exception as error:
        raise SchemaCacheError(f"Could not read schema from Redis: {error}") from error

    if not raw_payload:
        # Redis miss — attempt to restore from PostgreSQL durable backup.
        schema = _load_schema_from_postgres(cache_key, history_database_url)
        if schema is not None:
            # Repopulate Redis so subsequent requests are fast again.
            _repopulate_redis(redis_key, schema, redis_url)
            return schema

        raise SchemaCacheError(
            f"Schema not found for '{cache_key}'. "
            "Run get_schema_details.py to populate the cache."
        )

    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as error:
        raise SchemaCacheError(f"Schema cache value is not valid JSON: {redis_key}") from error

    # Current cache format stores an envelope with a "schema" field.  The bare
    # schema fallback keeps the loader tolerant if a developer manually inserts
    # a schema JSON value into Redis while testing.
    if isinstance(payload, dict) and "schema" in payload:
        return payload["schema"]

    return payload


def _load_schema_from_postgres(
    cache_key: str,
    history_database_url: str,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """
    Load schema from the PostgreSQL schema_store table.  Returns None on miss.

    This is only called when Redis has no entry for the cache key.
    """
    try:
        store = SessionStore(history_database_url=history_database_url)
        return store.load_schema(cache_key)
    except Exception:
        return None


def _repopulate_redis(
    redis_key: str,
    schema: dict[str, Any] | list[dict[str, Any]],
    redis_url: str,
) -> None:
    """
    Write the schema back into Redis after a PostgreSQL fallback read.

    Restores the standard envelope format with a fresh TTL so the next read
    hits Redis again without touching PostgreSQL.  Non-fatal on failure.
    """
    try:
        ttl = DEFAULT_SCHEMA_CACHE_TTL_SECONDS
        now = datetime.now(timezone.utc).isoformat()
        envelope = {"schema": schema, "cached_at": now, "ttl_seconds": ttl}
        client = _redis_client(redis_url)
        client.setex(redis_key, ttl, json.dumps(envelope, default=str))
    except Exception:
        # Non-fatal: the schema was already returned from PostgreSQL.
        pass
