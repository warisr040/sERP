#!/usr/bin/env python3
"""
End-to-end checks on a throw-away copy of the sample database (erp_sample_v2.sql).
Hypothetical rule changes below are TEST DATA ONLY - they are not real law.

  python verify.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = "erpverify"
PG = ["-h", "/tmp/pg", "-p", "5544", "-U", "postgres"]
os.environ["ERP_DSN"] = f"host=/tmp/pg port=5544 user=postgres dbname={DB}"
results = []


def run(*args, user="napat"):
    r = subprocess.run([sys.executable, "taxdb.py", "--user", user, *args], cwd=HERE, text=True, capture_output=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def sql(q, role=None, company=None):
    pre = ""
    if role:
        pre = f"SET ROLE {role}; SET erp.company_id = '{company}'; "
    r = subprocess.run(["psql", *PG, "-d", DB, "-Atq", "-v", "ON_ERROR_STOP=1",
                        "-c", f"SET search_path=erp,public; {pre}{q}"], text=True, capture_output=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def check(name, cond, detail=""):
    results.append(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"\n        {detail}" if detail and not cond else ""))


def expect_error(name, rc_out, text):
    rc, out = rc_out
    check(name, rc != 0 and text in out, out[-300:])
    if rc != 0 and text in out:
        print(f"        -> {[l for l in out.splitlines() if 'ERROR' in l or text in l][0][:140]}")


# ---------- fresh copy from the dump ----------
subprocess.run(["psql", *PG, "-q", "-c", f"DROP DATABASE IF EXISTS {DB}", "-c", f"CREATE DATABASE {DB}"], check=True)
r = subprocess.run(["psql", *PG, "-d", DB, "-q", "-v", "ON_ERROR_STOP=1", "-f", "erp_sample_v2.sql"],
                   cwd=HERE, text=True, capture_output=True)
check("dump loads into an empty database", r.returncode == 0, r.stderr[-400:])

print("\n== Books ==")
_, o = sql("SELECT sum(total_debit) = sum(total_credit) FROM v_trial_balance")
check("trial balance balances", o == "t")
_, o = sql("SELECT balance FROM v_trial_balance WHERE account_code IN ('2130','2131','2140') AND balance <> 0")
check("tax payables cleared after remittance", o == "", o)
_, o = sql("SELECT sum(sso_employee), min(sso_employee), max(sso_employee) FROM payroll_line WHERE run_id='PR-2610'")
check("Oct SSO = 875 per employee (2026 ceiling)", o == "4375.00|875.00|875.00", o)
_, o = sql("SELECT count(*) FROM payroll_line WHERE sso_rule_version_id IS NULL OR pnd1_rule_version_id IS NULL")
check("every payroll line records the rule versions used", o == "0", o)
_, o = sql("""SELECT (SELECT sum(debit) FROM journal_line WHERE je_no='JE-2610-009' AND account_code='6110')
                    = (SELECT sum(gross_pay) FROM payroll_line WHERE run_id='PR-2610')""")
check("payroll journal matches payroll lines", o == "t")
_, o = sql("SELECT tax_profile FROM v_payslip WHERE run_id='PR-2610' AND employee_id='E005'")
check("employee without profile is flagged 'default'", o == "default", o)
_, o = sql("SELECT count(*) FROM invoice WHERE vat_rate = 7 AND vat_rule_version_id IS NULL")
check("VAT invoices linked to a VAT rule version", o == "0", o)

print("\n== Rule set round trip ==")
rc, o = run("export", "/tmp/v_export.json")
src = json.loads((HERE / "tax_rules_TH.json").read_text())
exp = json.loads(Path("/tmp/v_export.json").read_text())
check("DB export has the same checksum as the imported file", exp["base_checksum"] == src["base_checksum"],
      f"{exp['base_checksum']} vs {src['base_checksum']}")
rc, o = run("export", "/tmp/v_export.xlsx")
check("DB export to Excel works", rc == 0, o)

print("\n== Update from Claude: VAT statutory 10% from 2027-10-01 (draft) ==")
upd = json.loads(Path("/tmp/v_export.json").read_text())
vat = next(r for r in upd["rules"] if r["code"] == "VAT_STD")
vat["versions"].append({"version": 3, "company_id": None, "status": "draft", "effective_from": "2027-10-01",
                        "effective_to": None, "params": {"rate": 0.10}, "rounding": "half_up:0.01",
                        "legal_reference": "Revenue Code s.80 (statutory rate if no new reduction decree)",
                        "source_url": None, "notes": "Fallback until a new decree is known"})
upd["test_cases"].append({"id": "TC-VAT-004", "rule": "VAT_STD", "company_id": None, "as_of": "2027-10-15",
                          "description": "Statutory 10%", "inputs": {"amount": 200000}, "expected": {"tax": 20000}})
upd["change_summary"] = "VAT_STD v3 draft: statutory 10% from 2027-10-01 after Royal Decree 807 expires."
Path("/tmp/v_update.json").write_text(json.dumps(upd, ensure_ascii=False, indent=1))
rc, o = run("import", "/tmp/v_update.json")
check("import applies the update as draft", rc == 0 and "+ VERSION VAT_STD v3  [draft]" in o, o)
rc, o = run("import", "/tmp/v_update.json")
expect_error("re-importing the same file is rejected (rule set changed since export)", (rc, o), "base_checksum")
rc, o = run("submit", "VAT_STD", "3")
check("maker submits", rc == 0, o)
expect_error("maker cannot approve own change", run("approve", "VAT_STD", "3"), "Maker-checker")
expect_error("user without approver role cannot approve", run("approve", "VAT_STD", "3", user="audit"), "approver roles")
rc, o = run("approve", "VAT_STD", "3", user="malee")
check("finance manager approves (VAT policy needs 1)", rc == 0 and "APPROVED (1/1)" in o, o)
rc, o = run("in-force", "2027-10-15")
check("rules in force on 2027-10-15 show VAT v3 at 10%", "VAT_STD              v3" in o and '"rate": 0.1' in o, o)

print("\n== Database guards (direct SQL bypassing the tools) ==")
expect_error("approved version cannot be edited",
             sql("UPDATE tax_rule_version SET params = '{\"rate\":0.08}' WHERE version_no=2 AND rule_id=(SELECT rule_id FROM tax_rule WHERE rule_code='VAT_STD')"),
             "only closing an open effective_to")
expect_error("approved version cannot be deleted",
             sql("DELETE FROM tax_rule_version WHERE version_no=1 AND rule_id=(SELECT rule_id FROM tax_rule WHERE rule_code='SSO')"),
             "cannot be deleted")
expect_error("new version cannot be inserted as approved",
             sql("INSERT INTO tax_rule_version (rule_id, version_no, status, effective_from, params, rounding, created_by) "
                 "SELECT rule_id, 9, 'approved', '2040-01-01', '{\"rate\":0.05}', 'none', 'napat' FROM tax_rule WHERE rule_code='VAT_ZERO'"),
             "must be created as draft")
expect_error("approving a version that overlaps an approved one is rejected",
             sql("BEGIN; SELECT set_config('erp.user','napat',true); "
                 "INSERT INTO tax_rule_version (rule_id, version_no, status, effective_from, params, rounding, created_by) "
                 "SELECT rule_id, 9, 'draft', '2040-01-01', '{\"rate\":0}', 'none', 'napat' FROM tax_rule WHERE rule_code='VAT_ZERO'; "
                 "UPDATE tax_rule_version SET status='submitted', submitted_by='napat' WHERE version_no=9; "
                 "INSERT INTO tax_rule_approval SELECT rule_version_id, 'malee', 'approve' FROM tax_rule_version WHERE version_no=9; "
                 "UPDATE tax_rule_version SET status='approved' WHERE version_no=9; COMMIT;"),
             "no_overlapping_versions")
expect_error("draft cannot jump straight to approved",
             sql("BEGIN; INSERT INTO tax_rule_version (rule_id, version_no, status, effective_from, params, rounding, created_by) "
                 "SELECT rule_id, 2, 'draft', '2040-01-01', '{\"rate\":0}', 'none', 'napat' FROM tax_rule WHERE rule_code='VAT_ZERO'; "
                 "UPDATE tax_rule_version SET status='approved' WHERE version_no=2 AND rule_id=(SELECT rule_id FROM tax_rule WHERE rule_code='VAT_ZERO'); COMMIT;"),
             "is not allowed")
_, o = sql("SELECT count(*) FROM tax_change_log")
check("changes are logged", int(o) > 0, o)

print("\n== Back-dating (HYPOTHETICAL test changes - not real law) ==")
rc, o = run("export", "/tmp/v_base2.json")
d = json.loads(Path("/tmp/v_base2.json").read_text())
vz = next(r for r in d["rules"] if r["code"] == "VAT_ZERO")
vz["versions"][0]["effective_to"] = "2026-10-14"
vz["versions"].append({"version": 2, "company_id": None, "status": "draft", "effective_from": "2026-10-15",
                       "effective_to": None, "params": {"rate": 0}, "rounding": "half_up:1",
                       "legal_reference": "HYPOTHETICAL TEST", "source_url": None, "notes": None})
d["change_summary"] = "HYPOTHETICAL back-dated VAT change"
Path("/tmp/v_retro_vat.json").write_text(json.dumps(d, ensure_ascii=False))
rc, o = run("import", "/tmp/v_retro_vat.json")
check("hypothetical back-dated VAT draft imports", rc == 0, o)
run("submit", "VAT_ZERO", "2")
expect_error("VAT back-dating into closed October is blocked by policy",
             run("approve", "VAT_ZERO", "2", user="malee"), 'policy for VAT is "block"')
run("reject", "VAT_ZERO", "2", "--comment", "test cleanup", user="somchai")
_, o = sql("SELECT effective_to IS NULL AND pending_effective_to IS NULL FROM tax_rule_version WHERE version_no=1 "
           "AND rule_id=(SELECT rule_id FROM tax_rule WHERE rule_code='VAT_ZERO')")
check("rejecting the successor cancels the predecessor's pending close (no gap)", o == "t", o)

rc, o = run("export", "/tmp/v_base3.json")
d = json.loads(Path("/tmp/v_base3.json").read_text())
pe = next(r for r in d["rules"] if r["code"] == "PIT_EXPENSE")
pe["versions"][0]["effective_to"] = "2026-09-30"
pe["versions"].append({"version": 2, "company_id": None, "status": "draft", "effective_from": "2026-10-01",
                       "effective_to": None, "params": {"rate": 0.5, "max_amount": 120000}, "rounding": "half_up:0.01",
                       "legal_reference": "HYPOTHETICAL TEST", "source_url": None, "notes": None})
for t in d["test_cases"]:
    if t["id"] == "TC-PIT-003":
        t["as_of"] = "2026-09-30"
    if t["id"] == "TC-PND1-004":          # updater must fix tests the change affects (FR-15)
        t["expected"]["monthly_wht"] = 1141.65
d["change_summary"] = "HYPOTHETICAL back-dated expense cap"
# the rejected VAT_ZERO v2 is removed from the file (rejected versions may be deleted)
vz = next(r for r in d["rules"] if r["code"] == "VAT_ZERO")
vz["versions"] = [v for v in vz["versions"] if v["version"] != 2]
Path("/tmp/v_retro_pit.json").write_text(json.dumps(d, ensure_ascii=False))
rc, o = run("import", "/tmp/v_retro_pit.json")
check("hypothetical back-dated PIT draft imports", rc == 0, o)
run("submit", "PIT_EXPENSE", "2")
run("approve", "PIT_EXPENSE", "2", user="malee")
expect_error("2nd PIT approval without a confirmed recalculation report is refused",
             run("approve", "PIT_EXPENSE", "2", user="somchai"), "recalculation report must be confirmed")
rc, o = run("recalc-report", "PIT_EXPENSE", "2")
check("recalculation report lists old -> new per employee", rc == 0 and "PR-2610" in o, o)
print("        " + "\n        ".join(o.splitlines()[:4]))
rid = o.split()[2]
expect_error("report creator cannot confirm own report", run("confirm-recalc", rid), "second person")
rc, o = run("confirm-recalc", rid, user="malee")
check("second person confirms the report", rc == 0, o)
rc, o = run("approve", "PIT_EXPENSE", "2", user="somchai")
check("PIT change approved after 2 approvals + confirmed report", rc == 0 and "APPROVED (2/2)" in o, o)
_, o = sql("SELECT effective_to FROM tax_rule_version WHERE version_no=1 AND rule_id=(SELECT rule_id FROM tax_rule WHERE rule_code='PIT_EXPENSE')")
check("approving the successor closes the predecessor at the same moment", o == "2026-09-30", o)
_, o = sql("SELECT wht_pnd1 FROM payroll_line WHERE run_id='PR-2610' AND employee_id='E001'")
check("posted October payroll is NOT silently changed", o == "4410.41", o)

print("\n== Company isolation (row-level security, as application role) ==")
_, o = sql("SELECT count(*) FROM employee", role="erp_app", company=1)
check("company 1 sees its 5 employees", o.splitlines()[-1] == "5", o)
_, o = sql("SELECT count(*) FROM employee", role="erp_app", company=2)
check("another company sees none", o.splitlines()[-1] == "0", o)

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
