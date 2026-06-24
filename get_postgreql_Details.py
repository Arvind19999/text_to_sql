"""
get_postgreql_Details.py — PostgreSQL schema metadata fetcher for AI query generation.

Purpose
-------
This script connects to a PostgreSQL database, inspects its information_schema
and system catalog views, and produces a structured JSON document that describes
the tables, columns, primary keys, and foreign-key relationships for a chosen
starting table and its related tables up to a configurable depth.

The output JSON is designed to be fed directly into the SQL AI generation
pipeline (query_ai_with_olama_and_deepseek.py) as the `--schema-file` argument.

Typical workflow
----------------
  # 1. Fetch schema for the `orders` table and its directly related tables:
  python get_postgreql_Details.py \\
      --connection-string "postgresql://user:pass@host:5432/mydb" \\
      --table orders \\
      --depth 1 \\
      --output selected_schema.json

  # 2. Use the saved schema to generate SQL:
  python query_ai_with_olama_and_deepseek.py \\
      --driver postgresql \\
      --question "Show total revenue per customer" \\
      --schema-file selected_schema.json

Output JSON shape
-----------------
{
  "database_type": "PostgreSQL",
  "selected_table": {"schema": "public", "name": "orders"},
  "relationship_depth": 1,
  "tables": [
    {
      "schema": "public",
      "name": "orders",
      "type": "BASE TABLE",
      "columns": [{"name": "id", "type": "integer"}, ...],
      "primary_key": ["id"]
    },
    ...
  ],
  "relationships": [
    {
      "from_schema": "public", "from_table": "orders", "from_column": "customer_id",
      "to_schema": "public",   "to_table": "customers", "to_column": "id"
    },
    ...
  ]
}
"""

import argparse
import json
from typing import Any


# ---
# Default connection string
#
# Points to the TPC-H sample database used during development.
# Override at runtime with --connection-string.
# ---

DEFAULT_CONNECTION_STRING = (
    "postgresql://db_superadmin:DbSup3rAdm1n!2026@pg.db.datafuseai.org:15432/tpch_pg_source_sf1"
)


# ---
# Database connection factory
# ---


def connect_postgres(connection_string: str):
    """
    Open and return a new psycopg (v3) or psycopg2 connection.

    Tries psycopg (v3) first for its improved async support and cleaner API;
    falls back to psycopg2 for environments that only have the older driver.

    Raises SystemExit with installation instructions if neither driver is
    available, since without a DB connection this script cannot do anything.
    """
    try:
        import psycopg
        return psycopg.connect(connection_string)
    except ImportError:
        try:
            import psycopg2
        except ImportError as error:
            raise SystemExit(
                "Install a PostgreSQL driver first:\n"
                "  pip install psycopg[binary]\n"
                "or:\n"
                "  pip install psycopg2-binary"
            ) from error
        return psycopg2.connect(connection_string)


# ---
# Generic query helper
# ---


