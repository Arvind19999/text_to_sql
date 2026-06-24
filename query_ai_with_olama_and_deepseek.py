# -*- coding: utf-8 -*-
"""
SQL generation helper using Ollama + deepseek-coder-v2.

Full generation + validation flow
----------------------------------
1. The caller supplies a driver name, a natural-language instruction, and a
   JSON schema (tables + optional foreign-key relationships).
2. `generate_sql()` builds a [system, user] prompt, calls the local Ollama
   server, strips markdown fences from the response, then validates the result
   through three layers — fail-fast, in order:

     a. Read-only keyword check  (`validate_read_only_sql`)
        Pure regex; always runs; no external dependency.

     b. Syntax parse via sqlglot  (`validate_sql_syntax`)
        Dialect-aware AST parse.  Silently skipped if sqlglot is not installed
        or if the driver has no matching sqlglot dialect.

     c. Live EXPLAIN round-trip  (`validate_sql_with_explain`)
        Executes EXPLAIN <sql> against the real database via SQLAlchemy.
        Silently skipped when no connection_string is supplied or when
        sqlalchemy is not installed.

3. If any validation layer raises SqlGenerationError, the failed SQL and the
   error message are appended to the message history and the model is asked to
   self-correct.  This retry loop runs up to `max_retries` times.

4. `generate_sql_with_session()` wraps the above with Redis + PostgreSQL
   session history so the model can resolve follow-up references ("same
   filter", "now group by X") across multiple turns.

5. A CLI (`main()`) exposes all parameters for quick terminal testing.

This module is designed to be imported by a backend/API layer.  Your UI should
supply the driver name, natural-language instruction, and schema for each
request.
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

# ---
# Model + Ollama endpoint configuration
# ---

# Local Ollama model tag.  Override via --model on the CLI.
MODEL_NAME = "deepseek-coder-v2:16b"

# Ollama chat completions endpoint (non-streaming).
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"

# ---
# Driver → human-readable SQL dialect
#
# Used to tell the model which SQL dialect to target.  Keys are normalised
# (lower-cased, whitespace collapsed) driver names as they arrive from the UI.
# ---

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

# ---
# Driver → sqlglot dialect identifier
#
# sqlglot uses its own internal dialect names.  Drivers that have no matching
# sqlglot dialect are simply absent from this dict; validate_sql_syntax() will
# skip them silently.
# ---

DRIVER_TO_SQLGLOT_DIALECT: dict[str, str] = {
    "postgresql": "postgres",
    "rds postgresql": "postgres",
    "rds postgresql aurora": "postgres",
    "azure postgresql": "postgres",
    "azure cosmos postgresql": "postgres",
    "cockroachdb": "postgres",
    "mysql": "mysql",
    "rds mysql": "mysql",
    "rds mysql aurora": "mysql",
    "azure mysql": "mysql",
    "mariadb": "mysql",
    "rds mariadb": "mysql",
    "mssql": "tsql",
    "rds mssql": "tsql",
    "azure sql server": "tsql",
    "oracle": "oracle",
    "rds oracle": "oracle",
    "snowflake": "snowflake",
    "redshift": "redshift",
    "bigquery": "bigquery",
    "teradata": "teradata",
    # sap hana, vertica, monetdb, ibm db2 are not supported by sqlglot — skipped
}

# ---
# EXPLAIN support groupings
#
# Different engines require different syntax to validate a statement without
# actually executing it.  We group normalised driver names so that
# validate_sql_with_explain() can pick the right approach.
# ---

# Databases that support the standard  EXPLAIN <sql>  syntax.
EXPLAIN_STANDARD_DRIVERS: set[str] = {
    "postgresql", "rds postgresql", "rds postgresql aurora",
    "azure postgresql", "azure cosmos postgresql", "cockroachdb",
    "mysql", "rds mysql", "rds mysql aurora", "azure mysql",
    "mariadb", "rds mariadb", "redshift", "snowflake", "vertica", "monetdb",
}

# SQL Server / T-SQL: uses  SET PARSEONLY ON  to parse without executing.
EXPLAIN_MSSQL_DRIVERS: set[str] = {
    "mssql", "rds mssql", "azure sql server",
}

# Oracle: uses  EXPLAIN PLAN FOR <sql>  which writes to PLAN_TABLE without
# running the query.
EXPLAIN_ORACLE_DRIVERS: set[str] = {
    "oracle", "rds oracle",
}

# Drivers where no EXPLAIN-style validation is feasible via SQLAlchemy.
# For these we fall back to sqlglot-only syntax checking.
EXPLAIN_UNSUPPORTED_DRIVERS: set[str] = {
    "bigquery", "sap hana", "teradata", "ibm db2", "rds ibm db2",
}

# ---
# Drivers that do not speak SQL at all (NoSQL, file-based, object storage …)
# generate_sql() raises UnsupportedDriverError for these immediately.
# ---

UNSUPPORTED_SQL_DRIVERS: set[str] = {
    "mongodb",
    "cassandra",
    "casssandra",           # common typo — kept for backwards compatibility
    "couchbase",
    "azure cosmos nosql",
    "azure cosmos mongodb",
    "s3",
    "ftp",
    "sftp",
    "file",
    "upload",
}

# ---
# Forbidden SQL keywords for the read-only guard
#
# After stripping comments, if any of these words appear in the generated SQL
# it is rejected.  Stripping comments first prevents the model from hiding
# forbidden words in comment blocks.
# ---

FORBIDDEN_SQL_WORDS: set[str] = {
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

# ---
# SQLAlchemy engine cache
#
# SQLAlchemy engines are expensive to create because they initialise a
# connection pool.  We cache one engine per connection string so subsequent
# calls to validate_sql_with_explain() reuse the same pool.
# ---

_engine_cache: dict[str, Any] = {}

# ---
# Immutable schema data model
# ---


@dataclass(frozen=True)
class Column:
    """A single column with a name and an optional SQL data type."""
    name: str
    type: str | None = None


@dataclass(frozen=True)
class Table:
    """A database table with its containing schema (namespace) and column list."""
    name: str
    columns: list[Column]
    schema: str | None = None


@dataclass(frozen=True)
class Relationship:
    """A foreign-key relationship linking two columns across (optionally) two schemas."""
    from_schema: str | None
    from_table: str
    from_column: str
    to_schema: str | None
    to_table: str
    to_column: str


# ---
# Custom exceptions
# ---


class UnsupportedDriverError(ValueError):
    """Raised when the selected driver is not a SQL database (e.g. MongoDB, S3)."""


class SqlGenerationError(ValueError):
    """Raised when the model output cannot be accepted as safe, valid SQL."""


# ---
# Driver normalisation helpers
# ---


def normalize_driver(driver_name: str) -> str:
    """Return a canonical lower-cased, single-space driver name for dict lookups."""
    return re.sub(r"\s+", " ", driver_name.strip().lower())


def get_dialect(driver_name: str) -> str:
    """
    Map a driver name to its human-readable SQL dialect label.

    Raises UnsupportedDriverError for NoSQL drivers or any driver name not
    present in DRIVER_TO_DIALECT.
    """
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


# ---
# Schema prompt builders
# ---


def table_to_prompt(table: Table) -> str:
    """
    Render a Table as a single compact prompt line.

    Example output:
        - public.orders(id INTEGER, customer_id INTEGER, amount NUMERIC)
    """
    table_name = f"{table.schema}.{table.name}" if table.schema else table.name
    columns = ", ".join(
        f"{column.name} {column.type}" if column.type else column.name
        for column in table.columns
    )
    return f"- {table_name}({columns})"


def extract_schema_payload(schema: list[dict[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
    """
    Normalise the schema argument to a plain list of table dicts.

    Accepts either:
    - A bare list of table dicts (legacy / simple format).
    - A wrapper dict with a "tables" key (as produced by get_postgreql_Details.py).
    """
    if isinstance(schema, dict):
        return schema.get("tables", [])

    return schema


def parse_schema(schema: list[dict[str, Any]] | dict[str, Any]) -> list[Table]:
    """
    Convert the raw schema payload into a list of Table dataclass instances.

    Skips any entry that has no name or no columns to avoid injecting empty or
    malformed table definitions into the prompt.
    """
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
    """
    Extract foreign-key relationships from a schema wrapper dict.

    Returns an empty list when:
    - The schema is a bare list (no relationship metadata available).
    - Individual relationship entries are missing required table/column fields.
    """
    if not isinstance(schema, dict):
        return []

    relationships: list[Relationship] = []
    for item in schema.get("relationships", []):
        # Skip relationships that are missing any required reference field
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
    """
    Render the full schema context string that is injected into the model prompt.

    Format when relationships are present:
        Tables:
        - schema.table(col TYPE, ...)
        ...

        Relationships:
        - schema.table.col -> schema.table.col
        ...

    Format when there are no relationships: just the table block, no headings.
    """
    if not tables:
        raise ValueError("Schema context is empty. At least one table is required.")

    table_context = "\n".join(table_to_prompt(table) for table in tables)

    # If there are no FK relationships, return only the table block
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
    """
    Construct the [system, user] message list to send to Ollama.

    The system prompt instructs the model to:
    - Generate exactly one read-only SQL query.
    - Return raw SQL only (no markdown, no explanation).
    - Use only the tables, columns, and relationships provided.
    - Return the sentinel comment "-- cannot answer from schema" if the
      request cannot be satisfied from the given schema.

    The user prompt includes the target dialect, driver name, an optional
    database name header, the rendered schema context, and the instruction.
    """
    dialect = get_dialect(driver_name)
    schema_context = build_schema_context(tables, relationships or [])

    # Prepend a database name line only if one was provided
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


# ---
# SQL post-processing and validation
# ---


def remove_markdown_fences(text: str) -> str:
    """
    Strip ```sql ... ``` or plain ``` ... ``` fences from a model response.

    The model frequently wraps its output in a markdown code block even when
    explicitly instructed not to.  This function strips those fences so the
    downstream validators receive raw SQL.
    """
    cleaned = text.strip()

    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:sql)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    return cleaned.strip()


def validate_read_only_sql(sql: str) -> None:
    """
    Verify that `sql` is a read-only statement starting with SELECT or WITH.

    Steps:
      1. Strip any markdown fences.
      2. Remove single-line (--) and block (/* */) comments so forbidden words
         cannot be concealed inside comments.
      3. Confirm the first keyword is SELECT or WITH.
      4. Scan all keywords for any member of FORBIDDEN_SQL_WORDS.

    Raises SqlGenerationError if the SQL fails any check.
    """
    cleaned = remove_markdown_fences(sql)

    # Strip comments before scanning for forbidden keywords
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


def validate_sql_syntax(sql: str, driver_name: str) -> None:
    """
    Parse `sql` with sqlglot to catch syntax errors without hitting a database.

    The check is dialect-aware: the driver is mapped to a sqlglot dialect via
    DRIVER_TO_SQLGLOT_DIALECT.

    Silently skips when:
    - sqlglot is not installed (ImportError).
    - The driver has no sqlglot dialect mapping (e.g. SAP HANA).

    Raises SqlGenerationError when sqlglot is available and reports a parse error.
    """
    # Lazy import — do not require sqlglot as a hard dependency
    try:
        import sqlglot
        import sqlglot.errors
    except ImportError:
        # sqlglot not installed; skip syntax validation gracefully
        return

    normalized = normalize_driver(driver_name)
    dialect = DRIVER_TO_SQLGLOT_DIALECT.get(normalized)

    if dialect is None:
        # No sqlglot dialect mapping for this driver — skip silently
        return

    try:
        sqlglot.parse_one(sql, dialect=dialect)
    except sqlglot.errors.ParseError as error:
        raise SqlGenerationError(
            f"SQL syntax validation failed (sqlglot/{dialect}): {error}"
        ) from error


def get_engine(connection_string: str) -> Any:
    """
    Return a cached SQLAlchemy engine for `connection_string`.

    Engines hold an internal connection pool, so we cache one per unique
    connection string for the life of the process to avoid redundant pool
    creation on every validation call.

    Returns None if sqlalchemy is not installed (lazy import pattern).
    """
    # Lazy import — do not require sqlalchemy as a hard dependency
    try:
        from sqlalchemy import create_engine
    except ImportError:
        return None

    if connection_string not in _engine_cache:
        _engine_cache[connection_string] = create_engine(connection_string)

    return _engine_cache[connection_string]


def validate_sql_with_explain(
    sql: str,
    connection_string: str,
    driver_name: str,
) -> None:
    """
    Run an EXPLAIN (or equivalent) against a live database to catch semantic
    errors that sqlglot cannot detect (e.g. referencing non-existent columns).

    Behaviour by driver family:
    - EXPLAIN_UNSUPPORTED_DRIVERS : returns immediately — fall back to sqlglot only.
    - EXPLAIN_MSSQL_DRIVERS       : SET PARSEONLY ON; <sql>; SET PARSEONLY OFF
    - EXPLAIN_ORACLE_DRIVERS      : EXPLAIN PLAN FOR <sql>
    - All others                  : EXPLAIN <sql>

    Both sqlalchemy and the appropriate DB driver must be installed; if either
    is absent the function returns silently without raising.

    Raises SqlGenerationError when the database reports a parse or semantic error.
    """
    normalized = normalize_driver(driver_name)

    # Some drivers have no EXPLAIN equivalent — rely on sqlglot alone
    if normalized in EXPLAIN_UNSUPPORTED_DRIVERS:
        return

    engine = get_engine(connection_string)
    if engine is None:
        return

    try:
        from sqlalchemy import text as sa_text
        with engine.connect() as conn:
            if normalized in EXPLAIN_MSSQL_DRIVERS:
                # T-SQL PARSEONLY mode: syntax-checks the statement without executing it
                conn.execute(sa_text(f"SET PARSEONLY ON; {sql}; SET PARSEONLY OFF"))
            elif normalized in EXPLAIN_ORACLE_DRIVERS:
                # Oracle: writes the execution plan to PLAN_TABLE without running the query
                conn.execute(sa_text(f"EXPLAIN PLAN FOR {sql}"))
            else:
                # Standard EXPLAIN — supported by PostgreSQL, MySQL, Redshift, etc.
                conn.execute(sa_text(f"EXPLAIN {sql}"))
    except Exception as error:
        # Wrap all database-level errors in our own exception type for uniform handling
        raise SqlGenerationError(
            f"SQL failed EXPLAIN validation against the database: {error}"
        ) from error


# ---
# Ollama HTTP client
# ---


def chat_with_ollama(
    model: str,
    messages: list[dict[str, str]],
    options: dict[str, Any] | None = None,
    timeout: int = 600,
) -> dict[str, Any]:
    """
    Send a chat completion request to the local Ollama server and return the
    parsed JSON response dict.

    Uses only stdlib urllib so no extra HTTP library is required.

    Raises:
        TimeoutError     — Ollama took longer than `timeout` seconds.
        ConnectionError  — Could not reach http://localhost:11434.
    """
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


# ---
# Session history formatting helpers
# ---


def format_turns_for_follow_up(turns: list[SessionTurn]) -> str:
    """
    Format a list of prior session turns into a numbered text block for the
    follow-up resolution prompt.

    Each turn block shows:
    - The original user request (as typed).
    - The resolved standalone instruction (after context injection).
    - The SQL that was generated and accepted.
    """
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
    """
    Rewrite a potentially context-dependent instruction into a fully standalone
    SQL-generation instruction.

    If there are no previous turns (first message in a session) the instruction
    is returned unchanged without calling the model — saves one round-trip.

    The rewrite model is instructed to resolve references like "now", "same
    table", "that column", "also include", etc. using the prior turns.  It must
    NOT invent schema or generate SQL — only rewrite the instruction text.
    """
    # First message in session: no rewriting needed
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
            "temperature": 0.0,   # deterministic rewrite
            "top_p": 0.9,
            "num_predict": 256,
        },
        timeout=timeout,
    )

    resolved_instruction = remove_markdown_fences(response["message"]["content"])
    # Fall back to original if the model returns an empty response
    return resolved_instruction or user_instruction


# ---
# Core SQL generation with validation + self-correction retry loop
# ---


def generate_sql(
    user_instruction: str,
    driver_name: str,
    schema: list[dict[str, Any]] | dict[str, Any],
    database_name: str | None = None,
    model: str = MODEL_NAME,
    timeout: int = 600,
    connection_string: str | None = None,
    max_retries: int = 3,
) -> str:
    """
    Generate a validated, read-only SQL query from a natural-language instruction.

    Validation pipeline (fail-fast — each layer only runs if the previous passed):
      1. validate_read_only_sql   — keyword-level read-only guard (always runs).
      2. validate_sql_syntax      — sqlglot AST parse (skipped if not installed).
      3. validate_sql_with_explain — live EXPLAIN via SQLAlchemy (skipped when no
                                     connection_string or sqlalchemy not installed).

    Self-correction retry loop:
      On validation failure the failed SQL + error message are appended to the
      base message list so the model can see exactly what was wrong.  Each retry
      appends only the most recent failure (not a growing stack) to keep the
      context window tidy and to avoid confusing the model with a long error
      history.  After `max_retries` attempts the last error is surfaced as
      SqlGenerationError.

    Sentinel response:
      If the model returns "-- cannot answer from schema" (case-insensitive,
      whitespace-trimmed) it is returned as-is without further validation.

    Parameters
    ----------
    user_instruction   : Natural-language query request.
    driver_name        : Database driver name (used for dialect + validation routing).
    schema             : Tables/relationships as a list or wrapper dict.
    database_name      : Optional database name injected into the prompt.
    model              : Ollama model tag.
    timeout            : Ollama request timeout in seconds.
    connection_string  : Optional SQLAlchemy URL for live EXPLAIN validation.
    max_retries        : Maximum correction attempts on validation failure.

    Returns the validated SQL string.
    """
    tables = parse_schema(schema)
    relationships = parse_relationships(schema)

    # Build the base prompt messages once; reused and extended across retries
    base_messages = build_messages(
        user_instruction=user_instruction,
        driver_name=driver_name,
        tables=tables,
        relationships=relationships,
        database_name=database_name,
    )

    last_error: str | None = None
    last_sql: str | None = None

    for attempt in range(1, max_retries + 1):
        if attempt == 1:
            # First attempt: send the clean base prompt
            messages = list(base_messages)
        else:
            # Retry: append the previous failed SQL and a correction request so
            # the model knows what it got wrong.  We extend base_messages (not
            # the previous attempt's messages) to prevent stacking multiple
            # correction pairs and bloating the context window.
            messages = list(base_messages) + [
                {"role": "assistant", "content": last_sql or ""},
                {
                    "role": "user",
                    "content": (
                        f"That SQL is invalid. Error: {last_error}\n"
                        "Fix it and return only the corrected SQL."
                    ),
                },
            ]

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
        last_sql = sql

        # The model signals inability to answer with this exact sentinel.
        # Use lower() + strip() comparison to be robust against whitespace and
        # case variations in the model response (fixes case-sensitivity bug).
        if sql.strip().lower() == "-- cannot answer from schema":
            return "-- cannot answer from schema"

        # --- Validation layer 1: read-only keyword check ---
        try:
            validate_read_only_sql(sql)
        except SqlGenerationError as error:
            last_error = str(error)
            if attempt == max_retries:
                raise SqlGenerationError(
                    f"SQL generation failed after {max_retries} attempt(s). "
                    f"Last error: {last_error}"
                ) from error
            # Loop to next retry with correction context injected
            continue

        # --- Validation layer 2: sqlglot syntax parse ---
        try:
            validate_sql_syntax(sql, driver_name)
        except SqlGenerationError as error:
            last_error = str(error)
            if attempt == max_retries:
                raise SqlGenerationError(
                    f"SQL generation failed after {max_retries} attempt(s). "
                    f"Last error: {last_error}"
                ) from error
            continue

        # --- Validation layer 3: live EXPLAIN (only when connection_string provided) ---
        if connection_string:
            try:
                validate_sql_with_explain(sql, connection_string, driver_name)
            except SqlGenerationError as error:
                last_error = str(error)
                if attempt == max_retries:
                    raise SqlGenerationError(
                        f"SQL generation failed after {max_retries} attempt(s). "
                        f"Last error: {last_error}"
                    ) from error
                continue

        # All validation layers passed — return the accepted SQL
        return sql

    # Logically unreachable: the loop always returns or raises on the final
    # attempt.  Present only to satisfy type checkers.
    raise SqlGenerationError("SQL generation failed: exhausted retries.")


# ---
# Session-aware entry point
# ---


def generate_sql_with_session(
    session_id: str,
    user_instruction: str,
    driver_name: str,
    schema: list[dict[str, Any]] | dict[str, Any],
    session_store: SessionStore,
    database_name: str | None = None,
    model: str = MODEL_NAME,
    timeout: int = 600,
    connection_string: str | None = None,
    max_retries: int = 3,
) -> tuple[str, str]:
    """
    Generate SQL with full session history context (Redis + PostgreSQL).

    Steps:
      1. Ensure PostgreSQL history tables exist (idempotent CREATE IF NOT EXISTS).
      2. Load the most recent turns for this session from Redis (fast path)
         or fall back to PostgreSQL if the Redis cache is cold.
      3. Rewrite the user instruction into a standalone form using prior turns.
      4. Call generate_sql() with the resolved instruction and any optional
         connection_string / max_retries for validation.
      5. Persist the completed turn to both Redis (hot cache) and PostgreSQL
         (durable store).

    Parameters
    ----------
    session_id         : Opaque session identifier string.
    connection_string  : Optional SQLAlchemy URL forwarded to generate_sql().
    max_retries        : Forwarded to generate_sql().

    Returns (sql, resolved_instruction).
    """
    # Ensure history tables exist before any read or write
    session_store.init_postgres()

    # Load prior turns to enable follow-up context resolution
    previous_turns = session_store.load_recent_turns(session_id)

    # Rewrite any context-dependent references into a standalone instruction
    resolved_instruction = resolve_follow_up_instruction(
        user_instruction=user_instruction,
        previous_turns=previous_turns,
        model=model,
        timeout=timeout,
    )

    # Generate and validate the SQL (with retries if needed)
    sql = generate_sql(
        user_instruction=resolved_instruction,
        driver_name=driver_name,
        schema=schema,
        database_name=database_name,
        model=model,
        timeout=timeout,
        connection_string=connection_string,
        max_retries=max_retries,
    )

    # Persist the turn so future follow-ups in this session can reference it
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


# ---
# CLI entry point
# ---


def main() -> None:
    """
    Command-line interface for testing SQL generation without a backend server.

    Examples
    --------
    # Basic single-turn query:
    python query_ai_with_olama_and_deepseek.py \\
        --driver postgresql \\
        --question "Show me total orders per customer" \\
        --schema-file selected_schema.json

    # Follow-up in a session (requires Redis + PostgreSQL):
    python query_ai_with_olama_and_deepseek.py \\
        --driver postgresql \\
        --question "Now filter to only last month" \\
        --schema-file selected_schema.json \\
        --session-id my-session-1 \\
        --show-resolved

    # With live EXPLAIN validation and 5 retry attempts:
    python query_ai_with_olama_and_deepseek.py \\
        --driver postgresql \\
        --question "List all products with their suppliers" \\
        --schema-file selected_schema.json \\
        --connection-string "postgresql://user:pass@localhost:5432/mydb" \\
        --max-retries 5
    """
    parser = argparse.ArgumentParser(description="Generate SQL using Ollama.")

    # --- Required arguments ---
    parser.add_argument("--driver", required=True, help="Database driver name.")
    parser.add_argument("--question", required=True, help="Natural-language request.")
    parser.add_argument("--schema-file", required=True, help="JSON schema file path.")

    # --- Optional generation arguments ---
    parser.add_argument("--database", help="Optional database name.")
    parser.add_argument("--model", default=MODEL_NAME, help="Ollama model name.")
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Ollama request timeout in seconds. Default: 600.",
    )
    parser.add_argument(
        "--connection-string",
        default=None,
        help=(
            "Optional SQLAlchemy connection string.  When supplied the generated SQL "
            "is validated with an EXPLAIN query against the live database before "
            "being returned.  Requires sqlalchemy and the matching DB driver."
        ),
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help=(
            "Maximum number of self-correction retries when validation fails.  "
            "On each retry the error is fed back to the model so it can fix the SQL.  "
            "Default: 3."
        ),
    )

    # --- Session / history arguments ---
    parser.add_argument(
        "--session-id",
        help="Conversation session id.  Enables Redis + PostgreSQL follow-up memory.",
    )
    parser.add_argument(
        "--redis-url",
        default=DEFAULT_REDIS_URL,
        help=f"Redis URL for the active session cache. Default: {DEFAULT_REDIS_URL}",
    )
    parser.add_argument(
        "--history-db-url",
        default=DEFAULT_HISTORY_DATABASE_URL,
        help=(
            "PostgreSQL URL for permanent chat history storage. "
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
        help="Redis session TTL in seconds. Default: 86400 (24 h).",
    )
    parser.add_argument(
        "--show-resolved",
        action="store_true",
        help="Print the resolved standalone instruction as a SQL comment before the output.",
    )

    args = parser.parse_args()

    # Load the JSON schema from disk
    with open(args.schema_file, "r", encoding="utf-8") as file:
        schema = json.load(file)

    try:
        if args.session_id:
            # Session mode: load history, resolve follow-ups, persist new turn
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
                connection_string=args.connection_string,
                max_retries=args.max_retries,
            )
        else:
            # Stateless mode: single-turn generation without session history
            resolved_instruction = args.question
            sql = generate_sql(
                user_instruction=args.question,
                driver_name=args.driver,
                schema=schema,
                database_name=args.database,
                model=args.model,
                timeout=args.timeout,
                connection_string=args.connection_string,
                max_retries=args.max_retries,
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
