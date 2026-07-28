"""
Main DDL generation orchestrator.

Responsibilities:
1. Load all bundles + platform config
2. Read current Snowflake state (what actually exists)
3. Compute the diff: creates, alters, drops
4. Enforce the drop-safety rule: breaking-change drops (table/view/dynamic_table)
   require an explicit entry in bundle.yaml's `confirmed_drops` list
5. Emit Liquibase formatted-SQL changesets in execution order
6. Write the master changelog that includes all generated files

Usage:
    python -m engine.generate_ddl --env DEV --bundles-root bundles \\
        --platform-root platform --output-root output/ddl [--offline]

The --offline flag skips Snowflake state reading and assumes a fresh
environment (all objects are new). Useful for local testing.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

from .config_loader import Bundle, ObjectDef, load_all_bundles, load_platform_config
from .generators import dq_rules as dq
from .generators import grants as gg
from .generators import objects as og
from .generators import tables as tg
from .state_reader import ExistingObject, SnowflakeStateReader


# Object types where DROP is a breaking change and requires explicit confirmation
BREAKING_DROP_TYPES = {"table", "view", "dynamic_table"}

# Execution order for creates/alters
CREATE_ORDER = [
    "file_format",
    "stage",
    "table",
    "stream",
    "view",
    "dynamic_table",
    "data_metric_function",
    "stored_procedure",
    "task",
]

# FQN suffixes for infrastructure/system tables that should never be
# flagged as drift. These are created by tools (e.g. Liquibase) and are
# not managed through bundles.
IGNORED_OBJECT_SUFFIXES = {
    "DATABASECHANGELOG",
    "DATABASECHANGELOGLOCK",
}

# Execution order for drops (reverse dependency order)
DROP_ORDER = [
    "task",
    "stored_procedure",
    "data_metric_function",
    "dynamic_table",
    "view",
    "stream",
    "table",
    "stage",
    "file_format",
]


@dataclass
class Changeset:
    id: str
    author: str
    object_type: str
    operation: str  # create, alter, drop
    fqn: str
    sql: str
    rollback: str | None = None
    runOnChange: bool = False  # true for objects we want to re-apply on change

    def to_formatted_sql(self) -> str:
        """Render as Liquibase formatted SQL."""
        header = f"--changeset {self.author}:{self.id}"
        attrs = []
        if self.runOnChange:
            attrs.append("runOnChange:true")
        attrs.append(f"labels:{self.object_type},{self.operation}")
        attrs.append(f"context:{self.operation}")
        if "$$" in self.sql:
            attrs.append("splitStatements:false")
            attrs.append("endDelimiter:")
        if attrs:
            header += " " + " ".join(attrs)
        header += f"\n--comment {self.operation.upper()} {self.object_type} {self.fqn}"
        parts = [header, "", self.sql]
        if self.rollback:
            parts.append("")
            parts.append(f"--rollback {self.rollback}")
        return "\n".join(parts) + "\n"


class DropSafetyError(Exception):
    """Raised when a breaking drop is detected without explicit confirmation."""


def _hash_id(obj_type: str, fqn: str, sql: str) -> str:
    """Deterministic changeset id from type + fqn + sql hash."""
    h = hashlib.sha256(sql.encode()).hexdigest()[:8]
    safe_fqn = fqn.replace(".", "_").lower()
    return f"{obj_type}-{safe_fqn}-{h}"


def _collect_desired(bundles: list[Bundle]) -> dict[str, ObjectDef]:
    """Flatten all bundles into FQN -> ObjectDef."""
    desired: dict[str, ObjectDef] = {}
    for bundle in bundles:
        for obj in bundle.objects:
            if obj.fqn in desired:
                raise ValueError(
                    f"Duplicate object {obj.fqn} defined in bundle {bundle.name}"
                )
            desired[obj.fqn] = obj
    return desired


def _all_confirmed_drops(bundles: list[Bundle]) -> set[str]:
    confirmed: set[str] = set()
    for b in bundles:
        confirmed.update(b.confirmed_drops)
    return confirmed


def compute_changesets(
    bundles: list[Bundle],
    existing: dict[str, ExistingObject],
    author: str = "engine",
    liquibase_tracked: set[str] | None = None,
) -> list[Changeset]:
    """Compute the ordered list of changesets to apply.

    Args:
        liquibase_tracked: FQNs that were deployed via Liquibase. If provided,
            only objects in this set can trigger breaking-change drops. Objects
            in 'existing' but NOT in this set are assumed manually created and
            are silently skipped. When None (offline / tests), all existing
            objects are considered tracked (backwards-compatible behavior).
    """
    desired = _collect_desired(bundles)
    confirmed_drops = _all_confirmed_drops(bundles)
    changesets: list[Changeset] = []

    # ---- Creates and alters, in dependency order ----
    for obj_type in CREATE_ORDER:
        for fqn, obj in sorted(desired.items()):
            if obj.object_type != obj_type:
                continue

            if obj_type == "table":
                if fqn not in existing:
                    sql = tg.generate_create_table(obj)
                    changesets.append(Changeset(
                        id=_hash_id("table", fqn, sql),
                        author=author,
                        object_type="table",
                        operation="create",
                        fqn=fqn, sql=sql,
                        rollback=tg.generate_drop_table(fqn),
                    ))
                else:
                    alter_stmts = tg.generate_alter_table(obj, existing[fqn])
                    for stmt in alter_stmts:
                        changesets.append(Changeset(
                            id=_hash_id("table-alter", fqn, stmt),
                            author=author,
                            object_type="table",
                            operation="alter",
                            fqn=fqn, sql=stmt,
                        ))

            elif obj_type == "view":
                sql = og.generate_create_view(obj)
                changesets.append(Changeset(
                    id=_hash_id("view", fqn, sql),
                    author=author,
                    object_type="view",
                    operation="create",
                    fqn=fqn, sql=sql,
                    rollback=og.generate_drop_view(fqn),
                    runOnChange=True,
                ))

            elif obj_type == "stream":
                sql = og.generate_create_stream(obj)
                changesets.append(Changeset(
                    id=_hash_id("stream", fqn, sql),
                    author=author,
                    object_type="stream",
                    operation="create",
                    fqn=fqn, sql=sql,
                    rollback=og.generate_drop_stream(fqn),
                    runOnChange=True,
                ))

            elif obj_type == "task":
                sql = og.generate_create_task(obj)
                changesets.append(Changeset(
                    id=_hash_id("task", fqn, sql),
                    author=author,
                    object_type="task",
                    operation="create",
                    fqn=fqn, sql=sql,
                    rollback=og.generate_drop_task(fqn),
                    runOnChange=True,
                ))

            elif obj_type == "stored_procedure":
                sql = og.generate_create_procedure(obj)
                changesets.append(Changeset(
                    id=_hash_id("proc", fqn, sql),
                    author=author,
                    object_type="stored_procedure",
                    operation="create",
                    fqn=fqn, sql=sql,
                    rollback=og.generate_drop_procedure(obj),
                    runOnChange=True,
                ))

            elif obj_type == "dynamic_table":
                sql = og.generate_create_dynamic_table(obj)
                changesets.append(Changeset(
                    id=_hash_id("dyntable", fqn, sql),
                    author=author,
                    object_type="dynamic_table",
                    operation="create",
                    fqn=fqn, sql=sql,
                    rollback=og.generate_drop_dynamic_table(fqn),
                    runOnChange=True,
                ))

            elif obj_type == "stage":
                sql = og.generate_create_stage(obj)
                changesets.append(Changeset(
                    id=_hash_id("stage", fqn, sql),
                    author=author,
                    object_type="stage",
                    operation="create",
                    fqn=fqn, sql=sql,
                    rollback=og.generate_drop_stage(fqn),
                    runOnChange=True,
                ))

            elif obj_type == "file_format":
                sql = og.generate_create_file_format(obj)
                changesets.append(Changeset(
                    id=_hash_id("ff", fqn, sql),
                    author=author,
                    object_type="file_format",
                    operation="create",
                    fqn=fqn, sql=sql,
                    rollback=og.generate_drop_file_format(fqn),
                    runOnChange=True,
                ))

            elif obj_type == "data_metric_function":
                sql = og.generate_create_data_metric_function(obj)
                changesets.append(Changeset(
                    id=_hash_id("dmf", fqn, sql),
                    author=author,
                    object_type="data_metric_function",
                    operation="create",
                    fqn=fqn, sql=sql,
                    rollback=og.generate_drop_data_metric_function(fqn),
                    runOnChange=True,
                ))

    # ---- Grants: emit GRANT statements for bundles that define them ----
    for bundle in bundles:
        grant_stmts = gg.generate_grants(bundle)
        for stmt in grant_stmts:
            changesets.append(Changeset(
                id=_hash_id("grant", bundle.name, stmt),
                author=author,
                object_type="grant",
                operation="grant",
                fqn=bundle.name,
                sql=stmt,
                runOnChange=True,
            ))

    # ---- Drops: objects in existing but not in desired ----
    unconfirmed_breaking: list[str] = []
    drop_changesets: dict[str, list[Changeset]] = {t: [] for t in DROP_ORDER}

    for fqn, ex_obj in existing.items():
        if fqn in desired:
            continue

        if ex_obj.name.upper() in IGNORED_OBJECT_SUFFIXES:
            continue

        # Skip objects not tracked by Liquibase (manually created in DB)
        if liquibase_tracked is not None and fqn not in liquibase_tracked:
            continue

        if ex_obj.object_type in BREAKING_DROP_TYPES:
            if fqn not in confirmed_drops:
                unconfirmed_breaking.append(f"{ex_obj.object_type.upper()} {fqn}")
                continue

        # Emit the drop
        if ex_obj.object_type == "table":
            sql = tg.generate_drop_table(fqn)
        elif ex_obj.object_type == "view":
            sql = og.generate_drop_view(fqn)
        elif ex_obj.object_type == "stream":
            sql = og.generate_drop_stream(fqn)
        elif ex_obj.object_type == "task":
            sql = og.generate_drop_task(fqn)
        elif ex_obj.object_type == "stored_procedure":
            sql = og.generate_drop_procedure(fqn)
        elif ex_obj.object_type == "dynamic_table":
            sql = og.generate_drop_dynamic_table(fqn)
        elif ex_obj.object_type == "stage":
            sql = og.generate_drop_stage(fqn)
        elif ex_obj.object_type == "file_format":
            sql = og.generate_drop_file_format(fqn)
        elif ex_obj.object_type == "data_metric_function":
            sql = og.generate_drop_data_metric_function(fqn)
        else:
            continue

        drop_changesets[ex_obj.object_type].append(Changeset(
            id=_hash_id(f"drop-{ex_obj.object_type}", fqn, sql),
            author=author,
            object_type=ex_obj.object_type,
            operation="drop",
            fqn=fqn, sql=sql,
        ))

    if unconfirmed_breaking:
        msg = (
            "\n\n*** BREAKING CHANGE DETECTED ***\n\n"
            "The following objects exist in Snowflake but are no longer\n"
            "defined in any bundle.yaml. Dropping them is a breaking change.\n\n"
            "To proceed, add each FQN to the 'confirmed_drops' list in the\n"
            "bundle.yaml that previously owned the object:\n\n"
            + "\n".join(f"  - {x}" for x in unconfirmed_breaking)
            + "\n\n"
            "Example:\n"
            "  confirmed_drops:\n"
            f"    - {unconfirmed_breaking[0].split()[-1]}\n"
        )
        raise DropSafetyError(msg)

    # Append drops in DROP_ORDER
    for t in DROP_ORDER:
        changesets.extend(drop_changesets[t])

    return changesets


def write_changesets(changesets: list[Changeset], output_root: Path) -> Path:
    """Write one .sql file per logical group and a master changelog."""
    output_root.mkdir(parents=True, exist_ok=True)
    print(f"[engine] Output directory (absolute): {output_root.resolve()}")
    changesets_dir = output_root / "changesets"
    changesets_dir.mkdir(exist_ok=True)

    # Group by operation + object_type for neat file naming
    grouped: dict[tuple[str, str], list[Changeset]] = {}
    for cs in changesets:
        key = (cs.operation, cs.object_type)
        grouped.setdefault(key, []).append(cs)

    generated_files: list[str] = []
    for (op, obj_type), items in grouped.items():
        fname = f"{op}_{obj_type}.sql"
        fpath = changesets_dir / fname
        body = (
            f"--liquibase formatted sql\n"
            f"-- Generated by engine: {op} {obj_type} changesets\n\n"
            + "\n".join(cs.to_formatted_sql() for cs in items)
        )
        fpath.write_text(body)
        generated_files.append(f"changesets/{fname}")

    # Write master changelog
    master_path = output_root / "master.xml"
    master = ['<?xml version="1.0" encoding="UTF-8"?>']
    master.append(
        '<databaseChangeLog\n'
        '    xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
        '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '    xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog\n'
        '        http://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-4.20.xsd">'
    )
    # Include in execution order
    for op in ["create", "alter", "seed", "grant", "drop"]:
        if op == "grant":
            f = "changesets/grant_grant.sql"
            if f in generated_files:
                master.append(f'  <include file="{f}" relativeToChangelogFile="true"/>')
        elif op == "seed":
            for f in sorted(generated_files):
                if f.startswith("changesets/seed_"):
                    master.append(f'  <include file="{f}" relativeToChangelogFile="true"/>')
        else:
            order = CREATE_ORDER if op != "drop" else DROP_ORDER
            for obj_type in order:
                f = f"changesets/{op}_{obj_type}.sql"
                if f in generated_files:
                    master.append(f'  <include file="{f}" relativeToChangelogFile="true"/>')
    master.append("</databaseChangeLog>")
    master_path.write_text("\n".join(master) + "\n")
    print(f"[engine] Master changelog (absolute): {master_path.resolve()}")

    return master_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Liquibase DDL changesets")
    parser.add_argument("--env", required=True, help="Target env (DEV, UAT, PROD)")
    parser.add_argument("--bundles-root", default="bundles", type=Path)
    parser.add_argument("--platform-root", default="platform", type=Path)
    parser.add_argument("--output-root", default="output/ddl", type=Path)
    parser.add_argument(
        "--offline", action="store_true",
        help="Skip Snowflake state read (treat as fresh environment)"
    )
    args = parser.parse_args()

    print(f"[engine] Loading platform config for env={args.env}")
    platform_cfg = load_platform_config(args.platform_root, args.env)

    print(f"[engine] Discovering bundles under {args.bundles_root}")
    bundles = load_all_bundles(args.bundles_root, args.env, platform_cfg)
    print(f"[engine] Loaded {len(bundles)} bundle(s):")
    for b in bundles:
        print(f"         - {b.name}: {len(b.objects)} object(s)")

    liquibase_tracked: set[str] | None = None

    if args.offline:
        print("[engine] OFFLINE mode: assuming no existing objects in Snowflake")
        existing: dict[str, ExistingObject] = {}
    else:
        print("[engine] Reading current Snowflake state...")
        reader = SnowflakeStateReader.from_env(args.env)
        databases = {b.database for b in bundles}
        existing = {}
        liquibase_tracked = set()
        for db in databases:
            print(f"[engine]   scanning database {db}")
            existing.update(reader.read_database(db))
            tracked = reader.read_liquibase_tracked_fqns(db)
            liquibase_tracked.update(tracked)
            print(f"[engine]   found {len(tracked)} Liquibase-tracked object(s) in {db}")
        print(f"[engine]   found {len(existing)} existing object(s) total")
        print(f"[engine]   {len(existing) - len(liquibase_tracked)} manually created (will be ignored for drop safety)")

    print("[engine] Computing changesets...")
    try:
        changesets = compute_changesets(bundles, existing, liquibase_tracked=liquibase_tracked)
    except DropSafetyError as e:
        print(str(e), file=sys.stderr)
        return 2

    # ---- DQ Rule Seeding ----
    print("[engine] Discovering DQ rules from bundles...")
    platform_vars = platform_cfg.get("variables", {}) or {}
    platform_vars_str = {k: str(v) for k, v in platform_vars.items()}
    all_dq_rules: list[dict] = []
    for bundle in bundles:
        bundle_rules = dq.load_dq_rules(bundle, args.env, platform_vars_str)
        if bundle_rules:
            print(f"         - {bundle.name}: {len(bundle_rules)} DQ rule(s)")
            all_dq_rules.extend(bundle_rules)

    dq_seed_changeset: Changeset | None = None
    if all_dq_rules:
        dq_database = platform_vars_str.get("DATABASE", f"{args.env.upper()}_FS_DB")
        dq_sql = dq.generate_dq_seed_sql(all_dq_rules, dq_database)
        dq_seed_changeset = Changeset(
            id=_hash_id("dq-seed", "dq_rules", dq_sql),
            author="engine",
            object_type="dq_seed",
            operation="seed",
            fqn=f"{dq_database}.DQ.DQ_RULES",
            sql=dq_sql,
            runOnChange=True,
        )
        changesets.append(dq_seed_changeset)
        print(f"[engine] Generated DQ seed changeset with {len(all_dq_rules)} rule(s)")

        # Generate DMF DDL from rules marked with dmf: true
        dmf_definitions = dq.generate_dmf_ddl(all_dq_rules, dq_database)
        if dmf_definitions:
            for dmf_def in dmf_definitions:
                changesets.append(Changeset(
                    id=_hash_id("dmf", dmf_def["fqn"], dmf_def["sql"]),
                    author="engine",
                    object_type="data_metric_function",
                    operation="create",
                    fqn=dmf_def["fqn"],
                    sql=dmf_def["sql"],
                    rollback=dmf_def["drop_sql"],
                    runOnChange=True,
                ))
            print(f"[engine] Generated {len(dmf_definitions)} DMF(s) from DQ rules")
    else:
        print("[engine] No DQ rules found in bundles")

    print(f"[engine] Writing {len(changesets)} changeset(s) to {args.output_root}")
    master = write_changesets(changesets, args.output_root)

    if not changesets:
        print("[engine] No changes required. Empty changelog written for pipeline compatibility.")
        return 0
    print(f"[engine] Master changelog: {master}")

    # Print summary
    op_counts: dict[str, int] = {}
    for cs in changesets:
        key = f"{cs.operation}:{cs.object_type}"
        op_counts[key] = op_counts.get(key, 0) + 1
    print("[engine] Summary:")
    for k, v in sorted(op_counts.items()):
        print(f"         {k}: {v}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
