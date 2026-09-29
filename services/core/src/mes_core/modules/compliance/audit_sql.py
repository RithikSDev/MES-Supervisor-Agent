"""DDL for the immutable audit log. Applied by migrations (as the owner role), never at runtime.

Design:
- `audit.log_change()` is an AFTER row trigger on every business table. It captures before/after as JSONB and
  the actor/request/reason/signature from transaction-local settings. If no actor is set, it raises, so a write
  that bypasses the unit of work fails instead of going unaudited.
- The runtime role (mes_app) can only SELECT from audit tables. The trigger function is SECURITY DEFINER.
- A guard trigger rejects UPDATE/DELETE, except the one-time sealing update that sets seal_seq/chain_hash.
- `audit.seal()` chains rows in seal order: hash_n = sha256(hash_{n-1} || canonical(row)). Sealing order (not id
  order) avoids gaps from transactions that commit out of id order. `audit.verify_chain()` recomputes it.
- Monthly range partitions, created ahead of time by `audit.ensure_partitions()`.
"""

APP_ROLE = "mes_app"

BUSINESS_SCHEMAS = ["org", "identity", "masterdata", "planning", "execution", "inventory", "quality", "maintenance", "ticketing", "compliance"]

CREATE_AUDIT = r"""
CREATE SCHEMA IF NOT EXISTS audit;

CREATE TABLE audit.audit_log (
    id              bigint GENERATED ALWAYS AS IDENTITY,
    occurred_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    tx_id           bigint NOT NULL DEFAULT (pg_current_xact_id()::text)::bigint,
    plant_id        uuid,
    actor_id        text NOT NULL,
    actor_name      text,
    actor_type      text NOT NULL,
    request_id      text,
    schema_name     text NOT NULL,
    table_name      text NOT NULL,
    record_id       text,
    action          char(1) NOT NULL CHECK (action IN ('I','U','D')),
    changed_fields  text[],
    before          jsonb,
    after           jsonb,
    reason          text,
    signature_id    uuid,
    seal_seq        bigint,
    chain_hash      text,
    PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);

CREATE TABLE audit.audit_log_default PARTITION OF audit.audit_log DEFAULT;
CREATE INDEX ix_audit_record ON audit.audit_log (table_name, record_id);
CREATE INDEX ix_audit_occurred ON audit.audit_log (occurred_at DESC);
CREATE INDEX ix_audit_actor ON audit.audit_log (actor_id);
CREATE INDEX ix_audit_tx ON audit.audit_log (tx_id);
CREATE INDEX ix_audit_unsealed ON audit.audit_log (id) WHERE seal_seq IS NULL;
CREATE INDEX ix_audit_seal_seq ON audit.audit_log (seal_seq) WHERE seal_seq IS NOT NULL;

CREATE TABLE audit.seal_state (
    id          int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_seq    bigint NOT NULL DEFAULT 0,
    last_hash   text NOT NULL DEFAULT repeat('0', 64),
    sealed_at   timestamptz
);
INSERT INTO audit.seal_state DEFAULT VALUES;

CREATE FUNCTION audit.ensure_partitions(months_ahead int DEFAULT 2) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog SET TimeZone = 'UTC' AS $$
DECLARE
    m date;
    part text;
BEGIN
    FOR i IN 0..months_ahead LOOP
        m := (date_trunc('month', now()) + make_interval(months => i))::date;
        part := 'audit_log_' || to_char(m, 'YYYYMM');
        IF to_regclass('audit.' || part) IS NULL THEN
            EXECUTE format('CREATE TABLE audit.%I PARTITION OF audit.audit_log FOR VALUES FROM (%L) TO (%L)',
                           part, m, (m + interval '1 month')::date);
        END IF;
    END LOOP;
END $$;

SELECT audit.ensure_partitions(2);

CREATE FUNCTION audit.log_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    v_actor   text := nullif(current_setting('mes.actor_id', true), '');
    v_before  jsonb;
    v_after   jsonb;
    v_changed text[];
    v_row     jsonb;
    v_plant   uuid;
BEGIN
    IF v_actor IS NULL THEN
        RAISE EXCEPTION 'audit: write to %.% without actor context (mes.actor_id not set)',
            TG_TABLE_SCHEMA, TG_TABLE_NAME
            USING ERRCODE = 'insufficient_privilege', HINT = 'Writes must go through Database.unit_of_work()';
    END IF;
    IF TG_OP <> 'INSERT' THEN v_before := to_jsonb(OLD); END IF;
    IF TG_OP <> 'DELETE' THEN v_after := to_jsonb(NEW); END IF;
    IF TG_OP = 'UPDATE' THEN
        SELECT array_agg(k ORDER BY k) INTO v_changed
        FROM jsonb_object_keys(v_after) AS k
        WHERE k NOT IN ('updated_at', 'updated_by', 'row_version')
          AND (v_after -> k) IS DISTINCT FROM (v_before -> k);
        IF v_changed IS NULL THEN
            RETURN NULL;  -- bookkeeping-only update, nothing worth auditing
        END IF;
    END IF;
    v_row := coalesce(v_after, v_before);
    IF TG_TABLE_SCHEMA = 'org' AND TG_TABLE_NAME = 'plant' THEN
        v_plant := (v_row ->> 'id')::uuid;
    ELSIF v_row ? 'plant_id' THEN
        v_plant := nullif(v_row ->> 'plant_id', '')::uuid;
    END IF;
    INSERT INTO audit.audit_log (plant_id, actor_id, actor_name, actor_type, request_id, schema_name, table_name,
                                 record_id, action, changed_fields, before, after, reason, signature_id)
    VALUES (v_plant, v_actor,
            nullif(current_setting('mes.actor_name', true), ''),
            coalesce(nullif(current_setting('mes.actor_type', true), ''), 'USER'),
            nullif(current_setting('mes.request_id', true), ''),
            TG_TABLE_SCHEMA, TG_TABLE_NAME, v_row ->> 'id', left(TG_OP, 1),
            v_changed, v_before, v_after,
            nullif(current_setting('mes.reason', true), ''),
            nullif(current_setting('mes.signature_id', true), '')::uuid);
    RETURN NULL;
END $$;

CREATE FUNCTION audit.guard_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'audit log is append-only' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.seal_seq IS NOT NULL OR NEW.seal_seq IS NULL
       OR (to_jsonb(NEW) - 'seal_seq' - 'chain_hash') IS DISTINCT FROM (to_jsonb(OLD) - 'seal_seq' - 'chain_hash') THEN
        RAISE EXCEPTION 'audit log rows are immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER audit_log_guard BEFORE UPDATE OR DELETE ON audit.audit_log
    FOR EACH ROW EXECUTE FUNCTION audit.guard_immutable();

CREATE FUNCTION audit.digest_input(r audit.audit_log) RETURNS text
LANGUAGE sql STABLE SET TimeZone = 'UTC' AS $$
    SELECT jsonb_build_object(
        'id', r.id, 'occurred_at', r.occurred_at, 'tx_id', r.tx_id, 'plant_id', r.plant_id,
        'actor_id', r.actor_id, 'actor_type', r.actor_type, 'request_id', r.request_id,
        'table', r.schema_name || '.' || r.table_name, 'record_id', r.record_id, 'action', r.action,
        'changed_fields', r.changed_fields, 'before', r.before, 'after', r.after,
        'reason', r.reason, 'signature_id', r.signature_id)::text
$$;

CREATE FUNCTION audit.seal(batch int DEFAULT 1000) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public SET TimeZone = 'UTC' AS $$
DECLARE
    st   audit.seal_state%ROWTYPE;
    r    audit.audit_log%ROWTYPE;
    h    text;
    seq  bigint;
    n    int := 0;
BEGIN
    SELECT * INTO st FROM audit.seal_state WHERE id = 1 FOR UPDATE;  -- one sealer at a time
    h := st.last_hash;
    seq := st.last_seq;
    FOR r IN SELECT * FROM audit.audit_log WHERE seal_seq IS NULL ORDER BY id LIMIT batch LOOP
        seq := seq + 1;
        h := encode(sha256(convert_to(h || audit.digest_input(r), 'UTF8')), 'hex');
        UPDATE audit.audit_log SET seal_seq = seq, chain_hash = h WHERE id = r.id AND occurred_at = r.occurred_at;
        n := n + 1;
    END LOOP;
    IF n > 0 THEN
        UPDATE audit.seal_state SET last_seq = seq, last_hash = h, sealed_at = now() WHERE id = 1;
    END IF;
    RETURN n;
END $$;

CREATE FUNCTION audit.verify_chain()
RETURNS TABLE (checked bigint, expected_last_seq bigint, first_broken_seq bigint, ok boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public SET TimeZone = 'UTC' AS $$
DECLARE
    r   audit.audit_log%ROWTYPE;
    h   text := repeat('0', 64);
    n   bigint := 0;
    st  audit.seal_state%ROWTYPE;
BEGIN
    SELECT * INTO st FROM audit.seal_state WHERE id = 1;
    FOR r IN SELECT * FROM audit.audit_log WHERE seal_seq IS NOT NULL ORDER BY seal_seq LOOP
        n := n + 1;
        h := encode(sha256(convert_to(h || audit.digest_input(r), 'UTF8')), 'hex');
        IF r.seal_seq <> n OR r.chain_hash <> h THEN
            checked := n - 1; expected_last_seq := st.last_seq; first_broken_seq := n; ok := false;
            RETURN NEXT;
            RETURN;
        END IF;
    END LOOP;
    checked := n;
    expected_last_seq := st.last_seq;
    ok := (n = st.last_seq AND h = st.last_hash);
    first_broken_seq := CASE WHEN ok THEN NULL ELSE n + 1 END;
    RETURN NEXT;
END $$;

REVOKE ALL ON SCHEMA audit FROM PUBLIC;
GRANT USAGE ON SCHEMA audit TO {app_role};
GRANT SELECT ON audit.audit_log, audit.seal_state TO {app_role};
REVOKE ALL ON FUNCTION audit.seal(int), audit.verify_chain(), audit.ensure_partitions(int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION audit.seal(int), audit.verify_chain(), audit.ensure_partitions(int) TO {app_role};
""".replace("{app_role}", APP_ROLE)


