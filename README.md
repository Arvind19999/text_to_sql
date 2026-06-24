# SQL AI — Natural-Language to SQL Generator

Generate validated, read-only SQL queries from plain English using a local
[Ollama](https://ollama.com/) instance running `deepseek-coder-v2:16b`.

---

## Project Overview

The tool takes three inputs:

1. A **database driver name** (e.g. `postgresql`, `mysql`, `snowflake`)
2. A **natural-language instruction** (e.g. "Show total revenue per customer last month")
3. A **Redis schema cache key** containing the available tables, columns, and foreign-key relationships

It produces one validated, read-only SQL query in the correct dialect for the
chosen database.

Key features:
- Three-layer SQL validation (keyword guard → sqlglot syntax parse → live EXPLAIN)
- Automatic self-correction retry loop (model sees its own error and fixes it)
- Conversation memory across turns (Redis cache + PostgreSQL durable store)
- Multi-schema sessions — same session ID works across different schemas without context leaking
- Follow-up resolution ("now filter by last month" → standalone instruction)
- Schema auto-extraction from any supported SQL database (via SQLAlchemy Inspector)
- Runtime schema loading from Redis, with JSON output kept only for validation/debugging
- ChatGPT-style session history viewer grouped by recency

---

## Architecture

```
                      ┌──────────────────────────────────┐
                      │  Your backend / API / CLI         │
                      └────────────┬─────────────────────┘
                                   │ driver + question + schema cache key
                                   ▼
          ┌────────────────────────────────────────────────┐
          │  query_ai_with_olama_and_deepseek.py            │
          │                                                  │
          │  1. parse_schema()      — tables + columns       │
          │  2. build_messages()    — system + user prompt   │
          │  3. chat_with_ollama()  — HTTP POST to Ollama    │
          │  4. Validation pipeline (fail-fast):             │
          │       a. validate_read_only_sql()                │
          │       b. validate_sql_syntax()   [sqlglot]       │
          │       c. validate_sql_with_explain() [SQLAlchemy]│
          │  5. Retry loop on failure (max_retries)          │
          │  6. Return validated SQL                         │
          └────────┬──────────────────────┬─────────────────┘
                   │ (session mode)        │ (stateless mode)
                   ▼                       ▼
   ┌───────────────────────────┐      SQL string returned
   │   session_store.py         │      to caller
   │                            │
   │  Redis (hot cache)         │
   │  └─ key per session+schema │
   │     last N turns as JSON   │
   │                            │
   │  PostgreSQL (durable)      │
   │  └─ chat_sessions table    │
   │  └─ chat_turns table       │
   └──────────┬────────────────┘
              │ (read-only)
              ▼
   ┌───────────────────────────┐
   │  session_history.py        │   (view past conversations)
   │                            │
   │  Sidebar: all sessions     │
   │  grouped Today/Yesterday   │
   │  Detail: turns per session │
   │  + schema context          │
   │  JSON output for frontend  │
   └───────────────────────────┘

   ┌───────────────────────────┐
   │  get_schema_details.py     │   (run separately to cache schema + write JSON)
   │                            │
   │  Works for ALL databases   │
   │  via SQLAlchemy Inspector  │
   │  Resolve selected table    │
   │  BFS over FK graph         │
   │  Fetch columns + PKs       │
   │  Output JSON for review    │
   │  Save schema to Redis      │
   └───────────────────────────┘

   ┌───────────────────────────┐
   │  get_postgreql_Details.py  │   (PostgreSQL-only, kept as optimized fallback)
   │                            │
   │  Uses direct psycopg +     │
   │  information_schema SQL    │
   └───────────────────────────┘

   ┌───────────────────────────┐
   │  new.py                    │   (quick demo — no schema, no session)
   │                            │
   │  Guess DB type from URL    │
   │  Send prompt to Ollama     │
   │  Print raw output          │
   └───────────────────────────┘
```

---

## Script Descriptions

### `get_schema_details.py`

Connects to any supported SQL database via a SQLAlchemy connection string,
resolves the selected table, expands outward via FK relationships up to a
configurable depth, and outputs the schema as a JSON payload. Writes the
runtime schema to Redis and optionally writes the same payload to a JSON file
for manual inspection.

### `get_postgreql_Details.py`

PostgreSQL-only alternative to `get_schema_details.py`. Uses direct psycopg
connections and `information_schema` queries instead of SQLAlchemy Inspector.
Kept as an optimized fallback for PostgreSQL-only deployments.

### `query_ai_with_olama_and_deepseek.py`

The main SQL generation engine. Accepts a Redis schema cache key, a driver
name, and a natural-language instruction. Builds a prompt, calls Ollama,
validates and (if needed) self-corrects the output, and returns the final SQL
string. Supports session memory for multi-turn conversations scoped per
schema context.

### `session_store.py`

Manages conversation history in a dual-layer store:
- **Redis** — fast in-memory access to recent turns, scoped per `(session_id, schema_cache_key)` so multiple schemas can coexist in the same session without context leaking between them.
- **PostgreSQL** — durable fallback and permanent log. Stores every turn with its `schema_cache_key` so history can be filtered by schema context.

### `session_history.py`

ChatGPT-style conversation history viewer. Reads from PostgreSQL and displays
sessions grouped by recency (Today / Yesterday / Last 7 days / Older). Each
`(session_id, schema_cache_key)` pair is shown as one history entry — matching
the way ChatGPT shows one sidebar item per focused conversation. Supports both
a human-readable terminal output and JSON output for frontend consumption.

### `new.py`

A minimal prototype that asks for a question, guesses the database type from
a connection string, and calls Ollama without any schema. Useful for quick
demos and connectivity testing.

---

## Dependencies and Installation

### System requirements

- Python 3.11+
- [Ollama](https://ollama.com/) running locally on `http://localhost:11434`
- The model pulled: `ollama pull deepseek-coder-v2:16b`

### Python packages

```bash
pip install -r requirements.txt
```

The `requirements.txt` includes:

| Package | Purpose |
|---|---|
| `psycopg[binary]>=3.2` | PostgreSQL adapter (session history + schema fetch) |
| `redis>=5.0` | Redis client for session cache |
| `SQLAlchemy>=2.0` | Engine + EXPLAIN validation (optional) |
| `PyMySQL>=1.1` | MySQL support for SQLAlchemy |
| `mariadb>=1.1` | MariaDB support for SQLAlchemy |
| `snowflake-sqlalchemy>=1.10.1` | Snowflake support for SQLAlchemy |
| `sqlglot==30.11.0` | Dialect-aware SQL syntax validation |

**Optional packages** (install to enable the respective features):

```bash
pip install psycopg2-binary  # alternative PostgreSQL adapter (fallback)
```

SQLAlchemy is installed by default. Live EXPLAIN validation still only runs
when you pass `--connection-string`.

### Infrastructure (Docker Compose)

Start Redis and PostgreSQL for session history:

```bash
docker compose up -d
```

---

## How To Run

Use the project virtualenv for all Python commands:

```bash
cd /home/arvind/Desktop/courses_practice/sql_ai
source venv/bin/activate
pip install -r requirements.txt
```

Start the local infrastructure:

```bash
docker compose up -d
```

Start Ollama in another terminal if it is not already running:

```bash
ollama serve
```

Pull the model once:

```bash
ollama pull deepseek-coder-v2:16b
```

### 1. Cache Schema And Write JSON

This step connects to the source database, extracts schema metadata, writes it
to Redis for runtime use, and also writes `selected_schema.json` for your
manual validation.

Generic extractor (all databases):

```bash
venv/bin/python get_schema_details.py \
  --connection-string 'postgresql://user:password@host:5432/database' \
  --table public.employee \
  --depth 1 \
  --schema-cache-key postgresql:database:public.employee \
  --output selected_schema.json
```

PostgreSQL-specific extractor (optimized fallback):

```bash
venv/bin/python get_postgreql_Details.py \
  --connection-string 'postgresql://user:password@host:5432/database' \
  --table public.customer \
  --depth 2 \
  --schema-cache-key postgresql:database:public.customer \
  --output selected_schema.json
```

The cache key can be any stable name, but keep it unique per connection and
schema/table scope:

```text
<driver>:<database_or_connection>:<schema.table>
```

Examples:

```text
postgresql:tpch:public.customer
mysql:telecoms:customers
snowflake:SNOWFLAKE_SAMPLE_DATA:TPCH_SF1.LINEITEM
mssql:salesdb:dbo.orders
```

Schema cache TTL defaults to 20 minutes. Override it when needed:

```bash
--schema-cache-ttl 600
```

### 2. Run Query Without Chat Memory

Use this for one-shot questions. The LLM reads schema from Redis using
`--schema-cache-key`; it does not read `selected_schema.json`.

```bash
venv/bin/python query_ai_with_olama_and_deepseek.py \
  --driver postgresql \
  --schema-cache-key postgresql:database:public.customer \
  --question "show total orders by customer"
```

### 3. Run Query With Chat Memory

Use this for a chat screen and follow-up questions. Keep the same
`--session-id` for the same user. The same session ID can be reused across
different `--schema-cache-key` values — turns are isolated per schema context
automatically.

```bash
venv/bin/python query_ai_with_olama_and_deepseek.py \
  --driver postgresql \
  --schema-cache-key postgresql:database:public.customer \
  --session-id user-123 \
  --question "show total orders by customer"
```

Follow-up in the same schema context:

```bash
venv/bin/python query_ai_with_olama_and_deepseek.py \
  --driver postgresql \
  --schema-cache-key postgresql:database:public.customer \
  --session-id user-123 \
  --show-resolved \
  --question "now only for 2024"
```

Switch to a different schema — same session ID, isolated history:

```bash
venv/bin/python query_ai_with_olama_and_deepseek.py \
  --driver postgresql \
  --schema-cache-key postgresql:database:demo.products \
  --session-id user-123 \
  --question "show top 10 products by revenue"
```

### 4. Run Query With Live EXPLAIN Validation

Pass the source database connection string if you want the generated SQL to be
validated against the live database before it is returned.

```bash
venv/bin/python query_ai_with_olama_and_deepseek.py \
  --driver postgresql \
  --schema-cache-key postgresql:database:public.customer \
  --connection-string 'postgresql://user:password@host:5432/database' \
  --max-retries 5 \
  --question "show top 10 customers by revenue"
```

### 5. View Session History

`session_history.py` displays past conversations in a ChatGPT-style format.
Each `(session_id, schema_cache_key)` pair appears as one entry in the sidebar
— the same way ChatGPT shows one item per focused conversation.

**Show the sidebar (all sessions grouped by recency):**

```bash
venv/bin/python session_history.py
```

Output:

```
──── Today ──────────────────────────────────────────────────────────────────
    1  Show total orders by customer                postgresql › postgresql:db:public.customer
       session: user-123   3 turns   last active: 2026-06-24 14:22

    2  Top employees by salary                      postgresql › postgresql:db:public.employee
       session: user-123   5 turns   last active: 2026-06-24 11:05

──── Yesterday ──────────────────────────────────────────────────────────────
    3  Monthly revenue by region                    snowflake › snowflake:db:sales.orders
       session: user-123   2 turns   last active: 2026-06-23 17:44
```

**Limit to last 20 entries:**

```bash
venv/bin/python session_history.py --limit 20
```

**Show all turns for one session + schema (open a chat from the sidebar):**

```bash
venv/bin/python session_history.py \
  --session-id "user-123" \
  --schema-cache-key "postgresql:database:public.employee"
```

Output:

```
════════════════════════════════════════════════════════════════════════════════
  Session : user-123
  Schema  : postgresql:database:public.employee
  Turns   : 3
════════════════════════════════════════════════════════════════════════════════

  Turn 1  ·  2026-06-24 11:00
  ┌ User
  │  show all employees
  └ SQL
     SELECT * FROM public.employee;

  Turn 2  ·  2026-06-24 11:02
  ┌ User
  │  now filter by department HR
  ├ Resolved
  │  show all employees where department is HR
  └ SQL
     SELECT * FROM public.employee WHERE department = 'HR';
```

**Show ALL turns for a session across every schema (full audit view):**

```bash
venv/bin/python session_history.py --session-id "user-123"
```

**JSON output for frontend or API consumption:**

```bash
# Sidebar as JSON
venv/bin/python session_history.py --json

# Turn detail as JSON
venv/bin/python session_history.py \
  --session-id "user-123" \
  --schema-cache-key "postgresql:database:public.employee" \
  --json
```

### 6. Check Raw History in PostgreSQL

Session metadata:

```sql
SELECT
  memory_key,
  schema_cache_key,
  driver_name,
  database_name,
  updated_at
FROM chat_sessions
ORDER BY updated_at DESC;
```

Turn history:

```sql
SELECT
  cs.memory_key    AS session_id,
  ct.schema_cache_key,
  ct.user_instruction,
  ct.generated_sql,
  ct.created_at
FROM chat_turns ct
JOIN chat_sessions cs ON cs.id = ct.session_id
ORDER BY ct.id DESC;
```

---

## Command Flags

### `query_ai_with_olama_and_deepseek.py`

| Flag | Default | Description |
|---|---|---|
| `--driver` | _(required)_ | Database driver name |
| `--question` | _(required)_ | Natural-language instruction |
| `--schema-cache-key` | _(none)_ | Redis key used as the runtime schema source |
| `--schema-file` | _(none)_ | JSON schema fallback when no cache key is provided |
| `--model` | `deepseek-coder-v2:16b` | Ollama model tag |
| `--timeout` | `600` | Ollama request timeout (seconds) |
| `--max-retries` | `3` | Self-correction retry attempts |
| `--connection-string` | _(none)_ | SQLAlchemy URL for live EXPLAIN validation |
| `--session-id` | _(none)_ | Enable session memory |
| `--history-turns` | `5` | Number of prior turns to load |
| `--session-ttl` | `86400` | Redis key TTL in seconds (24 h) |
| `--show-resolved` | _(off)_ | Print the resolved instruction as a comment |
| `--redis-url` | `redis://localhost:6380/0` | Redis connection URL |
| `--history-db-url` | `postgresql://admin:admin@localhost:5432/sql_ai` | PostgreSQL history DB URL |
| `--database` | _(none)_ | Optional database name passed into the prompt |

### `session_history.py`

| Flag | Default | Description |
|---|---|---|
| `--history-db-url` | `postgresql://admin:admin@localhost:5432/sql_ai` | PostgreSQL history DB URL |
| `--limit` | `50` | Max sessions to show in sidebar (list mode) |
| `--session-id` | _(none)_ | Show turns for this session (detail mode) |
| `--schema-cache-key` | _(none)_ | Scope detail mode to one schema context |
| `--json` | _(off)_ | Output raw JSON instead of human-readable format |

### `get_schema_details.py`

| Flag | Default | Description |
|---|---|---|
| `--connection-string` | _(required)_ | SQLAlchemy connection string |
| `--table` | _(required)_ | Starting table (`table` or `schema.table`) |
| `--schema` | _(none)_ | Schema containing --table |
| `--depth` | `1` | FK relationship hops to include |
| `--schema-cache-key` | _(required)_ | Redis key to write schema under |
| `--redis-url` | `redis://localhost:6380/0` | Redis URL for schema writes |
| `--schema-cache-ttl` | `1200` | Schema TTL in seconds (20 min) |
| `--output` | _(none)_ | Optional JSON file path for manual inspection |

---

## SQL Generation + Validation + Retry Flow

```
User instruction
      │
      ▼
build_messages()          ← dialect from DRIVER_TO_DIALECT
      │                     schema context (tables + relationships)
      ▼
chat_with_ollama()        ← POST to http://localhost:11434/api/chat
      │
      ▼
remove_markdown_fences()  ← strip ```sql ... ``` wrappers
      │
      ├─ "-- cannot answer from schema" → return immediately (no validation)
      │
      ▼
[Layer 1] validate_read_only_sql()
      │   Checks: first keyword is SELECT/WITH
      │           no forbidden words (INSERT, UPDATE, DROP, etc.)
      │
      ▼
[Layer 2] validate_sql_syntax()          ← uses sqlglot
      │   Dialect-aware AST parse via sqlglot.parse_one(sql, dialect=...)
      │   Skip only if the driver has no sqlglot dialect mapping
      │
      ▼
[Layer 3] validate_sql_with_explain()    ← requires sqlalchemy + DB (optional)
      │   PostgreSQL/MySQL/etc.: EXPLAIN <sql>
      │   SQL Server:            SET PARSEONLY ON; <sql>; SET PARSEONLY OFF
      │   Oracle:                EXPLAIN PLAN FOR <sql>
      │   BigQuery/Teradata/etc: skipped (unsupported)
      │
      ▼ (all layers passed)
  Return SQL
      │
      │ (any layer failed AND attempts remaining)
      ▼
Append to messages:
  {"role": "assistant", "content": <failed_sql>}
  {"role": "user",      "content": "That SQL is invalid. Error: ... Fix it."}
      │
      └─────────────────────────────► retry (up to max_retries)
                                            │
                                            └─ exhausted → raise SqlGenerationError
```

---

## Session Management (Redis + PostgreSQL)

### Multi-schema isolation within one session

The same `session_id` can be used across different schemas. Turns are isolated
per `(session_id, schema_cache_key)` in both Redis and PostgreSQL — so querying
`public.employee` and `demo.products` in the same session never mixes context.

```
session: user-123 + schema: postgresql:db:public.employee
  Redis key : sql_ai:session:user-123:postgresql:db:public.employee:turns
  PG filter : WHERE session_id = ? AND schema_cache_key = 'postgresql:db:public.employee'

session: user-123 + schema: postgresql:db:demo.products
  Redis key : sql_ai:session:user-123:postgresql:db:demo.products:turns
  PG filter : WHERE session_id = ? AND schema_cache_key = 'postgresql:db:demo.products'
```

### Read/write flow

```
First call with session_id="user-123", schema="postgresql:db:public.employee"
────────────────────────────────────────────────────────────────────────────
load_recent_turns("user-123", schema_cache_key="postgresql:db:public.employee")
  → Redis miss (new context)
  → PostgreSQL miss (no rows yet)
  → returns []

generate_sql(...)  → validated SQL returned

save_turn("user-123", turn, schema_cache_key="postgresql:db:public.employee")
  → Redis:      RPUSH schema-scoped key; LTRIM; EXPIRE
  → PostgreSQL: UPSERT chat_sessions; INSERT chat_turns (with schema_cache_key)


Second call — same session, same schema
────────────────────────────────────────
load_recent_turns("user-123", "postgresql:db:public.employee")
  → Redis HIT → returns [turn1]   (fast path, no SQL query)

resolve_follow_up_instruction("now filter by date", turns=[turn1])
  → rewrites into standalone instruction


Second call — same session, DIFFERENT schema
─────────────────────────────────────────────
load_recent_turns("user-123", "postgresql:db:demo.products")
  → Redis miss (different key — isolated from employee turns)
  → PostgreSQL miss (no turns under this schema yet)
  → returns []   ← clean slate, no contamination
```

---

## Schema Cache / JSON Format

The schema extractor saves this payload into Redis under `--schema-cache-key`.
It can also write the same payload to `--output` as JSON for manual validation.
At runtime, `query_ai_with_olama_and_deepseek.py --schema-cache-key ...` reads
from Redis and does not read the JSON file.

The fallback `--schema-file` JSON format can be either:

### Simple list format (minimum required)

```json
[
  {
    "name": "orders",
    "schema": "public",
    "columns": [
      {"name": "id",          "type": "integer"},
      {"name": "customer_id", "type": "integer"},
      {"name": "amount",      "type": "numeric"},
      {"name": "created_at",  "type": "timestamp"}
    ]
  }
]
```

### Full wrapper format (produced by schema extractors)

```json
{
  "database_type": "PostgreSQL",
  "selected_table": {"schema": "public", "name": "orders"},
  "relationship_depth": 1,
  "tables": [
    {
      "schema": "public",
      "name": "orders",
      "columns": [
        {"name": "id",          "type": "integer"},
        {"name": "customer_id", "type": "integer"},
        {"name": "amount",      "type": "numeric"}
      ],
      "primary_key": ["id"]
    }
  ],
  "relationships": [
    {
      "from_schema": "public",
      "from_table": "orders",
      "from_column": "customer_id",
      "to_schema": "public",
      "to_table": "customers",
      "to_column": "id"
    }
  ]
}
```

---

## Supported Database Drivers

| Driver name (pass to `--driver`) | SQL Dialect |
|---|---|
| `postgresql` | PostgreSQL |
| `rds postgresql` | PostgreSQL |
| `rds postgresql aurora` | PostgreSQL |
| `azure postgresql` | PostgreSQL |
| `azure cosmos postgresql` | PostgreSQL |
| `cockroachdb` | CockroachDB (PostgreSQL-compatible) |
| `mysql` | MySQL |
| `rds mysql` | MySQL |
| `rds mysql aurora` | MySQL |
| `azure mysql` | MySQL |
| `mariadb` | MariaDB SQL |
| `rds mariadb` | MariaDB SQL |
| `mssql` | Microsoft SQL Server T-SQL |
| `rds mssql` | Microsoft SQL Server T-SQL |
| `azure sql server` | Microsoft SQL Server T-SQL |
| `oracle` | Oracle SQL |
| `rds oracle` | Oracle SQL |
| `snowflake` | Snowflake SQL |
| `redshift` | Amazon Redshift SQL |
| `bigquery` | Google BigQuery Standard SQL |
| `sap hana` | SAP HANA SQL |
| `vertica` | Vertica SQL |
| `teradata` | Teradata SQL |
| `monetdb` | MonetDB SQL |
| `ibm db2` | IBM Db2 SQL |
| `rds ibm db2` | IBM Db2 SQL |

The following drivers are **not supported** (NoSQL / non-SQL data sources) and
will raise `UnsupportedDriverError`:

`mongodb`, `cassandra`, `couchbase`, `azure cosmos nosql`,
`azure cosmos mongodb`, `s3`, `ftp`, `sftp`, `file`, `upload`

---

## Quick-Start Example

```bash
# 1. Start Ollama and pull the model
ollama serve &
ollama pull deepseek-coder-v2:16b

# 2. Start Redis + PostgreSQL (for session history)
docker compose up -d

# 3. Extract schema from your database
venv/bin/python get_schema_details.py \
    --connection-string "postgresql://user:pass@localhost:5432/mydb" \
    --table public.orders \
    --schema-cache-key postgresql:mydb:public.orders \
    --output selected_schema.json

# 4. Generate SQL (stateless)
venv/bin/python query_ai_with_olama_and_deepseek.py \
    --driver postgresql \
    --schema-cache-key postgresql:mydb:public.orders \
    --question "Top 10 customers by total spend in 2024"

# 5. Generate SQL with session memory
venv/bin/python query_ai_with_olama_and_deepseek.py \
    --driver postgresql \
    --schema-cache-key postgresql:mydb:public.orders \
    --session-id user-123 \
    --question "Top 10 customers by total spend in 2024"

# 6. Follow-up question
venv/bin/python query_ai_with_olama_and_deepseek.py \
    --driver postgresql \
    --schema-cache-key postgresql:mydb:public.orders \
    --session-id user-123 \
    --question "now filter to only USA customers"

# 7. View session history
venv/bin/python session_history.py
venv/bin/python session_history.py \
    --session-id "user-123" \
    --schema-cache-key "postgresql:mydb:public.orders"
```
