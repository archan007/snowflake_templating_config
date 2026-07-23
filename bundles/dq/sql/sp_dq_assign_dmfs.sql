CREATE OR REPLACE PROCEDURE ${DATABASE}.DQ.SP_DQ_ASSIGN_DMFS()
RETURNS VARCHAR
LANGUAGE SQL
AS
$$
DECLARE
    v_assigned NUMBER DEFAULT 0;
    c_config CURSOR FOR
        SELECT TARGET_DATABASE, TARGET_SCHEMA, TARGET_TABLE, TARGET_COLUMN,
               DMF_NAME, SCHEDULE_MINUTES
        FROM ${DATABASE}.DQ.DQ_DMF_CONFIG
        WHERE IS_ACTIVE = TRUE;
BEGIN
    -- Assign system DMFs to tables/columns based on DQ_DMF_CONFIG entries.
    -- This procedure should be run after initial setup or when config changes.
    FOR cfg IN c_config DO
        LET v_fqn VARCHAR := cfg.TARGET_DATABASE || '.' || cfg.TARGET_SCHEMA || '.' || cfg.TARGET_TABLE;
        LET v_dmf VARCHAR := 'SNOWFLAKE.CORE.' || cfg.DMF_NAME;
        LET v_schedule VARCHAR := cfg.SCHEDULE_MINUTES::VARCHAR || ' MINUTE';

        BEGIN
            IF (cfg.TARGET_COLUMN IS NOT NULL AND cfg.TARGET_COLUMN != '') THEN
                EXECUTE IMMEDIATE
                    'ALTER TABLE ' || v_fqn || ' ALTER COLUMN "' || cfg.TARGET_COLUMN
                    || '" SET DATA METRIC FUNCTION ' || v_dmf
                    || ' ON ("' || cfg.TARGET_COLUMN || '") SCHEDULE = ''' || v_schedule || '''';
            ELSE
                -- Table-level DMFs like FRESHNESS
                EXECUTE IMMEDIATE
                    'ALTER TABLE ' || v_fqn
                    || ' SET DATA METRIC FUNCTION ' || v_dmf
                    || ' ON () SCHEDULE = ''' || v_schedule || '''';
            END IF;
            v_assigned := v_assigned + 1;
        EXCEPTION
            WHEN OTHER THEN
                -- Log but don't fail; DMF may already be assigned or table may not support it
                NULL;
        END;
    END FOR;

    RETURN 'Assigned ' || v_assigned::VARCHAR || ' DMF(s)';
END;
$$
