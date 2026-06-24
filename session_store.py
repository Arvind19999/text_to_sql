from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any


DEFAULT_REDIS_URL = "redis://localhost:6380/0"
DEFAULT_HISTORY_DATABASE_URL = (
    "postgresql://admin:admin@localhost:5432/sql_ai"
)


@dataclass(frozen=True)
class SessionTurn:
    user_instruction: str
    resolved_instruction: str
    generated_sql: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionTurn":
        return cls(
            user_instruction=str(data.get("user_instruction", "")),
            resolved_instruction=str(data.get("resolved_instruction", "")),
            generated_sql=str(data.get("generated_sql", "")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "user_instruction": self.user_instruction,
            "resolved_instruction": self.resolved_instruction,
            "generated_sql": self.generated_sql,
        }


class SessionStore:
    def __init__(
        self,
        redis_url: str = DEFAULT_REDIS_URL,
        history_database_url: str = DEFAULT_HISTORY_DATABASE_URL,
        max_recent_turns: int = 5,
        redis_ttl_seconds: int = 86400,
    ) -> None:
        self.redis_url = redis_url
        self.history_database_url = history_database_url
        self.max_recent_turns = max(max_recent_turns, 1)
        self.redis_ttl_seconds = max(redis_ttl_seconds, 60)
        self._redis = None

    def load_recent_turns(self, session_id: str) -> list[SessionTurn]:
        turns = self._load_recent_turns_from_redis(session_id)
        if turns:
            return turns

        turns = self._load_recent_turns_from_postgres(session_id)
        if turns:
            self._refresh_redis(session_id, turns)

        return turns

    def save_turn(
        self,
        session_id: str,
        turn: SessionTurn,
        driver_name: str,
        database_name: str | None = None,
    ) -> None:
        self._save_turn_to_redis(session_id, turn)
        self._save_turn_to_postgres(
            session_id=session_id,
            turn=turn,
            driver_name=driver_name,
            database_name=database_name,
        )

    def init_postgres(self) -> None:
        with self._connect_postgres() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_sessions (
                        id UUID PRIMARY KEY,
                        user_id TEXT,
                        database_name TEXT,
                        driver_name TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
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

    def _redis_key(self, session_id: str) -> str:
        return f"sql_ai:session:{session_id}:turns"

    def _history_session_id(self, session_id: str) -> str:
        try:
            return str(uuid.UUID(session_id))
        except ValueError:
            return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sql-ai-session:{session_id}"))

    def _redis_client(self):
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

    def _load_recent_turns_from_redis(self, session_id: str) -> list[SessionTurn]:
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
                continue

        return turns

    def _save_turn_to_redis(self, session_id: str, turn: SessionTurn) -> None:
        redis_client = self._redis_client()
        key = self._redis_key(session_id)
        redis_client.rpush(key, json.dumps(turn.to_dict()))
        redis_client.ltrim(key, -self.max_recent_turns, -1)
        redis_client.expire(key, self.redis_ttl_seconds)

    def _refresh_redis(self, session_id: str, turns: list[SessionTurn]) -> None:
        redis_client = self._redis_client()
        key = self._redis_key(session_id)
        redis_client.delete(key)
        for turn in turns[-self.max_recent_turns :]:
            redis_client.rpush(key, json.dumps(turn.to_dict()))
        redis_client.expire(key, self.redis_ttl_seconds)

    def _connect_postgres(self):
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

    def _load_recent_turns_from_postgres(self, session_id: str) -> list[SessionTurn]:
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
    ) -> None:
        history_session_id = self._history_session_id(session_id)

        with self._connect_postgres() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO chat_sessions (
                        id,
                        driver_name,
                        database_name,
                        updated_at
                    )
                    VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (id)
                    DO UPDATE SET
                        driver_name = EXCLUDED.driver_name,
                        database_name = EXCLUDED.database_name,
                        updated_at = CURRENT_TIMESTAMP;
                    """,
                    (history_session_id, driver_name, database_name),
                )
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
