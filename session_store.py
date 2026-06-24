"""
session_store.py — Redis + PostgreSQL dual-layer session/history store.

Purpose
-------
This module manages conversation history for the SQL AI assistant across
multiple user turns.  It uses two storage backends that serve different roles:

  Redis (hot cache)
  -----------------
  - Stores the most recent N turns per session as a JSON list in a Redis list
    key ("sql_ai:session:<id>:turns").
  - Fast sub-millisecond reads for active conversations.
  - TTL-based expiry (default 24 h) automatically evicts idle sessions.
  - Used as the primary read source; if a hit is found here PostgreSQL is not
    queried at all.

  PostgreSQL (durable store)
  --------------------------
  - Stores every turn permanently in two tables: chat_sessions and chat_turns.
  - Survives Redis restarts, evictions, and server reboots.
  - Acts as the fallback when Redis has no data for a session (cold cache on
    first start, after a Redis flush, or after TTL expiry).
  - After a cold-cache PostgreSQL read, the turns are written back into Redis
    ("cache warming") so subsequent reads are fast again.

Typical read path for a returning session
------------------------------------------
  1. _load_recent_turns_from_redis() → hit → return immediately.
  2. Miss → _load_recent_turns_from_postgres() → rows found.
  3. _refresh_redis() warms the cache with those rows.
  4. Return turns.

Write path (every turn)
-----------------------
  _save_turn_to_redis()    — append + trim + reset TTL
  _save_turn_to_postgres() — upsert session row + insert turn row
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any


# ---
# Default connection URLs
#
# These match the Docker Compose setup shipped with this project.
# Override them via the constructor or the CLI --redis-url / --history-db-url flags.
# ---

DEFAULT_REDIS_URL = "redis://localhost:6380/0"
DEFAULT_HISTORY_DATABASE_URL = (
    "postgresql://admin:admin@localhost:5432/sql_ai"
)


# ---
# Data transfer object — one conversation turn
# ---


@dataclass(frozen=True)
class SessionTurn:
    """
    An immutable snapshot of a single user↔assistant exchange.

    Fields
    ------
    user_instruction      : The raw text the user typed.
    resolved_instruction  : The instruction after follow-up references have been
                            resolved into a standalone form (may equal
                            user_instruction for the first turn).
    generated_sql         : The validated SQL that was returned to the user.
    """

    user_instruction: str
    resolved_instruction: str
    generated_sql: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionTurn":
        """
        Deserialise a SessionTurn from a plain dict (e.g. parsed from JSON).

        Missing keys default to empty string so partial data never causes a
        KeyError.
        """
        return cls(
            user_instruction=str(data.get("user_instruction", "")),
            resolved_instruction=str(data.get("resolved_instruction", "")),
            generated_sql=str(data.get("generated_sql", "")),
        )

    def to_dict(self) -> dict[str, str]:
        """Serialise a SessionTurn to a plain dict suitable for json.dumps()."""
        return {
            "user_instruction": self.user_instruction,
            "resolved_instruction": self.resolved_instruction,
            "generated_sql": self.generated_sql,
        }


# ---
# SessionStore — the main class
# ---


class SessionStore:
    """
    Dual-layer session and history store backed by Redis and PostgreSQL.

    Usage
    -----
    store = SessionStore()
    store.init_postgres()                         # create tables once
    turns = store.load_recent_turns(session_id)   # read history
    store.save_turn(session_id, turn, driver, db) # write history

    Thread safety: Redis and psycopg connections are per-request (not shared
    across threads).  The lazy Redis client (`self._redis`) is fine for
    single-threaded async or process-per-request deployments.  For multi-
    threaded servers create one SessionStore instance per thread/worker.
    """

    def __init__(
        self,
        redis_url: str = DEFAULT_REDIS_URL,
        history_database_url: str = DEFAULT_HISTORY_DATABASE_URL,
        max_recent_turns: int = 5,
        redis_ttl_seconds: int = 86400,
    ) -> None:
        """
        Initialise the store with connection parameters.

        Parameters
        ----------
        redis_url             : Redis connection URL (redis://host:port/db).
        history_database_url  : PostgreSQL DSN / connection URL.
        max_recent_turns      : Maximum number of turns to load/store in Redis
                                and to pass to the follow-up resolution model.
                                Minimum enforced: 1.
        redis_ttl_seconds     : Idle TTL for Redis list keys.  Keys older than
                                this are evicted automatically.  Minimum: 60 s.
        """
        self.redis_url = redis_url
        self.history_database_url = history_database_url
        # Clamp to sensible minimum values
        self.max_recent_turns = max(max_recent_turns, 1)
        self.redis_ttl_seconds = max(redis_ttl_seconds, 60)
        # Lazy Redis client — created on first use in _redis_client()
        self._redis = None

    # ---
    # Public API
    # ---

    def load_recent_turns(self, session_id: str) -> list[SessionTurn]:
        """
        Load up to `max_recent_turns` recent turns for `session_id`.

        Read strategy (Redis-first):
        1. Try Redis — O(1) list slice, no SQL.
        2. On cache miss, fall back to PostgreSQL — slower but always consistent.
        3. If PostgreSQL returns data, warm the Redis cache before returning.

        Returns an empty list for a brand-new session (no error raised).
        """
        # Fast path: Redis cache hit
        turns = self._load_recent_turns_from_redis(session_id)
        if turns:
            return turns

        # Slow path: cold cache — fetch from durable PostgreSQL storage
        turns = self._load_recent_turns_from_postgres(session_id)
        if turns:
            # Warm the Redis cache so the next request is fast
            self._refresh_redis(session_id, turns)

        return turns

    def save_turn(
        self,
        session_id: str,
        turn: SessionTurn,
        driver_name: str,
        database_name: str | None = None,
        schema_cache_key: str | None = None,
    ) -> None:
        """
        Persist a completed turn to both Redis and PostgreSQL.

        Redis write  : append → trim to max_recent_turns → reset TTL.
        Postgres write: upsert the chat_sessions row (updates driver/db/schema
                        context/timestamp) and insert a new chat_turns row.

        Both writes happen for every turn to keep the two stores in sync.
        """
        self._save_turn_to_redis(session_id, turn)
        self._save_turn_to_postgres(
            session_id=session_id,
            turn=turn,
            driver_name=driver_name,
            database_name=database_name,
            schema_cache_key=schema_cache_key,
        )

    def init_postgres(self) -> None:
        """
        Create the chat_sessions and chat_turns tables if they do not exist.

        This method is idempotent (safe to call on every startup) because it
        uses CREATE TABLE IF NOT EXISTS.  It should be called once before any
        read or write operations.

        Schema
        ------
        chat_sessions — one row per logical conversation session.
          id           UUID PRIMARY KEY — the normalised session UUID.
          memory_key   TEXT             — original readable session id.
          schema_cache_key TEXT         — Redis schema key used by this chat.
          user_id      TEXT             — optional external user identifier.
          database_name TEXT            — the database being queried.
          driver_name   TEXT            — the database driver/dialect.
          created_at   TIMESTAMP        — when the session was first seen.
          updated_at   TIMESTAMP        — refreshed on every save_turn() call.

        chat_turns — one row per user↔assistant exchange.
          id                   BIGSERIAL PRIMARY KEY — auto-increment row id.
          session_id           UUID REFERENCES chat_sessions(id).
          user_instruction     TEXT — raw user text.
          resolved_instruction TEXT — standalone resolved instruction.
          generated_sql        TEXT — validated SQL returned to the user.
          created_at           TIMESTAMP — when this turn was saved.
        """
        with self._connect_postgres() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_sessions (
                        id UUID PRIMARY KEY,
                        memory_key TEXT,
                        schema_cache_key TEXT,
                        user_id TEXT,
                        database_name TEXT,
                        driver_name TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                # Older local databases may already have chat_sessions without
                # these metadata columns. Add them idempotently so the readable
                # memory key and Redis schema key become visible without
                # dropping existing chat history.
                cursor.execute(
                    """
                    ALTER TABLE chat_sessions
                    ADD COLUMN IF NOT EXISTS memory_key TEXT;
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE chat_sessions
                    ADD COLUMN IF NOT EXISTS schema_cache_key TEXT;
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_chat_sessions_memory_key
                    ON chat_sessions (memory_key);
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_chat_sessions_schema_cache_key
                    ON chat_sessions (schema_cache_key);
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_turns (
                        id BIGSERIAL PRIMARY KEY,
                        session_id UUID REFERENCES chat_sessions(id),
                        user_instruction TEXT NOT NULL,
                        resolved_instruction TEXT NOT NULL,
                        generated_sql TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )

    # ---
    # Key / ID helpers
    # ---

    def _redis_key(self, session_id: str) -> str:
        """
        Build the Redis list key for a session's turn history.

        Format: sql_ai:session:<session_id>:turns

        Using a namespaced key avoids collisions with other applications that
        share the same Redis instance.
        """
        return f"sql_ai:session:{session_id}:turns"

    def _history_session_id(self, session_id: str) -> str:
        """
        Normalise an arbitrary session_id string into a valid UUID for
        PostgreSQL storage.

        Two cases:
        1. session_id is already a valid UUID string → parse and re-serialise to
           ensure canonical form (lower-case, hyphens).
        2. session_id is an opaque string (e.g. "user-123-chat") → derive a
           deterministic UUID v5 from it using NAMESPACE_URL so the same
           string always maps to the same UUID.  This avoids inserting
           duplicate rows while still accepting free-form session identifiers
           from callers.
        """
        try:
            # Try to treat it as a standard UUID — returns canonical form
            return str(uuid.UUID(session_id))
        except ValueError:
            # Derive a stable UUID from the opaque string
            return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sql-ai-session:{session_id}"))

    # ---
    # Redis client factory
    # ---

    def _redis_client(self):
        """
        Return the lazily-initialised Redis client.

        The client is created once on first call and reused thereafter.
        decode_responses=True ensures all values come back as str (not bytes).

        Raises RuntimeError if the `redis` package is not installed.
        """
        if self._redis is None:
            try:
                from redis import Redis
            except ImportError as error:
                raise RuntimeError(
                    "Redis support requires the redis package. "
                    "Install dependencies with: pip install -r requirements.txt"
                ) from error

            self._redis = Redis.from_url(self.redis_url, decode_responses=True)

        return self._redis

    # ---
    # Redis read/write helpers
    # ---

    def _load_recent_turns_from_redis(self, session_id: str) -> list[SessionTurn]:
        """
        Fetch the last `max_recent_turns` entries from the Redis list for this
        session.

        Uses LRANGE with negative indices so only the tail of the list is
        returned even if the list is longer (Redis LTRIM keeps it trimmed but
        we use the slice here as a defensive measure too).

        Silently skips entries whose JSON cannot be parsed (corrupt data).
        Returns an empty list when the key does not exist.
        """
        redis_client = self._redis_client()
        rows = redis_client.lrange(
            self._redis_key(session_id),
            -self.max_recent_turns,
            -1,
        )

        turns = []
        for row in rows:
            try:
                turns.append(SessionTurn.from_dict(json.loads(row)))
            except json.JSONDecodeError:
                # Skip malformed entries rather than crashing the whole request
                continue

        return turns

    def _save_turn_to_redis(self, session_id: str, turn: SessionTurn) -> None:
        """
        Append a turn to the Redis list, trim to the rolling window, and
        refresh the TTL.

        RPUSH   — append to the tail (chronological order).
        LTRIM   — keep only the last max_recent_turns entries.
        EXPIRE  — reset TTL so active sessions never expire mid-conversation.
        """
        redis_client = self._redis_client()
        key = self._redis_key(session_id)
        redis_client.rpush(key, json.dumps(turn.to_dict()))
        redis_client.ltrim(key, -self.max_recent_turns, -1)
        redis_client.expire(key, self.redis_ttl_seconds)

    def _refresh_redis(self, session_id: str, turns: list[SessionTurn]) -> None:
        """
        Warm (or re-warm) the Redis cache for a session from a list of turns.

        Called when load_recent_turns() falls back to PostgreSQL so that the
        next request for the same session hits Redis.

        Steps:
        1. DELETE the existing key (stale or empty).
        2. RPUSH each turn in chronological order (oldest first).
        3. EXPIRE with the configured TTL.

        Only the most recent max_recent_turns entries are written — avoids
        inflating Redis with more history than the model will ever use.
        """
        redis_client = self._redis_client()
        key = self._redis_key(session_id)
        redis_client.delete(key)
        for turn in turns[-self.max_recent_turns :]:
            redis_client.rpush(key, json.dumps(turn.to_dict()))
        redis_client.expire(key, self.redis_ttl_seconds)

    # ---
    # PostgreSQL connection factory
    # ---

    def _connect_postgres(self):
        """
        Return a new psycopg (v3) or psycopg2 connection to the history DB.

        Tries psycopg (v3) first; falls back to psycopg2 for environments that
        have the older driver installed.  Raises RuntimeError if neither is
        available.
        """
        try:
            import psycopg

            return psycopg.connect(self.history_database_url)
        except ImportError:
            try:
                import psycopg2
            except ImportError as error:
                raise RuntimeError(
                    "PostgreSQL history support requires psycopg. "
                    "Install dependencies with: pip install -r requirements.txt"
                ) from error

            return psycopg2.connect(self.history_database_url)

    # ---
    # PostgreSQL read/write helpers
    # ---

    def _load_recent_turns_from_postgres(self, session_id: str) -> list[SessionTurn]:
        """
        Fetch the most recent `max_recent_turns` turns from PostgreSQL for the
        given session.

        Query strategy:
        - ORDER BY id DESC LIMIT N  → fetches the N newest rows efficiently
          using the BIGSERIAL primary key index.
        - reversed(rows)            → re-orders them oldest-first before
          returning so callers receive turns in chronological order.

        Returns an empty list when the session has no stored turns.
        """
        history_session_id = self._history_session_id(session_id)

        with self._connect_postgres() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT user_instruction, resolved_instruction, generated_sql
                    FROM chat_turns
                    WHERE session_id = %s
                    ORDER BY id DESC
                    LIMIT %s;
                    """,
                    (history_session_id, self.max_recent_turns),
                )
                rows = cursor.fetchall()

        # reversed() restores chronological (oldest → newest) order
        return [
            SessionTurn(
                user_instruction=row[0],
                resolved_instruction=row[1],
                generated_sql=row[2],
            )
            for row in reversed(rows)
        ]

    def _save_turn_to_postgres(
        self,
        session_id: str,
        turn: SessionTurn,
        driver_name: str,
        database_name: str | None = None,
        schema_cache_key: str | None = None,
    ) -> None:
        """
        Persist a turn to PostgreSQL with an upsert on the session row.

        Two SQL operations inside a single transaction:

        1. INSERT ... ON CONFLICT DO UPDATE on chat_sessions:
           - Creates the session row on first write.
           - Stores both ids:
             * id is a UUID-safe stable version for joins.
             * memory_key is the original readable session id from the app.
           - On subsequent writes, updates driver_name, database_name,
             schema_cache_key, and updated_at so the session row always
             reflects the latest context.

        2. INSERT into chat_turns:
           - Appends the new turn row with the session FK.
           - The BIGSERIAL id preserves insertion order for later pagination.
        """
        history_session_id = self._history_session_id(session_id)

        with self._connect_postgres() as connection:
            with connection.cursor() as cursor:
                # Upsert the parent session row — creates or refreshes metadata
                cursor.execute(
                    """
                    INSERT INTO chat_sessions (
                        id,
                        memory_key,
                        schema_cache_key,
                        driver_name,
                        database_name,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (id)
                    DO UPDATE SET
                        memory_key = EXCLUDED.memory_key,
                        schema_cache_key = EXCLUDED.schema_cache_key,
                        driver_name = EXCLUDED.driver_name,
                        database_name = EXCLUDED.database_name,
                        updated_at = CURRENT_TIMESTAMP;
                    """,
                    (
                        history_session_id,
                        session_id,
                        schema_cache_key,
                        driver_name,
                        database_name,
                    ),
                )
                # Insert the turn — always a new row, never updated
                cursor.execute(
                    """
                    INSERT INTO chat_turns (
                        session_id,
                        user_instruction,
                        resolved_instruction,
                        generated_sql
                    )
                    VALUES (%s, %s, %s, %s);
                    """,
                    (
                        history_session_id,
                        turn.user_instruction,
                        turn.resolved_instruction,
                        turn.generated_sql,
                    ),
                )
