# Data Quality Tower - Architecture Document

## Executive Summary

The Data Quality (DQ) Tower is a Snowflake-native framework that provides automated, declarative data quality monitoring across all layers of the data platform. It combines Snowflake's built-in Data Metric Functions (DMFs) for statistical profiling with a custom rules engine for business-specific validations, unified under a single results store and Streamlit-powered dashboard.

The framework operates on a **tiered ownership model**: platform engineers define rules as version-controlled YAML (engine-managed), while business users create ad-hoc rules through the Streamlit interface (user-managed). Both coexist without divergence.

---

## 1. Design Principles

| Principle | Description |
|-----------|-------------|
| **Snowflake-Native** | All execution happens inside Snowflake (stored procedures, tasks, DMFs). No external Python runtimes or third-party frameworks required. |
| **Declarative-First** | Rules are defined in YAML and transformed into executable SQL by the engine. No manual SQL authoring for standard rule types. |
| **Tiered Ownership** | Clear separation between engine-managed (version-controlled) and user-managed (runtime-editable) rules. No divergence. |
| **Unified Results** | All results (finite rules + DMF stats) flow into a single `DQ_RESULTS` table for consistent querying, dashboarding, and alerting. |
| **Observational Scope** | The DQ Tower logs and alerts. It does not block downstream pipelines (by design, for initial rollout). |
| **Zero External Dependencies** | No Great Expectations, no dbt tests, no Airflow. The DQ framework is an extension of the existing engine/bundle architecture. |

---

## 2. High-Level Architecture

```
+-----------------------------------------------------------------------------------+
|                              DATA QUALITY TOWER                                    |
+-----------------------------------------------------------------------------------+
|                                                                                   |
|   +-------------------+      +-------------------+      +--------------------+    |
|   |   YAML / CSV      |      |    ENGINE          |      |   SNOWFLAKE        |    |
|   |   Rule Definitions |----->|   (Python)         |----->|   DDL + SEED SQL   |    |
|   |   (Git-managed)   |      |   generate_ddl.py  |      |   via Liquibase    |    |
|   +-------------------+      +-------------------+      +--------------------+    |
|                                                                                   |
|   +-------------------+      +-------------------+      +--------------------+    |
|   |   STREAMLIT       |      |   DQ SCHEMA        |      |   SNOWFLAKE DMFs   |    |
|   |   Dashboard       |<-----|   (Tables + Procs)  |<-----|   (System Metrics) |    |
|   |   + Rule Mgmt     |      |                   |      |                    |    |
|   +-------------------+      +-------------------+      +--------------------+    |
|                                                                                   |
+-----------------------------------------------------------------------------------+
```

---

## 3. Component Architecture

### 3.1 DQ Schema Objects

All DQ infrastructure resides in the `DQ` schema of the per-environment database (e.g., `DEV_FS_DB.DQ`, `PROD_FS_DB.DQ`).

#### Tables

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `DQ_RULES` | Rule definitions (both engine and user sources) | RULE_ID, RULE_TYPE, SOURCE, TARGET_TABLE, SEVERITY, PARAMETERS, IS_ACTIVE |
| `DQ_RESULTS` | Execution results from all rule evaluations | RESULT_ID, RULE_ID, RUN_ID, STATUS, ACTUAL_VALUE, ROWS_FAILED |
| `DQ_RUN_LOG` | Run-level metadata (aggregated pass/fail counts) | RUN_ID, RUN_TYPE, TOTAL_RULES, PASSED, FAILED, ERRORED |
| `DQ_DMF_CONFIG` | Configuration for Snowflake DMF assignments | CONFIG_ID, TARGET_TABLE, DMF_NAME, SCHEDULE_MINUTES |

#### Stored Procedures

