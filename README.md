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
- Follow-up resolution ("now filter by last month" → standalone instruction)
- Schema auto-extraction from any supported SQL database (via SQLAlchemy Inspector)
- Runtime schema loading from Redis, with JSON output kept only for validation/debugging

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
   │  └─ list key per session   │
   │     last N turns as JSON   │
   │                            │
   │  PostgreSQL (durable)      │
   │  └─ chat_sessions table    │
   │  └─ chat_turns table       │
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

### `get_postgreql_Details.py`

Connects to a PostgreSQL database and extracts schema metadata for a chosen
starting table plus all tables reachable via foreign keys up to a configurable
depth. It writes the runtime schema snapshot to Redis and can also write the
same schema to JSON so you can inspect it manually.

### `query_ai_with_olama_and_deepseek.py`

The main SQL generation engine. Accepts a Redis schema cache key, a driver
name, and a natural-language instruction. Builds a prompt, calls Ollama,
validates and (if needed) self-corrects the output, and returns the final SQL
string. It still supports `--schema-file` as a fallback for local testing, but
the normal runtime path is `--schema-cache-key`.

### `session_store.py`

Manages conversation history in a dual-layer store:
- **Redis** for fast in-memory access to recent turns.
- **PostgreSQL** as a durable fallback and permanent log.

### `new.py`

A minimal prototype that asks for a question, guesses the database type from
a connection string, and calls Ollama without any schema.  Useful for quick
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

PostgreSQL-specific extractor:

```bash
venv/bin/python get_postgreql_Details.py \
  --connection-string 'postgresql://user:password@host:5432/database' \
  --table public.customer \
  --depth 2 \
  --schema-cache-key postgresql:database:public.customer \
  --output selected_schema.json
```

Generic extractor for other databases:

```bash
venv/bin/python for_different_src/get_schema_details.py \
  --driver snowflake \
  --connection-string 'snowflake://user:password@account/database/schema?warehouse=COMPUTE_WH&role=ROLE_NAME' \
  --table schema.table_name \
  --depth 2 \
  --schema-cache-key snowflake:database:schema.table_name \
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
`--session-id` for the same chat plus same connection/schema scope.

```bash
venv/bin/python query_ai_with_olama_and_deepseek.py \
  --driver postgresql \
  --schema-cache-key postgresql:database:public.customer \
  --session-id chat-123:postgresql:database:public.customer \
  --question "show total orders by customer"
```

Follow-up:

```bash
venv/bin/python query_ai_with_olama_and_deepseek.py \
  --driver postgresql \
  --schema-cache-key postgresql:database:public.customer \
  --session-id chat-123:postgresql:database:public.customer \
  --show-resolved \
  --question "now only for 2024"
```

For a different database or different schema/table scope, use a different
`--session-id` and a different `--schema-cache-key`.

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

### 5. Check Stored Session Metadata

Session rows store the internal UUID plus readable metadata:

```sql
SELECT
  id,
  memory_key,
  schema_cache_key,
  driver_name,
  database_name,
  updated_at
FROM chat_sessions
ORDER BY updated_at DESC;
```

Turn history is stored in `chat_turns`:

```sql
SELECT
  session_id,
  user_instruction,
  resolved_instruction,
  generated_sql,
  created_at
FROM chat_turns
ORDER BY id DESC;
```

---

## Command Flags

| Flag | Default | Description |
|---|---|---|
| `--model` | `deepseek-coder-v2:16b` | Ollama model tag |
| `--timeout` | `600` | Ollama request timeout (seconds) |
| `--max-retries` | `3` | Self-correction retry attempts |
| `--connection-string` | _(none)_ | SQLAlchemy URL for EXPLAIN validation |
| `--schema-cache-key` | _(none)_ | Redis key used as the runtime schema source |
| `--schema-file` | _(none)_ | JSON schema fallback when no cache key is provided |
| `--session-id` | _(none)_ | Enable session memory |
| `--history-turns` | `5` | Number of prior turns to load |
| `--session-ttl` | `86400` | Redis key TTL in seconds (24 h) |
| `--show-resolved` | _(off)_ | Print the resolved instruction |
| `--redis-url` | `redis://localhost:6380/0` | Redis connection URL |
| `--history-db-url` | `postgresql://admin:admin@localhost:5432/sql_ai` | PostgreSQL history DB URL |

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

```
First call with session_id="abc"
────────────────────────────────
load_recent_turns("abc")
  → Redis miss (new session)
  → PostgreSQL miss (no rows yet)
  → returns []

resolve_follow_up_instruction(instruction, turns=[])
  → returns instruction unchanged (no model call)

generate_sql(...)
  → validated SQL returned

save_turn("abc", turn)
  → Redis:      RPUSH key <json>; LTRIM to N; EXPIRE
  → PostgreSQL: UPSERT chat_sessions; INSERT chat_turns


Second call with session_id="abc"
───────────────────────────────────
load_recent_turns("abc")
  → Redis HIT  → returns [turn1]   (fast path, no SQL query)

resolve_follow_up_instruction("now filter by date", turns=[turn1])
  → calls Ollama to rewrite into standalone instruction

generate_sql(resolved_instruction, ...)
  → ...


After Redis eviction or restart
────────────────────────────────
load_recent_turns("abc")
  → Redis miss
  → PostgreSQL returns rows
  → _refresh_redis() warms the cache
  → returns turns
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
  },
  {
    "name": "customers",
    "schema": "public",
    "columns": [
      {"name": "id",   "type": "integer"},
      {"name": "name", "type": "text"}
    ]
  }
]
```

### Full wrapper format (produced by `get_postgreql_Details.py`)

```json
{
  "database_type": "PostgreSQL",
  "selected_table": {"schema": "public", "name": "orders"},
  "relationship_depth": 1,
  "tables": [
    {
      "schema": "public",
      "name": "orders",
      "type": "BASE TABLE",
      "columns": [
        {"name": "id",          "type": "integer"},
        {"name": "customer_id", "type": "integer"},
        {"name": "amount",      "type": "numeric"}
      ],
      "primary_key": ["id"]
    },
    {
      "schema": "public",
      "name": "customers",
      "type": "BASE TABLE",
      "columns": [
        {"name": "id",   "type": "integer"},
        {"name": "name", "type": "text"}
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

The `schema` field on each table is optional.  If omitted, the table is
referenced without a schema prefix in the prompt.

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
venv/bin/python get_postgreql_Details.py \
    --connection-string "postgresql://user:pass@localhost:5432/mydb" \
    --table orders \
    --schema-cache-key postgresql:mydb:orders \
    --output selected_schema.json

# 4. Generate SQL
venv/bin/python query_ai_with_olama_and_deepseek.py \
    --driver postgresql \
    --question "Top 10 customers by total spend in 2024" \
    --schema-cache-key postgresql:mydb:orders
```
