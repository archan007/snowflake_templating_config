"""
Data Quality Dashboard - Streamlit Application

Runs as a Streamlit-in-Snowflake (SiS) app. The connection is provided
automatically by the Snowflake runtime -- no credentials required.
The app inherits the role and warehouse assigned at deployment time.

When running locally for development, set SNOWFLAKE_PRIVATE_KEY_PATH
(or SNOWFLAKE_PRIVATE_KEY as a PEM string) along with SNOWFLAKE_ACCOUNT,
SNOWFLAKE_USER, SNOWFLAKE_DATABASE, SNOWFLAKE_ROLE, SNOWFLAKE_WAREHOUSE.
Password auth is intentionally not supported.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

_IS_SIS = os.getenv("SNOWFLAKE_IS_SIS", "").lower() in ("1", "true", "yes") or \
    "SNOWFLAKE_HOST" in os.environ


def _get_sis_session():
    """Get the active Snowpark session provided by the SiS runtime."""
    from snowflake.snowpark.context import get_active_session
    return get_active_session()


def _get_local_connection():
    """Build a key-pair authenticated connection for local development."""
    import snowflake.connector
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    pk_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH", "")
    pk_raw = os.environ.get("SNOWFLAKE_PRIVATE_KEY", "")

    if pk_path and os.path.isfile(pk_path):
        with open(pk_path, "rb") as f:
            pem_bytes = f.read()
    elif pk_raw:
        pem_bytes = pk_raw.encode()
    else:
        raise RuntimeError(
            "No Snowflake credentials configured. Set SNOWFLAKE_PRIVATE_KEY_PATH "
            "or SNOWFLAKE_PRIVATE_KEY for local development."
        )

    pk = serialization.load_pem_private_key(pem_bytes, password=None, backend=default_backend())
    pk_der = pk.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        private_key=pk_der,
        database=os.environ.get("SNOWFLAKE_DATABASE", ""),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", ""),
        role=os.environ.get("SNOWFLAKE_ROLE", ""),
    )


@st.cache_data(ttl=60)
def run_query(query: str) -> pd.DataFrame:
    """Execute a query and return results as a DataFrame."""
    if _IS_SIS:
        session = _get_sis_session()
        return session.sql(query).to_pandas()
    else:
        conn = _get_local_connection()
        try:
            cur = conn.cursor()
            cur.execute(query)
            cols = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            return pd.DataFrame(rows, columns=cols)
        finally:
            conn.close()


def get_database() -> str:
    if _IS_SIS:
        session = _get_sis_session()
        return session.get_current_database().replace('"', '')
    return os.environ.get("SNOWFLAKE_DATABASE", "")


# ---------------------------------------------------------------------------
# Page Configuration
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Data Quality Dashboard",
    page_icon=":::",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .metric-card {
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
        border-radius: 12px;
        padding: 1.5rem;
        border: 1px solid #2a3a5e;
    }
    .status-passed { color: #10b981; font-weight: 700; }
    .status-failed { color: #ef4444; font-weight: 700; }
    .status-error { color: #f59e0b; font-weight: 700; }
    .rule-engine { background-color: #1e3a5f; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; }
    .rule-user { background-color: #3b1f5e; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; }
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Sidebar Navigation
# ---------------------------------------------------------------------------

st.sidebar.title("DQ Dashboard")
page = st.sidebar.radio(
    "Navigation",
    ["Overview", "Run History", "Rule Detail", "Exploratory Stats", "Rule Management"],
    label_visibility="collapsed",
)

# ---------------------------------------------------------------------------
# Demo mode fallback (when running outside Snowflake without key-pair config)
# ---------------------------------------------------------------------------

DEMO_MODE = not _IS_SIS and not (
    os.getenv("SNOWFLAKE_ACCOUNT") and
    (os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH") or os.getenv("SNOWFLAKE_PRIVATE_KEY"))
)

if DEMO_MODE:
    st.sidebar.warning(
        "Demo mode: using sample data. "
        "When deployed as Streamlit-in-Snowflake, live data is used automatically."
    )


def _demo_rules() -> pd.DataFrame:
    return pd.DataFrame({
        "RULE_ID": ["raw_orders_order_id_not_null", "raw_orders_order_id_unique",
                    "raw_orders_status_accepted_values", "fct_sales_revenue_non_negative",
                    "fct_sales_freshness", "raw_customers_customer_id_not_null"],
        "RULE_NAME": ["raw_orders_order_id_not_null", "raw_orders_order_id_unique",
                      "raw_orders_status_accepted_values", "fct_sales_revenue_non_negative",
                      "fct_sales_freshness", "raw_customers_customer_id_not_null"],
        "RULE_TYPE": ["NOT_NULL", "UNIQUE", "ACCEPTED_VALUES", "EXPRESSION", "RECENCY", "NOT_NULL"],
        "SOURCE": ["engine", "engine", "engine", "engine", "engine", "engine"],
        "TARGET_TABLE": ["RAW_ORDERS", "RAW_ORDERS", "RAW_ORDERS", "FCT_SALES", "FCT_SALES", "RAW_CUSTOMERS"],
        "TARGET_SCHEMA": ["BRONZE", "BRONZE", "BRONZE", "GOLD", "GOLD", "BRONZE"],
        "SEVERITY": ["CRITICAL", "CRITICAL", "WARNING", "WARNING", "WARNING", "CRITICAL"],
        "IS_ACTIVE": [True, True, True, True, True, True],
        "OWNER": ["data-engineering"] * 3 + ["analytics-team"] * 2 + ["data-engineering"],
    })


def _demo_results() -> pd.DataFrame:
    now = datetime.now()
    return pd.DataFrame({
        "RULE_ID": ["raw_orders_order_id_not_null", "raw_orders_order_id_unique",
                    "raw_orders_status_accepted_values", "fct_sales_revenue_non_negative",
                    "fct_sales_freshness", "raw_customers_customer_id_not_null"] * 3,
        "STATUS": (["PASSED", "PASSED", "FAILED", "PASSED", "PASSED", "PASSED"] +
                   ["PASSED", "PASSED", "PASSED", "PASSED", "FAILED", "PASSED"] +
                   ["PASSED", "FAILED", "PASSED", "PASSED", "PASSED", "PASSED"]),
        "RUN_TIMESTAMP": ([now - timedelta(hours=1)] * 6 +
                          [now - timedelta(hours=2)] * 6 +
                          [now - timedelta(hours=3)] * 6),
        "ACTUAL_VALUE": ["0", "0", "3 rows with invalid values", "0", "0", "0",
                         "0", "0", "0", "0", "4 hours since last record", "0",
                         "0", "2", "0", "0", "0", "0"],
        "ROWS_FAILED": [0, 0, 3, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 2, 0, 0, 0, 0],
        "EXECUTION_TIME_MS": [45, 120, 89, 67, 34, 41, 52, 115, 78, 71, 29, 38,
                              48, 130, 82, 69, 31, 44],
    })


def _demo_runs() -> pd.DataFrame:
    now = datetime.now()
    return pd.DataFrame({
        "RUN_ID": ["run-001", "run-002", "run-003"],
        "RUN_TYPE": ["SCHEDULED", "SCHEDULED", "SCHEDULED"],
        "START_TIME": [now - timedelta(hours=1), now - timedelta(hours=2), now - timedelta(hours=3)],
        "END_TIME": [now - timedelta(hours=1, minutes=-2), now - timedelta(hours=2, minutes=-2),
                     now - timedelta(hours=3, minutes=-2)],
        "TOTAL_RULES": [6, 6, 6],
        "PASSED": [5, 5, 5],
        "FAILED": [1, 1, 1],
        "ERRORED": [0, 0, 0],
        "ENVIRONMENT": ["DEV", "DEV", "DEV"],
    })


def get_rules() -> pd.DataFrame:
    if DEMO_MODE:
        return _demo_rules()
    db = get_database()
    return run_query(f"SELECT * FROM {db}.DQ.DQ_RULES WHERE IS_ACTIVE = TRUE ORDER BY SEVERITY, RULE_NAME")


def get_results(hours: int = 24) -> pd.DataFrame:
    if DEMO_MODE:
        return _demo_results()
    db = get_database()
    return run_query(
        f"SELECT * FROM {db}.DQ.DQ_RESULTS "
        f"WHERE RUN_TIMESTAMP >= DATEADD(HOUR, -{hours}, CURRENT_TIMESTAMP()) "
        f"ORDER BY RUN_TIMESTAMP DESC"
    )


def get_runs(hours: int = 72) -> pd.DataFrame:
    if DEMO_MODE:
        return _demo_runs()
    db = get_database()
    return run_query(
        f"SELECT * FROM {db}.DQ.DQ_RUN_LOG "
        f"WHERE START_TIME >= DATEADD(HOUR, -{hours}, CURRENT_TIMESTAMP()) "
        f"ORDER BY START_TIME DESC"
    )


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

if page == "Overview":
    st.title("Data Quality Overview")

    rules_df = get_rules()
    results_df = get_results(hours=24)

    if results_df.empty:
        st.info("No results in the last 24 hours. DQ tasks may not have run yet.")
    else:
        latest_results = results_df.sort_values("RUN_TIMESTAMP", ascending=False).drop_duplicates("RULE_ID")

        col1, col2, col3, col4 = st.columns(4)
        total = len(latest_results)
        passed = len(latest_results[latest_results["STATUS"] == "PASSED"])
        failed = len(latest_results[latest_results["STATUS"] == "FAILED"])
        errored = len(latest_results[latest_results["STATUS"] == "ERROR"])

        col1.metric("Total Rules", total)
        col2.metric("Passed", passed, delta=None)
        col3.metric("Failed", failed, delta=None, delta_color="inverse")
        col4.metric("Errored", errored, delta=None, delta_color="inverse")

        pass_rate = (passed / total * 100) if total > 0 else 0
        st.progress(pass_rate / 100, text=f"Pass Rate: {pass_rate:.1f}%")

        st.subheader("Latest Results by Rule")
        display_df = latest_results.merge(
            rules_df[["RULE_ID", "RULE_TYPE", "SEVERITY", "TARGET_TABLE", "SOURCE"]],
            on="RULE_ID", how="left"
        )[["RULE_ID", "RULE_TYPE", "SEVERITY", "TARGET_TABLE", "STATUS", "ACTUAL_VALUE", "ROWS_FAILED", "SOURCE"]]
        st.dataframe(
            display_df.style.map(
                lambda v: "color: #10b981" if v == "PASSED" else ("color: #ef4444" if v == "FAILED" else "color: #f59e0b"),
                subset=["STATUS"]
            ),
            use_container_width=True,
            hide_index=True,
        )

        st.subheader("Failures by Severity")
        if failed > 0:
            failed_rules = latest_results[latest_results["STATUS"] == "FAILED"].merge(
                rules_df[["RULE_ID", "SEVERITY", "DESCRIPTION", "TARGET_TABLE"]],
                on="RULE_ID", how="left"
            )
            for _, row in failed_rules.iterrows():
                severity_color = "#ef4444" if row.get("SEVERITY") == "CRITICAL" else "#f59e0b"
                st.markdown(
                    f"**:{severity_color}[{row.get('SEVERITY', 'N/A')}]** | "
                    f"`{row['RULE_ID']}` on `{row.get('TARGET_TABLE', '')}` - "
                    f"{row.get('ACTUAL_VALUE', '')}"
                )
        else:
            st.success("All rules passed in the latest run.")


elif page == "Run History":
    st.title("Run History")

    runs_df = get_runs(hours=72)

    if runs_df.empty:
        st.info("No runs recorded in the last 72 hours.")
    else:
        st.dataframe(runs_df, use_container_width=True, hide_index=True)

        st.subheader("Pass/Fail Trend")
        results_df = get_results(hours=72)
        if not results_df.empty:
            results_df["RUN_TIMESTAMP"] = pd.to_datetime(results_df["RUN_TIMESTAMP"])
            trend = results_df.groupby([
                pd.Grouper(key="RUN_TIMESTAMP", freq="1h"), "STATUS"
            ]).size().unstack(fill_value=0).reset_index()
            trend = trend.set_index("RUN_TIMESTAMP")
            st.line_chart(trend)


elif page == "Rule Detail":
    st.title("Rule Detail")

    rules_df = get_rules()
    if rules_df.empty:
        st.info("No active rules found.")
    else:
        selected_rule = st.selectbox(
            "Select a rule",
            rules_df["RULE_ID"].tolist(),
            format_func=lambda x: f"{x} ({rules_df[rules_df['RULE_ID']==x]['RULE_TYPE'].values[0]})"
        )

        if selected_rule:
            rule_info = rules_df[rules_df["RULE_ID"] == selected_rule].iloc[0]

            col1, col2, col3 = st.columns(3)
            col1.markdown(f"**Type:** `{rule_info['RULE_TYPE']}`")
            col2.markdown(f"**Severity:** `{rule_info['SEVERITY']}`")
            col3.markdown(f"**Source:** `{rule_info['SOURCE']}`")

            st.markdown(f"**Target:** `{rule_info.get('TARGET_SCHEMA', '')}.{rule_info['TARGET_TABLE']}`")
            st.markdown(f"**Owner:** {rule_info.get('OWNER', 'N/A')}")

            st.subheader("Execution History")
            results_df = get_results(hours=168)
            rule_results = results_df[results_df["RULE_ID"] == selected_rule].sort_values("RUN_TIMESTAMP", ascending=False)

            if rule_results.empty:
                st.info("No execution history for this rule.")
            else:
                st.dataframe(
                    rule_results[["RUN_TIMESTAMP", "STATUS", "ACTUAL_VALUE", "EXPECTED_VALUE", "ROWS_FAILED", "EXECUTION_TIME_MS"]],
                    use_container_width=True,
                    hide_index=True,
                )

                avg_exec = rule_results["EXECUTION_TIME_MS"].mean()
                fail_rate = len(rule_results[rule_results["STATUS"] == "FAILED"]) / len(rule_results) * 100
                st.markdown(f"**Avg Execution Time:** {avg_exec:.0f}ms | **Failure Rate:** {fail_rate:.1f}%")


elif page == "Exploratory Stats":
    st.title("Exploratory Stats (DMF Results)")
    st.markdown("Statistics from Snowflake Data Metric Functions synced into the unified results table.")

    if DEMO_MODE:
        st.info("DMF stats are only available when connected to Snowflake with DMFs configured.")
        st.markdown("""
        **What you will see here when connected:**
        - NULL_COUNT per column
        - DUPLICATE_COUNT per column
        - UNIQUE_COUNT per column
        - FRESHNESS per table

        Configure DMF assignments via the `DQ_DMF_CONFIG` table or Streamlit Rule Management page.
        """)
    else:
        db = get_database()
        dmf_results = run_query(
            f"SELECT * FROM {db}.DQ.DQ_RESULTS "
            f"WHERE RULE_ID LIKE 'DMF_%' "
            f"AND RUN_TIMESTAMP >= DATEADD(HOUR, -24, CURRENT_TIMESTAMP()) "
            f"ORDER BY RUN_TIMESTAMP DESC"
        )
        if dmf_results.empty:
            st.info("No DMF results found. Run SP_DQ_SYNC_DMF_RESULTS or check DMF assignments.")
        else:
            st.dataframe(dmf_results, use_container_width=True, hide_index=True)


elif page == "Rule Management":
    st.title("Rule Management")

    rules_df = get_rules()

    tab1, tab2 = st.tabs(["View All Rules", "Create New Rule"])

    with tab1:
        if rules_df.empty:
            st.info("No rules configured yet.")
        else:
            filter_source = st.multiselect("Filter by source", ["engine", "user"], default=["engine", "user"])
            filter_severity = st.multiselect("Filter by severity", ["CRITICAL", "WARNING", "INFO"],
                                             default=["CRITICAL", "WARNING", "INFO"])
            filtered = rules_df[
                rules_df["SOURCE"].isin(filter_source) & rules_df["SEVERITY"].isin(filter_severity)
            ]
            st.dataframe(filtered, use_container_width=True, hide_index=True)
            st.caption(f"Showing {len(filtered)} of {len(rules_df)} active rules. Engine rules are read-only.")

    with tab2:
        if DEMO_MODE:
            st.info("Rule creation requires a live Snowflake connection.")
        else:
            st.markdown("Create a **user-managed** rule. Engine-managed rules must be defined in `dq_rules/` YAML files.")
            with st.form("create_rule"):
                rule_name = st.text_input("Rule Name (unique identifier)")
                rule_type = st.selectbox("Rule Type", [
                    "NOT_NULL", "UNIQUE", "ACCEPTED_VALUES", "ROW_COUNT_RANGE",
                    "REFERENTIAL_INTEGRITY", "RECENCY", "CUSTOM_SQL", "EXPRESSION"
                ])
                target_table = st.text_input("Target Table (FQN: DATABASE.SCHEMA.TABLE)")
                target_column = st.text_input("Target Column (leave empty for table-level rules)")
                severity = st.selectbox("Severity", ["CRITICAL", "WARNING", "INFO"])
                description = st.text_area("Description")
                parameters_json = st.text_area("Parameters (JSON)", value="{}")

                submitted = st.form_submit_button("Create Rule")
                if submitted and rule_name and target_table:
                    parts = target_table.split(".")
                    if len(parts) != 3:
                        st.error("Target table must be in DATABASE.SCHEMA.TABLE format")
                    else:
                        db = get_database()
                        insert_sql = f"""
                        INSERT INTO {db}.DQ.DQ_RULES
                            (RULE_ID, RULE_NAME, RULE_TYPE, SOURCE, TARGET_DATABASE,
                             TARGET_SCHEMA, TARGET_TABLE, TARGET_COLUMN, SEVERITY,
                             PARAMETERS, DESCRIPTION, IS_ACTIVE)
                        VALUES (
                            '{rule_name}', '{rule_name}', '{rule_type}', 'user',
                            '{parts[0]}', '{parts[1]}', '{parts[2]}',
                            {'NULL' if not target_column else f"'{target_column}'"},
                            '{severity}',
                            PARSE_JSON('{parameters_json}'),
                            '{description}', TRUE
                        )
                        """
                        try:
                            run_query(insert_sql)
                            st.success(f"Rule '{rule_name}' created successfully.")
                            st.cache_data.clear()
                        except Exception as e:
                            st.error(f"Failed to create rule: {e}")
