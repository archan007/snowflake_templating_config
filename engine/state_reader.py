"""
Snowflake state reader: queries INFORMATION_SCHEMA to discover what objects
currently exist in the target environment. Used to detect drops (drift between
bundle definitions and live state).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:
    import snowflake.connector
except ImportError:  # allow import without the dep for unit tests
    snowflake = None  # type: ignore


@dataclass
class ExistingColumn:
    name: str
    data_type: str
    nullable: bool
    default: str | None
    ordinal_position: int


@dataclass
class ExistingObject:
    object_type: str  # table, view, stream, task, etc.
    database: str
    schema: str
    name: str
    columns: list[ExistingColumn] = field(default_factory=list)

    @property
    def fqn(self) -> str:
        return f"{self.database}.{self.schema}.{self.name}".upper()


class SnowflakeStateReader:
    """Reads current Snowflake state. Accepts an injected connection for testability."""

    def __init__(self, connection: Any):
        self._conn = connection

    @classmethod
    def from_env(cls, env: str) -> "SnowflakeStateReader":
        """Build a connection from env vars. Used by CI."""
        import os
        if snowflake is None:
            raise RuntimeError("snowflake-connector-python not installed")
        conn = snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            private_key=_load_private_key(
                os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")
                or os.environ["SNOWFLAKE_PRIVATE_KEY"]
            ),
            role=os.environ.get("SNOWFLAKE_ROLE", f"ROLE_{env.upper()}_ADMIN"),
            warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", f"WH_{env.upper()}_XS"),
        )
        return cls(conn)

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            cur.close()

    def read_database(self, database: str) -> dict[str, ExistingObject]:
        """Return all managed objects in the database keyed by FQN."""
        objects: dict[str, ExistingObject] = {}

        # Tables and views
        rows = self._query(
            f"SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
            f"FROM {database}.INFORMATION_SCHEMA.TABLES "
            f"WHERE TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA')"
        )
        for schema, name, table_type in rows:
            obj_type = {
                "BASE TABLE": "table",
                "VIEW": "view",
                "MATERIALIZED VIEW": "view",
                "DYNAMIC TABLE": "dynamic_table",
            }.get(table_type, "table")
            obj = ExistingObject(
                object_type=obj_type, database=database, schema=schema, name=name
            )
            objects[obj.fqn] = obj

        # Columns for tables
        rows = self._query(
            f"SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, "
            f"IS_NULLABLE, COLUMN_DEFAULT, ORDINAL_POSITION "
            f"FROM {database}.INFORMATION_SCHEMA.COLUMNS "
            f"WHERE TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA') "
            f"ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
        )
        for schema, table, col, dtype, is_null, default, pos in rows:
            fqn = f"{database}.{schema}.{table}".upper()
            if fqn in objects:
                objects[fqn].columns.append(
                    ExistingColumn(
                        name=col,
                        data_type=dtype,
                        nullable=(is_null == "YES"),
                        default=default,
                        ordinal_position=pos,
                    )
                )

        # Streams
        try:
            rows = self._query(f"SHOW STREAMS IN DATABASE {database}")
            cur = self._conn.cursor()
            cur.execute(f"SHOW STREAMS IN DATABASE {database}")
            cols = [c[0].lower() for c in cur.description]
            for row in cur.fetchall():
                rowd = dict(zip(cols, row))
                obj = ExistingObject(
                    object_type="stream",
                    database=database,
                    schema=rowd.get("schema_name", ""),
                    name=rowd.get("name", ""),
                )
                objects[obj.fqn] = obj
            cur.close()
        except Exception:
            pass  # no streams in this DB yet

        # Tasks
        try:
            cur = self._conn.cursor()
            cur.execute(f"SHOW TASKS IN DATABASE {database}")
            cols = [c[0].lower() for c in cur.description]
            for row in cur.fetchall():
                rowd = dict(zip(cols, row))
                obj = ExistingObject(
                    object_type="task",
                    database=database,
                    schema=rowd.get("schema_name", ""),
                    name=rowd.get("name", ""),
                )
                objects[obj.fqn] = obj
            cur.close()
        except Exception:
            pass

        # Stored procedures
        rows = self._query(
            f"SELECT PROCEDURE_SCHEMA, PROCEDURE_NAME "
            f"FROM {database}.INFORMATION_SCHEMA.PROCEDURES "
            f"WHERE PROCEDURE_SCHEMA NOT IN ('INFORMATION_SCHEMA')"
        )
        for schema, name in rows:
            obj = ExistingObject(
                object_type="stored_procedure",
                database=database, schema=schema, name=name,
            )
            objects[obj.fqn] = obj

        # Stages
        try:
            cur = self._conn.cursor()
            cur.execute(f"SHOW STAGES IN DATABASE {database}")
            cols = [c[0].lower() for c in cur.description]
            for row in cur.fetchall():
                rowd = dict(zip(cols, row))
                obj = ExistingObject(
                    object_type="stage",
                    database=database,
                    schema=rowd.get("schema_name", ""),
                    name=rowd.get("name", ""),
                )
                objects[obj.fqn] = obj
            cur.close()
        except Exception:
            pass

        # File formats
        rows = self._query(
            f"SELECT FILE_FORMAT_SCHEMA, FILE_FORMAT_NAME "
            f"FROM {database}.INFORMATION_SCHEMA.FILE_FORMATS "
            f"WHERE FILE_FORMAT_SCHEMA NOT IN ('INFORMATION_SCHEMA')"
        )
        for schema, name in rows:
            obj = ExistingObject(
                object_type="file_format",
                database=database, schema=schema, name=name,
            )
            objects[obj.fqn] = obj

        return objects


    def read_liquibase_tracked_fqns(self, database: str) -> set[str]:
        """Return FQNs of objects that were deployed through Liquibase.

        Reads the DATABASECHANGELOG table (in the LIQUIBASE schema) and parses
        the COMMENTS column. Engine-generated comments follow the format:
            CREATE table DEV_FS_DB.BRONZE.RAW_ORDERS
        We extract the third token (the FQN) from each comment.

        Objects found in Snowflake but NOT in this set were created manually
        and should not trigger breaking-change errors.
        """
        fqns: set[str] = set()
        try:
            rows = self._query(
                f"SELECT COMMENTS FROM {database}.LIQUIBASE.DATABASECHANGELOG "
                f"WHERE AUTHOR = 'engine' AND COMMENTS IS NOT NULL"
            )
            for (comment,) in rows:
                parts = comment.strip().split()
                if len(parts) >= 3:
                    fqn = parts[-1].upper()
                    if fqn.count(".") == 2:
                        fqns.add(fqn)
        except Exception:
            pass
        return fqns


def _load_private_key(pem_or_path: str):
    """Load a private key from either a PEM string or a file path."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    import os

    if os.path.isfile(pem_or_path):
        with open(pem_or_path, "rb") as f:
            pem_bytes = f.read()
    else:
        pem_bytes = pem_or_path.encode()

    pk = serialization.load_pem_private_key(
        pem_bytes, password=None, backend=default_backend()
    )
    return pk.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
