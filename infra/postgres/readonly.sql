-- Read-only access to the MES database for AI support (the data-mcp service). Run as superuser after the core
-- migrations, on every start (idempotent): `psql -v ro_password=... -f readonly.sql mes_core`.
--   mes_readonly  LOGIN, read-only transactions, 5 s statement timeout, SELECT on business schemas only.
--   Identity data only through support_ro views (no PIN hashes, device tokens or e-mail addresses);
--   no e-signatures, no audit seal state.

SELECT format('CREATE ROLE mes_readonly LOGIN PASSWORD %L', :'ro_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mes_readonly') \gexec
ALTER ROLE mes_readonly PASSWORD :'ro_password';
ALTER ROLE mes_readonly SET default_transaction_read_only = on;
ALTER ROLE mes_readonly SET statement_timeout = '5s';
ALTER ROLE mes_readonly SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE mes_readonly CONNECTION LIMIT 10;
GRANT CONNECT ON DATABASE mes_core TO mes_readonly;
REVOKE CREATE ON SCHEMA public FROM mes_readonly;

DO $$
DECLARE s text;
BEGIN
  FOREACH s IN ARRAY ARRAY['org', 'masterdata', 'planning', 'execution', 'inventory', 'quality', 'maintenance',
                           'ticketing', 'messaging'] LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO mes_readonly', s);
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO mes_readonly', s);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE mes_owner IN SCHEMA %I GRANT SELECT ON TABLES TO mes_readonly', s);
  END LOOP;
END $$;

CREATE SCHEMA IF NOT EXISTS support_ro;
COMMENT ON SCHEMA support_ro IS 'Safe views for AI support: identity and audit data without secrets';

CREATE OR REPLACE VIEW support_ro.app_user AS
  SELECT id, username, display_name, active, last_login_at, created_at FROM identity.app_user;
CREATE OR REPLACE VIEW support_ro.terminal_session AS
  SELECT id, terminal_id, user_id, plant_id, roles, started_at, expires_at, ended_at, end_reason
  FROM identity.terminal_session;
CREATE OR REPLACE VIEW support_ro.operator_pin AS
  SELECT id, user_id, plant_id, pin_set_at, terminal_roles FROM identity.operator_credential;
CREATE OR REPLACE VIEW support_ro.user_scope AS SELECT id, user_id, plant_id, line_id FROM identity.user_scope;
CREATE OR REPLACE VIEW support_ro.audit_log AS
  SELECT id, occurred_at, plant_id, actor_name, actor_type, request_id, schema_name, table_name, record_id, action,
         changed_fields, before, after, reason
  FROM audit.audit_log;

GRANT USAGE ON SCHEMA support_ro TO mes_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA support_ro TO mes_readonly;
