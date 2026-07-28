"""
Config loader: reads bundle.yaml files, CSV schemas, and platform config.
Resolves ${ENV} and other placeholders.
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class Column:
    column_name: str
    data_type: str
    nullable: bool = True
    default_value: str | None = None
    rule: str | None = None
    transform_logic: str | None = None
    clustering_key: bool = False
    business_key: bool = False
    description: str | None = None

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> "Column":
        def _bool(v: str | None) -> bool:
            return str(v).strip().lower() in ("true", "1", "yes", "y")

        def _opt(v: str | None) -> str | None:
            if v is None:
                return None
            v = v.strip()
            return v or None

        return cls(
            column_name=row["column_name"].strip(),
            data_type=row["data_type"].strip(),
            nullable=_bool(row.get("nullable", "true")),
            default_value=_opt(row.get("default_value")),
            rule=_opt(row.get("rule")),
            transform_logic=_opt(row.get("transform_logic")),
            clustering_key=_bool(row.get("clustering_key", "false")),
            business_key=_bool(row.get("business_key", "false")),
            description=_opt(row.get("description")),
        )


@dataclass
class ObjectDef:
    """A Snowflake object definition from bundle.yaml."""
    object_type: str   # table, view, stream, task, stored_procedure, dynamic_table, stage, file_format
    name: str
    schema: str
    database: str
    bundle_path: Path
    props: dict[str, Any] = field(default_factory=dict)
    columns: list[Column] = field(default_factory=list)  # only for tables
    # Resolved env context (ENV, ENV_LOWER, DATABASE, ...). Used by generators
    # to substitute placeholders in referenced SQL files at generation time
    # so user-authored SQL can be portable across environments.
    context: dict[str, str] = field(default_factory=dict)

    @property
    def fqn(self) -> str:
        return f"{self.database}.{self.schema}.{self.name}".upper()


@dataclass
class Bundle:
    name: str                      # e.g. "bronze", "gold.product_a"
    path: Path
    database: str
    default_schema: str
    objects: list[ObjectDef] = field(default_factory=list)
    confirmed_drops: list[str] = field(default_factory=list)  # FQNs explicitly allowed to drop
    grants: list[dict[str, Any]] = field(default_factory=list)


def resolve_placeholders(value: Any, context: dict[str, str]) -> Any:
    """Recursively resolve ${VAR} placeholders in strings/dicts/lists."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            key = m.group(1)
            if key not in context:
                raise ValueError(f"Unresolved placeholder: ${{{key}}}")
            return context[key]
        return PLACEHOLDER_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: resolve_placeholders(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_placeholders(v, context) for v in value]
    return value


def load_platform_config(platform_dir: Path, env: str) -> dict[str, Any]:
    """Load platform.yaml merged with the env override."""
    base_path = platform_dir / "platform.yaml"
    override_path = platform_dir / "overrides" / f"{env.lower()}.yaml"

    if not base_path.exists():
        raise FileNotFoundError(f"platform.yaml not found at {base_path}")

    with base_path.open() as f:
        base = yaml.safe_load(f) or {}

    override: dict[str, Any] = {}
    if override_path.exists():
        with override_path.open() as f:
            override = yaml.safe_load(f) or {}

    merged = _deep_merge(base, override)

    context = {"ENV": env.upper(), "ENV_LOWER": env.lower()}
    env_vars_from_override = override.get("variables", {}) or {}
    context.update({k: str(v) for k, v in env_vars_from_override.items()})

    return resolve_placeholders(merged, context)


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


REQUIRED_CSV_COLUMNS = {
    "column_name", "data_type", "nullable", "default_value",
    "rule", "transform_logic", "clustering_key", "business_key", "description",
}


def _load_csv_schema(csv_path: Path) -> list[Column]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Schema CSV not found: {csv_path}")
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"{csv_path}: empty or unreadable CSV")
        missing = REQUIRED_CSV_COLUMNS - set(reader.fieldnames)
        if missing:
            raise ValueError(
                f"{csv_path}: missing required columns: {sorted(missing)}"
            )
        columns: list[Column] = []
        for lineno, row in enumerate(reader, start=2):
            # Defensive: catch common bugs like unquoted NUMBER(18,2) which
            # splits into extra fields and leaves 'nullable' looking like '2)'
            dt = (row.get("data_type") or "").strip()
            if dt.count("(") != dt.count(")"):
                raise ValueError(
                    f"{csv_path} line {lineno}: unbalanced parentheses in "
                    f"data_type '{dt}'. If the type contains a comma "
                    f"(e.g. NUMBER(18,2)), it must be wrapped in double quotes: "
                    f'"NUMBER(18,2)"'
                )
            nullable_raw = (row.get("nullable") or "").strip().lower()
            if nullable_raw and nullable_raw not in ("true", "false", "1", "0", "yes", "no", "y", "n"):
                raise ValueError(
                    f"{csv_path} line {lineno}: invalid 'nullable' value "
                    f"'{row.get('nullable')}'. Expected true/false."
                )
            columns.append(Column.from_csv_row(row))
        if not columns:
            raise ValueError(f"{csv_path}: no columns defined")
        return columns


