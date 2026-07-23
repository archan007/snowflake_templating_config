"""
Generators for non-table objects: views, streams, tasks, stored procs,
dynamic tables, stages, file formats.

These objects are all CREATE OR REPLACE (they don't hold data or their state
is either trivial or rebuilt cheaply).
"""
from __future__ import annotations

from pathlib import Path

from ..config_loader import ObjectDef, resolve_placeholders


def _read_sql(obj: ObjectDef, key: str = "sql_file") -> str:
    """Read a referenced SQL file relative to the bundle and resolve any
    ${VAR} placeholders against the bundle's env context. This lets user
    SQL reference ${DATABASE} and stay portable across DEV/UAT/PROD."""
    rel = obj.props.get(key)
    if not rel:
        raise ValueError(
            f"{obj.object_type} '{obj.fqn}' must specify '{key}' in bundle.yaml"
        )
    sql_path = obj.bundle_path / rel
    if not sql_path.exists():
        raise FileNotFoundError(f"SQL file not found: {sql_path}")
    raw = sql_path.read_text().strip().rstrip(";")
    try:
        return resolve_placeholders(raw, obj.context)
    except ValueError as e:
        raise ValueError(f"{sql_path}: {e}") from e


# ---------- VIEW ----------

def generate_create_view(obj: ObjectDef) -> str:
    sql = _read_sql(obj)
    return f"CREATE OR REPLACE VIEW {obj.fqn} AS\n{sql};"


def generate_drop_view(fqn: str) -> str:
    return f"DROP VIEW IF EXISTS {fqn};"


# ---------- STREAM ----------

def generate_create_stream(obj: ObjectDef) -> str:
    source = obj.props.get("on_table") or obj.props.get("on_view")
    if not source:
        raise ValueError(
            f"Stream '{obj.fqn}' must specify 'on_table' or 'on_view' in bundle.yaml"
        )
    source_kind = "TABLE" if "on_table" in obj.props else "VIEW"
    append_only = obj.props.get("append_only", False)
    show_initial = obj.props.get("show_initial_rows", False)

    clauses = []
    if append_only:
        clauses.append("APPEND_ONLY = TRUE")
    if show_initial:
        clauses.append("SHOW_INITIAL_ROWS = TRUE")
    tail = (" " + " ".join(clauses)) if clauses else ""

    return (
        f"CREATE OR REPLACE STREAM {obj.fqn} ON {source_kind} {source}{tail};"
    )


def generate_drop_stream(fqn: str) -> str:
    return f"DROP STREAM IF EXISTS {fqn};"


# ---------- TASK ----------

def generate_create_task(obj: ObjectDef) -> str:
    sql = _read_sql(obj)
    warehouse = obj.props.get("warehouse")
    if not warehouse:
        raise ValueError(f"Task '{obj.fqn}' must specify 'warehouse'")

    schedule = obj.props.get("schedule")
    after = obj.props.get("after", [])
    when = obj.props.get("when")

    lines = [f"CREATE OR REPLACE TASK {obj.fqn}"]
    lines.append(f"  WAREHOUSE = {warehouse}")
    if schedule:
        lines.append(f"  SCHEDULE = '{schedule}'")
    if after:
        if isinstance(after, str):
            after = [after]
        lines.append(f"  AFTER {', '.join(after)}")
    if when:
        lines.append(f"  WHEN {when}")
    lines.append("AS")
    lines.append(sql + ";")
    return "\n".join(lines)


def generate_drop_task(fqn: str) -> str:
    return f"DROP TASK IF EXISTS {fqn};"


# ---------- STORED PROCEDURE ----------

def generate_create_procedure(obj: ObjectDef) -> str:
    sql = _read_sql(obj)
    # For stored procs the sql_file contains the full CREATE OR REPLACE PROCEDURE
    # statement, because signature + language + return type vary widely.
    # The engine just wraps it with FQN substitution.
    # We require the SQL file to contain the full statement; we only validate
    # it references the right FQN.
    if obj.fqn.split(".")[-1].upper() not in sql.upper():
        raise ValueError(
            f"Stored procedure SQL file for {obj.fqn} does not reference "
            f"the procedure name. Ensure the CREATE OR REPLACE PROCEDURE "
            f"statement uses the name '{obj.name}'."
        )
    return sql + ";"


def generate_drop_procedure(obj_or_fqn) -> str:
    """Procedures need a signature to drop. Expects either an ObjectDef with
    'signature' in props, or a raw FQN string (which will attempt DROP with
    no args — caller should use bundle info where possible)."""
    if isinstance(obj_or_fqn, ObjectDef):
        sig = obj_or_fqn.props.get("signature", "()")
        return f"DROP PROCEDURE IF EXISTS {obj_or_fqn.fqn}{sig};"
    return f"DROP PROCEDURE IF EXISTS {obj_or_fqn}();"


# ---------- DYNAMIC TABLE ----------

def generate_create_dynamic_table(obj: ObjectDef) -> str:
    sql = _read_sql(obj)
    warehouse = obj.props.get("warehouse")
    target_lag = obj.props.get("target_lag")
    if not warehouse or not target_lag:
        raise ValueError(
            f"Dynamic table '{obj.fqn}' must specify 'warehouse' and 'target_lag'"
        )
    refresh_mode = obj.props.get("refresh_mode", "AUTO")
    return (
        f"CREATE OR REPLACE DYNAMIC TABLE {obj.fqn}\n"
        f"  TARGET_LAG = '{target_lag}'\n"
        f"  WAREHOUSE = {warehouse}\n"
        f"  REFRESH_MODE = {refresh_mode}\n"
        f"AS\n{sql};"
    )


def generate_drop_dynamic_table(fqn: str) -> str:
    return f"DROP DYNAMIC TABLE IF EXISTS {fqn};"


# ---------- STAGE ----------

def generate_create_stage(obj: ObjectDef) -> str:
    url = obj.props.get("url")
    storage_integration = obj.props.get("storage_integration")
    file_format = obj.props.get("file_format")

    lines = [f"CREATE OR REPLACE STAGE {obj.fqn}"]
    if url:
        lines.append(f"  URL = '{url}'")
    if storage_integration:
        lines.append(f"  STORAGE_INTEGRATION = {storage_integration}")
    if file_format:
        lines.append(f"  FILE_FORMAT = ( FORMAT_NAME = {file_format} )")
    return "\n".join(lines) + ";"


def generate_drop_stage(fqn: str) -> str:
    return f"DROP STAGE IF EXISTS {fqn};"


# ---------- FILE FORMAT ----------

def generate_create_file_format(obj: ObjectDef) -> str:
    fmt_type = obj.props.get("type")
    if not fmt_type:
        raise ValueError(f"File format '{obj.fqn}' must specify 'type'")
    options = obj.props.get("options", {}) or {}

    lines = [f"CREATE OR REPLACE FILE FORMAT {obj.fqn}"]
    lines.append(f"  TYPE = {fmt_type}")
    for k, v in options.items():
        if isinstance(v, bool):
            lines.append(f"  {k.upper()} = {str(v).upper()}")
        elif isinstance(v, str):
            lines.append(f"  {k.upper()} = '{v}'")
        else:
            lines.append(f"  {k.upper()} = {v}")
    return "\n".join(lines) + ";"


def generate_drop_file_format(fqn: str) -> str:
    return f"DROP FILE FORMAT IF EXISTS {fqn};"
