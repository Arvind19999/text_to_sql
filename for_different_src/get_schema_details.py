from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# When this helper is executed as "python for_different_src/get_schema_details.py",
# Python only puts for_different_src/ on sys.path. Add the project root so the
# shared Redis schema cache helper can be imported reliably.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from schema_cache import (
    DEFAULT_REDIS_URL,
    DEFAULT_SCHEMA_CACHE_TTL_SECONDS,
    SchemaCacheError,
    save_schema_to_cache,
)


DRIVER_TO_DATABASE_TYPE = {
    "mysql": "MySQL",
    "mariadb": "MariaDB",
    "mssql": "Microsoft SQL Server",
    "sqlserver": "Microsoft SQL Server",
    "oracle": "Oracle",
    "oracledb": "Oracle",
    "ibmdb2": "IBM Db2",
    "db2": "IBM Db2",
    "sap hana": "SAP HANA",
    "saphana": "SAP HANA",
    "hana": "SAP HANA",
    "snowflake": "Snowflake",
    "postgresql": "PostgreSQL",
    "postgres": "PostgreSQL",
}

SYSTEM_SCHEMAS = {
    "information_schema",
    "pg_catalog",
    "pg_toast",
    "mysql",
    "performance_schema",
    "sys",
    "dbo.sys",
    "guest",
    "db_owner",
    "db_accessadmin",
    "db_securityadmin",
    "db_ddladmin",
    "db_backupoperator",
    "db_datareader",
    "db_datawriter",
    "db_denydatareader",
    "db_denydatawriter",
    "syscat",
    "sysibm",
    "sysibmadm",
    "sysstat",
    "sysproc",
    "system",
    "outln",
    "xdb",
    "dbsnmp",
    "ctxsys",
    "mdsys",
    "olapsys",
    "orddata",
    "ordsys",
    "wmsys",
}


def normalize_driver(driver_name: str) -> str:
    return re.sub(r"\s+", " ", driver_name.strip().lower())


def get_database_type(driver_name: str) -> str:
    normalized = normalize_driver(driver_name)
    if normalized not in DRIVER_TO_DATABASE_TYPE:
        raise ValueError(f"Unsupported schema driver: {driver_name}")

    return DRIVER_TO_DATABASE_TYPE[normalized]


def connect_engine(connection_string: str):
    try:
        from sqlalchemy import create_engine
    except ImportError as error:
        raise SystemExit(
            "Install SQLAlchemy first:\n"
            "  pip install SQLAlchemy\n"
            "You also need the correct DB driver package for your connection URL."
        ) from error

    return create_engine(connection_string)