| Procedure | Purpose | Signature |
|-----------|---------|-----------|
| `SP_DQ_EXECUTE_ALL` | Executes all active rules, logs a full run | (P_ENVIRONMENT, P_TRIGGERED_BY) |
| `SP_DQ_EXECUTE_RULE` | Executes a single rule by ID, routes to correct rule-type handler | (P_RULE_ID, P_RUN_ID, P_ENVIRONMENT) |
| `SP_DQ_EXECUTE_BY_TABLE` | Executes all rules targeting a specific table | (P_TARGET_TABLE, P_ENVIRONMENT) |
| `SP_DQ_SYNC_DMF_RESULTS` | Syncs Snowflake DMF monitoring results into DQ_RESULTS | (P_ENVIRONMENT) |
| `SP_DQ_ASSIGN_DMFS` | Assigns system DMFs to tables per DQ_DMF_CONFIG | () |

#### Tasks

| Task | Schedule | Action |
|------|----------|--------|
| `TSK_DQ_RUN_FINITE` | Every 60 minutes | Calls `SP_DQ_EXECUTE_ALL` |
| `TSK_DQ_SYNC_DMF` | Every 120 minutes | Calls `SP_DQ_SYNC_DMF_RESULTS` |

---

### 3.2 Rule Type Vocabulary

The DQ Tower supports 8 rule types, each mapping to a parameterized SQL template inside `SP_DQ_EXECUTE_RULE`:

| Rule Type | Scope | Description | Parameters |
|-----------|-------|-------------|------------|
| `NOT_NULL` | Column | Asserts zero NULL values in the target column | None |
| `UNIQUE` | Column | Asserts zero duplicate values (ignoring NULLs) | None |
| `ACCEPTED_VALUES` | Column | Values must be within a provided list | `values: [list]` |
| `ROW_COUNT_RANGE` | Table | Table row count must fall within min/max bounds | `min`, `max` |
| `REFERENTIAL_INTEGRITY` | Column | All non-null values must exist in a reference table/column | `reference_table`, `reference_column` |
| `RECENCY` | Column | Most recent timestamp must be within N hours of current time | `max_hours` |
| `CUSTOM_SQL` | Table | User-provided SQL scalar must equal an expected value | `sql`, `expected` |
| `EXPRESSION` | Table | SQL expression must evaluate TRUE for all rows | `expression` |

---

### 3.3 Tiered Ownership Model

```
+-----------------------------------------------+
|              DQ_RULES TABLE                    |
+-----------------------------------------------+
|                                               |
|   SOURCE = 'engine'         SOURCE = 'user'   |
|   +-------------------+    +----------------+ |
|   | Defined in YAML   |    | Created in     | |
|   | Version-controlled|    | Streamlit UI   | |
|   | Deployed via CI/CD|    | Lives in DB    | |
|   | Read-only in UI   |    | Editable in UI | |
|   | MERGE on deploy   |    | Never touched  | |
|   |                   |    | by engine      | |
|   +-------------------+    +----------------+ |
|                                               |
+-----------------------------------------------+
```

**Deployment Behavior:**

1. Engine reads `bundles/<layer>/dq_rules/*.yaml` files
2. Generates a `MERGE` statement targeting rows where `SOURCE = 'engine'`
3. New rules are inserted, changed rules are updated, removed rules are deactivated (`IS_ACTIVE = FALSE`)
4. Rows where `SOURCE = 'user'` are never modified by the engine

**Promotion Workflow:**

When a user-created rule proves valuable and should become production-grade:
1. Developer extracts the rule definition into the appropriate `dq_rules/*.yaml` file
2. Sets the same `RULE_ID` as the user rule
3. On next deployment, the MERGE takes ownership (flips `SOURCE` to `engine`)
4. Rule becomes read-only in the Streamlit interface

---

### 3.4 Snowflake DMF Integration

```
+-------------------+       +-------------------+       +-------------------+
|  DQ_DMF_CONFIG    |       | SP_DQ_ASSIGN_DMFS |       | Snowflake Internal|
|  (what to track)  |------>| (assigns DMFs to  |------>| DMF Execution     |
|                   |       |  tables/columns)  |       | (automatic)       |
+-------------------+       +-------------------+       +-------------------+
                                                                |
                                                                v
+-------------------+       +-------------------+       +-------------------+
|  DQ_RESULTS       |<------| SP_DQ_SYNC_DMF    |<------| DATA_QUALITY_     |
|  (unified store)  |       | _RESULTS          |       | MONITORING_RESULTS|
+-------------------+       +-------------------+       +-------------------+
```

