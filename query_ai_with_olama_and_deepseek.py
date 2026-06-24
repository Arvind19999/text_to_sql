# -*- coding: utf-8 -*-
"""
SQL generation helper using Ollama + deepseek-coder-v2.

This file is meant to be used from your backend/API. Your UI should pass:
1. the selected database driver/profile,
2. the user's natural-language instruction,
3. the schema/tables available for that connection.

The model should generate SQL for the selected database dialect only.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from session_store import (
    DEFAULT_HISTORY_DATABASE_URL,
    DEFAULT_REDIS_URL,
    SessionStore,
    SessionTurn,
)


MODEL_NAME = "deepseek-coder-v2:16b"
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"


DRIVER_TO_DIALECT: dict[str, str] = {
    "postgresql": "PostgreSQL",
    "rds postgresql": "PostgreSQL",
    "rds postgresql aurora": "PostgreSQL",
    "azure postgresql": "PostgreSQL",
    "azure cosmos postgresql": "PostgreSQL",
    "cockroachdb": "CockroachDB SQL, PostgreSQL-compatible",
    "mysql": "MySQL",
    "rds mysql": "MySQL",
    "rds mysql aurora": "MySQL",
    "azure mysql": "MySQL",
    "mariadb": "MariaDB SQL",
    "rds mariadb": "MariaDB SQL",
    "mssql": "Microsoft SQL Server T-SQL",
    "rds mssql": "Microsoft SQL Server T-SQL",
    "azure sql server": "Microsoft SQL Server T-SQL",
    "oracle": "Oracle SQL",
    "rds oracle": "Oracle SQL",
    "snowflake": "Snowflake SQL",
    "redshift": "Amazon Redshift SQL",
    "bigquery": "Google BigQuery Standard SQL",
    "sap hana": "SAP HANA SQL",
    "vertica": "Vertica SQL",
    "teradata": "Teradata SQL",
    "monetdb": "MonetDB SQL",
    "ibm db2": "IBM Db2 SQL",
    "rds ibm db2": "IBM Db2 SQL",
}

UNSUPPORTED_SQL_DRIVERS = {
    "mongodb",
    "cassandra",
    "casssandra",
    "couchbase",
    "azure cosmos nosql",
    "azure cosmos mongodb",
    "s3",
    "ftp",
    "sftp",
    "file",
    "upload",
}

FORBIDDEN_SQL_WORDS = {
    "alter",
    "analyze",
    "attach",
    "call",
    "copy",
    "create",
    "delete",
    "detach",
    "drop",
    "execute",
    "grant",
    "insert",
    "merge",
    "reindex",
    "replace",
    "revoke",
    "set",
    "truncate",
    "update",
    "vacuum",
}


@dataclass(frozen=True)
class Column:
    name: str
    type: str | None = None


@dataclass(frozen=True)
class Table:
    name: str
    columns: list[Column]
    schema: str | None = None


@dataclass(frozen=True)
class Relationship:
    from_schema: str | None
    from_table: str
    from_column: str
    to_schema: str | None
    to_table: str
    to_column: str


class UnsupportedDriverError(ValueError):
    """Raised when the selected driver is not a SQL database."""


class SqlGenerationError(ValueError):
    """Raised when the model output cannot be accepted as safe SQL."""


def normalize_driver(driver_name: str) -> str:
    return re.sub(r"\s+", " ", driver_name.strip().lower())


def get_dialect(driver_name: str) -> str:
    normalized = normalize_driver(driver_name)

    if normalized in UNSUPPORTED_SQL_DRIVERS:
        raise UnsupportedDriverError(
            f"{driver_name} is not a SQL driver. Use a separate NoSQL query generator."
        )

    if normalized not in DRIVER_TO_DIALECT:
        raise UnsupportedDriverError(
            f"No SQL dialect mapping found for driver: {driver_name}"
        )

    return DRIVER_TO_DIALECT[normalized]


def table_to_prompt(table: Table) -> str:
    table_name = f"{table.schema}.{table.name}" if table.schema else table.name
    columns = ", ".join(
        f"{column.name} {column.type}" if column.type else column.name
        for column in table.columns
    )
    return f"- {table_name}({columns})"


def extract_schema_payload(schema: list[dict[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(schema, dict):
        return schema.get("tables", [])

    return schema


def parse_schema(schema: list[dict[str, Any]] | dict[str, Any]) -> list[Table]:
    tables: list[Table] = []

    for item in extract_schema_payload(schema):
        columns = [
            Column(
                name=str(column["name"]),
                type=str(column["type"]) if column.get("type") else None,
            )
            for column in item.get("columns", [])
            if column.get("name")
        ]

        if item.get("name") and columns:
            tables.append(
                Table(
                    name=str(item["name"]),
                    schema=str(item["schema"]) if item.get("schema") else None,
                    columns=columns,
                )
            )

    return tables


def parse_relationships(schema: list[dict[str, Any]] | dict[str, Any]) -> list[Relationship]:
    if not isinstance(schema, dict):
        return []

    relationships: list[Relationship] = []
    for item in schema.get("relationships", []):
        if not all(
            item.get(key)
            for key in ("from_table", "from_column", "to_table", "to_column")
        ):
            continue

        relationships.append(
            Relationship(
                from_schema=str(item["from_schema"]) if item.get("from_schema") else None,
                from_table=str(item["from_table"]),
                from_column=str(item["from_column"]),
                to_schema=str(item["to_schema"]) if item.get("to_schema") else None,
                to_table=str(item["to_table"]),
                to_column=str(item["to_column"]),
            )
        )

    return relationships


def build_schema_context(tables: list[Table], relationships: list[Relationship]) -> str:
    if not tables:
        raise ValueError("Schema context is empty. At least one table is required.")

    table_context = "\n".join(table_to_prompt(table) for table in tables)

    if not relationships:
        return table_context

    relationship_lines = []
    for relationship in relationships:
        from_table = (
            f"{relationship.from_schema}.{relationship.from_table}"
            if relationship.from_schema
            else relationship.from_table
        )
        to_table = (
            f"{relationship.to_schema}.{relationship.to_table}"
            if relationship.to_schema
            else relationship.to_table
        )
        relationship_lines.append(
            f"- {from_table}.{relationship.from_column} -> "
            f"{to_table}.{relationship.to_column}"
        )

    return (
        f"Tables:\n{table_context}\n\n"
        f"Relationships:\n" + "\n".join(relationship_lines)
    )


def build_messages(
    user_instruction: str,
    driver_name: str,
    tables: list[Table],
    relationships: list[Relationship] | None = None,
    database_name: str | None = None,
) -> list[dict[str, str]]:
    dialect = get_dialect(driver_name)
    schema_context = build_schema_context(tables, relationships or [])
    database_line = f"Database: {database_name}\n" if database_name else ""

    system_prompt = """
