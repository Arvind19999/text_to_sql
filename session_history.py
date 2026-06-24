# -*- coding: utf-8 -*-
"""
session_history.py — ChatGPT-style session history viewer for the SQL AI assistant.

Purpose
-------
This script reads conversation history from PostgreSQL and displays it in a
structured, human-readable format — similar to the ChatGPT left sidebar that
shows past conversations grouped by recency.

Two modes:
  1. List mode  (default) — shows the sidebar: all (session, schema) contexts
     grouped as Today / Yesterday / Last 7 days / Older, with title, driver,
     schema, turn count, and last active time.

  2. Detail mode (--session-id) — shows all turns for one session+schema,
     similar to opening a chat from the sidebar and reading the full exchange.

Usage
-----
  # Show the sidebar (all sessions)
  python session_history.py --history-db-url "postgresql://..."

  # Show all sessions in JSON (for API/frontend use)
  python session_history.py --json

  # Show turns for a specific session + schema
  python session_history.py --session-id "user-123" --schema-cache-key "postgresql:db:public.employee"

  # Show ALL turns for a session across every schema (full audit view)
  python session_history.py --session-id "user-123"

  # Limit sidebar to last 20 entries
  python session_history.py --limit 20
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from session_store import DEFAULT_HISTORY_DATABASE_URL, SessionStore


# ---
# Date group labels — mirrors the ChatGPT sidebar grouping
# ---

def _date_group(last_active: datetime | None) -> str:
    """
    Return a human-readable recency label for a session's last_active timestamp.

    Groups match the ChatGPT sidebar convention:
      Today / Yesterday / Last 7 days / Last 30 days / Older
    """
    if last_active is None:
        return "Unknown"

    # Normalise to UTC-aware datetime for safe comparison
    now = datetime.now(timezone.utc)
    if last_active.tzinfo is None:
        last_active = last_active.replace(tzinfo=timezone.utc)

    delta_days = (now - last_active).days

    if delta_days == 0:
        return "Today"
    if delta_days == 1:
        return "Yesterday"
    if delta_days <= 7:
        return "Last 7 days"
    if delta_days <= 30:
        return "Last 30 days"
    return "Older"


# ---
# Formatting helpers
# ---

def _truncate(text: str, max_len: int = 60) -> str:
    """Truncate a string and append '…' if it exceeds max_len characters."""
    if not text:
        return "(no title)"
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


def _format_ts(ts: datetime | None) -> str:
    """Format a datetime as 'YYYY-MM-DD HH:MM' or '-' if None."""
    if ts is None:
        return "-"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.strftime("%Y-%m-%d %H:%M")


def _divider(char: str = "─", width: int = 80) -> str:
    return char * width


# ---
# List mode — sidebar view
# ---

def print_session_list(sessions: list[dict]) -> None:
    """
    Print all sessions grouped by recency, mimicking the ChatGPT sidebar.

    Output example:

      ── Today ───────────────────────────────────────────────────────────────
       1  Show total orders by customer          postgresql › public.customer   3 turns  2026-06-24 14:22
       2  Top employees by salary                postgresql › public.employee   5 turns  2026-06-24 11:05

      ── Yesterday ───────────────────────────────────────────────────────────
       3  Monthly revenue by region              snowflake › sales.orders       2 turns  2026-06-23 17:44
    """
    if not sessions:
        print("No session history found.")
        return

    # Group sessions by recency label
    groups: dict[str, list[tuple[int, dict]]] = {}
    group_order = ["Today", "Yesterday", "Last 7 days", "Last 30 days", "Older", "Unknown"]

    for index, session in enumerate(sessions, start=1):
        group = _date_group(session.get("last_active"))
        groups.setdefault(group, []).append((index, session))

    # Print each group in chronological order
    for group_label in group_order:
        if group_label not in groups:
            continue

        print(f"\n{_divider('─', 4)} {group_label} {_divider('─', 80 - len(group_label) - 6)}")

        for index, session in groups[group_label]:
            title = _truncate(session.get("title") or "", 55)
            schema = session.get("schema_cache_key") or "—"
            driver = session.get("driver_name") or "—"
            turns = session.get("turn_count", 0)
            last_active = _format_ts(session.get("last_active"))
            session_id = session.get("session_id") or "—"

            # Row 1: index + title + schema context + turns + timestamp
            print(
                f"  {index:>3}  {title:<56}  {driver} › {schema}"
            )
            # Row 2: metadata in muted style
            print(
                f"       session: {session_id}   {turns} turn{'s' if turns != 1 else ''}   last active: {last_active}"
            )

    print()


# ---
# Detail mode — full turn view for one session+schema
# ---

def print_session_detail(
    session_id: str,
    turns: list[dict],
    schema_cache_key: str | None = None,
) -> None:
    """
    Print all turns for a session in a readable chat-style format.

    Output example:

      Session : user-123
      Schema  : postgresql:db:public.employee
      Turns   : 3
      ────────────────────────────────────────────────────────────────────────

      Turn 1  ·  2026-06-24 14:05
      ┌ User
      │  show all employees
      ├ Resolved
      │  show all employees
      └ SQL
         SELECT * FROM public.employee;

      Turn 2  ·  2026-06-24 14:06
      ┌ User
      │  now filter by department HR
      ├ Resolved
      │  show all employees where department is HR
      └ SQL
         SELECT * FROM public.employee WHERE department = 'HR';
    """
    if not turns:
        print(f"No turns found for session '{session_id}'" +
              (f" / schema '{schema_cache_key}'" if schema_cache_key else "") + ".")
        return

    # Header
    print()
    print(_divider("═"))
    print(f"  Session : {session_id}")
    if schema_cache_key:
        print(f"  Schema  : {schema_cache_key}")
    print(f"  Turns   : {len(turns)}")
    print(_divider("═"))

    for turn in turns:
        turn_num = turn.get("turn_number", "?")
        created = _format_ts(turn.get("created_at"))
        user_instr = turn.get("user_instruction", "").strip()
        resolved = turn.get("resolved_instruction", "").strip()
        sql = turn.get("generated_sql", "").strip()
        schema = turn.get("schema_cache_key") or "—"

        print(f"\n  Turn {turn_num}  ·  {created}" +
              (f"  ·  {schema}" if not schema_cache_key else ""))
        print("  ┌ User")
        print(f"  │  {user_instr}")

        # Only show resolved if it differs from the original (follow-up was resolved)
        if resolved and resolved != user_instr:
            print("  ├ Resolved")
            print(f"  │  {resolved}")

        print("  └ SQL")
        # Indent each line of the SQL for readability
        for line in sql.splitlines():
            print(f"     {line}")

    print()


# ---
# JSON output — for frontend / API consumers
# ---

def _serialise(obj: object) -> str:
    """JSON default serialiser that handles datetime objects."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serialisable")


