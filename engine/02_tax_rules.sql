-- =====================================================================
--  simply handle IT ERP — Tax rule engine: database migration
--  Requires: 01_core.sql loaded, PostgreSQL 16, extension btree_gist
--  Implements tax_rule_requirements.md v0.2 sections 4 and 6
-- =====================================================================
CREATE EXTENSION IF NOT EXISTS btree_gist;
SET search_path TO erp, public;

-- ---------------------------------------------------------------------
-- 1. Countries, companies, users
-- ---------------------------------------------------------------------
CREATE TABLE jurisdiction (
    jurisdiction_code CHAR(2) PRIMARY KEY,
    name              TEXT NOT NULL,
    base_currency     CHAR(3) NOT NULL,
    default_rounding  TEXT NOT NULL
);

ALTER TABLE company
    ADD COLUMN company_code      TEXT UNIQUE,
    ADD COLUMN jurisdiction_code CHAR(2) REFERENCES jurisdiction;

CREATE TABLE erp_user (
    username   TEXT PRIMARY KEY,
    full_name  TEXT NOT NULL,
    roles      TEXT[] NOT NULL DEFAULT '{}',          -- tax_admin, finance_manager, md, payroll, auditor ...
    is_active  BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE TABLE erp_user_company (
    username   TEXT REFERENCES erp_user,
    company_id SMALLINT REFERENCES company,
    PRIMARY KEY (username, company_id)
);

-- company_id on transactional tables (multi-company, FR-33)
ALTER TABLE partner       ADD COLUMN company_id SMALLINT REFERENCES company;
ALTER TABLE employee      ADD COLUMN company_id SMALLINT REFERENCES company;
ALTER TABLE journal_entry ADD COLUMN company_id SMALLINT REFERENCES company;
ALTER TABLE invoice       ADD COLUMN company_id SMALLINT REFERENCES company;
ALTER TABLE payment       ADD COLUMN company_id SMALLINT REFERENCES company;
ALTER TABLE payroll_run   ADD COLUMN company_id SMALLINT REFERENCES company;

-- ---------------------------------------------------------------------
-- 2. Rules and versions
-- ---------------------------------------------------------------------
CREATE TABLE tax_import_batch (
    batch_id          SERIAL PRIMARY KEY,
    file_name         TEXT NOT NULL,
    file_format       TEXT NOT NULL CHECK (file_format IN ('json','xlsx')),
    file_sha256       TEXT NOT NULL,
    base_checksum     TEXT,
    jurisdiction_code CHAR(2) NOT NULL REFERENCES jurisdiction,
    company_id        SMALLINT REFERENCES company,
    change_summary    TEXT,
    imported_by       TEXT NOT NULL REFERENCES erp_user,
    imported_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    result            TEXT NOT NULL CHECK (result IN ('applied','rejected','bootstrap')),
    report            TEXT
);

CREATE TABLE tax_rule (
    rule_id           SERIAL PRIMARY KEY,
    jurisdiction_code CHAR(2) NOT NULL REFERENCES jurisdiction,
    rule_code         TEXT NOT NULL CHECK (rule_code ~ '^[A-Z][A-Z0-9_]{1,40}$'),
    name_en           TEXT NOT NULL,
    name_th           TEXT,
    category          TEXT NOT NULL CHECK (category IN ('VAT','WHT','PIT','SSO','OTHER')),
    rule_type         TEXT NOT NULL CHECK (rule_type IN ('flat_rate','rate_by_category','rate_with_cap',
                                                         'fixed_amount','bracket_table','threshold','formula')),
    is_mandatory      BOOLEAN NOT NULL DEFAULT FALSE,
    description       TEXT,
    sort_order        INT NOT NULL DEFAULT 0,
    UNIQUE (jurisdiction_code, rule_code)
);

CREATE TABLE tax_rule_version (
    rule_version_id  SERIAL PRIMARY KEY,
    rule_id          INT NOT NULL REFERENCES tax_rule,
    company_id       SMALLINT REFERENCES company,          -- NULL = country rule, set = company override
    version_no       INT NOT NULL CHECK (version_no >= 1),
    status           TEXT NOT NULL DEFAULT 'draft'
                     CHECK (status IN ('draft','submitted','approved','rejected','retired')),
    effective_from   DATE NOT NULL,
    effective_to     DATE,
    pending_effective_to DATE,     -- close requested by an import; applied only when the successor is approved
    params           JSONB NOT NULL,
    rounding         TEXT NOT NULL CHECK (rounding IN ('half_up:0.01','half_up:1','down:0.01','down:1','up:1','none')),
    legal_reference  TEXT,
    source_url       TEXT,
    notes            TEXT,
    created_by       TEXT NOT NULL REFERENCES erp_user,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    submitted_by     TEXT REFERENCES erp_user,
    submitted_at     TIMESTAMPTZ,
    approved_at      TIMESTAMPTZ,
    import_batch_id  INT REFERENCES tax_import_batch,
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    UNIQUE NULLS NOT DISTINCT (rule_id, company_id, version_no),
    -- FR-07: no two APPROVED versions of the same rule (per company stream) may cover the same day.
    -- Drafts may overlap an approved predecessor whose close is pending; the validator checks the planned dates.
    CONSTRAINT no_overlapping_versions EXCLUDE USING gist (
        rule_id WITH =,
        (COALESCE(company_id, 0)) WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    ) WHERE (status = 'approved') DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX ix_trv_lookup ON tax_rule_version (rule_id, company_id, effective_from) WHERE status = 'approved';

CREATE TABLE tax_rule_approval (
    rule_version_id INT NOT NULL REFERENCES tax_rule_version,
    approver        TEXT NOT NULL REFERENCES erp_user,
    decision        TEXT NOT NULL CHECK (decision IN ('approve','reject')),
    comment         TEXT,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (rule_version_id, approver)
);

CREATE TABLE tax_test_case (
    jurisdiction_code CHAR(2) NOT NULL REFERENCES jurisdiction,
    test_id     TEXT NOT NULL,
    rule_id     INT NOT NULL REFERENCES tax_rule,
    company_id  SMALLINT REFERENCES company,
    as_of       DATE NOT NULL,
    description TEXT NOT NULL,
    inputs      JSONB NOT NULL,
    expected    JSONB NOT NULL,
    sort_order  INT NOT NULL DEFAULT 0,
    PRIMARY KEY (jurisdiction_code, test_id)
);

-- ---------------------------------------------------------------------
-- 3. Policies (FR-09, FR-13a) — company_id NULL = system default
-- ---------------------------------------------------------------------
CREATE TABLE approval_policy (
    policy_id           SERIAL PRIMARY KEY,
    company_id          SMALLINT REFERENCES company,
    category            TEXT NOT NULL CHECK (category IN ('VAT','WHT','PIT','SSO','OTHER')),
    required_approvals  INT NOT NULL CHECK (required_approvals BETWEEN 1 AND 5),
    approver_roles      TEXT[] NOT NULL CHECK (cardinality(approver_roles) > 0),
    escalate_after_days INT CHECK (escalate_after_days > 0),
    UNIQUE NULLS NOT DISTINCT (company_id, category)
);

CREATE TABLE retro_policy (
    policy_id  SERIAL PRIMARY KEY,
    company_id SMALLINT REFERENCES company,
    category   TEXT NOT NULL CHECK (category IN ('VAT','WHT','PIT','SSO','OTHER')),
    mode       TEXT NOT NULL CHECK (mode IN ('block','adjust_next_period','recalculate_reopen')),
    UNIQUE NULLS NOT DISTINCT (company_id, category)
);

-- Back-dating recalculation report (FR-09): must be confirmed before a retroactive version is approved
CREATE TABLE tax_recalc_run (
    recalc_id          SERIAL PRIMARY KEY,
    trigger_version_id INT NOT NULL REFERENCES tax_rule_version,
    company_id         SMALLINT NOT NULL REFERENCES company,
    mode               TEXT NOT NULL CHECK (mode IN ('adjust_next_period','recalculate_reopen')),
    period_from        DATE NOT NULL,
    period_to          DATE NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','applied','cancelled')),
    report             JSONB NOT NULL,                      -- per document / employee: old -> new
    total_difference   NUMERIC(15,2) NOT NULL DEFAULT 0,
    created_by         TEXT NOT NULL REFERENCES erp_user,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_by       TEXT REFERENCES erp_user,
    confirmed_at       TIMESTAMPTZ
);
-- The workflow functions below set erp.user to the confirming user before updating tax_recalc_run.

CREATE TABLE tax_change_log (
    log_id      BIGSERIAL PRIMARY KEY,
    object_type TEXT NOT NULL,
    object_id   TEXT NOT NULL,
    action      TEXT NOT NULL,
    old_value   JSONB,
    new_value   JSONB,
    reason      TEXT,
    actor       TEXT,
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- 4. Employee tax profile (FR-34..39) and opening year-to-date balances
-- ---------------------------------------------------------------------
CREATE TABLE employee_tax_profile (
    employee_id TEXT NOT NULL REFERENCES employee,
    tax_year    INT  NOT NULL,
    allowances  JSONB NOT NULL DEFAULT '{}',   -- keys = input_field of PIT_ALLOW_* rules, e.g. {"children_count": 2}
    status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved')),
    approved_by TEXT REFERENCES erp_user,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (employee_id, tax_year)
);

CREATE TABLE employee_tax_evidence (
    employee_id   TEXT NOT NULL REFERENCES employee,
    tax_year      INT  NOT NULL,
    allowance_key TEXT NOT NULL,
    file_ref      TEXT NOT NULL,                -- document store reference, never the file itself
    verified_by   TEXT REFERENCES erp_user,
    verified_at   TIMESTAMPTZ,
    PRIMARY KEY (employee_id, tax_year, allowance_key, file_ref)
);

-- Year-to-date figures carried over from a previous payroll system (go-live mid-year)
CREATE TABLE employee_tax_opening (
    employee_id  TEXT NOT NULL REFERENCES employee,
    tax_year     INT  NOT NULL,
    as_of        DATE NOT NULL,
    ytd_income   NUMERIC(15,2) NOT NULL DEFAULT 0,
    ytd_withheld NUMERIC(15,2) NOT NULL DEFAULT 0,
    ytd_sso      NUMERIC(15,2) NOT NULL DEFAULT 0,
    source       TEXT,
    PRIMARY KEY (employee_id, tax_year)
);

-- ---------------------------------------------------------------------
-- 5. Links from transactions to the rule version used (FR-08)
-- ---------------------------------------------------------------------
ALTER TABLE payroll_line
    ADD COLUMN sso_rule_version_id  INT REFERENCES tax_rule_version,
    ADD COLUMN pnd1_rule_version_id INT REFERENCES tax_rule_version,
    ADD COLUMN calc_trace           JSONB,          -- inputs + rule versions used, for audit and reprint
    ADD COLUMN adjusts_run_id       TEXT REFERENCES payroll_run;
ALTER TABLE invoice
    ADD COLUMN vat_rule_version_id  INT REFERENCES tax_rule_version;
ALTER TABLE journal_line
    ADD COLUMN tax_rule_version_id  INT REFERENCES tax_rule_version,
    ADD COLUMN adjusts_je_no        TEXT REFERENCES journal_entry;

-- ---------------------------------------------------------------------
-- 6. Guards (database-level, so no client can bypass them)
-- ---------------------------------------------------------------------
CREATE FUNCTION tax_is_bootstrap() RETURNS BOOLEAN LANGUAGE sql STABLE AS
$$ SELECT COALESCE(current_setting('erp.bootstrap', true), '') = 'on' $$;

-- Effective approval policy for a version: company-specific first, then system default
CREATE FUNCTION tax_approval_policy_for(p_version INT) RETURNS approval_policy LANGUAGE sql STABLE AS $$
    SELECT ap.* FROM tax_rule_version v
    JOIN tax_rule r USING (rule_id)
    JOIN approval_policy ap ON ap.category = r.category
                           AND (ap.company_id = v.company_id OR ap.company_id IS NULL)
    WHERE v.rule_version_id = p_version
    ORDER BY ap.company_id NULLS LAST
    LIMIT 1
$$;

CREATE FUNCTION tax_version_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    pol approval_policy;
    n_ok INT;
    cat TEXT;
    closed_end DATE;
    rmode TEXT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' AND NOT tax_is_bootstrap() THEN
            RAISE EXCEPTION 'New rule versions must be created as draft (got %)', NEW.status;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('approved','submitted') THEN
            RAISE EXCEPTION 'Version % is % and cannot be deleted', OLD.rule_version_id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;

    -- UPDATE ------------------------------------------------------------
    IF OLD.status = 'approved' THEN
        IF (to_jsonb(NEW) - 'effective_to' - 'status' - 'pending_effective_to')
               IS DISTINCT FROM (to_jsonb(OLD) - 'effective_to' - 'status' - 'pending_effective_to')
           OR (OLD.effective_to IS NOT NULL AND NEW.effective_to IS DISTINCT FROM OLD.effective_to)
           OR NEW.status NOT IN ('approved','retired') THEN
            RAISE EXCEPTION 'Version % is approved: only closing an open effective_to or retiring is allowed',
                OLD.rule_version_id;
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.status = 'rejected' AND OLD.status <> 'rejected' THEN
        -- the successor was rejected: cancel the pending close of its predecessor
        UPDATE tax_rule_version p SET pending_effective_to = NULL
         WHERE p.rule_id = NEW.rule_id AND p.company_id IS NOT DISTINCT FROM NEW.company_id
           AND p.status = 'approved' AND p.pending_effective_to = NEW.effective_from - 1;
    END IF;

    IF OLD.status = 'submitted' AND NEW.status = 'submitted'
       AND (to_jsonb(NEW) - 'submitted_at') IS DISTINCT FROM (to_jsonb(OLD) - 'submitted_at') THEN
        RAISE EXCEPTION 'Version % is under review: reject it before editing', OLD.rule_version_id;
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT ((OLD.status = 'draft'     AND NEW.status = 'submitted')
             OR (OLD.status = 'submitted' AND NEW.status IN ('approved','rejected'))
             OR (OLD.status = 'rejected'  AND NEW.status = 'draft')
             OR tax_is_bootstrap()) THEN
            RAISE EXCEPTION 'Status change % -> % is not allowed', OLD.status, NEW.status;
        END IF;
    END IF;

    IF NEW.status = 'approved' AND OLD.status <> 'approved' AND NOT tax_is_bootstrap() THEN
        pol := tax_approval_policy_for(NEW.rule_version_id);
        IF pol.policy_id IS NULL THEN
            RAISE EXCEPTION 'No approval policy for version %', NEW.rule_version_id;
        END IF;
        SELECT count(*) INTO n_ok FROM tax_rule_approval
         WHERE rule_version_id = NEW.rule_version_id AND decision = 'approve';
        IF n_ok < pol.required_approvals THEN
            RAISE EXCEPTION 'Version % needs % approval(s), has %', NEW.rule_version_id, pol.required_approvals, n_ok;
        END IF;

        -- FR-09: back-dating into a closed period
        SELECT r.category INTO cat FROM tax_rule r WHERE r.rule_id = NEW.rule_id;
        SELECT max(fp.end_date) INTO closed_end
          FROM fiscal_period fp JOIN company c USING (company_id)
          JOIN tax_rule r ON r.rule_id = NEW.rule_id AND r.jurisdiction_code = c.jurisdiction_code
         WHERE fp.status IN ('closed','locked')
           AND (NEW.company_id IS NULL OR fp.company_id = NEW.company_id);
        IF closed_end IS NOT NULL AND NEW.effective_from <= closed_end THEN
            SELECT rp.mode INTO rmode FROM retro_policy rp
             WHERE rp.category = cat AND (rp.company_id = NEW.company_id OR rp.company_id IS NULL)
             ORDER BY rp.company_id NULLS LAST LIMIT 1;
            rmode := COALESCE(rmode, 'block');
            IF rmode = 'block' AND COALESCE(current_setting('erp.retro_override', true), '') <> 'on' THEN
                RAISE EXCEPTION 'Version % starts % inside a closed period (to %); back-dating policy for % is "block"',
                    NEW.rule_version_id, NEW.effective_from, closed_end, cat;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM tax_recalc_run
                            WHERE trigger_version_id = NEW.rule_version_id AND status IN ('confirmed','applied')) THEN
                RAISE EXCEPTION 'Version % is back-dated: a recalculation report must be confirmed first (mode %)',
                    NEW.rule_version_id, rmode;
            END IF;
        END IF;
        NEW.approved_at := now();
        -- apply the pending close of the predecessor in the same transaction
        UPDATE tax_rule_version p SET effective_to = p.pending_effective_to, pending_effective_to = NULL
         WHERE p.rule_id = NEW.rule_id AND p.company_id IS NOT DISTINCT FROM NEW.company_id
           AND p.status = 'approved' AND p.pending_effective_to = NEW.effective_from - 1;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_tax_version_guard
    BEFORE INSERT OR UPDATE OR DELETE ON tax_rule_version
    FOR EACH ROW EXECUTE FUNCTION tax_version_guard();

-- Maker-checker: approver must differ from maker/submitter and hold an allowed role
CREATE FUNCTION tax_approval_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v tax_rule_version; pol approval_policy; roles TEXT[];
BEGIN
    SELECT * INTO v FROM tax_rule_version WHERE rule_version_id = NEW.rule_version_id;
    IF v.status <> 'submitted' THEN
        RAISE EXCEPTION 'Version % is %, not submitted', NEW.rule_version_id, v.status;
    END IF;
    IF NEW.approver IN (v.created_by, v.submitted_by) THEN
        RAISE EXCEPTION 'Maker-checker: % created or submitted version % and cannot approve it', NEW.approver, v.rule_version_id;
    END IF;
    pol := tax_approval_policy_for(NEW.rule_version_id);
    SELECT u.roles INTO roles FROM erp_user u WHERE u.username = NEW.approver AND u.is_active;
    IF roles IS NULL OR NOT (roles && pol.approver_roles) THEN
        RAISE EXCEPTION 'User % has none of the approver roles %', NEW.approver, pol.approver_roles;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_tax_approval_guard BEFORE INSERT ON tax_rule_approval
    FOR EACH ROW EXECUTE FUNCTION tax_approval_guard();

-- Generic change log (FR-26)
CREATE FUNCTION tax_log_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE oid_txt TEXT; o JSONB; n JSONB;
BEGIN
    o := CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END;
    n := CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END;
    oid_txt := COALESCE(n, o) ->> TG_ARGV[0];
    IF TG_ARGV[1] IS NOT NULL THEN oid_txt := oid_txt || '/' || (COALESCE(n, o) ->> TG_ARGV[1]); END IF;
    INSERT INTO tax_change_log(object_type, object_id, action, old_value, new_value, reason, actor)
    VALUES (TG_TABLE_NAME, oid_txt, lower(TG_OP), o, n,
            NULLIF(current_setting('erp.reason', true), ''), NULLIF(current_setting('erp.user', true), ''));
    RETURN NULL;
END $$;
CREATE TRIGGER trg_log_trv   AFTER INSERT OR UPDATE OR DELETE ON tax_rule_version
    FOR EACH ROW EXECUTE FUNCTION tax_log_change('rule_version_id');
CREATE TRIGGER trg_log_tra   AFTER INSERT ON tax_rule_approval
    FOR EACH ROW EXECUTE FUNCTION tax_log_change('rule_version_id', 'approver');
CREATE TRIGGER trg_log_ap    AFTER INSERT OR UPDATE OR DELETE ON approval_policy
    FOR EACH ROW EXECUTE FUNCTION tax_log_change('policy_id');
CREATE TRIGGER trg_log_rp    AFTER INSERT OR UPDATE OR DELETE ON retro_policy
    FOR EACH ROW EXECUTE FUNCTION tax_log_change('policy_id');
CREATE TRIGGER trg_log_prof  AFTER INSERT OR UPDATE OR DELETE ON employee_tax_profile
    FOR EACH ROW EXECUTE FUNCTION tax_log_change('employee_id', 'tax_year');
CREATE TRIGGER trg_log_recalc AFTER INSERT OR UPDATE ON tax_recalc_run
    FOR EACH ROW EXECUTE FUNCTION tax_log_change('recalc_id');

-- ---------------------------------------------------------------------
-- 7. Reporting
-- ---------------------------------------------------------------------
-- FR-27: rules in force on a date for a company (company override wins over country rule)
CREATE FUNCTION tax_rules_in_force(p_date DATE, p_company SMALLINT)
RETURNS TABLE (rule_code TEXT, category TEXT, rule_type TEXT, version_no INT, source TEXT,
               rule_version_id INT, effective_from DATE, effective_to DATE, params JSONB, legal_reference TEXT)
LANGUAGE sql STABLE AS $$
    SELECT DISTINCT ON (r.rule_id)
           r.rule_code, r.category, r.rule_type, v.version_no,
           CASE WHEN v.company_id IS NULL THEN 'country' ELSE 'company override' END,
           v.rule_version_id, v.effective_from, v.effective_to, v.params, v.legal_reference
    FROM tax_rule r
    JOIN company c ON c.company_id = p_company AND c.jurisdiction_code = r.jurisdiction_code
    JOIN tax_rule_version v ON v.rule_id = r.rule_id
         AND v.status = 'approved'
         AND (v.company_id IS NULL OR v.company_id = p_company)
         AND p_date BETWEEN v.effective_from AND COALESCE(v.effective_to, 'infinity'::date)
    ORDER BY r.rule_id, v.company_id NULLS LAST
$$;

CREATE VIEW v_payslip AS
SELECT pr.company_id, pr.run_id, pr.period_id, pr.pay_date, pr.status AS run_status,
       e.employee_id, e.first_name || ' ' || e.last_name AS employee_name, e.position,
       pl.base_salary, pl.overtime, pl.commission, pl.allowance, pl.gross_pay,
       pl.sso_employee, pl.wht_pnd1, pl.other_deduction, pl.net_pay, pl.sso_employer,
       sv.version_no AS sso_rule_version, pv.version_no AS pnd1_rule_version,
       (pl.calc_trace ->> 'profile_status') AS tax_profile
FROM payroll_line pl
JOIN payroll_run pr USING (run_id)
JOIN employee e USING (employee_id)
LEFT JOIN tax_rule_version sv ON sv.rule_version_id = pl.sso_rule_version_id
LEFT JOIN tax_rule_version pv ON pv.rule_version_id = pl.pnd1_rule_version_id;

-- ---------------------------------------------------------------------
-- 8. Row-level security per company (NFR-08). Superusers bypass RLS;
--    the application connects as erp_app and sets erp.company_id.
-- ---------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_app') THEN CREATE ROLE erp_app; END IF;
END $$;
GRANT USAGE ON SCHEMA erp TO erp_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA erp TO erp_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA erp TO erp_app;

CREATE FUNCTION erp_current_company() RETURNS SMALLINT LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('erp.company_id', true), '')::SMALLINT $$;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['employee','journal_entry','invoice','payment','payroll_run','partner'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY company_isolation ON %I USING (company_id = erp_current_company())
                        WITH CHECK (company_id = erp_current_company())', t);
    END LOOP;
END $$;
ALTER TABLE payroll_line ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_isolation ON payroll_line
    USING (EXISTS (SELECT 1 FROM payroll_run pr WHERE pr.run_id = payroll_line.run_id
                   AND pr.company_id = erp_current_company()));
ALTER TABLE employee_tax_profile ENABLE ROW LEVEL SECURITY;      -- personal data (FR-39)
CREATE POLICY company_isolation ON employee_tax_profile
    USING (EXISTS (SELECT 1 FROM employee e WHERE e.employee_id = employee_tax_profile.employee_id
                   AND e.company_id = erp_current_company()));

-- ---------------------------------------------------------------------
-- 9. Setup data: Thailand, sample company, users, default policies
-- ---------------------------------------------------------------------
INSERT INTO jurisdiction VALUES ('TH', 'Thailand', 'THB', 'half_up:0.01');
UPDATE company SET company_code = 'DEMO01', jurisdiction_code = 'TH' WHERE company_id = 1;
ALTER TABLE company ALTER COLUMN company_code SET NOT NULL, ALTER COLUMN jurisdiction_code SET NOT NULL;
UPDATE partner SET company_id = 1;  UPDATE employee SET company_id = 1;  UPDATE journal_entry SET company_id = 1;
UPDATE invoice SET company_id = 1;  UPDATE payment SET company_id = 1;   UPDATE payroll_run SET company_id = 1;
ALTER TABLE partner       ALTER COLUMN company_id SET NOT NULL;
ALTER TABLE employee      ALTER COLUMN company_id SET NOT NULL;
ALTER TABLE journal_entry ALTER COLUMN company_id SET NOT NULL;
ALTER TABLE invoice       ALTER COLUMN company_id SET NOT NULL;
ALTER TABLE payment       ALTER COLUMN company_id SET NOT NULL;
ALTER TABLE payroll_run   ALTER COLUMN company_id SET NOT NULL;

INSERT INTO erp_user VALUES
 ('napat',   'Napat Wongsakul (Senior Accountant)', ARRAY['tax_admin','accountant','payroll'], TRUE),
 ('somchai', 'Somchai Prasert (Managing Director)', ARRAY['md'], TRUE),
 ('malee',   'Malee Thongdee (Finance Manager)',    ARRAY['finance_manager'], TRUE),
 ('audit',   'External auditor',                    ARRAY['auditor'], TRUE),
 ('system',  'System / migration',                  ARRAY['system'], TRUE);
INSERT INTO erp_user_company SELECT username, 1 FROM erp_user;

-- Suggested defaults from the requirements (section 8); admins can change them
INSERT INTO approval_policy (company_id, category, required_approvals, approver_roles, escalate_after_days) VALUES
 (NULL, 'VAT',   1, ARRAY['finance_manager','md'], 3),
 (NULL, 'WHT',   1, ARRAY['finance_manager','md'], 3),
 (NULL, 'PIT',   2, ARRAY['finance_manager','md'], 3),
 (NULL, 'SSO',   2, ARRAY['finance_manager','md'], 3),
 (NULL, 'OTHER', 1, ARRAY['finance_manager','md'], 5);
INSERT INTO retro_policy (company_id, category, mode) VALUES
 (NULL, 'VAT', 'block'), (NULL, 'WHT', 'block'),
 (NULL, 'PIT', 'adjust_next_period'), (NULL, 'SSO', 'adjust_next_period'), (NULL, 'OTHER', 'block');

-- ---------------------------------------------------------------------
-- 10. Functions resolve tables in erp even if a client forgets search_path
-- ---------------------------------------------------------------------
DO $$
DECLARE f RECORD;
BEGIN
    FOR f IN SELECT p.oid::regprocedure AS sig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'erp' LOOP
        EXECUTE format('ALTER FUNCTION %s SET search_path = erp, public', f.sig);
    END LOOP;
END $$;
