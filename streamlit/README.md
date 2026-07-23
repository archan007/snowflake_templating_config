# Streamlit DQ Dashboard

Data Quality dashboard for the Snowflake project.

## Setup

```bash
pip install streamlit pandas snowflake-connector-python
```

## Configuration

Set the following environment variables (or use Streamlit secrets in `.streamlit/secrets.toml`):

```
SNOWFLAKE_ACCOUNT=<your_account>
SNOWFLAKE_USER=<your_user>
SNOWFLAKE_PASSWORD=<your_password>
SNOWFLAKE_DATABASE=<DEV_FS_DB|UAT_FS_DB|PROD_FS_DB>
SNOWFLAKE_WAREHOUSE=<WH_DEV_DQ|WH_UAT_DQ|WH_PROD_DQ>
SNOWFLAKE_ROLE=<your_role>
```

## Running

```bash
streamlit run streamlit/app.py
```

The app runs in **demo mode** with sample data when Snowflake credentials are not configured.

## Pages

- **Overview**: High-level pass/fail metrics and latest rule results
- **Run History**: Chronological list of DQ runs with trend charts
- **Rule Detail**: Drill into individual rule execution history
- **Exploratory Stats**: DMF-sourced profiling statistics
- **Rule Management**: View all rules, create user-managed rules

## Streamlit in Snowflake (SiS)

This app is designed to be deployable as a Streamlit in Snowflake app. When running inside SiS,
replace `snowflake.connector` calls with `snowflake.snowpark` session from the active connection context.
