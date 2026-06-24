# -*- coding: utf-8 -*-
"""
get_schema_details.py
---------------------
Generalized schema fetcher for ALL supported SQL databases.

Uses SQLAlchemy's Inspector API so the same code works across PostgreSQL,
MySQL, MariaDB, MSSQL, Oracle, Snowflake, Redshift, BigQuery, SAP HANA,
Vertica, Teradata, MonetDB, IBM Db2, and CockroachDB without any
database-specific SQL queries.

Workflow:
  1. Connect via a SQLAlchemy connection string.
  2. Resolve the selected table (find its schema if not specified).
  3. Fetch all foreign keys in the schema to build a relationship graph.
  4. Expand the selected table outward by `depth` FK hops.
  5. Fetch columns and primary keys for the resolved table set.
  6. Output a JSON payload compatible with query_ai_with_olama_and_deepseek.py.

Usage:
  python get_schema_details.py \\
      --connection-string "postgresql://user:pass@host:5432/db" \\
      --table employee \\
      --depth 1 \\
      --output schemaJson/employee_schema.json

Connection string examples by database:
  PostgreSQL  : postgresql://user:pass@host:5432/dbname
  MySQL       : mysql+pymysql://user:pass@host:3306/dbname
  MariaDB     : mariadb+pymysql://user:pass@host:3306/dbname
  MSSQL       : mssql+pyodbc://user:pass@host:1433/db?driver=ODBC+Driver+17+for+SQL+Server
  Oracle      : oracle+oracledb://user:pass@host:1521/service
  Snowflake   : snowflake://user:pass@account/database/schema
  Redshift    : redshift+redshift_connector://user:pass@host:5439/dbname
  BigQuery    : bigquery://project/dataset
  SAP HANA    : hana://user:pass@host:30015
  Vertica     : vertica+vertica_python://user:pass@host:5433/dbname
  Teradata    : teradatasql://user:pass@host/dbname
  IBM Db2     : db2+ibm_db://user:pass@host:50000/dbname
"""

from __future__ import annotations

import argparse
import json
from typing import Any


# ---
# System schemas to skip when searching for user tables.
# These are internal schemas that exist in most databases and are never
# part of a user's data model.
# ---

SYSTEM_SCHEMAS: set[str] = {
    # ANSI / shared
    "information_schema",
    # PostgreSQL
    "pg_catalog",
    "pg_toast",
    "pg_temp",
    # MySQL / MariaDB
    "mysql",
    "performance_schema",
    "sys",
    # MSSQL
    "sys",
    "guest",
    # Oracle
    "SYS",
    "SYSTEM",
    "DBSNMP",
    "OUTLN",
    "XDB",
    "CTXSYS",
    "MDSYS",
    # Snowflake
    "INFORMATION_SCHEMA",
    # IBM Db2
    "SYSIBM",
    "SYSCAT",
    "SYSSTAT",
    # SAP HANA
    "SYS",
    "_SYS_STATISTICS",
    "_SYS_REPO",
}


# ---
# Dialect name → human-readable label used in the output JSON.
# engine.dialect.name returns the short SQLAlchemy dialect identifier.
# ---

DIALECT_TO_LABEL: dict[str, str] = {
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "mariadb": "MariaDB SQL",
    "mssql": "Microsoft SQL Server T-SQL",
    "oracle": "Oracle SQL",
    "snowflake": "Snowflake SQL",
    "redshift": "Amazon Redshift SQL",
    "bigquery": "Google BigQuery Standard SQL",
    "hana": "SAP HANA SQL",
    "vertica": "Vertica SQL",
    "teradata": "Teradata SQL",
    "monetdb": "MonetDB SQL",
    "db2": "IBM Db2 SQL",
    "cockroachdb": "CockroachDB SQL, PostgreSQL-compatible",
}


# ---------------------------------------------------------------------------
# Engine / Inspector helpers
# ---------------------------------------------------------------------------


