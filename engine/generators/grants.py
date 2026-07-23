"""
Generator for GRANT statements based on bundle-level grants configuration.
"""
from __future__ import annotations

from ..config_loader import Bundle


OBJECT_TYPE_TO_GRANT_TYPE = {
    "table": "TABLE",
    "view": "VIEW",
    "dynamic_table": "DYNAMIC TABLE",
    "stored_procedure": "PROCEDURE",
    "task": "TASK",
    "stream": "STREAM",
    "stage": "STAGE",
    "file_format": "FILE FORMAT",
}

OBJECT_TYPE_PLURAL_MAP = {
    "tables": "table",
    "views": "view",
    "dynamic_tables": "dynamic_table",
    "stored_procedures": "stored_procedure",
    "tasks": "task",
    "streams": "stream",
    "stages": "stage",
    "file_formats": "file_format",
}


def generate_grants(bundle: Bundle) -> list[str]:
    """Generate GRANT SQL statements for all objects matching the bundle's grants config."""
    statements: list[str] = []

    for grant_rule in bundle.grants:
        role = grant_rule["role"]
        privileges = grant_rule.get("privileges", {})

        for type_plural, privs in privileges.items():
            obj_type = OBJECT_TYPE_PLURAL_MAP.get(type_plural)
            if not obj_type:
                continue

            grant_type = OBJECT_TYPE_TO_GRANT_TYPE[obj_type]

            if isinstance(privs, str):
                privs = [privs]

            for obj in bundle.objects:
                if obj.object_type != obj_type:
                    continue

                priv_str = ", ".join(privs)
                fqn = obj.fqn
                if obj_type == "stored_procedure":
                    sig = obj.props.get("signature", "()")
                    statements.append(
                        f"GRANT {priv_str} ON PROCEDURE {fqn}{sig} TO ROLE {role};"
                    )
                else:
                    statements.append(
                        f"GRANT {priv_str} ON {grant_type} {fqn} TO ROLE {role};"
                    )

    return statements
