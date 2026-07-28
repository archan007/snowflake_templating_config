"""
Generator for DQ rule seeding SQL and Data Metric Function DDL.

Reads dq_rules/*.yaml files from each bundle directory and generates:
1. MERGE statements that upsert engine-managed rules into the DQ_RULES table.
2. CREATE OR REPLACE DATA METRIC FUNCTION DDL for rules marked with dmf: true.

Rule YAML format:
    target_table: <TABLE_NAME>
    target_schema: <SCHEMA>
    owner: <team-name>
    rules:
      - name: <unique_rule_name>
        type: NOT_NULL | UNIQUE | ACCEPTED_VALUES | ROW_COUNT_RANGE |
              REFERENTIAL_INTEGRITY | RECENCY | CUSTOM_SQL | EXPRESSION
        column: <optional column name>
        severity: CRITICAL | WARNING | INFO
        description: <text>
        dmf: true  # optional - generate a Snowflake Data Metric Function
        parameters:
          <rule-type-specific params>
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from ..config_loader import Bundle, resolve_placeholders


# Rule types that can be expressed as a Data Metric Function
DMF_SUPPORTED_TYPES = {"NOT_NULL", "UNIQUE", "EXPRESSION", "CUSTOM_SQL", "ACCEPTED_VALUES"}


def _escape_sql_string(s: str) -> str:
    return s.replace("'", "''")


def discover_dq_rules(bundle: Bundle) -> list[Path]:
    """Find all dq_rules/*.yaml files in a bundle directory."""
    dq_dir = bundle.path / "dq_rules"
    if not dq_dir.exists():
        return []
    return sorted(dq_dir.glob("*.yaml"))


def load_dq_rules(bundle: Bundle, env: str, platform_vars: dict[str, str]) -> list[dict[str, Any]]:
    """Load and resolve all DQ rule definitions from a bundle."""
    context = {"ENV": env.upper(), "ENV_LOWER": env.lower()}
    context.update(platform_vars)

    rule_files = discover_dq_rules(bundle)
    all_rules: list[dict[str, Any]] = []

    for rule_file in rule_files:
        with rule_file.open() as f:
            raw = yaml.safe_load(f) or {}

        raw = resolve_placeholders(raw, context)

        target_table = raw.get("target_table", "")
        target_schema = raw.get("target_schema", bundle.default_schema)
        owner = raw.get("owner", "")

        for rule_def in raw.get("rules", []):
            all_rules.append({
                "rule_id": rule_def["name"],
                "rule_name": rule_def["name"],
                "rule_type": rule_def["type"],
                "source": "engine",
                "target_database": bundle.database,
                "target_schema": target_schema,
                "target_table": target_table,
                "target_column": rule_def.get("column"),
                "severity": rule_def.get("severity", "WARNING"),
                "parameters": rule_def.get("parameters"),
                "description": rule_def.get("description", ""),
                "owner": owner,
                "dmf": rule_def.get("dmf", False),
            })

    return all_rules


def _generate_dmf_body(rule: dict[str, Any]) -> str | None:
    """Generate the SQL body for a DMF based on rule type.

    Returns None if the rule type cannot be expressed as a DMF.
    """
    rule_type = rule["rule_type"].upper()
    column = rule.get("target_column")
    params = rule.get("parameters") or {}

    if rule_type == "NOT_NULL" and column:
        return f'SELECT COUNT(*) FROM T WHERE "{column}" IS NULL'

    if rule_type == "UNIQUE" and column:
        return f'SELECT COUNT(*) - COUNT(DISTINCT "{column}") FROM T'

    if rule_type == "ACCEPTED_VALUES" and column:
        values = params.get("values", [])
        if not values:
            return None
        values_list = ", ".join(f"'{_escape_sql_string(str(v))}'" for v in values)
        return (
            f'SELECT COUNT(*) FROM T\n'
            f'WHERE "{column}" IS NOT NULL\n'
            f'  AND "{column}" NOT IN ({values_list})'
        )

    if rule_type == "EXPRESSION" and column:
        expression = params.get("expression", "")
        if not expression:
            return None
        return f'SELECT COUNT(*) FROM T WHERE NOT ({expression})'

    if rule_type == "CUSTOM_SQL":
        sql = params.get("sql", "")
        return sql if sql else None

    return None


def _dmf_data_type(rule: dict[str, Any]) -> str:
    """Determine the DMF input signature based on the rule."""
    rule_type = rule["rule_type"].upper()
    column = rule.get("target_column")

    if rule_type == "CUSTOM_SQL":
        # Table-level DMF: accepts the whole table
        col_spec = rule.get("parameters", {}).get("dmf_columns")
        if col_spec:
            return f"T TABLE({col_spec})"
        return "T TABLE(T TABLE(*))"

    if column:
        col_type = (rule.get("parameters") or {}).get("column_type", "VARCHAR")
        return f'T TABLE("{column}" {col_type})'

    return "T TABLE(T TABLE(*))"


def generate_dmf_ddl(rules: list[dict[str, Any]], database: str) -> list[dict[str, str]]:
    """Generate CREATE OR REPLACE DATA METRIC FUNCTION statements.

    Returns a list of dicts with keys: fqn, sql, drop_sql
    Only processes rules that have dmf: true and a supported type.
    """
    results: list[dict[str, str]] = []
    delim = chr(36) * 2

    for rule in rules:
        if not rule.get("dmf"):
            continue

        rule_type = rule["rule_type"].upper()
        if rule_type not in DMF_SUPPORTED_TYPES:
            continue

        body = _generate_dmf_body(rule)
        if body is None:
            continue

        schema = rule["target_schema"]
        dmf_name = f"DMF_{rule['rule_name'].upper()}"
        fqn = f"{database}.{schema}.{dmf_name}"
        data_type = _dmf_data_type(rule)
        return_type = "NUMBER"

        sql = (
            f"CREATE OR REPLACE DATA METRIC FUNCTION {fqn}\n"
            f"  ({data_type})\n"
            f"  RETURNS {return_type}\n"
            f"AS\n{delim}\n{body}\n{delim};"
        )

        drop_sql = f"DROP DATA METRIC FUNCTION IF EXISTS {fqn};"

        results.append({"fqn": fqn, "sql": sql, "drop_sql": drop_sql, "rule_id": rule["rule_id"]})

    return results


def generate_dq_seed_sql(rules: list[dict[str, Any]], database: str) -> str:
    """Generate a MERGE statement that upserts engine-managed DQ rules.

    Uses MERGE to:
    - Insert new engine rules
    - Update existing engine rules if definition changed
    - Deactivate engine rules that are no longer in YAML (soft delete)
    """
    if not rules:
        return ""

    dq_rules_fqn = f"{database}.DQ.DQ_RULES"

    rule_ids = [_escape_sql_string(r["rule_id"]) for r in rules]
    rule_id_list = ", ".join(f"'{rid}'" for rid in rule_ids)

    values_rows: list[str] = []
    for r in rules:
        params_json = json.dumps(r["parameters"]) if r["parameters"] else "NULL"
        params_sql = f"PARSE_JSON('{_escape_sql_string(params_json)}')" if r["parameters"] else "NULL"
        col_sql = f"'{_escape_sql_string(r['target_column'])}'" if r["target_column"] else "NULL"
        desc_sql = f"'{_escape_sql_string(r['description'])}'" if r["description"] else "NULL"
        owner_sql = f"'{_escape_sql_string(r['owner'])}'" if r["owner"] else "NULL"

        row = (
            f"  SELECT "
            f"'{_escape_sql_string(r['rule_id'])}', "
            f"'{_escape_sql_string(r['rule_name'])}', "
            f"'{_escape_sql_string(r['rule_type'])}', "
            f"'engine', "
            f"'{_escape_sql_string(r['target_database'])}', "
            f"'{_escape_sql_string(r['target_schema'])}', "
            f"'{_escape_sql_string(r['target_table'])}', "
            f"{col_sql}, "
            f"'{_escape_sql_string(r['severity'])}', "
            f"{params_sql}, "
            f"{desc_sql}, "
            f"{owner_sql}"
        )
        values_rows.append(row)

    source_union = "\n  UNION ALL\n".join(values_rows)

    merge_sql = f"""MERGE INTO {dq_rules_fqn} AS tgt
USING (
{source_union}
) AS src (RULE_ID, RULE_NAME, RULE_TYPE, SOURCE, TARGET_DATABASE, TARGET_SCHEMA,
          TARGET_TABLE, TARGET_COLUMN, SEVERITY, PARAMETERS, DESCRIPTION, OWNER)
ON tgt.RULE_ID = src.RULE_ID AND tgt.SOURCE = 'engine'
WHEN MATCHED THEN UPDATE SET
    RULE_NAME = src.RULE_NAME,
    RULE_TYPE = src.RULE_TYPE,
    TARGET_DATABASE = src.TARGET_DATABASE,
    TARGET_SCHEMA = src.TARGET_SCHEMA,
    TARGET_TABLE = src.TARGET_TABLE,
    TARGET_COLUMN = src.TARGET_COLUMN,
    SEVERITY = src.SEVERITY,
    PARAMETERS = src.PARAMETERS,
    DESCRIPTION = src.DESCRIPTION,
    OWNER = src.OWNER,
    IS_ACTIVE = TRUE,
    UPDATED_AT = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT
    (RULE_ID, RULE_NAME, RULE_TYPE, SOURCE, TARGET_DATABASE, TARGET_SCHEMA,
     TARGET_TABLE, TARGET_COLUMN, SEVERITY, PARAMETERS, DESCRIPTION, OWNER, IS_ACTIVE)
VALUES
    (src.RULE_ID, src.RULE_NAME, src.RULE_TYPE, src.SOURCE, src.TARGET_DATABASE,
     src.TARGET_SCHEMA, src.TARGET_TABLE, src.TARGET_COLUMN, src.SEVERITY,
     src.PARAMETERS, src.DESCRIPTION, src.OWNER, TRUE);"""

    deactivate_sql = f"""
UPDATE {dq_rules_fqn}
SET IS_ACTIVE = FALSE, UPDATED_AT = CURRENT_TIMESTAMP()
WHERE SOURCE = 'engine'
  AND IS_ACTIVE = TRUE
  AND RULE_ID NOT IN ({rule_id_list});"""

    return merge_sql + "\n\n" + deactivate_sql