def create_engine_for_connection(connection_string: str) -> Any:
    """
    Create a SQLAlchemy engine from `connection_string`.

    Raises SystemExit with an install hint if sqlalchemy is not installed.
    The engine holds an internal connection pool — callers should dispose it
    when done if running in a long-lived process.
    """
    try:
        from sqlalchemy import create_engine
    except ImportError as error:
        raise SystemExit(
            "sqlalchemy is required for multi-database schema fetching.\n"
            "Install it with:  pip install sqlalchemy"
        ) from error

    return create_engine(connection_string)


def get_inspector(engine: Any) -> Any:
    """
    Return a SQLAlchemy Inspector bound to `engine`.

    The Inspector provides a uniform API for fetching table names, columns,
    primary keys, and foreign keys regardless of the underlying database.
    """
    from sqlalchemy import inspect as sa_inspect  # lazy, engine already verified
    return sa_inspect(engine)


def get_database_label(engine: Any) -> str:
    """
    Return a human-readable database type label from the engine's dialect.

    Falls back to the raw dialect name (capitalized) if no mapping is found.
    """
    dialect_name = engine.dialect.name.lower()
    return DIALECT_TO_LABEL.get(dialect_name, dialect_name.capitalize())


def get_default_schema(inspector: Any) -> str | None:
    """
    Return the default schema for the current connection, or None if the
    database does not expose one (e.g. BigQuery uses datasets, not schemas).
    """
    try:
        return inspector.default_schema_name
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Table resolution
# ---------------------------------------------------------------------------


def _is_system_schema(schema_name: str) -> bool:
    """
    Return True if `schema_name` looks like a database-internal schema that
    should be excluded from user table searches.
    """
    # Exact match (case-insensitive) against known system schema names
    if schema_name.lower() in {s.lower() for s in SYSTEM_SCHEMAS}:
        return True

    # PostgreSQL internal schemas follow pg_toast_* / pg_temp_* patterns
    lower = schema_name.lower()
    if lower.startswith("pg_") or lower.startswith("_sys_"):
        return True

    return False


def resolve_selected_table(
    inspector: Any,
    table_name: str,
    schema_name: str | None = None,
) -> tuple[str | None, str]:
    """
    Resolve `table_name` to a (schema, table) pair that actually exists in
    the database.

    Resolution order:
    1. If `table_name` is "schema.table", split and verify that exact path.
    2. If `schema_name` is given, look in that schema only.
    3. Otherwise, search all non-system schemas and pick the unique match.

    Raises ValueError if the table is not found or is ambiguous (appears in
    multiple schemas without a schema qualifier).
    """
    # Support "schema.table" dot notation passed as the table argument
    if "." in table_name:
        parts = table_name.split(".", 1)
        schema_name = parts[0].strip('"')
        table_name = parts[1].strip('"')

    # --- Search in a specific schema ---
    if schema_name:
        try:
            tables_in_schema = inspector.get_table_names(schema=schema_name)
        except Exception as error:
            raise ValueError(
                f"Could not list tables in schema '{schema_name}': {error}"
            ) from error

        if table_name not in tables_in_schema:
            raise ValueError(
                f"Table '{table_name}' not found in schema '{schema_name}'."
            )
        return schema_name, table_name

    # --- Search across all user schemas ---
    try:
        all_schemas = inspector.get_schema_names()
    except Exception:
        # Some databases (BigQuery) do not support schema enumeration the
        # same way — fall back to no schema qualifier.
        return None, table_name

    matches: list[tuple[str | None, str]] = []

    for schema in all_schemas:
        if _is_system_schema(schema):
            continue
        try:
            tables_in_schema = inspector.get_table_names(schema=schema)
        except Exception:
            continue

        if table_name in tables_in_schema:
            matches.append((schema, table_name))

    if not matches:
        raise ValueError(
            f"Table '{table_name}' not found in any user schema. "
            "Use --schema to specify the schema explicitly."
        )

    if len(matches) > 1:
        candidates = ", ".join(f"{s}.{t}" for s, t in matches)
        raise ValueError(
            f"Table '{table_name}' is ambiguous — found in multiple schemas: "
            f"{candidates}. Use --schema or pass 'schema.table'."
        )

    return matches[0]


