"""Statement allowlist for the read-only database MCP server.

Policy: exactly one statement, and it must be a SELECT / set operation (optionally with CTEs)
containing no write, DDL, session, or side-effecting constructs anywhere in its tree.
This is layer 1; layer 2 is the read-only Postgres role (see demo_target/seed.py).
"""

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError


class GuardError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


_ALLOWED_ROOTS = (exp.Select, exp.SetOperation, exp.Subquery)

_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Into,
    exp.Set,
    exp.TruncateTable,
    exp.Grant,
    exp.Revoke,
    exp.Lock,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Use,
    exp.Analyze,
)

_FORBIDDEN_FUNCTION_PREFIXES = (
    "pg_sleep",
    "pg_read",
    "pg_ls",
    "pg_terminate",
    "pg_cancel",
    "pg_reload",
    "pg_advisory",
    "pg_stat_reset",
    "pg_create",
    "pg_drop",
    "pg_switch",
    "pg_replication",
    "pg_logical",
    "pg_export",
    "pg_import",
    "dblink",
    "lo_",
    "set_config",
    "nextval",
    "setval",
    "query_to_xml",
    "table_to_xml",
    "cursor_to_xml",
    "database_to_xml",
    "schema_to_xml",
)


def _function_name(node: exp.Expr) -> str | None:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    if isinstance(node, exp.Func):
        return node.sql_name().lower()
    return None


def validate_select(sql: str) -> exp.Expression:
    """Return the parsed statement if allowed, else raise GuardError."""
    if not sql or not sql.strip():
        raise GuardError("invalid_sql", "empty statement")
    try:
        statements = [s for s in sqlglot.parse(sql, dialect="postgres") if s is not None]
    except SqlglotError as exc:
        raise GuardError("invalid_sql", f"could not parse statement: {exc}") from exc
    if len(statements) != 1:
        raise GuardError("forbidden_statement", "exactly one statement is allowed")
    root = statements[0]
    if not isinstance(root, _ALLOWED_ROOTS):
        raise GuardError(
            "forbidden_statement", f"only SELECT statements are allowed, got {root.key.upper()}"
        )
    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise GuardError("forbidden_statement", f"{node.key.upper()} is not allowed in queries")
        name = _function_name(node)
        if name and name.startswith(_FORBIDDEN_FUNCTION_PREFIXES):
            raise GuardError("forbidden_function", f"function {name}() is not allowed")
    return root
