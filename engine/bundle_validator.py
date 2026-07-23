"""
Bundle validation for PR checks and deployment pre-flight.

Two categories of checks:
  1. Per-object validation — each object has valid properties, referenced files
     exist, options are Snowflake-supported, CSV schemas are well-formed.
  2. Inter-dependency validation — cross-references between objects within the
     same environment (streams reference existing tables, tasks reference
     existing stored procs, views reference existing tables, etc.).

Usage:
    python -m engine.bundle_validator --env DEV --bundles-root bundles \
        --platform-root platform

Exit code 0 = all checks pass, 1 = validation errors found.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config_loader import (
    Bundle,
    KNOWN_PROPERTIES,
    ObjectDef,
    load_all_bundles,
    load_platform_config,
    resolve_placeholders,
)


@dataclass
class ValidationError:
    bundle: str
    object_name: str
    object_type: str
    check: str
    message: str

    def __str__(self) -> str:
        return f"[{self.bundle}] {self.object_type} '{self.object_name}': {self.check} - {self.message}"


@dataclass
class ValidationResult:
    errors: list[ValidationError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def add(self, bundle: str, obj_name: str, obj_type: str, check: str, msg: str) -> None:
        self.errors.append(ValidationError(bundle, obj_name, obj_type, check, msg))

    def merge(self, other: "ValidationResult") -> None:
        self.errors.extend(other.errors)


# ---------------------------------------------------------------------------
# Snowflake-supported options for validation
# ---------------------------------------------------------------------------

SNOWFLAKE_DATA_TYPES = {
    "VARCHAR", "TEXT", "STRING", "CHAR", "CHARACTER",
    "NUMBER", "DECIMAL", "NUMERIC", "INT", "INTEGER", "BIGINT",
    "SMALLINT", "TINYINT", "BYTEINT", "FLOAT", "FLOAT4", "FLOAT8",
    "DOUBLE", "DOUBLE PRECISION", "REAL",
    "BOOLEAN",
    "DATE", "DATETIME", "TIME", "TIMESTAMP", "TIMESTAMP_LTZ",
    "TIMESTAMP_NTZ", "TIMESTAMP_TZ",
    "VARIANT", "OBJECT", "ARRAY",
    "BINARY", "VARBINARY",
    "GEOGRAPHY", "GEOMETRY",
}

FILE_FORMAT_TYPES = {"CSV", "JSON", "AVRO", "ORC", "PARQUET", "XML"}

CSV_FORMAT_OPTIONS = {
    "COMPRESSION", "RECORD_DELIMITER", "FIELD_DELIMITER",
    "FILE_EXTENSION", "PARSE_HEADER", "SKIP_HEADER",
    "SKIP_BLANK_LINES", "DATE_FORMAT", "TIME_FORMAT",
    "TIMESTAMP_FORMAT", "BINARY_FORMAT", "ESCAPE",
    "ESCAPE_UNENCLOSED_FIELD", "TRIM_SPACE", "FIELD_OPTIONALLY_ENCLOSED_BY",
    "NULL_IF", "ERROR_ON_COLUMN_COUNT_MISMATCH", "REPLACE_INVALID_CHARACTERS",
    "EMPTY_FIELD_AS_NULL", "SKIP_BYTE_ORDER_MARK", "ENCODING",
}

JSON_FORMAT_OPTIONS = {
    "COMPRESSION", "DATE_FORMAT", "TIME_FORMAT", "TIMESTAMP_FORMAT",
    "BINARY_FORMAT", "TRIM_SPACE", "NULL_IF", "FILE_EXTENSION",
    "ENABLE_OCTAL", "ALLOW_DUPLICATE", "STRIP_OUTER_ARRAY",
    "STRIP_NULL_VALUES", "REPLACE_INVALID_CHARACTERS",
    "IGNORE_UTF8_ERRORS", "SKIP_BYTE_ORDER_MARK",
}

AVRO_FORMAT_OPTIONS = {
    "COMPRESSION", "TRIM_SPACE", "REPLACE_INVALID_CHARACTERS",
    "NULL_IF",
}

PARQUET_FORMAT_OPTIONS = {
    "COMPRESSION", "SNAPPY_COMPRESSION", "BINARY_AS_TEXT",
    "USE_VECTORIZED_SCANNER", "TRIM_SPACE", "REPLACE_INVALID_CHARACTERS",
    "NULL_IF",
}

ORC_FORMAT_OPTIONS = {
    "TRIM_SPACE", "REPLACE_INVALID_CHARACTERS", "NULL_IF",
}

XML_FORMAT_OPTIONS = {
    "COMPRESSION", "IGNORE_UTF8_ERRORS", "PRESERVE_SPACE",
    "STRIP_OUTER_ELEMENT", "DISABLE_SNOWFLAKE_DATA",
    "DISABLE_AUTO_CONVERT", "REPLACE_INVALID_CHARACTERS",
    "SKIP_BYTE_ORDER_MARK",
}

FORMAT_OPTIONS_BY_TYPE = {
    "CSV": CSV_FORMAT_OPTIONS,
    "JSON": JSON_FORMAT_OPTIONS,
    "AVRO": AVRO_FORMAT_OPTIONS,
    "PARQUET": PARQUET_FORMAT_OPTIONS,
    "ORC": ORC_FORMAT_OPTIONS,
    "XML": XML_FORMAT_OPTIONS,
}

DYNAMIC_TABLE_REFRESH_MODES = {"AUTO", "FULL", "INCREMENTAL"}

STREAM_VALID_PROPS = {"name", "schema", "on_table", "on_view", "append_only", "show_initial_rows", "description"}
TASK_VALID_PROPS = {"name", "schema", "warehouse", "schedule", "after", "when", "sql_file", "description"}
STAGE_VALID_PROPS = {"name", "schema", "url", "storage_integration", "file_format", "description"}
FILE_FORMAT_VALID_PROPS = {"name", "schema", "type", "options", "description"}
DYNAMIC_TABLE_VALID_PROPS = {"name", "schema", "warehouse", "target_lag", "refresh_mode", "sql_file", "description"}
VIEW_VALID_PROPS = {"name", "schema", "sql_file", "description"}
TABLE_VALID_PROPS = {"name", "schema", "schema_csv", "description"}
STORED_PROC_VALID_PROPS = {"name", "schema", "signature", "sql_file", "description"}


# ---------------------------------------------------------------------------
# Per-object validators
# ---------------------------------------------------------------------------

def _validate_data_type(dt: str) -> bool:
    base = dt.upper().strip().split("(")[0]
    return base in SNOWFLAKE_DATA_TYPES


def validate_table(obj: ObjectDef, bundle_name: str, result: ValidationResult) -> None:
    csv_ref = obj.props.get("schema_csv")
    if not csv_ref:
        result.add(bundle_name, obj.name, "table", "missing_csv", "No 'schema_csv' specified")
        return

    csv_path = obj.bundle_path / csv_ref
    if not csv_path.exists():
        result.add(bundle_name, obj.name, "table", "csv_not_found",
                   f"CSV file not found: {csv_path}")
        return

    if not obj.columns:
        result.add(bundle_name, obj.name, "table", "empty_csv", "CSV schema has no columns")
        return

    for col in obj.columns:
        if not _validate_data_type(col.data_type):
            result.add(bundle_name, obj.name, "table", "invalid_data_type",
                       f"Column '{col.column_name}' has unsupported type '{col.data_type}'")

    _check_unknown_props(obj, TABLE_VALID_PROPS, bundle_name, result)


def validate_view(obj: ObjectDef, bundle_name: str, result: ValidationResult) -> None:
    sql_file = obj.props.get("sql_file")
    if not sql_file:
        result.add(bundle_name, obj.name, "view", "missing_sql_file", "No 'sql_file' specified")
        return

    sql_path = obj.bundle_path / sql_file
    if not sql_path.exists():
        result.add(bundle_name, obj.name, "view", "sql_not_found",
                   f"SQL file not found: {sql_path}")
        return

    content = sql_path.read_text().strip()
    if not content:
        result.add(bundle_name, obj.name, "view", "empty_sql", "SQL file is empty")

    _check_unknown_props(obj, VIEW_VALID_PROPS, bundle_name, result)


def validate_stream(obj: ObjectDef, bundle_name: str, result: ValidationResult) -> None:
    on_table = obj.props.get("on_table")
    on_view = obj.props.get("on_view")
    if not on_table and not on_view:
        result.add(bundle_name, obj.name, "stream", "missing_source",
                   "Must specify 'on_table' or 'on_view'")

    if on_table and on_view:
        result.add(bundle_name, obj.name, "stream", "ambiguous_source",
                   "Cannot specify both 'on_table' and 'on_view'")

    _check_unknown_props(obj, STREAM_VALID_PROPS, bundle_name, result)


def validate_task(obj: ObjectDef, bundle_name: str, result: ValidationResult) -> None:
    if not obj.props.get("warehouse"):
        result.add(bundle_name, obj.name, "task", "missing_warehouse", "No 'warehouse' specified")

    sql_file = obj.props.get("sql_file")
    if not sql_file:
        result.add(bundle_name, obj.name, "task", "missing_sql_file", "No 'sql_file' specified")
    else:
        sql_path = obj.bundle_path / sql_file
        if not sql_path.exists():
            result.add(bundle_name, obj.name, "task", "sql_not_found",
                       f"SQL file not found: {sql_path}")
        else:
            content = sql_path.read_text().strip()
            if not content:
                result.add(bundle_name, obj.name, "task", "empty_sql", "SQL file is empty")

    schedule = obj.props.get("schedule")
    after = obj.props.get("after")
    if not schedule and not after:
        result.add(bundle_name, obj.name, "task", "missing_trigger",
                   "Must specify 'schedule' or 'after' (or both)")

    _check_unknown_props(obj, TASK_VALID_PROPS, bundle_name, result)


def validate_stored_procedure(obj: ObjectDef, bundle_name: str, result: ValidationResult) -> None:
    sql_file = obj.props.get("sql_file")
    if not sql_file:
        result.add(bundle_name, obj.name, "stored_procedure", "missing_sql_file",
                   "No 'sql_file' specified")
        return

    sql_path = obj.bundle_path / sql_file
    if not sql_path.exists():
        result.add(bundle_name, obj.name, "stored_procedure", "sql_not_found",
                   f"SQL file not found: {sql_path}")
        return

    content = sql_path.read_text().strip()
    if not content:
        result.add(bundle_name, obj.name, "stored_procedure", "empty_sql", "SQL file is empty")
        return

    upper = content.upper()
    if "CREATE" not in upper or "PROCEDURE" not in upper:
        result.add(bundle_name, obj.name, "stored_procedure", "invalid_sql",
                   "SQL file must contain a CREATE ... PROCEDURE statement")

    _check_unknown_props(obj, STORED_PROC_VALID_PROPS, bundle_name, result)


def validate_dynamic_table(obj: ObjectDef, bundle_name: str, result: ValidationResult) -> None:
    if not obj.props.get("warehouse"):
        result.add(bundle_name, obj.name, "dynamic_table", "missing_warehouse",
                   "No 'warehouse' specified")

    if not obj.props.get("target_lag"):
        result.add(bundle_name, obj.name, "dynamic_table", "missing_target_lag",
                   "No 'target_lag' specified")

    refresh_mode = obj.props.get("refresh_mode", "AUTO")
    if str(refresh_mode).upper() not in DYNAMIC_TABLE_REFRESH_MODES:
        result.add(bundle_name, obj.name, "dynamic_table", "invalid_refresh_mode",
                   f"Unsupported refresh_mode '{refresh_mode}'. "
                   f"Must be one of: {', '.join(sorted(DYNAMIC_TABLE_REFRESH_MODES))}")

    sql_file = obj.props.get("sql_file")
    if not sql_file:
        result.add(bundle_name, obj.name, "dynamic_table", "missing_sql_file",
                   "No 'sql_file' specified")
    else:
        sql_path = obj.bundle_path / sql_file
        if not sql_path.exists():
            result.add(bundle_name, obj.name, "dynamic_table", "sql_not_found",
                       f"SQL file not found: {sql_path}")
        else:
            content = sql_path.read_text().strip()
            if not content:
                result.add(bundle_name, obj.name, "dynamic_table", "empty_sql",
                           "SQL file is empty")

    _check_unknown_props(obj, DYNAMIC_TABLE_VALID_PROPS, bundle_name, result)


def validate_stage(obj: ObjectDef, bundle_name: str, result: ValidationResult) -> None:
    _check_unknown_props(obj, STAGE_VALID_PROPS, bundle_name, result)


def validate_file_format(obj: ObjectDef, bundle_name: str, result: ValidationResult) -> None:
    fmt_type = obj.props.get("type")
    if not fmt_type:
        result.add(bundle_name, obj.name, "file_format", "missing_type",
                   "No 'type' specified")
        return

    fmt_upper = str(fmt_type).upper()
    if fmt_upper not in FILE_FORMAT_TYPES:
        result.add(bundle_name, obj.name, "file_format", "invalid_type",
                   f"Unsupported file format type '{fmt_type}'. "
                   f"Must be one of: {', '.join(sorted(FILE_FORMAT_TYPES))}")
        return

    options = obj.props.get("options") or {}
    allowed = FORMAT_OPTIONS_BY_TYPE.get(fmt_upper, set())
    for key in options:
        if key.upper() not in allowed:
            result.add(bundle_name, obj.name, "file_format", "unsupported_option",
                       f"Option '{key}' is not supported for type '{fmt_upper}'. "
                       f"Supported: {', '.join(sorted(allowed))}")

    _check_unknown_props(obj, FILE_FORMAT_VALID_PROPS, bundle_name, result)


SQL_PARAM_TYPES = {"view", "dynamic_table", "task", "stored_procedure"}


def _check_unknown_props(obj: ObjectDef, valid: set[str], bundle_name: str, result: ValidationResult) -> None:
    unknown = set(obj.props.keys()) - valid
    if obj.object_type in SQL_PARAM_TYPES:
        unknown = {k for k in unknown if not isinstance(obj.props.get(k), str)}
    if unknown:
        result.add(bundle_name, obj.name, obj.object_type, "unknown_properties",
                   f"Unknown properties: {', '.join(sorted(unknown))}")


OBJECT_VALIDATORS = {
    "table": validate_table,
    "view": validate_view,
    "stream": validate_stream,
    "task": validate_task,
    "stored_procedure": validate_stored_procedure,
    "dynamic_table": validate_dynamic_table,
    "stage": validate_stage,
    "file_format": validate_file_format,
}


def validate_objects(bundles: list[Bundle]) -> ValidationResult:
    """Run per-object validation on all bundles."""
    result = ValidationResult()
    for bundle in bundles:
        for obj in bundle.objects:
            validator = OBJECT_VALIDATORS.get(obj.object_type)
            if validator:
                validator(obj, bundle.name, result)
            else:
                result.add(bundle.name, obj.name, obj.object_type, "unknown_type",
                           f"No validator for object type '{obj.object_type}'")
    return result


# ---------------------------------------------------------------------------
# Inter-dependency validation
# ---------------------------------------------------------------------------

FQN_PATTERN = re.compile(r"[A-Z_][A-Z0-9_]*\.[A-Z_][A-Z0-9_]*\.[A-Z_][A-Z0-9_]*")


def _build_fqn_index(bundles: list[Bundle]) -> dict[str, ObjectDef]:
    """Map FQN -> ObjectDef across all bundles."""
    idx: dict[str, ObjectDef] = {}
    for bundle in bundles:
        for obj in bundle.objects:
            idx[obj.fqn] = obj
    return idx


def _build_type_index(bundles: list[Bundle]) -> dict[str, set[str]]:
    """Map object_type -> set of FQNs."""
    idx: dict[str, set[str]] = {}
    for bundle in bundles:
        for obj in bundle.objects:
            idx.setdefault(obj.object_type, set()).add(obj.fqn)
    return idx


def validate_dependencies(bundles: list[Bundle]) -> ValidationResult:
    """Check cross-references between objects."""
    result = ValidationResult()
    fqn_index = _build_fqn_index(bundles)
    type_index = _build_type_index(bundles)

    all_tables = type_index.get("table", set())
    all_views = type_index.get("view", set())
    all_procs = type_index.get("stored_procedure", set())
    all_file_formats = type_index.get("file_format", set())
    all_queryable = all_tables | all_views | type_index.get("dynamic_table", set())

    for bundle in bundles:
        for obj in bundle.objects:

            # Streams must reference a table or view that exists in bundles
            if obj.object_type == "stream":
                source_fqn = (obj.props.get("on_table") or obj.props.get("on_view") or "").upper()
                if source_fqn and source_fqn not in fqn_index:
                    result.add(bundle.name, obj.name, "stream", "missing_source_object",
                               f"References '{source_fqn}' which is not defined in any bundle")

            # Tasks: check if SQL references a CALL to a stored proc that exists
            if obj.object_type == "task":
                sql_file = obj.props.get("sql_file")
                if sql_file:
                    sql_path = obj.bundle_path / sql_file
                    if sql_path.exists():
                        content = sql_path.read_text().upper()
                        if "CALL " in content:
                            called_fqns = _extract_call_targets(content, obj.context)
                            for called in called_fqns:
                                base_name = called.split("(")[0].strip()
                                if base_name and base_name not in fqn_index:
                                    result.add(bundle.name, obj.name, "task",
                                               "missing_proc_reference",
                                               f"Calls '{base_name}' which is not defined in any bundle")

                # 'after' references must be tasks in the bundle set
                after = obj.props.get("after", [])
                if isinstance(after, str):
                    after = [after]
                all_tasks = type_index.get("task", set())
                for dep in after:
                    dep_upper = dep.upper()
                    if dep_upper not in all_tasks and dep_upper not in fqn_index:
                        result.add(bundle.name, obj.name, "task", "missing_after_task",
                                   f"'after' references '{dep}' which is not a known task")

            # Views / dynamic tables: check SQL for FQN references to tables
            if obj.object_type in ("view", "dynamic_table"):
                sql_file = obj.props.get("sql_file")
                if sql_file:
                    sql_path = obj.bundle_path / sql_file
                    if sql_path.exists():
                        raw_sql = sql_path.read_text()
                        try:
                            resolved_sql = resolve_placeholders(raw_sql, obj.context).upper()
                        except ValueError:
                            resolved_sql = raw_sql.upper()
                        referenced_fqns = FQN_PATTERN.findall(resolved_sql)
                        for ref in referenced_fqns:
                            if ref not in all_queryable and ref not in fqn_index:
                                result.add(bundle.name, obj.name, obj.object_type,
                                           "missing_table_reference",
                                           f"SQL references '{ref}' which is not defined in any bundle")

            # Stages: check file_format reference
            if obj.object_type == "stage":
                ff_ref = obj.props.get("file_format")
                if ff_ref:
                    ff_fqn = str(ff_ref).upper()
                    if ff_fqn not in all_file_formats and ff_fqn not in fqn_index:
                        result.add(bundle.name, obj.name, "stage", "missing_file_format",
                                   f"References file format '{ff_ref}' which is not defined in any bundle")

    # Duplicate FQN detection (across bundles)
    seen: dict[str, str] = {}
    for bundle in bundles:
        for obj in bundle.objects:
            if obj.fqn in seen:
                result.add(bundle.name, obj.name, obj.object_type, "duplicate_fqn",
                           f"FQN '{obj.fqn}' already defined in bundle '{seen[obj.fqn]}'")
            else:
                seen[obj.fqn] = bundle.name

    return result


def _extract_call_targets(sql_upper: str, context: dict[str, str]) -> list[str]:
    """Extract FQNs from CALL statements in SQL."""
    pattern = re.compile(r"CALL\s+([\w.${}]+(?:\.[\w.${}]+)*)\s*\(", re.IGNORECASE)
    matches = pattern.findall(sql_upper)
    results = []
    for m in matches:
        try:
            resolved = resolve_placeholders(m, {k: v.upper() for k, v in context.items()})
        except ValueError:
            resolved = m
        results.append(resolved.upper())
    return results


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def validate_all(bundles: list[Bundle]) -> ValidationResult:
    """Run all validation checks and return combined result."""
    result = validate_objects(bundles)
    result.merge(validate_dependencies(bundles))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate bundle definitions")
    parser.add_argument("--env", required=True, help="Target env (DEV, UAT, PROD)")
    parser.add_argument("--bundles-root", default="bundles", type=Path)
    parser.add_argument("--platform-root", default="platform", type=Path)
    args = parser.parse_args()

    print(f"[validator] Loading platform config for env={args.env}")
    platform_cfg = load_platform_config(args.platform_root, args.env)

    print(f"[validator] Discovering bundles under {args.bundles_root}")
    bundles = load_all_bundles(args.bundles_root, args.env, platform_cfg)
    print(f"[validator] Loaded {len(bundles)} bundle(s)")

    print("[validator] Running validation checks...")
    result = validate_all(bundles)

    if result.ok:
        print(f"[validator] All checks passed ({_count_objects(bundles)} objects across {len(bundles)} bundles)")
        return 0

    print(f"\n[validator] VALIDATION FAILED - {len(result.errors)} error(s):\n")
    for err in result.errors:
        print(f"  ERROR: {err}")
    print()
    return 1


def _count_objects(bundles: list[Bundle]) -> int:
    return sum(len(b.objects) for b in bundles)


if __name__ == "__main__":
    sys.exit(main())
