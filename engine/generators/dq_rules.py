"""
Generator for DQ rule seeding SQL.

Reads dq_rules/*.yaml files from each bundle directory and generates
MERGE statements that upsert engine-managed rules into the DQ_RULES table.

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
        parameters:
          <rule-type-specific params>
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from ..config_loader import Bundle, resolve_placeholders


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
            })

    return all_rules


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