Generate exactly one read-only SQL query.
Return SQL only, no markdown and no explanation.
Use only the provided tables, columns, and relationships.
If the request cannot be answered from the schema, return:
-- cannot answer from schema
Use joins, CTEs, nested queries, grouping, windows, or unions when useful.
Do not generate INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, MERGE, GRANT, REVOKE, COPY, CALL, EXECUTE, or stored procedure code.
""".strip()

    user_prompt = f"""
SQL dialect: {dialect}
Driver: {driver_name}
{database_line}
Available schema:
{schema_context}

User instruction:
{user_instruction}
""".strip()

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def remove_markdown_fences(text: str) -> str:
    cleaned = text.strip()

    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:sql)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    return cleaned.strip()


def validate_read_only_sql(sql: str) -> None:
    cleaned = remove_markdown_fences(sql)
    lowered = re.sub(r"--.*?$|/\*.*?\*/", " ", cleaned.lower(), flags=re.S | re.M)
    first_word = re.match(r"^\s*([a-z]+)", lowered)

    if not first_word or first_word.group(1) not in {"select", "with"}:
        raise SqlGenerationError(
            "The request did not produce a read-only SQL query. "
            "This tool only generates SELECT/WITH SQL from the provided schema."
        )

    found_forbidden = FORBIDDEN_SQL_WORDS.intersection(
        re.findall(r"\b[a-z_]+\b", lowered)
    )
    if found_forbidden:
        words = ", ".join(sorted(found_forbidden))
        raise SqlGenerationError(
            f"Generated SQL was blocked because it contains unsafe keyword(s): {words}."
        )


def chat_with_ollama(
    model: str,
    messages: list[dict[str, str]],
    options: dict[str, Any] | None = None,
    timeout: int = 600,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": options or {},
    }
    request = Request(
        OLLAMA_CHAT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except TimeoutError as error:
        raise TimeoutError(
            f"Ollama did not finish within {timeout} seconds. "
            "Try again, increase --timeout, or use a smaller/faster model."
        ) from error
    except URLError as error:
        raise ConnectionError(
            "Could not connect to Ollama. Make sure Ollama is running on "
            "http://localhost:11434 and the model is pulled."
        ) from error


def format_turns_for_follow_up(turns: list[SessionTurn]) -> str:
    formatted_turns = []
    for index, turn in enumerate(turns, start=1):
        formatted_turns.append(
            f"""
Turn {index}
User request: {turn.user_instruction}
Resolved instruction: {turn.resolved_instruction}
Generated SQL:
{turn.generated_sql}
""".strip()
        )

    return "\n\n".join(formatted_turns)


def resolve_follow_up_instruction(
    user_instruction: str,
    previous_turns: list[SessionTurn],
    model: str = MODEL_NAME,
    timeout: int = 600,
) -> str:
    if not previous_turns:
        return user_instruction

    system_prompt = """
Rewrite the current user request into one standalone SQL-generation instruction.
Use previous turns only to resolve references like now, same, that, it, include, remove, filter, group, or sort.
If the current request is already standalone, return it unchanged.
Do not generate SQL.
Do not explain.
Do not invent tables, columns, values, or business rules.
Return only the rewritten instruction.
""".strip()

    user_prompt = f"""
Previous turns:
{format_turns_for_follow_up(previous_turns)}