# ---------------------------------------------------------------------------
# Foreign key collection
# ---------------------------------------------------------------------------


def fetch_all_foreign_keys(
    inspector: Any,
    schema: str | None,
) -> list[dict[str, Any]]:
    """
    Collect all foreign keys defined in `schema` by iterating every table.

    SQLAlchemy's Inspector does not offer a single "get all FK constraints"
    call, so we iterate all table names and aggregate their FKs.  This is
    slightly slower for large schemas but works uniformly across every
    supported database.

    Multi-column FK constraints are expanded into one row per column pair so
    the rest of the code can treat every FK as a simple (from_col → to_col)
    edge.

    Databases that do not support FK introspection (e.g. BigQuery) simply
    return an empty list — schema expansion will fall back to the selected
    table only.
    """
    foreign_keys: list[dict[str, Any]] = []

    try:
        table_names = inspector.get_table_names(schema=schema)
    except Exception:
        return foreign_keys

    for table_name in table_names:
        try:
            fk_constraints = inspector.get_foreign_keys(table_name, schema=schema)
        except Exception:
            # Skip tables whose FK metadata cannot be read (permissions, etc.)
            continue

        for constraint in fk_constraints:
            # constrained_columns and referred_columns are parallel lists;
            # zip them to produce one edge per column pair.
            from_cols = constraint.get("constrained_columns") or []
            to_cols = constraint.get("referred_columns") or []
            to_schema = constraint.get("referred_schema") or schema
            to_table = constraint.get("referred_table", "")

            for from_col, to_col in zip(from_cols, to_cols):
                foreign_keys.append(
                    {
                        "from_schema": schema,
                        "from_table": table_name,
                        "from_column": from_col,
                        "to_schema": to_schema,
                        "to_table": to_table,
                        "to_column": to_col,
                    }
                )

    return foreign_keys


# ---------------------------------------------------------------------------
# Table expansion (BFS over FK graph) — reused logic from get_postgreql_Details.py
# ---------------------------------------------------------------------------


def expand_related_table_keys(
    selected_key: tuple[str | None, str],
    foreign_keys: list[dict[str, Any]],
    depth: int,
) -> set[tuple[str | None, str]]:
    """
    Starting from `selected_key`, expand outward by following FK edges up to
    `depth` hops in either direction (both from and to).

    Uses an iterative BFS-style expansion: each pass adds tables that are one
    FK hop away from the current set.  Stops early if no new tables are found,
    meaning the full FK neighbourhood is smaller than `depth`.

    Returns a set of (schema, table_name) tuples for all reachable tables.
    """
    selected_keys: set[tuple[str | None, str]] = {selected_key}

    for _ in range(depth):
        next_keys = set(selected_keys)

        for fk in foreign_keys:
            from_key = (fk["from_schema"], fk["from_table"])
            to_key = (fk["to_schema"], fk["to_table"])

            # Add the other side of any FK that touches the current set
            if from_key in selected_keys:
                next_keys.add(to_key)
            if to_key in selected_keys:
                next_keys.add(from_key)

        # Early exit: no new tables discovered this pass
        if next_keys == selected_keys:
            break

        selected_keys = next_keys

    return selected_keys


def filter_relationships(
    foreign_keys: list[dict[str, Any]],
    table_keys: set[tuple[str | None, str]],
) -> list[dict[str, Any]]:
    """
    Keep only FK edges where BOTH the source and target table are in
    `table_keys` (the set of tables we are including in the schema).

    This prevents the output from referencing tables that were not included
    due to depth limits or missing FK information.
    """
    return [
        fk for fk in foreign_keys
        if (fk["from_schema"], fk["from_table"]) in table_keys
        and (fk["to_schema"], fk["to_table"]) in table_keys
    ]


