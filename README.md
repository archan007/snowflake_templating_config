# Snowflake Platform

Config-driven Snowflake DDL platform. You describe what you want in `bundle.yaml` files and CSV schemas; the engine diffs that against live Snowflake state and emits Liquibase-compatible changesets; Liquibase applies them via CI.

---

## Repository layout

```
.
├── bundles/                 # What you want to exist in Snowflake
│   ├── bronze/              # → BRONZE schema
│   │   ├── bundle.yaml
│   │   └── schemas/*.csv
│   ├── silver/              # → SILVER schema
│   │   ├── bundle.yaml
│   │   └── schemas/*.csv
│   └── gold/                # → GOLD schema
│       ├── bundle.yaml
│       └── products/        # Logical grouping of objects by data product
│           └── product_a/
│               ├── schemas/*.csv
│               └── sql/*.sql
├── platform/                # Account-wide config + per-env overrides
│   ├── platform.yaml
│   └── overrides/{dev,uat,prod}.yaml
├── engine/                  # Python templating engine (do not hand-edit output)
│   ├── config_loader.py
│   ├── state_reader.py
│   ├── bundle_validator.py  # PR and deployment validation checks
│   ├── generators/
│   └── generate_ddl.py
├── liquibase/
│   ├── changelog-master.xml # Stable root changelog (committed)
│   └── liquibase.properties
├── output/                  # Generated DDL — gitignored, rebuilt every run
├── test_drop_safety.py
├── requirements.txt
└── .github/workflows/       # pr-checks + deploy-{dev,uat,prod}
```

### Database / schema model

One Snowflake account, one database per environment, three schemas per database:

| Environment | Database     | Schemas                  |
|-------------|--------------|--------------------------|
| DEV         | `DEV_FS_DB`  | `BRONZE`, `SILVER`, `GOLD` |
| UAT         | `UAT_FS_DB`  | `BRONZE`, `SILVER`, `GOLD` |
| PROD        | `PROD_FS_DB` | `BRONZE`, `SILVER`, `GOLD` |

The database name is exposed to bundles and SQL files as `${DATABASE}` and resolved per env from `platform/overrides/<env>.yaml`. Bundles never hardcode the database name. Inside the `GOLD` schema, objects belonging to a particular data product are organised under `products/<product_id>/` for convenience — naming is **not enforced**, so users may name their gold objects however they like (e.g. `PRODUCT_A__FCT_SALES`, `FCT_SALES_PA`, or just `FCT_SALES`). Avoid collisions across products.

---

## Local quickstart (offline)

You don't need Snowflake credentials to run the engine locally. The `--offline` flag skips state reading and assumes a fresh environment.

```bash
pip install -r requirements.txt

# Validate all bundle definitions
python -m engine.bundle_validator --env DEV

# Generate DDL for DEV as if Snowflake were empty
python -m engine.generate_ddl --env DEV --offline --output-root output/ddl

# Run the drop-safety regression tests
python test_drop_safety.py
```

Expected: 12 changesets generated, 6/6 drop-safety tests passing, validator passing with 12 objects across 3 bundles.

Inspect the output:

```bash
ls output/ddl/
cat output/ddl/master.xml
```

---

## Branch strategy and promotion

Three long-lived branches, one-directional promotion:

```
feature/* ──PR──▶ dev ──cherry-pick──▶ uat ──cherry-pick──▶ main (PROD)
```

- All work starts on a feature branch and is PR'd into `dev`.
- Promotion to `uat` and `main` is by **cherry-pick**, not merge.
- CI enforces a **commit-lineage guard rail**: any commit landing on `uat` must already exist on `dev`, and any commit landing on `main` must already exist on `uat`. A direct push that skips a stage will fail the deploy.
- `main` additionally requires manual approval from the team lead / data custodian via the `prod` GitHub Environment.

---

## CI / environment variables

Each deploy workflow reads key-pair credentials from GitHub Secrets. Configure these under Settings → Secrets and variables → Actions:

**Shared:**
- `SNOWFLAKE_ACCOUNT` — e.g. `xy12345.eu-west-2.aws`

