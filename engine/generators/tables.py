"""
Table DDL generator.

Strategy:
- If table does not exist in Snowflake -> CREATE TABLE
- If table exists -> compute column-level diff and emit ALTER statements
- Never CREATE OR REPLACE (would destroy data)
"""
from __future__ import annotations

from ..config_loader import Column, ObjectDef
from ..state_reader import ExistingObject


def _render_column(col: Column) -> str:
    parts = [f'"{col.column_name}"', col.data_type]
    if not col.nullable:
        parts.append("NOT NULL")
    if col.default_value:
        parts.append(f"DEFAULT {col.default_value}")
    if col.description:
        escaped = col.description.replace("'", "''")
        parts.append(f"COMMENT '{escaped}'")
    return " ".join(parts)


def _clustering_clause(cols: list[Column]) -> str:
    keys = [c.column_name for c in cols if c.clustering_key]
    if not keys:
        return ""
    quoted = ", ".join(f'"{k}"' for k in keys)
    return f"\nCLUSTER BY ({quoted})"


def generate_create_table(obj: ObjectDef) -> str:
    col_lines = [f"  {_render_column(c)}" for c in obj.columns]
    cluster = _clustering_clause(obj.columns)
    table_comment = obj.props.get("description", "")
    comment_clause = ""
    if table_comment:
        escaped = table_comment.replace("'", "''")
        comment_clause = f"\nCOMMENT = '{escaped}'"

    return (
        f"CREATE TABLE IF NOT EXISTS {obj.fqn} (\n"
        + ",\n".join(col_lines)
        + f"\n){comment_clause}{cluster};"
    )


def _normalize_type(t: str) -> str:
    """Normalise Snowflake type strings for comparison.

    Snowflake reports types as e.g. 'TEXT', 'NUMBER', 'TIMESTAMP_NTZ' without
    the parameters. CSV may have 'VARCHAR(256)', 'NUMBER(18,2)'. We compare
    base types only for ALTER detection; a PR wanting to change VARCHAR(50)
    to VARCHAR(100) would need to be detected separately (widening).
    """
    t = t.upper().strip()
    base = t.split("(")[0]
    return {
        "VARCHAR": "TEXT",
        "STRING": "TEXT",
        "INT": "NUMBER",
        "INTEGER": "NUMBER",
        "BIGINT": "NUMBER",
        "DECIMAL": "NUMBER",
        "NUMERIC": "NUMBER",
    }.get(base, base)


def generate_alter_table(obj: ObjectDef, existing: ExistingObject) -> list[str]:
    """Compute column-level diff and emit ALTER statements."""
    existing_cols = {c.name.upper(): c for c in existing.columns}
    desired_cols = {c.column_name.upper(): c for c in obj.columns}

    statements: list[str] = []

    # Columns to add
    for name, col in desired_cols.items():
        if name not in existing_cols:
            statements.append(
                f"ALTER TABLE {obj.fqn} ADD COLUMN {_render_column(col)};"
            )

    # Columns to drop (only happens if confirmed at the bundle level — the
    # orchestrator is responsible for checking that; we just emit the DDL)
    for name in existing_cols:
        if name not in desired_cols:
            statements.append(
                f'ALTER TABLE {obj.fqn} DROP COLUMN "{name}";'
            )

    # Type changes (widening only — anything else is flagged as breaking elsewhere)
    for name, desired in desired_cols.items():
        if name in existing_cols:
            ex = existing_cols[name]
            if _normalize_type(desired.data_type) != _normalize_type(ex.data_type):
                statements.append(
                    f'ALTER TABLE {obj.fqn} ALTER COLUMN "{name}" '
                    f"SET DATA TYPE {desired.data_type};"
                )

    return statements


def generate_drop_table(fqn: str) -> str:
    return f"DROP TABLE IF EXISTS {fqn};"