# ---------------------------------------------------------------------------
# Column / PK fetching via Inspector
# ---------------------------------------------------------------------------


def fetch_columns_for_keys(
    inspector: Any,
    table_keys: set[tuple[str | None, str]],
) -> dict[tuple[str | None, str], list[dict[str, str]]]:
    """
    Return a dict mapping (schema, table_name) → list of column dicts.

    Each column dict has:
      - "name"  : column name
      - "type"  : string representation of the SQLAlchemy column type

    Columns whose metadata cannot be fetched are silently skipped so a single
    unreadable table does not abort the whole operation.
    """
    columns_by_table: dict[tuple[str | None, str], list[dict[str, str]]] = {}

    for schema, table_name in table_keys:
        try:
            raw_columns = inspector.get_columns(table_name, schema=schema)
        except Exception:
            columns_by_table[(schema, table_name)] = []
            continue

        columns_by_table[(schema, table_name)] = [
            {
                "name": col["name"],
                # str() on the SQLAlchemy type gives a readable label like
                # "VARCHAR(255)", "INTEGER", "TIMESTAMP", etc.
                "type": str(col["type"]),
            }
            for col in raw_columns
        ]

    return columns_by_table


def fetch_primary_keys_for_keys(
    inspector: Any,
    table_keys: set[tuple[str | None, str]],
) -> dict[tuple[str | None, str], list[str]]:
    """
    Return a dict mapping (schema, table_name) → list of PK column names.

    Uses get_pk_constraint() which returns the primary key columns in their
    defined order.  Tables without a PK (or where PK info is unavailable)
    map to an empty list.
    """
    pk_by_table: dict[tuple[str | None, str], list[str]] = {}

    for schema, table_name in table_keys:
        try:
            pk_constraint = inspector.get_pk_constraint(table_name, schema=schema)
            pk_by_table[(schema, table_name)] = pk_constraint.get(
                "constrained_columns", []
            )
        except Exception:
            pk_by_table[(schema, table_name)] = []

    return pk_by_table


# ---------------------------------------------------------------------------
# Metadata assembly
# ---------------------------------------------------------------------------