# ---
# CLI entry point
# ---

def main() -> None:
    """
    Parse CLI arguments and run either list mode or detail mode.
    """
    parser = argparse.ArgumentParser(
        description=(
            "View SQL AI conversation history in a ChatGPT-style format.\n\n"
            "Without --session-id: shows the sidebar (all sessions grouped by recency).\n"
            "With --session-id: shows all turns for that session."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # --- Connection ---
    parser.add_argument(
        "--history-db-url",
        default=DEFAULT_HISTORY_DATABASE_URL,
        help=(
            "PostgreSQL connection URL for the history database. "
            f"Default: {DEFAULT_HISTORY_DATABASE_URL}"
        ),
    )

    # --- List mode options ---
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of sessions to show in list mode. Default: 50.",
    )

    # --- Detail mode options ---
    parser.add_argument(
        "--session-id",
        help=(
            "Show all turns for this session ID. "
            "Combine with --schema-cache-key to scope to one schema context."
        ),
    )
    parser.add_argument(
        "--schema-cache-key",
        help=(
            "Filter turns to this schema context when used with --session-id. "
            "Example: postgresql:mydb:public.employee"
        ),
    )

    # --- Output format ---
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of the human-readable format.",
    )

    args = parser.parse_args()

    # Build a minimal SessionStore (no Redis needed — history reads are PostgreSQL only)
    store = SessionStore(history_database_url=args.history_db_url)

    # Run migrations so schema_cache_key column exists on chat_turns before
    # any query attempts to filter or select it.
    store.init_postgres()

    if args.session_id:
        # --- Detail mode: show turns for one session ---
        turns = store.get_session_turns(
            session_id=args.session_id,
            schema_cache_key=args.schema_cache_key,
        )

        if args.json:
            print(json.dumps(turns, indent=2, default=_serialise))
        else:
            print_session_detail(
                session_id=args.session_id,
                turns=turns,
                schema_cache_key=args.schema_cache_key,
            )

    else:
        # --- List mode: show the sidebar ---
        sessions = store.list_sessions(limit=args.limit)

        if args.json:
            print(json.dumps(sessions, indent=2, default=_serialise))
        else:
            print_session_list(sessions)


if __name__ == "__main__":
    main()