Current user request:
{user_instruction}
""".strip()

    response = chat_with_ollama(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        options={
            "temperature": 0.0,
            "top_p": 0.9,
            "num_predict": 256,
        },
        timeout=timeout,
    )

    resolved_instruction = remove_markdown_fences(response["message"]["content"])
    return resolved_instruction or user_instruction


def generate_sql(
    user_instruction: str,
    driver_name: str,
    schema: list[dict[str, Any]] | dict[str, Any],
    database_name: str | None = None,
    model: str = MODEL_NAME,
    timeout: int = 600,
) -> str:
    tables = parse_schema(schema)
    relationships = parse_relationships(schema)
    messages = build_messages(
        user_instruction=user_instruction,
        driver_name=driver_name,
        tables=tables,
        relationships=relationships,
        database_name=database_name,
    )

    response = chat_with_ollama(
        model=model,
        messages=messages,
        options={
            "temperature": 0.1,
            "top_p": 0.9,
            "num_predict": 512,
        },
        timeout=timeout,
    )

    sql = remove_markdown_fences(response["message"]["content"])
    if sql != "-- cannot answer from schema":
        validate_read_only_sql(sql)

    return sql


def generate_sql_with_session(
    session_id: str,
    user_instruction: str,
    driver_name: str,
    schema: list[dict[str, Any]] | dict[str, Any],
    session_store: SessionStore,
    database_name: str | None = None,
    model: str = MODEL_NAME,
    timeout: int = 600,
) -> tuple[str, str]:
    session_store.init_postgres()
    previous_turns = session_store.load_recent_turns(session_id)
    resolved_instruction = resolve_follow_up_instruction(
        user_instruction=user_instruction,
        previous_turns=previous_turns,
        model=model,
        timeout=timeout,
    )
    sql = generate_sql(
        user_instruction=resolved_instruction,
        driver_name=driver_name,
        schema=schema,
        database_name=database_name,
        model=model,
        timeout=timeout,
    )

    session_store.save_turn(
        session_id=session_id,
        turn=SessionTurn(
            user_instruction=user_instruction,
            resolved_instruction=resolved_instruction,
            generated_sql=sql,
        ),
        driver_name=driver_name,
        database_name=database_name,
    )

    return sql, resolved_instruction


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate SQL using Ollama.")
    parser.add_argument("--driver", required=True, help="Database driver name.")
    parser.add_argument("--question", required=True, help="Natural-language request.")
    parser.add_argument("--schema-file", required=True, help="JSON schema file path.")
    parser.add_argument("--database", help="Optional database name.")
    parser.add_argument("--model", default=MODEL_NAME, help="Ollama model name.")
    parser.add_argument(
        "--session-id",
        help="Conversation/session id. Enables Redis + PostgreSQL follow-up memory.",
    )
    parser.add_argument(
        "--redis-url",
        default=DEFAULT_REDIS_URL,
        help=f"Redis URL for active session context. Default: {DEFAULT_REDIS_URL}",
    )
    parser.add_argument(
        "--history-db-url",
        default=DEFAULT_HISTORY_DATABASE_URL,
        help=(
            "PostgreSQL URL for permanent chat history. "
            f"Default: {DEFAULT_HISTORY_DATABASE_URL}"
        ),
    )
    parser.add_argument(
        "--history-turns",
        type=int,
        default=5,
        help="Number of recent turns to use for follow-up resolution. Default: 5.",
    )
    parser.add_argument(
        "--session-ttl",
        type=int,
        default=86400,
        help="Redis session TTL in seconds. Default: 86400.",
    )
    parser.add_argument(
        "--show-resolved",
        action="store_true",
        help="Print the resolved standalone instruction before the SQL.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Ollama request timeout in seconds. Default: 600.",
    )
    args = parser.parse_args()

    with open(args.schema_file, "r", encoding="utf-8") as file:
        schema = json.load(file)

    try:
        if args.session_id:
            session_store = SessionStore(
                redis_url=args.redis_url,
                history_database_url=args.history_db_url,
                max_recent_turns=args.history_turns,
                redis_ttl_seconds=args.session_ttl,
            )
            sql, resolved_instruction = generate_sql_with_session(
                session_id=args.session_id,
                user_instruction=args.question,
                driver_name=args.driver,
                schema=schema,
                session_store=session_store,
                database_name=args.database,
                model=args.model,
                timeout=args.timeout,
            )
        else:
            resolved_instruction = args.question
            sql = generate_sql(
                user_instruction=args.question,
                driver_name=args.driver,
                schema=schema,
                database_name=args.database,
                model=args.model,
                timeout=args.timeout,
            )
    except (TimeoutError, ConnectionError, RuntimeError, SqlGenerationError) as error:
        raise SystemExit(f"Error: {error}") from error
    except UnsupportedDriverError as error:
        raise SystemExit(f"Unsupported driver: {error}") from error
    except ValueError as error:
        raise SystemExit(f"Invalid request: {error}") from error

    if args.show_resolved:
        print(f"-- resolved instruction: {resolved_instruction}")

    print(sql)


if __name__ == "__main__":
    main()