KNOWN_PROPERTIES: dict[str, set[str]] = {
    "table": {"name", "schema", "description", "schema_csv"},
    "view": {"name", "schema", "description", "sql_file"},
    "stream": {"name", "schema", "description", "on_table", "on_view", "append_only", "show_initial_rows"},
    "task": {"name", "schema", "description", "sql_file", "warehouse", "schedule", "after", "when"},
    "stored_procedure": {"name", "schema", "description", "sql_file", "signature"},
    "dynamic_table": {"name", "schema", "description", "sql_file", "warehouse", "target_lag", "refresh_mode"},
    "stage": {"name", "schema", "description", "url", "storage_integration", "file_format"},
    "file_format": {"name", "schema", "description", "type", "options"},
    "data_metric_function": {"name", "schema", "description", "sql_file", "data_type", "return_type"},
}


def _extract_custom_params(item: dict[str, Any], object_type: str) -> dict[str, str]:
    """Return any YAML keys that are not known structural properties.
    These are treated as custom parameters available for SQL substitution."""
    known = KNOWN_PROPERTIES.get(object_type, set())
    return {k: v for k, v in item.items() if k not in known and isinstance(v, str)}


def load_bundle(bundle_yaml: Path, env: str, platform_cfg: dict[str, Any]) -> Bundle:
    """Load a single bundle.yaml and its referenced CSV schemas."""
    with bundle_yaml.open() as f:
        raw = yaml.safe_load(f) or {}

    context = {"ENV": env.upper(), "ENV_LOWER": env.lower()}
    env_vars = platform_cfg.get("variables", {}) or {}
    context.update({k: str(v) for k, v in env_vars.items()})

    raw = resolve_placeholders(raw, context)

    bundle_name = raw.get("bundle_name") or bundle_yaml.parent.name
    database = raw["database"]
    default_schema = raw.get("default_schema", "PUBLIC")
    confirmed_drops = [d.upper() for d in (raw.get("confirmed_drops") or [])]

    grants = raw.get("grants") or []

    bundle = Bundle(
        name=bundle_name,
        path=bundle_yaml.parent,
        database=database,
        default_schema=default_schema,
        confirmed_drops=confirmed_drops,
        grants=grants,
    )

    for obj_type_plural, object_type in [
        ("tables", "table"),
        ("views", "view"),
        ("streams", "stream"),
        ("tasks", "task"),
        ("stored_procedures", "stored_procedure"),
        ("dynamic_tables", "dynamic_table"),
        ("stages", "stage"),
        ("file_formats", "file_format"),
        ("data_metric_functions", "data_metric_function"),
    ]:
        for item in raw.get(obj_type_plural, []) or []:
            schema = item.get("schema", default_schema)
            obj_context = dict(context)
            obj_context["SCHEMA"] = schema
            custom_params = _extract_custom_params(item, object_type)
            for k, v in custom_params.items():
                obj_context[k] = str(v)
                obj_context[k.upper()] = str(v)
            obj = ObjectDef(
                object_type=object_type,
                name=item["name"],
                schema=schema,
                database=database,
                bundle_path=bundle_yaml.parent,
                props=item,
                context=obj_context,
            )
            if object_type == "table":
                schema_csv = item.get("schema_csv")
                if not schema_csv:
                    raise ValueError(
                        f"Table '{obj.fqn}' in {bundle_yaml} must specify 'schema_csv'."
                    )
                csv_path = bundle_yaml.parent / schema_csv
                obj.columns = _load_csv_schema(csv_path)
            bundle.objects.append(obj)

    return bundle


def discover_bundles(bundles_root: Path) -> list[Path]:
    """Find every bundle.yaml under the bundles/ root."""
    return sorted(bundles_root.rglob("bundle.yaml"))


def load_all_bundles(bundles_root: Path, env: str, platform_cfg: dict[str, Any]) -> list[Bundle]:
    return [load_bundle(p, env, platform_cfg) for p in discover_bundles(bundles_root)]
