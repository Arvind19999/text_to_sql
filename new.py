import json
import re
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


MODEL_NAME = "deepseek-coder-v2:16b"
OLLAMA_URL = "http://localhost:11434/api/chat"


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


def guess_database_type(connection_string):
    parsed = urlparse(connection_string)
    scheme = parsed.scheme.lower()

    for key, database_type in DRIVER_HINTS.items():
        if key in scheme:
            return database_type

    lowered = connection_string.lower()
    for key, database_type in DRIVER_HINTS.items():
        if key in lowered:
            return database_type

    return "Unknown database"


def remove_markdown_fences(text):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:sql|js|javascript|json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def ask_ollama(messages):
    payload = {
        "model": MODEL_NAME,
        "stream": False,
        "messages": messages,
        "options": {
            "temperature": 0.1,
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
        print("Could not connect to Ollama.")
        print("Make sure Ollama is running with: ollama serve")
        print(f"Details: {error}")
        raise SystemExit(1)

    return response["message"]["content"]


connection_string = "postgresql://db_superadmin:DbSup3rAdm1n!2026@mysql.db.datafuseai.org:15432/tpch_mysql_results"
user_question = input("What query do you want? ").strip()

database_type = guess_database_type(connection_string)

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

generated_query = ask_ollama(messages)
print(remove_markdown_fences(generated_query))
