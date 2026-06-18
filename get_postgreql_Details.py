import argparse
import json
from typing import Any


DEFAULT_CONNECTION_STRING = (
    "postgresql://db_superadmin:DbSup3rAdm1n!2026@pg.db.datafuseai.org:15432/tpch_pg_results"
)


def connect_postgres(connection_string: str):
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


def fetch_all(cursor, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cursor.execute(query, params)
    column_names = [description[0] for description in cursor.description]
    return [dict(zip(column_names, row)) for row in cursor.fetchall()]


def make_table_filter(
    table_keys: set[tuple[str, str]],
    schema_column: str,
    table_column: str,
) -> tuple[str, tuple[Any, ...]]:
    if not table_keys:
        raise ValueError("At least one table is required.")

    conditions = []
    params = []

    for schema_name, table_name in sorted(table_keys):
        conditions.append(f"({schema_column} = %s AND {table_column} = %s)")
        params.extend([schema_name, table_name])

    return " AND (" + " OR ".join(conditions) + ")", tuple(params)


def parse_table_name(table_name: str, schema_name: str | None = None) -> tuple[str | None, str]:
    cleaned = table_name.strip()
    if "." in cleaned:
        schema_part, table_part = cleaned.split(".", 1)
        return schema_part.strip('"'), table_part.strip('"')

    return schema_name, cleaned.strip('"')


def resolve_selected_table(
    cursor,
    table_name: str,
    schema_name: str | None = None,
) -> tuple[str, str]:
    parsed_schema, parsed_table = parse_table_name(table_name, schema_name)

    if parsed_schema:
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
        matches = ", ".join(f"{row['schema']}.{row['name']}" for row in rows)
        raise ValueError(
            f"Table name is ambiguous. Use --schema or schema.table. Matches: {matches}"
        )

    return rows[0]["schema"], rows[0]["name"]


def expand_related_table_keys(
    selected_key: tuple[str, str],
    foreign_keys: list[dict[str, Any]],
    depth: int,
) -> set[tuple[str, str]]:
    selected_keys = {selected_key}

    for _ in range(depth):
        next_keys = set(selected_keys)

        for foreign_key in foreign_keys:
            from_key = (foreign_key["from_schema"], foreign_key["from_table"])
            to_key = (foreign_key["to_schema"], foreign_key["to_table"])

            if from_key in selected_keys:
                next_keys.add(to_key)
            if to_key in selected_keys:
                next_keys.add(from_key)

        if next_keys == selected_keys:
            break

        selected_keys = next_keys

    return selected_keys


def filter_relationships(
    foreign_keys: list[dict[str, Any]],
    table_keys: set[tuple[str, str]],
) -> list[dict[str, Any]]:
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


def fetch_tables_for_keys(cursor, table_keys: set[tuple[str, str]]) -> list[dict[str, Any]]:
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
    table_filter, params = make_table_filter(table_keys, "table_schema", "table_name")
    return fetch_all(
        cursor,
        f"""
        SELECT
            table_schema AS schema,
            table_name AS table_name,
            column_name AS name,
            ordinal_position AS position,
            data_type,
            is_nullable
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


def group_columns_by_table(columns: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for column in columns:
        key = (column["schema"], column["table_name"])
        grouped.setdefault(key, []).append(
            {
                "name": column["name"],
                "type": column["data_type"],
                "nullable": column["is_nullable"] == "YES",
            }
        )

    return grouped


def group_primary_keys(primary_keys: list[dict[str, Any]]) -> dict[tuple[str, str], list[str]]:
    grouped: dict[tuple[str, str], list[str]] = {}

    for primary_key in primary_keys:
        key = (primary_key["schema"], primary_key["table_name"])
        grouped.setdefault(key, []).append(primary_key["column_name"])

    return grouped


def build_metadata(
    connection_string: str,
    table_name: str,
    schema_name: str | None = None,
    depth: int = 1,
) -> dict[str, Any]:
    with connect_postgres(connection_string) as connection:
        with connection.cursor() as cursor:
            foreign_keys = fetch_foreign_keys(cursor)
            selected_key = resolve_selected_table(cursor, table_name, schema_name)
            table_keys = expand_related_table_keys(selected_key, foreign_keys, depth)
            tables = fetch_tables_for_keys(cursor, table_keys)
            columns = fetch_columns_for_keys(cursor, table_keys)
            primary_keys = fetch_primary_keys_for_keys(cursor, table_keys)

    columns_by_table = group_columns_by_table(columns)
    primary_keys_by_table = group_primary_keys(primary_keys)

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


def main() -> None:
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
        help="Optional output JSON file. If omitted, JSON is printed.",
    )
    args = parser.parse_args()

    table_name = args.table or input("Enter table name: ").strip()
    if not table_name:
        raise SystemExit("Table name is required.")

    metadata = build_metadata(
        connection_string=args.connection_string,
        table_name=table_name,
        schema_name=args.schema,
        depth=max(args.depth, 0),
    )
    output = json.dumps(metadata, indent=2, default=str)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as file:
            file.write(output)
            file.write("\n")
    else:
        print(output)


if __name__ == "__main__":
    main()