def build_metadata(
    connection_string: str,
    table_name: str,
    schema_name: str | None = None,
    depth: int = 1,
) -> dict[str, Any]:
    """
    Orchestrate the full schema-fetch pipeline and return the metadata dict.

    Steps:
      1.  Create a SQLAlchemy engine + inspector.
      2.  Resolve the selected table (find its schema if not given).
      3.  Determine the working schema for FK discovery.
      4.  Fetch all FK constraints in that schema.
      5.  BFS-expand from the selected table by `depth` FK hops.
      6.  Fetch columns and PKs for all tables in the expanded set.
      7.  Filter FK edges to only those within the expanded set.
      8.  Assemble and return the output dict.

    The returned dict is directly serialisable to JSON and matches the format
    expected by query_ai_with_olama_and_deepseek.py.
    """
    engine = create_engine_for_connection(connection_string)

    try:
        inspector = get_inspector(engine)
        database_label = get_database_label(engine)

        # --- Step 1: resolve the selected table ---
        resolved_schema, resolved_table = resolve_selected_table(
            inspector, table_name, schema_name
        )

        # Use the resolved schema for FK discovery; fall back to the
        # Inspector's default schema if the table has no schema qualifier.
        working_schema = resolved_schema or get_default_schema(inspector)

        # --- Step 2: collect all FK edges in the working schema ---
        all_foreign_keys = fetch_all_foreign_keys(inspector, working_schema)

        # --- Step 3: expand the table set by depth FK hops ---
        selected_key = (resolved_schema, resolved_table)
        table_keys = expand_related_table_keys(selected_key, all_foreign_keys, depth)

        # --- Step 4: fetch columns and PKs for the expanded table set ---
        columns_by_table = fetch_columns_for_keys(inspector, table_keys)
        pk_by_table = fetch_primary_keys_for_keys(inspector, table_keys)

        # --- Step 5: filter FK edges to those within the expanded set ---
        relationships = filter_relationships(all_foreign_keys, table_keys)

    finally:
        # Always release the connection pool even if an error occurs
        engine.dispose()

    # --- Step 6: build the output tables list ---
    # Sort tables so the selected table comes first, rest alphabetically.
    def sort_key(key: tuple[str | None, str]) -> tuple[int, str, str]:
        schema_str = key[0] or ""
        is_selected = 0 if key == selected_key else 1
        return (is_selected, schema_str, key[1])

    model_tables = []
    for key in sorted(table_keys, key=sort_key):
        schema, name = key
        model_tables.append(
            {
                "schema": schema,
                "name": name,
                "columns": columns_by_table.get(key, []),
                "primary_key": pk_by_table.get(key, []),
            }
        )

    return {
        # Human-readable DB type label (e.g. "PostgreSQL", "MySQL")
        "database_type": database_label,
        # The anchor table the user selected
        "selected_table": {
            "schema": resolved_schema,
            "name": resolved_table,
        },
        # How many FK hops were used to expand the table set
        "relationship_depth": depth,
        # All tables in the expanded set with their columns and PKs
        "tables": model_tables,
        # FK edges between tables in the expanded set
        "relationships": relationships,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """
    CLI wrapper for build_metadata.

    Accepts a connection string, table name, optional schema, depth, and an
    optional output file path.  Prints JSON to stdout if no output file is given.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Fetch schema metadata for any supported SQL database. "
            "Outputs a JSON file compatible with query_ai_with_olama_and_deepseek.py."
        )
    )
    parser.add_argument(
        "--connection-string",
        required=True,
        help=(
            "SQLAlchemy connection string for the target database. "
            "Examples:\n"
            "  PostgreSQL : postgresql://user:pass@host:5432/db\n"
            "  MySQL      : mysql+pymysql://user:pass@host:3306/db\n"
            "  Snowflake  : snowflake://user:pass@account/db/schema\n"
            "  BigQuery   : bigquery://project/dataset"
        ),
    )
    parser.add_argument(
        "--table",
        help=(
            "Table to use as the starting point for schema expansion. "
            "Accepts plain table name or schema.table notation."
        ),
    )
    parser.add_argument(
        "--schema",
        help=(
            "Schema (or dataset/namespace) that contains --table. "
            "Not needed when using schema.table notation in --table."
        ),
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=1,
        help=(
            "Number of FK relationship hops to include around the selected table. "
            "0 = selected table only, 1 = direct FK neighbours (default), "
            "2 = neighbours of neighbours, etc."
        ),
    )
    parser.add_argument(
        "--output",
        help=(
            "Path to write the JSON output file. "
            "If omitted, JSON is printed to stdout."
        ),
    )
    args = parser.parse_args()

    # Prompt for table name interactively if not passed as a flag
    table_name = args.table or input("Enter table name (or schema.table): ").strip()
    if not table_name:
        raise SystemExit("Table name is required.")

    try:
        metadata = build_metadata(
            connection_string=args.connection_string,
            table_name=table_name,
            schema_name=args.schema,
            depth=max(args.depth, 0),
        )
    except ValueError as error:
        raise SystemExit(f"Schema fetch error: {error}") from error
    except Exception as error:
        raise SystemExit(f"Unexpected error: {error}") from error

    # Serialise: use default=str to handle SQLAlchemy type objects gracefully
    output_json = json.dumps(metadata, indent=2, default=str)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as file:
            file.write(output_json)
            file.write("\n")
        print(f"Schema written to {args.output}")
    else:
        print(output_json)


if __name__ == "__main__":
    main()