**Per environment** (prefix `SNOWFLAKE_<ENV>_`, where `<ENV>` ∈ `DEV`, `UAT`, `PROD`):
- `SNOWFLAKE_<ENV>_USER` — service user, e.g. `SVC_DEV_CI`
- `SNOWFLAKE_<ENV>_ROLE` — role with DDL grants on the target database
- `SNOWFLAKE_<ENV>_WAREHOUSE`
- `SNOWFLAKE_<ENV>_DATABASE` — `DEV_FS_DB` / `UAT_FS_DB` / `PROD_FS_DB`. The engine reads this from the per-env override file (`platform/overrides/<env>.yaml`); the secret value just needs to match so that Liquibase connects to the same database.
- `SNOWFLAKE_<ENV>_PRIVATE_KEY` — PEM-encoded private key (paste the full file contents, including `-----BEGIN PRIVATE KEY-----` headers)

Key-pair auth is manually rotated every 3 months. The CI job writes the key to a tmpfile, uses it, and shreds it on exit.

**GitHub Environments:**
- `dev`, `uat`, `prod` — create one per branch. Put the per-env secrets under the matching environment.
- On the `prod` environment, add required reviewers (data custodian, team lead) and restrict deployment branches to `main`.

**CI service users:** `SVC_DEV_CI`, `SVC_UAT_CI`, `SVC_PROD_CI` — DDL-capable, separate from Airflow's DML-only runtime users. Don't collapse these.

---

## Pipeline stages

### PR checks (every pull request)

The `pr-checks` workflow runs automatically on every PR targeting `dev`, `uat`, or `main`. It performs these steps in order:

