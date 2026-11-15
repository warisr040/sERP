#!/usr/bin/env python3
"""
Build the sample ERP database from scratch, with payroll CALCULATED from the tax rules.

  python build_sample.py [--db erpdev] [--dump erp_sample_v2.sql]

Steps
  1. 01_core.sql          core schema + non-payroll sample transactions
  2. 02_tax_rules.sql     tax rule tables, guards, policies, users
  3. taxdb import --bootstrap tax_rules_TH.json   (initial rule set, approved)
  4. employee tax profiles 2026 + opening year-to-date (go-live 1 Oct 2026, Jan-Sep from previous system)
  5. link VAT/WHT invoices and journal lines to the rule versions in force
  6. October payroll calculated + posted (JE-2610-009), remittance (JE-2611-002)
  7. November payroll calculated as draft (PR-2611)
  8. pg_dump -> one SQL file that recreates everything
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
PG = ["-h", "/tmp/pg", "-p", "5544", "-U", "postgres"]


def sh(cmd, **kw):
    r = subprocess.run(cmd, cwd=HERE, text=True, capture_output=True, **kw)
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        sys.exit(f"FAILED: {' '.join(cmd)}")
    return r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="erpdev")
    ap.add_argument("--dump", default="erp_sample_v2.sql")
    a = ap.parse_args()
    os.environ["ERP_DSN"] = f"host=/tmp/pg port=5544 user=postgres dbname={a.db}"
    psql = ["psql", *PG, "-v", "ON_ERROR_STOP=1", "-q"]

    sh(psql + ["-c", f"DROP DATABASE IF EXISTS {a.db}", "-c", f"CREATE DATABASE {a.db}"])
    sh(psql + ["-d", a.db, "-f", "01_core.sql"])
    sh(psql + ["-d", a.db, "-f", "02_tax_rules.sql"])
    print(sh([sys.executable, "taxdb.py", "--user", "system", "import", "tax_rules_TH.json", "--bootstrap"]).splitlines()[0])

    import psycopg
    import taxrules as TR
    from psycopg.types.json import Jsonb
    con = psycopg.connect(os.environ["ERP_DSN"], options="-c search_path=erp,public")
    cur = con.cursor()
    cur.execute("SELECT set_config('erp.user','system',false), set_config('erp.reason','sample build',false)")

    # ---- 4. tax profiles (approved by HR/payroll) ----
    profiles = {
        "E001": {"spouse_no_income": True, "children_count": 2},
        "E002": {},
        "E003": {"life_insurance_premium": 40000, "parents_count": 2},
        "E004": {"parents_count": 1},
        # E005 has no profile -> payroll uses defaults and flags it
    }
    for emp, alw in profiles.items():
        cur.execute("INSERT INTO employee_tax_profile (employee_id, tax_year, allowances, status, approved_by) "
                    "VALUES (%s, 2026, %s, 'approved', 'napat')", (emp, Jsonb(alw)))
    cur.execute("INSERT INTO employee_tax_evidence VALUES ('E001',2026,'children_count','docstore://hr/E001/2026/birth-certs.pdf','napat',now()),"
                " ('E003',2026,'life_insurance_premium','docstore://hr/E003/2026/insurance-cert.pdf','napat',now())")

    # ---- opening YTD: simulate Jan-Sep 2026 with the same engine (stands in for the previous payroll system) ----
    from taxdb import db_to_doc
    doc, _ = db_to_doc(cur, "TH", "DEMO01", calc=True)
    eng = TR.Engine(doc, "DEMO01", statuses={"approved"})
    cur.execute("SELECT employee_id, monthly_salary, hire_date FROM employee ORDER BY employee_id")
    for emp, sal, hired in cur.fetchall():
        first = 1 if hired.year < 2026 else hired.month
        inc = wht = sso_t = Decimal(0)
        alw = profiles.get(emp, {})
        for m in range(first, 10):
            day = dt.date(2026, m, 28)
            sso = eng.calc("SSO", day, {"base": sal})["amount"]
            other = sum((eng.calc(c, day, {"value": alw.get(f, 0)})["allowance"]
                         for c, f in (("PIT_ALLOW_SPOUSE", "spouse_no_income"), ("PIT_ALLOW_CHILD", "children_count"),
                                      ("PIT_ALLOW_PARENT", "parents_count"), ("PIT_ALLOW_LIFE_INS", "life_insurance_premium"))),
                        Decimal(0))
            p = eng.calc("PND1_MONTHLY", day, {"monthly_income": sal, "months_remaining": 13 - m, "ytd_income": inc,
                                               "ytd_withheld": wht, "annual_sso": sso_t + sso * (13 - m),
                                               "other_allowances": other})["monthly_wht"]
            inc, wht, sso_t = inc + sal, wht + p, sso_t + sso
        cur.execute("INSERT INTO employee_tax_opening VALUES (%s, 2026, '2026-09-30', %s, %s, %s, %s)",
                    (emp, inc, wht, sso_t, "Migrated from previous payroll system (Jan-Sep 2026)"))

    # ---- 5. link existing VAT / WHT documents to the rule version in force ----
    cur.execute("""UPDATE invoice i SET vat_rule_version_id =
                     (SELECT f.rule_version_id FROM tax_rules_in_force(i.doc_date, i.company_id) f
                      WHERE f.rule_code = 'VAT_STD') WHERE i.vat_rate = 7""")
    for code, accts in (("VAT_STD", ("1220", "2120")), ("WHT_PND3_53", ("1230", "2131"))):
        cur.execute("""UPDATE journal_line l SET tax_rule_version_id =
                         (SELECT f.rule_version_id FROM journal_entry e,
                                 LATERAL tax_rules_in_force(e.entry_date, e.company_id) f
                          WHERE e.je_no = l.je_no AND f.rule_code = %s)
                       WHERE l.account_code = ANY(%s)""", (code, list(accts)))
    # October was closed in the core sample; payroll is posted before closing, as it would be in real life
    cur.execute("UPDATE fiscal_period SET status = 'open' WHERE period_id = '2026-10'")
    con.commit()

    # ---- 6/7. payroll via taxdb ----
    Path("/tmp/inputs_2610.json").write_text(json.dumps({"E004": {"commission": 4000}}))
    Path("/tmp/inputs_2611.json").write_text(json.dumps({
        "E003": {"overtime": 3500}, "E004": {"commission": 6500},
        "E005": {"allowance": 1000, "other_deduction": 500}}))
    print(sh([sys.executable, "taxdb.py", "payroll", "PR-2610", "--pay-date", "2026-10-31",
              "--inputs", "/tmp/inputs_2610.json", "--post", "--je", "JE-2610-009"]))
    cur.execute("UPDATE fiscal_period SET status = 'closed' WHERE period_id = '2026-10'")
    con.commit()
    print(sh([sys.executable, "taxdb.py", "remit", "--period", "2026-10", "--date", "2026-11-07", "--je", "JE-2611-002"]))
    print(sh([sys.executable, "taxdb.py", "payroll", "PR-2611", "--pay-date", "2026-11-30",
              "--inputs", "/tmp/inputs_2611.json"]))
    con.close()

    # ---- 8. dump ----
    out = sh(["pg_dump", *PG, "-d", a.db, "--schema=erp", "--no-owner", "--no-privileges", "--inserts",
              "--rows-per-insert=50"])
    header = ("-- =====================================================================\n"
              "--  simply handle IT ERP — sample database v2 (generated by build_sample.py)\n"
              "--  Core + tax rule engine tables, Thai rule set, payroll calculated from rules.\n"
              "--  Load into an EMPTY database:  psql -d your_db -f erp_sample_v2.sql\n"
              "-- =====================================================================\n"
              "CREATE EXTENSION IF NOT EXISTS btree_gist;\n"
              "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='erp_app') THEN CREATE ROLE erp_app; END IF; END $$;\n")
    grants = ("\n-- application role grants (RLS policies above apply to it)\n"
              "GRANT USAGE ON SCHEMA erp TO erp_app;\n"
              "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA erp TO erp_app;\n"
              "GRANT USAGE ON ALL SEQUENCES IN SCHEMA erp TO erp_app;\n")
    # newer pg_dump adds psql-only meta-commands (\restrict ...); drop them so any client can run the file
    out = "\n".join(l for l in out.splitlines() if not l.startswith("\\")) + "\n"
    (HERE / a.dump).write_text(header + out + grants, encoding="utf-8")
    print(f"wrote {a.dump} ({len(out) // 1024} KB)")


if __name__ == "__main__":
    main()
