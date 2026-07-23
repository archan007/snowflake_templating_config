CREATE OR REPLACE PROCEDURE ${DATABASE}.DQ.SP_DQ_EXECUTE_ALL(P_ENVIRONMENT VARCHAR, P_TRIGGERED_BY VARCHAR DEFAULT 'SCHEDULED')
RETURNS VARIANT
LANGUAGE SQL
AS
$$
DECLARE
    v_run_id VARCHAR;
    v_start TIMESTAMP_NTZ;
    v_total NUMBER DEFAULT 0;
    v_passed NUMBER DEFAULT 0;
    v_failed NUMBER DEFAULT 0;
    v_errored NUMBER DEFAULT 0;
    c_rules CURSOR FOR
        SELECT RULE_ID, RULE_NAME, RULE_TYPE, TARGET_DATABASE, TARGET_SCHEMA,
               TARGET_TABLE, TARGET_COLUMN, PARAMETERS
        FROM ${DATABASE}.DQ.DQ_RULES
        WHERE IS_ACTIVE = TRUE
        ORDER BY SEVERITY DESC, RULE_NAME;
BEGIN
    v_run_id := UUID_STRING();
    v_start := CURRENT_TIMESTAMP();

    INSERT INTO ${DATABASE}.DQ.DQ_RUN_LOG (RUN_ID, RUN_TYPE, START_TIME, ENVIRONMENT, TRIGGERED_BY)
    VALUES (:v_run_id, :P_TRIGGERED_BY, :v_start, :P_ENVIRONMENT, :P_TRIGGERED_BY);

    FOR rule IN c_rules DO
        v_total := v_total + 1;
        BEGIN
            LET v_result VARIANT;
            CALL ${DATABASE}.DQ.SP_DQ_EXECUTE_RULE(:rule.RULE_ID, :v_run_id, :P_ENVIRONMENT)
                INTO :v_result;
            IF (v_result:status::VARCHAR = 'PASSED') THEN
                v_passed := v_passed + 1;
            ELSEIF (v_result:status::VARCHAR = 'FAILED') THEN
                v_failed := v_failed + 1;
            ELSE
                v_errored := v_errored + 1;
            END IF;
        EXCEPTION
            WHEN OTHER THEN
                v_errored := v_errored + 1;
                INSERT INTO ${DATABASE}.DQ.DQ_RESULTS
                    (RESULT_ID, RULE_ID, RUN_ID, RUN_TIMESTAMP, STATUS, ACTUAL_VALUE, EXECUTION_TIME_MS, ENVIRONMENT)
                VALUES (UUID_STRING(), rule.RULE_ID, :v_run_id, CURRENT_TIMESTAMP(), 'ERROR',
                        SUBSTR(SQLERRM, 1, 4096), 0, :P_ENVIRONMENT);
        END;
    END FOR;

    UPDATE ${DATABASE}.DQ.DQ_RUN_LOG
    SET END_TIME = CURRENT_TIMESTAMP(),
        TOTAL_RULES = :v_total,
        PASSED = :v_passed,
        FAILED = :v_failed,
        ERRORED = :v_errored
    WHERE RUN_ID = :v_run_id;

    RETURN OBJECT_CONSTRUCT(
        'run_id', v_run_id,
        'total', v_total,
        'passed', v_passed,
        'failed', v_failed,
        'errored', v_errored
    );
END;
$$