def names_equal(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return left == right

    return left == right or left.lower() == right.lower()


def clean_identifier(identifier: str) -> str:
    return identifier.strip().strip('"').strip("`").strip("[]")


def parse_table_name(
    table_name: str,
    schema_name: str | None = None,
) -> tuple[str | None, str]:
    parts = [clean_identifier(part) for part in table_name.strip().split(".")]
    parts = [part for part in parts if part]

    if len(parts) >= 3:
        return parts[-2], parts[-1]

    if len(parts) == 2:
        return parts[0], parts[1]

    return schema_name, parts[0]


def is_system_schema(schema_name: str | None) -> bool:
    if not schema_name:
        return False

    lowered = schema_name.lower()
    return lowered in SYSTEM_SCHEMAS or lowered.startswith("pg_toast")


def safe_get_schema_names(inspector) -> list[str | None]:
    try:
        schemas = inspector.get_schema_names()
    except Exception:
        schemas = []

    default_schema = getattr(inspector, "default_schema_name", None)
    candidates: list[str | None] = []

    if default_schema:
        candidates.append(default_schema)

    candidates.extend(schema for schema in schemas if schema not in candidates)

    if not candidates:
        candidates.append(None)

    return [schema for schema in candidates if not is_system_schema(schema)]


def safe_get_table_names(inspector, schema: str | None) -> list[str]:
    names: list[str] = []

    for getter_name in ("get_table_names", "get_view_names"):
        getter = getattr(inspector, getter_name, None)
        if not getter:
            continue

        try:
            names.extend(getter(schema=schema))
        except Exception:
            continue

    return sorted(set(names), key=lambda value: value.lower())


def resolve_selected_table(
    inspector,
    table_name: str,
    schema_name: str | None = None,
) -> tuple[str | None, str]:
    parsed_schema, parsed_table = parse_table_name(table_name, schema_name)

    if parsed_schema:
        table_names = safe_get_table_names(inspector, parsed_schema)
        for candidate in table_names:
            if names_equal(candidate, parsed_table):
                return parsed_schema, candidate

        raise ValueError(f"Table not found: {parsed_schema}.{parsed_table}")

    matches = []
    for schema in safe_get_schema_names(inspector):
        table_names = safe_get_table_names(inspector, schema)
        for candidate in table_names:
            if names_equal(candidate, parsed_table):
                matches.append((schema, candidate))

    if not matches:
        raise ValueError(f"Table not found: {table_name}")

    if len(matches) > 1:
        formatted_matches = ", ".join(
            f"{schema}.{table}" if schema else table for schema, table in matches
        )
        raise ValueError(
            "Table name is ambiguous. Use --schema or schema.table. "
            f"Matches: {formatted_matches}"
        )

    return matches[0]


def list_user_table_keys(inspector) -> set[tuple[str | None, str]]:
    table_keys = set()

    for schema in safe_get_schema_names(inspector):
        for table_name in safe_get_table_names(inspector, schema):
            table_keys.add((schema, table_name))

    return table_keys


def fetch_foreign_keys(
    inspector,
    table_keys: set[tuple[str | None, str]],
) -> list[dict[str, Any]]:
    foreign_keys = []

    for schema, table_name in sorted(table_keys, key=lambda item: ((item[0] or ""), item[1])):
        try:
            raw_foreign_keys = inspector.get_foreign_keys(table_name, schema=schema)
        except Exception:
            continue

        for foreign_key in raw_foreign_keys:
            from_columns = foreign_key.get("constrained_columns") or []
            to_columns = foreign_key.get("referred_columns") or []
            to_schema = foreign_key.get("referred_schema") or schema
            to_table = foreign_key.get("referred_table")

            if not to_table:
                continue

            for from_column, to_column in zip(from_columns, to_columns):
                foreign_keys.append(
                    {
                        "from_schema": schema,
                        "from_table": table_name,
                        "from_column": from_column,
                        "to_schema": to_schema,
                        "to_table": to_table,
                        "to_column": to_column,
                    }
                )

    return foreign_keys


def expand_related_table_keys(
    selected_key: tuple[str | None, str],
    foreign_keys: list[dict[str, Any]],
    depth: int,
) -> set[tuple[str | None, str]]:
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
    table_keys: set[tuple[str | None, str]],
) -> list[dict[str, Any]]:
    relationships = []

    for foreign_key in foreign_keys:
        from_key = (foreign_key["from_schema"], foreign_key["from_table"])
        to_key = (foreign_key["to_schema"], foreign_key["to_table"])

        if from_key in table_keys and to_key in table_keys:
            relationships.append(foreign_key)

    return relationships


def get_table_type(inspector, schema: str | None, table_name: str) -> str:
    try:
        if table_name in inspector.get_view_names(schema=schema):
            return "VIEW"
    except Exception:
        pass

    return "BASE TABLE"


def fetch_columns(inspector, schema: str | None, table_name: str) -> list[dict[str, str]]:
    try:
        columns = inspector.get_columns(table_name, schema=schema)
    except Exception as error:
        raise ValueError(f"Could not inspect columns for {schema}.{table_name}") from error

    return [
        {
            "name": str(column["name"]),
            "type": str(column["type"]),
        }
        for column in columns
        if column.get("name")
    ]