**Supported System DMFs:**

| DMF Name | Level | Description |
|----------|-------|-------------|
| `NULL_COUNT` | Column | Number of NULL values |
| `DUPLICATE_COUNT` | Column | Number of duplicate values |
| `UNIQUE_COUNT` | Column | Count of distinct values |
| `FRESHNESS` | Table | Time since last data modification |

---

## 4. Data Flow

### 4.1 Rule Seeding (Deployment Time)

```
dq_rules/*.yaml  -->  Engine (generate_ddl.py)  -->  MERGE SQL  -->  Liquibase  -->  DQ_RULES table
```

1. Developer defines rules in YAML within the appropriate bundle
2. Engine's `dq_rules.py` generator discovers all `dq_rules/*.yaml` files across bundles
3. Generates a single MERGE changeset with `runOnChange:true`
4. Liquibase applies the MERGE on deployment (idempotent)
5. Rules not present in YAML are deactivated (soft delete)

### 4.2 Rule Execution (Runtime)

```
TSK_DQ_RUN_FINITE (hourly)
    |
    v
SP_DQ_EXECUTE_ALL
    |
    +--> Creates RUN_LOG entry
    |
    +--> For each active rule:
    |       |
    |       v
    |    SP_DQ_EXECUTE_RULE
    |       |
    |       +--> Routes to rule_type handler
    |       +--> Executes parameterized SQL against target table
    |       +--> Writes result to DQ_RESULTS
    |       +--> Returns status (PASSED / FAILED / ERROR)
    |
    +--> Updates RUN_LOG with final counts
    |
    v
Returns summary: {run_id, total, passed, failed, errored}
```

### 4.3 DMF Sync (Runtime)

```
TSK_DQ_SYNC_DMF (every 2 hours)
    |
    v
SP_DQ_SYNC_DMF_RESULTS
    |
    +--> Queries INFORMATION_SCHEMA.DATA_QUALITY_MONITORING_RESULTS
    +--> Filters to tables configured in DQ_DMF_CONFIG
    +--> MERGEs into DQ_RESULTS with synthetic RULE_IDs (DMF_ prefix)
    |
    v
Unified results available alongside finite rule results
```

---

## 5. Rule Definition Format

### 5.1 YAML Schema

Rules are defined in `bundles/<layer>/dq_rules/<table_name>.yaml`:

```yaml
target_table: RAW_ORDERS
target_schema: BRONZE
owner: data-engineering

rules:
  - name: raw_orders_order_id_not_null     # Unique identifier (becomes RULE_ID)
    type: NOT_NULL                          # One of the 8 rule types
    column: ORDER_ID                        # Target column (optional for table-level)
    severity: CRITICAL                      # CRITICAL | WARNING | INFO
    description: "ORDER_ID must never be null"
    parameters:                             # Type-specific parameters (optional)
      <key>: <value>
```

### 5.2 Rule Naming Convention

Rules follow the pattern: `<table_name_lowercase>_<what_it_checks>`

Examples:
- `raw_orders_order_id_not_null`
- `fct_sales_referential_integrity_orders`
- `dim_customer_status_accepted_values`

### 5.3 Severity Levels

| Level | Meaning | Action |
|-------|---------|--------|
| `CRITICAL` | Data integrity violation that indicates a pipeline failure | Alert immediately, investigate |
| `WARNING` | Anomaly that may indicate a problem but is not blocking | Alert within SLA, review |
| `INFO` | Advisory check for monitoring trends | Dashboard only, no alert |

---

## 6. Execution Infrastructure

### 6.1 Warehouse Strategy