def fetch_all(cursor, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """
    Execute `query` with `params` and return all rows as a list of dicts.

    Column names are taken from cursor.description so the caller never needs
    to track positional column indices — every row is a {column: value} map.
    """
    cursor.execute(query, params)
    column_names = [description[0] for description in cursor.description]
    return [dict(zip(column_names, row)) for row in cursor.fetchall()]


# ---
# WHERE-clause builder for filtering by (schema, table) pairs
# ---


def make_table_filter(
    table_keys: set[tuple[str, str]],
    schema_column: str,
    table_column: str,
) -> tuple[str, tuple[Any, ...]]:
    """
    Build a parameterised SQL WHERE fragment that matches any of the given
    (schema, table) pairs.

    Each pair produces an  (schema_column = %s AND table_column = %s)  clause;
    the clauses are ORed together and wrapped in an AND prefix so the fragment
    can be safely appended to an existing WHERE clause.

    Example output for two pairs:
        " AND (schema_column = %s AND table_column = %s
              OR schema_column = %s AND table_column = %s)"
    with params ("public", "orders", "public", "customers").

    The pairs are sorted before building the query to ensure deterministic
    parameter ordering across Python's set iteration.

    Raises ValueError if table_keys is empty — at least one table must be
    present for the query to be valid.
    """
    if not table_keys:
        raise ValueError("At least one table is required.")

    conditions = []
    params = []

    for schema_name, table_name in sorted(table_keys):
        conditions.append(f"({schema_column} = %s AND {table_column} = %s)")
        params.extend([schema_name, table_name])

    return " AND (" + " OR ".join(conditions) + ")", tuple(params)


# ---
# Table name parser
# ---


def parse_table_name(table_name: str, schema_name: str | None = None) -> tuple[str | None, str]:
    """
    Split an optionally schema-qualified table name into (schema, table).

    Accepts two forms:
    - "orders"         → (schema_name, "orders")  — uses the provided default schema
    - "public.orders"  → ("public", "orders")      — extracts schema from the dot notation
    - '"public"."orders"' — double-quote wrappers are stripped

    The schema_name parameter is used as a fallback when the table name has no
    dot separator.
    """
    cleaned = table_name.strip()
    if "." in cleaned:
        schema_part, table_part = cleaned.split(".", 1)
        return schema_part.strip('"'), table_part.strip('"')

    return schema_name, cleaned.strip('"')


# ---
# Table resolver — confirms a table exists and resolves its schema
# ---


def resolve_selected_table(
    cursor,
    table_name: str,
    schema_name: str | None = None,
) -> tuple[str, str]:
    """
    Validate that `table_name` exists in the database and return its canonical
    (schema, name) pair.

    Two query strategies:
    - If a schema is known (explicit or parsed from dot notation): look up the
      exact (schema, table) pair — unambiguous.
    - If no schema is known: search all non-system schemas and prefer "public"
      when multiple schemas contain a table with the same name.

    Raises ValueError when:
    - The table is not found.
    - The name matches tables in more than one schema (requires disambiguation
      via --schema or schema.table notation).
    """
    parsed_schema, parsed_table = parse_table_name(table_name, schema_name)

    if parsed_schema:
        # Exact (schema, table) lookup — no ambiguity possible
        rows = fetch_all(
            cursor,
            """
            SELECT table_schema AS schema, table_name AS name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_name = %s
              AND table_schema NOT IN ('information_schema', 'pg_catalog')
              AND table_schema NOT LIKE 'pg_toast%%';
            """,
            (parsed_schema, parsed_table),
        )
    else:
        # Schema-less lookup — search all user schemas, prefer "public"
        rows = fetch_all(
            cursor,
            """
            SELECT table_schema AS schema, table_name AS name
            FROM information_schema.tables
            WHERE table_name = %s
              AND table_schema NOT IN ('information_schema', 'pg_catalog')
              AND table_schema NOT LIKE 'pg_toast%%'
            ORDER BY
              CASE WHEN table_schema = 'public' THEN 0 ELSE 1 END,
              table_schema;
            """,
            (parsed_table,),
        )

    if not rows:
        raise ValueError(f"Table not found: {table_name}")

    if len(rows) > 1:
        # Multiple schemas contain a table with this name — require disambiguation
        matches = ", ".join(f"{row['schema']}.{row['name']}" for row in rows)
        raise ValueError(
            f"Table name is ambiguous. Use --schema or schema.table. Matches: {matches}"
        )

    return rows[0]["schema"], rows[0]["name"]


# ---
# Related table discovery via BFS-style foreign-key traversal
# ---


def expand_related_table_keys(
    selected_key: tuple[str, str],
    foreign_keys: list[dict[str, Any]],
    depth: int,
) -> set[tuple[str, str]]:
    """
    Expand the set of (schema, table) keys by following foreign-key edges
    outward from `selected_key` up to `depth` hops.

    Algorithm (iterative BFS):
    - Start with {selected_key}.
    - For each depth level, scan all foreign keys and add the "other end" of
      any edge that connects to an already-selected table (bidirectional: both
      the "from" side and the "to" side are followed).
    - Early-exit when no new tables were discovered in a full pass.

    depth=0  — returns only the selected table itself.
    depth=1  — adds tables directly referenced by or referencing the selected table.
    depth=2  — also adds tables one hop beyond those, and so on.

    This is the core mechanism that lets the schema fetcher automatically pull
    in "customers" and "products" when you ask for "orders".
    """
    selected_keys = {selected_key}

    for _ in range(depth):
        next_keys = set(selected_keys)

        for foreign_key in foreign_keys:
            from_key = (foreign_key["from_schema"], foreign_key["from_table"])
            to_key = (foreign_key["to_schema"], foreign_key["to_table"])

            # Follow the edge in both directions — FK relationships are useful
            # regardless of which table holds the FK column
            if from_key in selected_keys:
                next_keys.add(to_key)
            if to_key in selected_keys:
                next_keys.add(from_key)

        # Early-exit: no new tables discovered this iteration
        if next_keys == selected_keys:
            break

        selected_keys = next_keys

    return selected_keys


# ---
# Relationship filter — keep only edges within the selected table set
# ---


def filter_relationships(
    foreign_keys: list[dict[str, Any]],
    table_keys: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """
    Return only the foreign keys where both the "from" and "to" tables are
    members of `table_keys`.

    This ensures the relationships section of the output JSON contains no
    references to tables outside the selected schema context, which would
    confuse the SQL generation model.
    """
    relationships = []

    for foreign_key in foreign_keys:
        from_key = (foreign_key["from_schema"], foreign_key["from_table"])
        to_key = (foreign_key["to_schema"], foreign_key["to_table"])

        if from_key in table_keys and to_key in table_keys:
            relationships.append(
                {
                    "from_schema": foreign_key["from_schema"],
                    "from_table": foreign_key["from_table"],
                    "from_column": foreign_key["from_column"],
                    "to_schema": foreign_key["to_schema"],
                    "to_table": foreign_key["to_table"],
                    "to_column": foreign_key["to_column"],
                }
            )

    return relationships


# ---
# information_schema fetch functions
# ---


def fetch_tables_for_keys(cursor, table_keys: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """
    Fetch table metadata (schema, name, type) from information_schema.tables
    for all (schema, table) pairs in `table_keys`.

    Filters out system schemas (information_schema, pg_catalog, pg_toast*)
    and orders results alphabetically for deterministic output.

    Returns rows with keys: schema, name, type (BASE TABLE / VIEW / etc.).
    """
    table_filter, params = make_table_filter(table_keys, "table_schema", "table_name")
    return fetch_all(
        cursor,
        f"""
        SELECT
            table_schema AS schema,
            table_name AS name,
            table_type AS type
        FROM information_schema.tables
        WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
          AND table_schema NOT LIKE 'pg_toast%%'
          {table_filter}
        ORDER BY table_schema, table_name;
        """,
        params,
    )


def fetch_columns_for_keys(cursor, table_keys: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """
    Fetch all column definitions from information_schema.columns for the
    specified tables.

    Ordered by (schema, table, ordinal_position) so columns always appear in
    their defined order when the results are grouped by table.

    Returns rows with keys: schema, table_name, name, position, data_type.
    """
    table_filter, params = make_table_filter(table_keys, "table_schema", "table_name")
    return fetch_all(
        cursor,
        f"""
        SELECT
            table_schema AS schema,
            table_name AS table_name,
            column_name AS name,
            ordinal_position AS position,
            data_type
        FROM information_schema.columns
        WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
          AND table_schema NOT LIKE 'pg_toast%%'
          {table_filter}
        ORDER BY table_schema, table_name, ordinal_position;
        """,
        params,
    )


def fetch_primary_keys_for_keys(
    cursor,
    table_keys: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """
    Fetch primary key column names for all specified tables by joining
    information_schema.table_constraints with key_column_usage.

    The JOIN on constraint_schema/name/table ensures we only get PK columns
    and not UK or FK columns (which share the same key_column_usage rows but
    have different constraint_type).

    Returns rows with keys: schema, table_name, column_name, ordinal_position.
    """
    table_filter, params = make_table_filter(
        table_keys,
        "tc.table_schema",
        "tc.table_name",
    )
    return fetch_all(
        cursor,
        f"""
        SELECT
            tc.table_schema AS schema,
            tc.table_name,
            kcu.column_name,
            kcu.ordinal_position
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_schema = kcu.constraint_schema
         AND tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
         AND tc.table_name = kcu.table_name
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema NOT IN ('information_schema', 'pg_catalog')
          AND tc.table_schema NOT LIKE 'pg_toast%%'
          {table_filter}
        ORDER BY tc.table_schema, tc.table_name, kcu.ordinal_position;
        """,
        params,
    )


def fetch_foreign_keys(cursor) -> list[dict[str, Any]]:
    """
    Fetch ALL foreign-key relationships in the database (not just for the
    selected tables).

    We fetch them all upfront so that expand_related_table_keys() can traverse
    the full FK graph without additional round-trips.

    Query joins three information_schema views:
    - table_constraints     — identifies FK constraint names and their owner table
    - key_column_usage      — maps constraint names to the FK (child) column(s)
    - constraint_column_usage — maps constraint names to the referenced (parent) column(s)

    Returns rows with keys: from_schema, from_table, from_column,
                             to_schema, to_table, to_column, ordinal_position.
    """
    return fetch_all(
        cursor,
        """
        SELECT
            tc.table_schema AS from_schema,
            tc.table_name AS from_table,
            kcu.column_name AS from_column,
            ccu.table_schema AS to_schema,
            ccu.table_name AS to_table,
            ccu.column_name AS to_column,
            kcu.ordinal_position
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_schema = kcu.constraint_schema
         AND tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
         AND tc.table_name = kcu.table_name
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_schema = tc.constraint_schema
         AND ccu.constraint_name = tc.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema NOT IN ('information_schema', 'pg_catalog')
          AND tc.table_schema NOT LIKE 'pg_toast%%'
        ORDER BY tc.table_schema, tc.table_name, tc.constraint_name, kcu.ordinal_position;
        """,
    )


# ---
# Grouping helpers — convert flat row lists to (schema, table) keyed dicts
# ---


def group_columns_by_table(columns: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """
    Group a flat list of column rows into a dict keyed by (schema, table_name).

    Each value is a list of {"name": ..., "type": ...} dicts in ordinal order
    (ordering is preserved from the fetch query which sorts by ordinal_position).
    """
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for column in columns:
        key = (column["schema"], column["table_name"])
        grouped.setdefault(key, []).append(
            {
                "name": column["name"],
                "type": column["data_type"],
            }
        )

    return grouped


def group_primary_keys(primary_keys: list[dict[str, Any]]) -> dict[tuple[str, str], list[str]]:
    """
    Group a flat list of primary key rows into a dict keyed by (schema, table_name).

    Each value is an ordered list of column names that form the primary key
    (composite PKs are represented as a list with multiple entries).
    """
    grouped: dict[tuple[str, str], list[str]] = {}

    for primary_key in primary_keys:
        key = (primary_key["schema"], primary_key["table_name"])
        grouped.setdefault(key, []).append(primary_key["column_name"])

    return grouped


# ---
# Orchestration — the main entry point for programmatic use
# ---


def build_metadata(
    connection_string: str,
    table_name: str,
    schema_name: str | None = None,
    depth: int = 1,
) -> dict[str, Any]:
    """
    Connect to PostgreSQL and build a complete schema metadata document for
    `table_name` and its FK-related tables up to `depth` hops away.

    Execution order inside a single connection/transaction:
    1. fetch_foreign_keys()         — all FK edges in the database.
    2. resolve_selected_table()     — validate and canonicalise the requested table.
    3. expand_related_table_keys()  — BFS traversal to find related tables.
    4. fetch_tables_for_keys()      — table metadata for the selected set.
    5. fetch_columns_for_keys()     — column definitions for the selected set.
    6. fetch_primary_keys_for_keys()— PK columns for the selected set.

    After the connection is closed:
    7. group_columns_by_table()     — restructure columns into per-table lists.
    8. group_primary_keys()         — restructure PKs into per-table lists.
    9. Assemble the output dict.
    10. filter_relationships()      — keep only FK edges within the selected set.

    Returns a dict matching the output JSON shape described in this module's
    docstring.  This dict can be serialised to JSON and passed to the SQL
    generation pipeline as --schema-file.
    """
    with connect_postgres(connection_string) as connection:
        with connection.cursor() as cursor:
            # Step 1: Fetch all FK edges upfront for BFS traversal
            foreign_keys = fetch_foreign_keys(cursor)

            # Step 2: Validate the requested table and get its canonical key
            selected_key = resolve_selected_table(cursor, table_name, schema_name)

            # Step 3: BFS expansion — collect all related (schema, table) keys
            table_keys = expand_related_table_keys(selected_key, foreign_keys, depth)

            # Steps 4-6: Fetch metadata for the discovered table set
            tables = fetch_tables_for_keys(cursor, table_keys)
            columns = fetch_columns_for_keys(cursor, table_keys)
            primary_keys = fetch_primary_keys_for_keys(cursor, table_keys)

    # Steps 7-8: Restructure flat row lists into per-table dicts
    columns_by_table = group_columns_by_table(columns)
    primary_keys_by_table = group_primary_keys(primary_keys)

    # Step 9: Assemble the per-table model list
    model_tables = []
    for table in tables:
        key = (table["schema"], table["name"])
        model_tables.append(
            {
                "schema": table["schema"],
                "name": table["name"],
                "type": table["type"],
                "columns": columns_by_table.get(key, []),
                "primary_key": primary_keys_by_table.get(key, []),
            }
        )

    # Step 10: Filter relationships to only those within the selected table set
    model_relationships = filter_relationships(foreign_keys, table_keys)

    return {
        "database_type": "PostgreSQL",
        "selected_table": {
            "schema": selected_key[0],
            "name": selected_key[1],
        },
        "relationship_depth": depth,
        "tables": model_tables,
        "relationships": model_relationships,
    }


# ---
# CLI entry point
# ---


def main() -> None:
    """
    Command-line interface for schema metadata extraction.

    Prompts for a table name interactively when --table is not provided so the
    script can be used quickly without memorising all flags.

    Output is printed as formatted JSON to stdout by default, or written to a
    file with --output for use as --schema-file in the SQL generator.
    """
    parser = argparse.ArgumentParser(
        description="Fetch PostgreSQL schema metadata for AI query generation."
    )
    parser.add_argument(
        "--connection-string",
        default=DEFAULT_CONNECTION_STRING,
        help="PostgreSQL connection string.",
    )
    parser.add_argument(
        "--table",
        help="Table name to inspect. You can pass table or schema.table.",
    )
    parser.add_argument(
        "--schema",
        help="Schema name for --table. Not needed if --table is schema.table.",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=1,
        help="Foreign-key relationship depth to include. Default: 1.",
    )
    parser.add_argument(
        "--output",
        help="Optional output JSON file. If omitted, JSON is printed to stdout.",
    )
    args = parser.parse_args()

    # Allow interactive input when --table is not provided on the command line
    table_name = args.table or input("Enter table name: ").strip()
    if not table_name:
        raise SystemExit("Table name is required.")

    metadata = build_metadata(
        connection_string=args.connection_string,
        table_name=table_name,
        schema_name=args.schema,
        depth=max(args.depth, 0),   # clamp negative depth to 0
    )
    output = json.dumps(metadata, indent=2, default=str)

    if args.output:
        # Write to file — newline at end for POSIX compliance
        with open(args.output, "w", encoding="utf-8") as file:
            file.write(output)
            file.write("\n")
    else:
        print(output)


if __name__ == "__main__":
    main()