def fetch_primary_key(inspector, schema: str | None, table_name: str) -> list[str]:
    try:
        primary_key = inspector.get_pk_constraint(table_name, schema=schema)
    except Exception:
        return []

    return [str(column) for column in primary_key.get("constrained_columns") or []]


def build_metadata(
    driver_name: str,
    connection_string: str,
    table_name: str,
    schema_name: str | None = None,
    depth: int = 1,
) -> dict[str, Any]:
    database_type = get_database_type(driver_name)
    engine = connect_engine(connection_string)

    try:
        inspector = __import__("sqlalchemy").inspect(engine)
        all_table_keys = list_user_table_keys(inspector)
        foreign_keys = fetch_foreign_keys(inspector, all_table_keys)
        selected_key = resolve_selected_table(inspector, table_name, schema_name)
        table_keys = expand_related_table_keys(selected_key, foreign_keys, depth)

        model_tables = []
        for schema, name in sorted(table_keys, key=lambda item: ((item[0] or ""), item[1])):
            model_tables.append(
                {
                    "schema": schema,
                    "name": name,
                    "type": get_table_type(inspector, schema, name),
                    "columns": fetch_columns(inspector, schema, name),
                    "primary_key": fetch_primary_key(inspector, schema, name),
                }
            )

        return {
            "database_type": database_type,
            "selected_table": {
                "schema": selected_key[0],
                "name": selected_key[1],
            },
            "relationship_depth": depth,
            "tables": model_tables,
            "relationships": filter_relationships(foreign_keys, table_keys),
        }
    finally:
        engine.dispose()


def main(
    argv: list[str] | None = None,
    default_driver: str | None = None,
) -> None:
    parser = argparse.ArgumentParser(
        description="Fetch schema metadata for AI query generation."
    )
    if default_driver:
        parser.add_argument("--driver", default=default_driver, help=argparse.SUPPRESS)
    else:
        parser.add_argument(
            "--driver",
            required=True,
            choices=sorted(DRIVER_TO_DATABASE_TYPE),
            help="Database driver/type.",
        )
    parser.add_argument(
        "--connection-string",
        required=True,
        help="SQLAlchemy connection string for the source database.",
    )
    parser.add_argument(
        "--table",
        help="Table name to inspect. You can pass table, schema.table, or database.schema.table.",
    )
    parser.add_argument(
        "--schema",
        help="Schema/owner name for --table. Not needed if --table is schema.table.",
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
    parser.add_argument(
        "--schema-cache-key",
        required=True,
        help=(
            "Redis cache key where this schema should be stored for runtime "
            "SQL generation. Example: snowflake:tpch_sf1:lineitem"
        ),
    )
    parser.add_argument(
        "--redis-url",
        default=DEFAULT_REDIS_URL,
        help=f"Redis URL for schema cache writes. Default: {DEFAULT_REDIS_URL}",
    )
    parser.add_argument(
        "--schema-cache-ttl",
        type=int,
        default=DEFAULT_SCHEMA_CACHE_TTL_SECONDS,
        help="Schema cache TTL in seconds. Default: 1200 (20 minutes).",
    )
    args = parser.parse_args(argv)

    table_name = args.table or input("Enter table name: ").strip()
    if not table_name:
        raise SystemExit("Table name is required.")

    metadata = build_metadata(
        driver_name=args.driver,
        connection_string=args.connection_string,
        table_name=table_name,
        schema_name=args.schema,
        depth=max(args.depth, 0),
    )
    output = json.dumps(metadata, indent=2, default=str)

    try:
        redis_key = save_schema_to_cache(
            schema=metadata,
            cache_key=args.schema_cache_key,
            redis_url=args.redis_url,
            ttl_seconds=args.schema_cache_ttl,
        )
    except SchemaCacheError as error:
        raise SystemExit(f"Schema cache error: {error}") from error

    print(f"Schema cached in Redis at {redis_key}")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as file:
            file.write(output)
            file.write("\n")
        print(f"Schema written to {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
