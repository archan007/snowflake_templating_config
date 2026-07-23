CREATE OR REPLACE PROCEDURE ${DATABASE}.DQ.SP_DQ_EXECUTE_RULE(P_RULE_ID VARCHAR, P_RUN_ID VARCHAR, P_ENVIRONMENT VARCHAR)
RETURNS VARIANT
LANGUAGE SQL
AS
$$
DECLARE
    v_rule_type VARCHAR;
    v_target_db VARCHAR;
    v_target_schema VARCHAR;
    v_target_table VARCHAR;
    v_target_column VARCHAR;
    v_params VARIANT;
    v_fqn VARCHAR;
    v_start TIMESTAMP_NTZ;
    v_status VARCHAR DEFAULT 'PASSED';
    v_actual VARCHAR;
    v_expected VARCHAR;
    v_rows_failed NUMBER DEFAULT 0;
    v_sql VARCHAR;
    v_result_count NUMBER;
BEGIN
    v_start := CURRENT_TIMESTAMP();

    SELECT RULE_TYPE, TARGET_DATABASE, TARGET_SCHEMA, TARGET_TABLE, TARGET_COLUMN, PARAMETERS
    INTO :v_rule_type, :v_target_db, :v_target_schema, :v_target_table, :v_target_column, :v_params
    FROM ${DATABASE}.DQ.DQ_RULES
    WHERE RULE_ID = :P_RULE_ID AND IS_ACTIVE = TRUE;

    v_fqn := v_target_db || '.' || v_target_schema || '.' || v_target_table;

    -- Route to the appropriate check based on rule_type
    CASE v_rule_type
        WHEN 'NOT_NULL' THEN
            v_sql := 'SELECT COUNT(*) FROM ' || v_fqn || ' WHERE "' || v_target_column || '" IS NULL';
            EXECUTE IMMEDIATE 'SELECT (' || v_sql || ')' INTO :v_rows_failed;
            v_actual := v_rows_failed::VARCHAR;
            v_expected := '0';
            IF (v_rows_failed > 0) THEN v_status := 'FAILED'; END IF;

        WHEN 'UNIQUE' THEN
            v_sql := 'SELECT COUNT(*) - COUNT(DISTINCT "' || v_target_column || '") FROM ' || v_fqn
                     || ' WHERE "' || v_target_column || '" IS NOT NULL';
            EXECUTE IMMEDIATE 'SELECT (' || v_sql || ')' INTO :v_rows_failed;
            v_actual := v_rows_failed::VARCHAR;
            v_expected := '0';
            IF (v_rows_failed > 0) THEN v_status := 'FAILED'; END IF;

        WHEN 'ACCEPTED_VALUES' THEN
            LET v_values_array ARRAY := v_params:values::ARRAY;
            LET v_values_list VARCHAR := '';
            FOR i IN 0 TO ARRAY_SIZE(v_values_array) - 1 DO
                IF (i > 0) THEN v_values_list := v_values_list || ','; END IF;
                v_values_list := v_values_list || '''' || v_values_array[i]::VARCHAR || '''';
            END FOR;
            v_sql := 'SELECT COUNT(*) FROM ' || v_fqn
                     || ' WHERE "' || v_target_column || '" IS NOT NULL AND "' || v_target_column
                     || '" NOT IN (' || v_values_list || ')';
            EXECUTE IMMEDIATE 'SELECT (' || v_sql || ')' INTO :v_rows_failed;
            v_actual := v_rows_failed::VARCHAR || ' rows with invalid values';
            v_expected := '0 rows with invalid values';
            IF (v_rows_failed > 0) THEN v_status := 'FAILED'; END IF;

        WHEN 'ROW_COUNT_RANGE' THEN
            LET v_min NUMBER := COALESCE(v_params:min::NUMBER, 0);
            LET v_max NUMBER := COALESCE(v_params:max::NUMBER, 999999999999);
            EXECUTE IMMEDIATE 'SELECT COUNT(*) FROM ' || v_fqn INTO :v_result_count;
            v_actual := v_result_count::VARCHAR;
            v_expected := v_min::VARCHAR || ' to ' || v_max::VARCHAR;
            IF (v_result_count < v_min OR v_result_count > v_max) THEN
                v_status := 'FAILED';
                v_rows_failed := v_result_count;
            END IF;

        WHEN 'REFERENTIAL_INTEGRITY' THEN
            LET v_ref_table VARCHAR := v_params:reference_table::VARCHAR;
            LET v_ref_column VARCHAR := v_params:reference_column::VARCHAR;
            v_sql := 'SELECT COUNT(*) FROM ' || v_fqn || ' s LEFT JOIN ' || v_ref_table
                     || ' r ON s."' || v_target_column || '" = r."' || v_ref_column
                     || '" WHERE r."' || v_ref_column || '" IS NULL AND s."'
                     || v_target_column || '" IS NOT NULL';
            EXECUTE IMMEDIATE 'SELECT (' || v_sql || ')' INTO :v_rows_failed;
            v_actual := v_rows_failed::VARCHAR || ' orphaned rows';
            v_expected := '0 orphaned rows';
            IF (v_rows_failed > 0) THEN v_status := 'FAILED'; END IF;

        WHEN 'RECENCY' THEN
            LET v_max_hours NUMBER := COALESCE(v_params:max_hours::NUMBER, 24);
            LET v_hours_since NUMBER;
            v_sql := 'SELECT COALESCE(DATEDIFF(HOUR, MAX("' || v_target_column
                     || '"), CURRENT_TIMESTAMP()), 999999) FROM ' || v_fqn;
            EXECUTE IMMEDIATE 'SELECT (' || v_sql || ')' INTO :v_hours_since;
            v_actual := v_hours_since::VARCHAR || ' hours since last record';
            v_expected := 'Within ' || v_max_hours::VARCHAR || ' hours';
            IF (v_hours_since > v_max_hours) THEN
                v_status := 'FAILED';
                v_rows_failed := 1;
            END IF;

        WHEN 'CUSTOM_SQL' THEN
            LET v_custom_sql VARCHAR := v_params:sql::VARCHAR;
            LET v_expect_value VARCHAR := COALESCE(v_params:expected::VARCHAR, '0');
            LET v_custom_result VARCHAR;
            EXECUTE IMMEDIATE 'SELECT (' || v_custom_sql || ')' INTO :v_custom_result;
            v_actual := v_custom_result;
            v_expected := v_expect_value;
            IF (v_custom_result != v_expect_value) THEN
                v_status := 'FAILED';
                v_rows_failed := 1;
            END IF;

        WHEN 'EXPRESSION' THEN
            LET v_expr VARCHAR := v_params:expression::VARCHAR;
            v_sql := 'SELECT COUNT(*) FROM ' || v_fqn || ' WHERE NOT (' || v_expr || ')';
            EXECUTE IMMEDIATE 'SELECT (' || v_sql || ')' INTO :v_rows_failed;
            v_actual := v_rows_failed::VARCHAR || ' rows violating expression';
            v_expected := '0 rows violating expression';
            IF (v_rows_failed > 0) THEN v_status := 'FAILED'; END IF;

        ELSE
            v_status := 'ERROR';
            v_actual := 'Unknown rule_type: ' || v_rule_type;
    END CASE;

    LET v_exec_ms NUMBER := DATEDIFF(MILLISECOND, v_start, CURRENT_TIMESTAMP());

    INSERT INTO ${DATABASE}.DQ.DQ_RESULTS
        (RESULT_ID, RULE_ID, RUN_ID, RUN_TIMESTAMP, STATUS, ACTUAL_VALUE, EXPECTED_VALUE,
         ROWS_FAILED, EXECUTION_TIME_MS, ENVIRONMENT)
    VALUES (UUID_STRING(), :P_RULE_ID, :P_RUN_ID, CURRENT_TIMESTAMP(), :v_status,
            :v_actual, :v_expected, :v_rows_failed, :v_exec_ms, :P_ENVIRONMENT);

    RETURN OBJECT_CONSTRUCT('status', v_status, 'actual', v_actual, 'expected', v_expected,
                            'rows_failed', v_rows_failed, 'execution_time_ms', v_exec_ms);
END;
$$
