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

from session_store import DEFAULT_REDIS_URL


# Default schema cache lifetime: 20 minutes.
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
) -> str:
    """
    Save schema metadata to Redis and return the full Redis key.

    The payload wraps the schema with cache metadata.  The generator reads only
    the "schema" field for prompting, while the timestamps help you inspect the
    cache during debugging.
    """
    redis_key = normalize_schema_cache_key(cache_key)
    ttl = max(int(ttl_seconds), 60)
    now = datetime.now(timezone.utc).isoformat()
    envelope = {
        "schema": schema,
        "cached_at": now,
        "ttl_seconds": ttl,
    }

    try:
        client = _redis_client(redis_url)
        client.setex(redis_key, ttl, json.dumps(envelope, default=str))
    except Exception as error:
        raise SchemaCacheError(f"Could not write schema to Redis: {error}") from error

    return redis_key


def load_schema_from_cache(
    cache_key: str,
    redis_url: str = DEFAULT_REDIS_URL,
) -> dict[str, Any] | list[dict[str, Any]]:
    """
    Load schema metadata from Redis for SQL generation.

    A missing key usually means the TTL expired, Redis was restarted, or schema
    extraction has not been run yet.  In that case the caller should refresh the
    schema cache before asking the LLM to generate SQL.
    """
    redis_key = normalize_schema_cache_key(cache_key)

    try:
        client = _redis_client(redis_url)
        raw_payload = client.get(redis_key)
    except Exception as error:
        raise SchemaCacheError(f"Could not read schema from Redis: {error}") from error

    if not raw_payload:
        raise SchemaCacheError(
            f"Schema cache miss for '{redis_key}'. "
            "Refresh schema metadata before generating SQL."
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
