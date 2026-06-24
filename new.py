"""
new.py — Simple prototype / demo script for quick ad-hoc query generation.

Purpose
-------
This is a lightweight standalone demo that asks the user for a query, detects
the database type from a hard-coded connection string, and calls a local Ollama
model to generate the appropriate SQL or NoSQL query.

Differences from the production pipeline (query_ai_with_olama_and_deepseek.py)
-------------------------------------------------------------------------------
- No schema is provided — the model must infer reasonable table/field names
  from the user's request and the detected database type alone.
- No session history — every invocation is independent (no Redis/PostgreSQL).
- No validation — the raw model output is printed without read-only guards,
  sqlglot checks, or EXPLAIN round-trips.
- Supports NoSQL outputs (MongoDB, Cassandra CQL, Couchbase N1QL) in addition
  to SQL dialects — the model is instructed to pick the right query language.

Use this script for quick prototyping or testing connectivity to Ollama.
For production use, use query_ai_with_olama_and_deepseek.py with a real schema.
"""

import json
import re
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# ---
# Model + Ollama configuration
# ---

# Local Ollama model tag — must be pulled before running (ollama pull <model>).
MODEL_NAME = "deepseek-coder-v2:16b"

# Ollama chat completions endpoint.
OLLAMA_URL = "http://localhost:11434/api/chat"

# ---
# Driver hint map — used by guess_database_type()
#
# Maps substrings found in a connection string scheme or URL to a
# human-readable database/query-language label.  This label is injected into
# the prompt so the model knows which dialect to use.
# ---

DRIVER_HINTS = {
    "postgresql": "PostgreSQL",
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "mariadb": "MariaDB",
    "mssql": "Microsoft SQL Server T-SQL",
    "sqlserver": "Microsoft SQL Server T-SQL",
    "oracle": "Oracle SQL",
    "snowflake": "Snowflake SQL",
    "redshift": "Amazon Redshift SQL",
    "bigquery": "Google BigQuery Standard SQL",
    "mongodb": "MongoDB query or aggregation pipeline",
    "mongo": "MongoDB query or aggregation pipeline",
    "cassandra": "Cassandra CQL",
    "couchbase": "Couchbase N1QL",
}


def guess_database_type(connection_string: str) -> str:
    """
    Heuristically detect the database type from a connection string.

    Strategy:
    1. Parse the URL scheme with urllib and check it against DRIVER_HINTS.
       This handles standard DSNs like "postgresql://..." or "mysql://...".
    2. Fall back to a substring search in the full (lowercased) connection
       string for cases where the scheme alone is insufficient (e.g. JDBC URLs
       or non-standard formats).
    3. Return "Unknown database" if no hint matches — the model will still
       attempt to generate a generic SQL query.
    """
    # First pass: check the URL scheme
    parsed = urlparse(connection_string)
    scheme = parsed.scheme.lower()

    for key, database_type in DRIVER_HINTS.items():
        if key in scheme:
            return database_type

    # Second pass: substring search across the full connection string
    lowered = connection_string.lower()
    for key, database_type in DRIVER_HINTS.items():
        if key in lowered:
            return database_type

    return "Unknown database"


def remove_markdown_fences(text: str) -> str:
    """
    Strip code-block fences (```sql, ```js, ```json, etc.) from model output.

    The model wraps its response in markdown fences even when asked not to.
    This function strips them before printing to the terminal.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Remove opening fence with optional language tag
        cleaned = re.sub(r"^```(?:sql|js|javascript|json)?\s*", "", cleaned, flags=re.I)
        # Remove closing fence
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def ask_ollama(messages: list[dict]) -> str:
    """
    Send a chat completion request to Ollama and return the raw response text.

    Uses stdlib urllib (no extra HTTP library needed).  Times out after 180 s
    and prints a friendly error message if Ollama is unreachable.

    Returns the assistant message content string.
    """
    payload = {
        "model": MODEL_NAME,
        "stream": False,
        "messages": messages,
        "options": {
            "temperature": 0.1,   # low temperature for more deterministic output
            "top_p": 0.9,
        },
    }

    request = Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    try:
        with urlopen(request, timeout=180) as result:
            response = json.loads(result.read().decode("utf-8"))
    except URLError as error:
        # Surface a helpful message before exiting — common on first run
        print("Could not connect to Ollama.")
        print("Make sure Ollama is running with: ollama serve")
        print(f"Details: {error}")
        raise SystemExit(1)

    return response["message"]["content"]


# ---
# Demo execution
#
# Hard-coded connection string for a sample database.
# Change this to point at your own database for a different demo.
# ---

connection_string = "postgresql://db_superadmin:DbSup3rAdm1n!2026@mysql.db.datafuseai.org:15432/tpch_mysql_results"

# Prompt the user for a query request
user_question = input("What query do you want? ").strip()

# Detect the database type from the connection string
database_type = guess_database_type(connection_string)

# ---
# Build the prompt messages
#
# System prompt: instructs the model on its role and output format.
# User prompt: provides the connection string, detected DB type, and the
#              user's question.
# ---

messages = [
    {
        "role": "system",
        "content": """
You are an expert database query generator.
Return only one query.
Do not use markdown.
Do not explain the query.
Use the connection string only to detect the database type and dialect.
You cannot actually connect to the database or inspect its schema.
Do not invent unrelated table names.
If the user request mentions a table or collection name, use that exact name.
If table or field names are unknown, make the smallest reasonable guess from the user request.
For SQL databases, prefer read-only SELECT/WITH queries unless the user explicitly asks for DDL or DML.
For MongoDB, return a valid db.collection.find(...) or db.collection.aggregate([...]) query.
For Cassandra, return valid CQL.
For Couchbase, return valid N1QL.
For complex requests, use joins, CTEs, nested queries, window functions, grouping, or unions when useful and supported by the database.
""".strip(),
    },
    {
        "role": "user",
        "content": f"""
Connection string:
{connection_string}

Detected database type:
{database_type}

User request:
{user_question}
""".strip(),
    },
]

# ---
# Call the model and print the result
# ---

generated_query = ask_ollama(messages)
print(remove_markdown_fences(generated_query))