def split_statements(sql: str) -> list[str]:
    """Split a script into single statements (asyncpg prepares each one), respecting $$-quoted bodies."""
    out, buf, in_dollar = [], [], False
    for line in sql.splitlines():
        buf.append(line)
        in_dollar ^= line.count("$$") % 2 == 1
        if not in_dollar and line.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def attach_audit_trigger(qualified_table: str, redact: list[str] | None = None) -> str:
    """`redact`: columns whose values are replaced by *** in before/after (secrets such as PIN hashes)."""
    args = ", ".join(f"'{c}'" for c in (redact or []))
    return (
        f"CREATE TRIGGER audit_row AFTER INSERT OR UPDATE OR DELETE ON {qualified_table} "
        f"FOR EACH ROW EXECUTE FUNCTION audit.log_change({args});"
    )


def grant_schema(schema: str) -> str:
    return f"""
GRANT USAGE ON SCHEMA {schema} TO {APP_ROLE};
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {APP_ROLE};
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {APP_ROLE};
ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE};
ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE};
"""


# Migration 0002: same function, plus redaction of columns named in the trigger arguments. The change is still
# recorded (changed_fields lists the column), only the value is masked.
LOG_CHANGE_WITH_REDACTION = r"""
CREATE OR REPLACE FUNCTION audit.log_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    v_actor   text := nullif(current_setting('mes.actor_id', true), '');
    v_before  jsonb;
    v_after   jsonb;
    v_changed text[];
    v_row     jsonb;
    v_plant   uuid;
    v_col     text;
BEGIN
    IF v_actor IS NULL THEN
        RAISE EXCEPTION 'audit: write to %.% without actor context (mes.actor_id not set)',
            TG_TABLE_SCHEMA, TG_TABLE_NAME
            USING ERRCODE = 'insufficient_privilege', HINT = 'Writes must go through Database.unit_of_work()';
    END IF;
    IF TG_OP <> 'INSERT' THEN v_before := to_jsonb(OLD); END IF;
    IF TG_OP <> 'DELETE' THEN v_after := to_jsonb(NEW); END IF;
    IF TG_OP = 'UPDATE' THEN
        SELECT array_agg(k ORDER BY k) INTO v_changed
        FROM jsonb_object_keys(v_after) AS k
        WHERE k NOT IN ('updated_at', 'updated_by', 'row_version')
          AND (v_after -> k) IS DISTINCT FROM (v_before -> k);
        IF v_changed IS NULL THEN
            RETURN NULL;
        END IF;
    END IF;
    v_row := coalesce(v_after, v_before);
    IF TG_TABLE_SCHEMA = 'org' AND TG_TABLE_NAME = 'plant' THEN
        v_plant := (v_row ->> 'id')::uuid;
    ELSIF v_row ? 'plant_id' THEN
        v_plant := nullif(v_row ->> 'plant_id', '')::uuid;
    END IF;
    IF TG_NARGS > 0 THEN
        FOREACH v_col IN ARRAY TG_ARGV LOOP
            IF v_before ? v_col THEN v_before := jsonb_set(v_before, ARRAY[v_col], '"***"'); END IF;
            IF v_after ? v_col THEN v_after := jsonb_set(v_after, ARRAY[v_col], '"***"'); END IF;
        END LOOP;
    END IF;
    INSERT INTO audit.audit_log (plant_id, actor_id, actor_name, actor_type, request_id, schema_name, table_name,
                                 record_id, action, changed_fields, before, after, reason, signature_id)
    VALUES (v_plant, v_actor,
            nullif(current_setting('mes.actor_name', true), ''),
            coalesce(nullif(current_setting('mes.actor_type', true), ''), 'USER'),
            nullif(current_setting('mes.request_id', true), ''),
            TG_TABLE_SCHEMA, TG_TABLE_NAME, v_row ->> 'id', left(TG_OP, 1),
            v_changed, v_before, v_after,
            nullif(current_setting('mes.reason', true), ''),
            nullif(current_setting('mes.signature_id', true), '')::uuid);
    RETURN NULL;
END $$;
"""