1. **YAML syntax validation** — all `bundle.yaml` and platform override files are parsed to catch syntax errors.
2. **Bundle validation** (DEV, UAT, PROD) — the bundle validator (`engine/bundle_validator.py`) runs per-object and inter-dependency checks across all three environments. See [Bundle validation checks](#bundle-validation-checks) below for the full list.
3. **Drop-safety regression tests** — `test_drop_safety.py` verifies all 6 scenarios.
4. **Engine dry-run** (offline, per env) — the engine generates DDL for each environment without Snowflake credentials, verifying the end-to-end generation pipeline.

### Deployment (merge to dev/uat/main)

When a PR is merged, the deploy workflow for that environment:

1. Reads live Snowflake state (tables, views, streams, tasks, procs, stages, file formats).
2. Reads the Liquibase changelog to determine which objects were deployed via the engine (vs. manually created).
3. Computes changesets, applying drop-safety rules only to Liquibase-tracked objects.
4. Writes the generated DDL and runs Liquibase to apply it.

---

## Bundle validation checks

The bundle validator (`python -m engine.bundle_validator --env <ENV>`) performs two categories of checks.

### Per-object checks

| Object type       | Check                | What it catches                                                                 |
|-------------------|----------------------|---------------------------------------------------------------------------------|
| **table**         | `missing_csv`        | No `schema_csv` property specified                                              |
| **table**         | `csv_not_found`      | The referenced CSV file does not exist on disk                                  |
| **table**         | `empty_csv`          | CSV file has no columns defined                                                 |
| **table**         | `invalid_data_type`  | A column uses a data type not supported by Snowflake                            |
| **view**          | `missing_sql_file`   | No `sql_file` property specified                                                |
| **view**          | `sql_not_found`      | The referenced SQL file does not exist on disk                                  |
| **view**          | `empty_sql`          | SQL file is empty                                                               |
| **stream**        | `missing_source`     | Neither `on_table` nor `on_view` is specified                                   |
| **stream**        | `ambiguous_source`   | Both `on_table` and `on_view` are specified (must be one or the other)          |
| **task**          | `missing_warehouse`  | No `warehouse` specified                                                        |
| **task**          | `missing_sql_file`   | No `sql_file` property specified                                                |
| **task**          | `sql_not_found`      | The referenced SQL file does not exist on disk                                  |
| **task**          | `empty_sql`          | SQL file is empty                                                               |
| **task**          | `missing_trigger`    | Neither `schedule` nor `after` is specified (a task needs a trigger)            |
| **stored_procedure** | `missing_sql_file` | No `sql_file` property specified                                               |
| **stored_procedure** | `sql_not_found`    | The referenced SQL file does not exist on disk                                  |
| **stored_procedure** | `empty_sql`        | SQL file is empty                                                               |
| **stored_procedure** | `invalid_sql`      | SQL file does not contain a `CREATE ... PROCEDURE` statement                    |
| **dynamic_table** | `missing_warehouse`  | No `warehouse` specified                                                        |
| **dynamic_table** | `missing_target_lag` | No `target_lag` specified                                                       |
| **dynamic_table** | `invalid_refresh_mode` | `refresh_mode` is not one of `AUTO`, `FULL`, `INCREMENTAL`                   |
| **dynamic_table** | `missing_sql_file`   | No `sql_file` property specified                                                |
| **dynamic_table** | `sql_not_found`      | The referenced SQL file does not exist on disk                                  |
| **dynamic_table** | `empty_sql`          | SQL file is empty                                                               |
| **file_format**   | `missing_type`       | No `type` property specified                                                    |
| **file_format**   | `invalid_type`       | Type is not one of `CSV`, `JSON`, `AVRO`, `ORC`, `PARQUET`, `XML`              |
| **file_format**   | `unsupported_option` | An option key is not valid for the declared format type                          |
| **All types**     | `unknown_properties` | Properties in the YAML that are not recognised for the object type              |

### Supported file format options

Each file format type has a specific set of valid options:

- **CSV**: `COMPRESSION`, `RECORD_DELIMITER`, `FIELD_DELIMITER`, `FILE_EXTENSION`, `PARSE_HEADER`, `SKIP_HEADER`, `SKIP_BLANK_LINES`, `DATE_FORMAT`, `TIME_FORMAT`, `TIMESTAMP_FORMAT`, `BINARY_FORMAT`, `ESCAPE`, `ESCAPE_UNENCLOSED_FIELD`, `TRIM_SPACE`, `FIELD_OPTIONALLY_ENCLOSED_BY`, `NULL_IF`, `ERROR_ON_COLUMN_COUNT_MISMATCH`, `REPLACE_INVALID_CHARACTERS`, `EMPTY_FIELD_AS_NULL`, `SKIP_BYTE_ORDER_MARK`, `ENCODING`
- **JSON**: `COMPRESSION`, `DATE_FORMAT`, `TIME_FORMAT`, `TIMESTAMP_FORMAT`, `BINARY_FORMAT`, `TRIM_SPACE`, `NULL_IF`, `FILE_EXTENSION`, `ENABLE_OCTAL`, `ALLOW_DUPLICATE`, `STRIP_OUTER_ARRAY`, `STRIP_NULL_VALUES`, `REPLACE_INVALID_CHARACTERS`, `IGNORE_UTF8_ERRORS`, `SKIP_BYTE_ORDER_MARK`
- **AVRO**: `COMPRESSION`, `TRIM_SPACE`, `REPLACE_INVALID_CHARACTERS`, `NULL_IF`
- **PARQUET**: `COMPRESSION`, `SNAPPY_COMPRESSION`, `BINARY_AS_TEXT`, `USE_VECTORIZED_SCANNER`, `TRIM_SPACE`, `REPLACE_INVALID_CHARACTERS`, `NULL_IF`
- **ORC**: `TRIM_SPACE`, `REPLACE_INVALID_CHARACTERS`, `NULL_IF`
- **XML**: `COMPRESSION`, `IGNORE_UTF8_ERRORS`, `PRESERVE_SPACE`, `STRIP_OUTER_ELEMENT`, `DISABLE_SNOWFLAKE_DATA`, `DISABLE_AUTO_CONVERT`, `REPLACE_INVALID_CHARACTERS`, `SKIP_BYTE_ORDER_MARK`

### Inter-dependency checks

| Relationship                 | Check                     | What it catches                                                                     |
|------------------------------|---------------------------|-------------------------------------------------------------------------------------|
| Stream → Table/View          | `missing_source_object`   | Stream's `on_table` or `on_view` references an FQN not defined in any bundle        |
| Task → Stored Procedure      | `missing_proc_reference`  | Task's SQL contains a `CALL` to a procedure not defined in any bundle               |
| Task → Task (DAG)            | `missing_after_task`      | Task's `after` property references a task not defined in any bundle                 |
| View → Table                 | `missing_table_reference` | View's SQL references a fully-qualified table/view not defined in any bundle        |
| Dynamic Table → Table        | `missing_table_reference` | Dynamic table's SQL references a fully-qualified table/view not defined in any bundle |
| Stage → File Format          | `missing_file_format`     | Stage's `file_format` references a format not defined in any bundle                 |
| Cross-bundle duplicates      | `duplicate_fqn`           | Two objects across different bundles share the same fully-qualified name             |

---

## How drop-safety works

Not all object removals are equal. The engine splits them into two groups:

**Breaking drops** — `table`, `view`, `dynamic_table`. Removing one from the bundle is potentially destructive (data loss, dependent queries breaking). The engine **refuses to generate a drop** unless you explicitly confirm it in the owning `bundle.yaml`:

```yaml
# bundles/bronze/bundle.yaml
confirmed_drops:
  - ${DATABASE}.BRONZE.RAW_LEGACY_ORPHAN
```

Without that line, the engine fails the build with:

```
[engine] ERROR: breaking-change drop detected without confirmation:
  - TABLE DEV_FS_DB.BRONZE.RAW_LEGACY_ORPHAN
  To proceed, add each FQN to the 'confirmed_drops' list ...
```

> **Use `${DATABASE}` in confirmed_drops, not a hardcoded DB name.** The engine resolves `${DATABASE}` per environment, so a single entry covers DEV, UAT, and PROD as the change is cherry-picked through. The drop is still reviewed at each PR stage (because the cherry-pick lands in a separate PR per branch), but you don't have to maintain three separate FQN strings.

This forces the person removing the object to put the FQN in the PR, which makes it visible in review.

**Silent drops** — `stream`, `task`, `stored_procedure`, `stage`, `file_format`. Removing one from the bundle just generates a `DROP` changeset without ceremony. These objects are cheap to recreate and don't hold data.

### Manually created objects

During deployment, the engine reads the Liquibase `DATABASECHANGELOG` table to determine which objects were deployed through the engine. Objects that exist in Snowflake but are **not** tracked by Liquibase (i.e., created manually via a console, worksheet, or script outside of this repo) are silently ignored.

This means:
- Manually created tables, views, or other objects will **not** trigger breaking-change errors.
- Only objects previously deployed via Liquibase can trigger the `confirmed_drops` requirement.
- This prevents ad-hoc objects (temp tables, one-off reports, cleanup leftovers) from blocking new deployments.

In offline mode (PR checks, local development), this distinction is not available because there is no Liquibase connection. All existing objects are treated as tracked (backwards-compatible).

---

## Unit tests (`test_drop_safety.py`)

The test suite covers 6 scenarios that lock in the drop-safety and state-awareness behavior. Run with `python test_drop_safety.py`.

| # | Test | Scenario | Expected outcome |
|---|------|----------|------------------|
| 1 | `test_unconfirmed_table_drop_fails` | A table exists in Snowflake but is removed from a bundle without adding it to `confirmed_drops` | `DropSafetyError` is raised naming the orphaned table |
| 2 | `test_confirmed_table_drop_succeeds` | Same scenario, but the FQN is added to `confirmed_drops` in the owning `bundle.yaml` | DROP changeset is generated successfully |
| 3 | `test_non_breaking_drops_silent` | A stream and task exist in Snowflake but are removed from bundles (no confirmation needed) | DROP changesets are generated silently for both |
| 4 | `test_liquibase_tables_ignored` | Liquibase's own tracking tables (`DATABASECHANGELOG`, `DATABASECHANGELOGLOCK`) exist in Snowflake | They are silently excluded from the diff, never dropped |
| 5 | `test_manually_created_objects_ignored` | A table and view exist in Snowflake but were created manually (not in Liquibase changelog) | They are silently ignored — no breaking-change error, no drop |
| 6 | `test_liquibase_tracked_drop_still_flagged` | A table was deployed via Liquibase (is in the tracked set) but is now removed from bundles | `DropSafetyError` is raised — Liquibase-tracked objects still require `confirmed_drops` |

### What the tests protect against

- **Accidental data loss**: Tests 1 and 6 ensure that removing a table/view/dynamic_table from a bundle always requires explicit acknowledgment, preventing silent data destruction.
- **Manual-object interference**: Test 5 ensures that ad-hoc objects someone created directly in Snowflake (outside of this repo) will never block deployments or trigger false positives.
- **Infrastructure table safety**: Test 4 ensures the engine never attempts to drop Liquibase's own tracking tables.
- **Graceful non-breaking drops**: Test 3 verifies that ephemeral objects (streams, tasks) are cleaned up automatically without requiring manual confirmation.
- **Explicit confirmation flow**: Test 2 verifies the happy path where a team member intentionally removes a breaking object and the engine generates the correct DROP statement.

---

## Adding a new table

1. Drop a CSV in the bundle's `schemas/` folder with the column definitions:

   ```
   bundles/bronze/schemas/raw_orders.csv
   ```

   ```csv
   column_name,data_type,nullable,default_value,rule,transform_logic,clustering_key,business_key,description
   ORDER_ID,NUMBER(18,0),false,,,,,true,Primary key
   CUSTOMER_ID,NUMBER(18,0),false,,,,,,FK to customers
   ORDER_TOTAL,"NUMBER(18,2)",false,,,,,,Total in GBP
   CREATED_AT,TIMESTAMP_NTZ,false,,,,,,Ingest timestamp
   ```

   > **Quote anything with a comma inside parentheses** like `"NUMBER(18,2)"`. The engine's CSV validator will fail the build with a clear error if you forget.

2. Reference it in `bundle.yaml`:

   ```yaml
   tables:
     - name: RAW_ORDERS
       schema: BRONZE
       description: "Raw orders landed from S3"
       schema_csv: schemas/raw_orders.csv
   ```

3. Run the validator and engine locally to sanity-check:

   ```bash
   python -m engine.bundle_validator --env DEV
   python -m engine.generate_ddl --env DEV --offline --output-root output/ddl
   ```

4. Open a PR against `dev`. The `pr-checks` workflow will run the validator across all three envs, execute drop-safety tests, and dry-run the engine.

### Adding a column

Just edit the CSV. The engine's table generator detects column-level diffs and emits `ALTER TABLE ... ADD COLUMN` — never `CREATE OR REPLACE TABLE`, so existing data is preserved.

### Removing a column

Same: edit the CSV to drop the row. Column removal is treated as a breaking change and must be reviewed carefully — verify no downstream DBT models or consumers depend on it before merging.

### Using `${DATABASE}` in user SQL files

When you write SQL files for views, dynamic tables, stored procedures, or tasks, **never hardcode the database name**. Use `${DATABASE}` and the engine will resolve it per environment at generation time:

```sql
-- bundles/gold/products/product_a/sql/vw_sales_summary.sql
SELECT
    ORDER_DATE,
    SUM(REVENUE_USD) AS TOTAL_REVENUE
FROM ${DATABASE}.GOLD.FCT_SALES
GROUP BY ORDER_DATE
```

Resolves to `DEV_FS_DB.GOLD.FCT_SALES` in DEV, `UAT_FS_DB.GOLD.FCT_SALES` in UAT, etc. The same goes for `confirmed_drops` entries in `bundle.yaml` — use `${DATABASE}.SCHEMA.OBJECT_NAME` and one entry covers all environments.

`${ENV}` and `${ENV_LOWER}` are also available, plus anything you add to a `variables:` block in the platform overrides.

---

## Related docs

- Full blueprint: see the platform blueprint doc (link in the team wiki)
- Drop-safety rationale and worked examples: `test_drop_safety.py`
- Engine internals: docstrings in `engine/generate_ddl.py`
- Validation checks: docstrings in `engine/bundle_validator.py`

---

## Troubleshooting

**Engine fails with `breaking-change drop detected`** — You (or someone) removed a table/view/dynamic_table from a bundle without adding its FQN to `confirmed_drops`. Either put it back or confirm the drop explicitly.

**Bundle validator fails with `missing_source_object`** — A stream references a table or view that doesn't exist in any bundle. Check for typos in the `on_table`/`on_view` FQN, or make sure the referenced object is defined in a bundle.

**Bundle validator fails with `unsupported_option`** — A file format has an option that isn't valid for its type. For example, `strip_outer_array` is valid for JSON but not for CSV. Check the [supported options table](#supported-file-format-options) above.

**Bundle validator fails with `missing_proc_reference`** — A task's SQL has a `CALL` to a stored procedure that isn't defined in any bundle. Make sure the procedure exists and the FQN matches.

**Bundle validator fails with `invalid_data_type`** — A column in a CSV schema uses a type not recognised by Snowflake. Check for typos (e.g. `VARCHA` instead of `VARCHAR`).

**CSV validator complains about `NUMBER(18,2)`** — Unquoted comma inside a CSV field. Wrap the data type in double quotes: `"NUMBER(18,2)"`.

**UAT deploy fails with `Lineage guard FAILED`** — A commit landed on `uat` that isn't on `dev`. Don't push directly to `uat`; cherry-pick from `dev`.

**PROD deploy is stuck** — Waiting for a reviewer to approve in the `prod` GitHub Environment. Ping the data custodian.

**Pipeline fails even though no DDL changes were made** — The engine always generates an empty `output/ddl/master.xml` and creates the directory structure even when there are zero changesets. If you still see failures, check the engine logs for errors that occur before the output step.
