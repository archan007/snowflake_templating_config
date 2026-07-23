CREATE OR REPLACE PROCEDURE ${DATABASE}.DQ.SP_DQ_SYNC_DMF_RESULTS(P_ENVIRONMENT VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
AS
$$
DECLARE
    v_synced NUMBER DEFAULT 0;
BEGIN
    -- Sync DMF results from Snowflake's internal monitoring into our unified DQ_RESULTS table.
    -- This creates synthetic results that appear alongside finite rule results in the dashboard.
    -- Note: DATA_QUALITY_MONITORING_RESULTS is available in INFORMATION_SCHEMA for each database.

    MERGE INTO ${DATABASE}.DQ.DQ_RESULTS tgt
    USING (
        SELECT
            UUID_STRING() AS RESULT_ID,
            'DMF_' || dmr.METRIC_NAME || '_' || dmr.TABLE_NAME || '_' || COALESCE(dmr.COLUMN_NAME, 'TABLE') AS RULE_ID,
            'DMF_SYNC_' || TO_CHAR(CURRENT_TIMESTAMP(), 'YYYYMMDDHH24MISS') AS RUN_ID,
            dmr.MEASUREMENT_TIME AS RUN_TIMESTAMP,
            CASE
                WHEN dmr.METRIC_NAME = 'NULL_COUNT' AND dmr.VALUE > 0 THEN 'FAILED'
                WHEN dmr.METRIC_NAME = 'DUPLICATE_COUNT' AND dmr.VALUE > 0 THEN 'FAILED'
                ELSE 'PASSED'
            END AS STATUS,
            dmr.VALUE::VARCHAR AS ACTUAL_VALUE,
            '0' AS EXPECTED_VALUE,
            dmr.VALUE AS ROWS_FAILED,
            0 AS EXECUTION_TIME_MS,
            :P_ENVIRONMENT AS ENVIRONMENT
        FROM TABLE(INFORMATION_SCHEMA.DATA_QUALITY_MONITORING_RESULTS(
            START_TIME => DATEADD(HOUR, -24, CURRENT_TIMESTAMP())
        )) dmr
        WHERE dmr.TABLE_SCHEMA = 'DQ'
           OR dmr.TABLE_SCHEMA IN (
               SELECT DISTINCT TARGET_SCHEMA
               FROM ${DATABASE}.DQ.DQ_DMF_CONFIG
               WHERE IS_ACTIVE = TRUE
           )
    ) src
    ON tgt.RESULT_ID = src.RESULT_ID
    WHEN NOT MATCHED THEN
        INSERT (RESULT_ID, RULE_ID, RUN_ID, RUN_TIMESTAMP, STATUS, ACTUAL_VALUE,
                EXPECTED_VALUE, ROWS_FAILED, EXECUTION_TIME_MS, ENVIRONMENT)
        VALUES (src.RESULT_ID, src.RULE_ID, src.RUN_ID, src.RUN_TIMESTAMP, src.STATUS,
                src.ACTUAL_VALUE, src.EXPECTED_VALUE, src.ROWS_FAILED,
                src.EXECUTION_TIME_MS, src.ENVIRONMENT);

    v_synced := SQLROWCOUNT;
    RETURN 'Synced ' || v_synced::VARCHAR || ' DMF results';
END;
$$