| Environment | Warehouse | Purpose |
|-------------|-----------|---------|
| DEV | `WH_DEV_DQ` | DQ task execution in development |
| UAT | `WH_UAT_DQ` | DQ task execution in user acceptance testing |
| PROD | `WH_PROD_DQ` | DQ task execution in production |

The DQ warehouse is dedicated and isolated from pipeline warehouses to:
- Prevent DQ checks from consuming pipeline compute budget
- Allow independent scaling based on rule count and complexity
- Provide clear cost attribution for DQ operations

### 6.2 Task Scheduling

```
 0:00    1:00    2:00    3:00    4:00    5:00    ...
   |       |       |       |       |       |
   +-- TSK_DQ_RUN_FINITE (every 60 min) --------->
   |               |               |
   +---- TSK_DQ_SYNC_DMF (every 120 min) -------->
```

Both tasks are independent (no `AFTER` dependency on pipeline tasks). This keeps the DQ Tower purely observational.

---

## 7. Streamlit Dashboard

### 7.1 Pages

| Page | Purpose | Key Visuals |
|------|---------|-------------|
| **Overview** | Executive summary of current DQ state | Pass rate gauge, latest results table, failures by severity |
| **Run History** | Time-series view of all DQ runs | Run log table, pass/fail trend line chart |
| **Rule Detail** | Deep-dive into individual rule performance | Execution history, average runtime, failure rate |
| **Exploratory Stats** | DMF-sourced profiling statistics | Per-column null counts, uniqueness, freshness |
| **Rule Management** | View/create/filter rules | Filterable rule list, user-rule creation form |

### 7.2 Access Control

| Source | Editable in UI | Visible in UI |
|--------|---------------|---------------|
| `engine` | No (read-only) | Yes (with visual indicator) |
| `user` | Yes (edit/deactivate) | Yes |

### 7.3 Deployment Options

The Streamlit app is designed for two deployment models:

1. **Streamlit in Snowflake (SiS)**: Deployed as a native Snowflake app, uses the active session context. No credentials to manage.
2. **Standalone**: Deployed externally (VM, container, Streamlit Cloud), connects via `snowflake-connector-python` with credentials in environment variables or Streamlit secrets.

---

## 8. Integration Points

### 8.1 CI/CD Pipeline Integration

```
PR Opened
    |
    v
pr-checks.yaml
    |
    +--> Bundle validation (includes DQ bundle)
    +--> DDL generation (includes DQ seed changeset)
    +--> Dry-run validation
    |
    v
Merge to main
    |
    v
deploy-dev.yaml --> deploy-uat.yaml --> deploy-prod.yaml
    |
    +--> Liquibase applies DQ table DDL
    +--> Liquibase applies DQ procedure DDL
    +--> Liquibase applies DQ seed (MERGE rules)
    +--> Tasks are created/resumed
```

### 8.2 Alerting Integration (Future)

The `DQ_RESULTS` table is the single source of truth for alerting:

```sql
-- Query for alerting service (Teams / Outlook / PagerDuty)
SELECT r.RULE_ID, r.STATUS, r.ACTUAL_VALUE, r.ROWS_FAILED,
       ru.SEVERITY, ru.TARGET_TABLE, ru.DESCRIPTION
FROM DQ.DQ_RESULTS r
JOIN DQ.DQ_RULES ru ON r.RULE_ID = ru.RULE_ID
WHERE r.RUN_TIMESTAMP >= DATEADD(HOUR, -1, CURRENT_TIMESTAMP())
  AND r.STATUS = 'FAILED'
  AND ru.SEVERITY IN ('CRITICAL', 'WARNING')
ORDER BY ru.SEVERITY DESC, r.RUN_TIMESTAMP DESC;
```

Alerting can be implemented via:
- Snowflake Alert objects (native)
- External function calling a webhook (Teams/Slack)
- Snowpipe Streaming to an event bus
- Streamlit scheduled email reports

### 8.3 Pipeline Gating (Future)

When the DQ Tower matures from observational to enforcement mode:

```sql
-- Check in a downstream task's WHEN clause
WHEN SYSTEM$STREAM_HAS_DATA('...')
 AND (SELECT FAILED FROM DQ.DQ_RUN_LOG
      WHERE RUN_ID = (SELECT MAX(RUN_ID) FROM DQ.DQ_RUN_LOG
                      WHERE ENVIRONMENT = 'PROD')
     ) = 0
```

This would only proceed if the latest DQ run had zero failures.

---

## 9. Security and Access

### 9.1 Roles

| Role | Permissions | Use Case |
|------|-------------|----------|
| `ROLE_DQ_READER` | SELECT on DQ tables and views | Dashboard consumers, analysts |
| `ROLE_DQ_WRITER` | SELECT, INSERT, UPDATE on DQ tables + USAGE on procedures | Streamlit app service account, DQ operators |
| Pipeline Role | Full ownership of DQ schema | CI/CD deployment |

### 9.2 Data Sensitivity

- `DQ_RULES` contains no sensitive data (rule definitions only)
- `DQ_RESULTS` may contain column values in `SAMPLE_FAILURES` -- scope access accordingly
- `CUSTOM_SQL` rules can query any table the executing role has access to -- restrict `ROLE_DQ_WRITER` to appropriate schemas

---

## 10. File Structure

```
project/
├── bundles/
│   ├── bronze/
│   │   ├── bundle.yaml
│   │   ├── schemas/
│   │   │   ├── raw_orders.csv
│   │   │   └── raw_customers.csv
│   │   └── dq_rules/                    <-- DQ rules for bronze tables
│   │       ├── raw_orders.yaml
│   │       └── raw_customers.yaml
│   ├── silver/
│   │   └── ...
│   ├── gold/
│   │   ├── bundle.yaml
│   │   ├── dq_rules/                    <-- DQ rules for gold tables
│   │   │   └── fct_sales.yaml
│   │   └── products/
│   │       └── ...
│   └── dq/                              <-- DQ infrastructure bundle
│       ├── bundle.yaml                  <-- Defines all DQ objects
│       ├── schemas/
│       │   ├── dq_rules.csv
│       │   ├── dq_results.csv
│       │   ├── dq_run_log.csv
│       │   └── dq_dmf_config.csv
│       └── sql/
│           ├── sp_dq_execute_all.sql
│           ├── sp_dq_execute_rule.sql
│           ├── sp_dq_execute_by_table.sql
│           ├── sp_dq_sync_dmf_results.sql
│           ├── sp_dq_assign_dmfs.sql
│           ├── tsk_dq_run_finite.sql
│           └── tsk_dq_sync_dmf.sql
├── engine/
│   ├── generate_ddl.py                  <-- Extended to discover and seed DQ rules
│   └── generators/
│       ├── tables.py
│       ├── objects.py
│       ├── grants.py
│       └── dq_rules.py                  <-- NEW: DQ rule parser + seed SQL generator
├── streamlit/
│   ├── app.py                           <-- DQ Dashboard application
│   └── README.md
├── platform/
│   └── overrides/
│       ├── dev.yaml                     <-- Includes DQ_WAREHOUSE variable
│       ├── uat.yaml
│       └── prod.yaml
└── output/
    └── ddl/
        └── changesets/
            └── seed_dq_seed.sql         <-- Generated MERGE changeset
```

---

## 11. Environment Configuration

### Platform Variables

| Variable | DEV | UAT | PROD |
|----------|-----|-----|------|
| `DATABASE` | DEV_FS_DB | UAT_FS_DB | PROD_FS_DB |
| `DQ_WAREHOUSE` | WH_DEV_DQ | WH_UAT_DQ | WH_PROD_DQ |
| `DEFAULT_WAREHOUSE` | WH_DEV_XS | WH_UAT_S | WH_PROD_M |

---

## 12. Decision Log

| Decision | Rationale | Alternative Considered |
|----------|-----------|----------------------|
| Snowflake-native execution | Zero external dependencies, data stays in-platform, consistent with existing architecture | Great Expectations (rejected: external Python runtime, competing declarative format, DMF redundancy) |
| MERGE-based rule seeding | Idempotent, handles insert/update/deactivation in one statement, `runOnChange` ensures re-execution on any rule change | INSERT with DELETE (rejected: not atomic, risk of orphaned rules) |
| Tiered ownership (engine + user) | Gives platform teams version control while allowing business agility | Single source only (rejected: either too rigid or creates drift) |
| Dedicated DQ warehouse | Cost isolation, independent scaling, clear attribution | Shared pipeline warehouse (rejected: resource contention, unclear cost allocation) |
| Observational-only (no pipeline blocking) | Lower risk for initial rollout, builds trust in rule accuracy before enforcing | Enforcement from day one (rejected: false positives would block production) |
| Single DQ_RESULTS table for finite + DMF | Simplifies dashboarding, alerting, and querying; one table to monitor | Separate tables (rejected: doubles query complexity, multiple data sources for alerts) |
| 8 parameterized rule types | Covers 95% of use cases without custom SQL; CUSTOM_SQL escape hatch for the rest | Free-form SQL only (rejected: harder to validate, no standardized parameters, inconsistent results) |

---

## 13. Future Roadmap

| Phase | Capability | Status |
|-------|-----------|--------|
| **Phase 1** | Core framework (tables, procedures, tasks, YAML rules, Streamlit) | Delivered |
| **Phase 2** | Teams / Outlook alerting via Snowflake Alert or external function | Planned |
| **Phase 3** | Pipeline gating (CRITICAL failures block downstream tasks) | Planned |
| **Phase 4** | Data lineage integration (trace failures to upstream sources) | Planned |
| **Phase 5** | Anomaly detection (ML-based thresholds replacing static min/max) | Planned |
| **Phase 6** | Self-service rule builder (visual UI in Streamlit for non-SQL users) | Planned |

---

## 14. Operational Runbook

### Ad-Hoc Rule Execution

```sql
-- Run all rules for a specific table
CALL DQ.SP_DQ_EXECUTE_BY_TABLE('PROD_FS_DB.BRONZE.RAW_ORDERS', 'PROD');

-- Run a single rule for testing
CALL DQ.SP_DQ_EXECUTE_RULE('raw_orders_order_id_not_null', 'manual-run-001', 'DEV');
```

### Assign DMFs to a New Table

```sql
INSERT INTO DQ.DQ_DMF_CONFIG (CONFIG_ID, TARGET_DATABASE, TARGET_SCHEMA, TARGET_TABLE, TARGET_COLUMN, DMF_NAME, SCHEDULE_MINUTES)
VALUES (UUID_STRING(), 'PROD_FS_DB', 'BRONZE', 'RAW_ORDERS', 'ORDER_ID', 'NULL_COUNT', 60);

-- Then run assignment
CALL DQ.SP_DQ_ASSIGN_DMFS();
```

### Check Latest Failures

```sql
SELECT r.RULE_ID, ru.SEVERITY, ru.TARGET_TABLE, r.STATUS, r.ACTUAL_VALUE, r.ROWS_FAILED
FROM DQ.DQ_RESULTS r
JOIN DQ.DQ_RULES ru ON r.RULE_ID = ru.RULE_ID
WHERE r.RUN_TIMESTAMP = (SELECT MAX(RUN_TIMESTAMP) FROM DQ.DQ_RESULTS)
  AND r.STATUS = 'FAILED'
ORDER BY ru.SEVERITY DESC;
```

### Deactivate a Rule

```sql
-- For user-managed rules only (engine rules should be removed from YAML)
UPDATE DQ.DQ_RULES SET IS_ACTIVE = FALSE, UPDATED_AT = CURRENT_TIMESTAMP()
WHERE RULE_ID = 'my_rule_name' AND SOURCE = 'user';
```

---

*Document Version: 1.0*
*Last Updated: July 2026*
*Author: Platform Engineering*
