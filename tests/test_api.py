#!/usr/bin/env python3
"""
API checks against a running sERP server loaded with the sample (erp_sample_v2.sql).
  python tests/test_api.py [http://127.0.0.1:47394]
Uses only the standard library. Changes data - run it on a test database.
"""
import json
import sys
import urllib.request
import urllib.error

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:47394").rstrip("/")
ok_all = []


def call(path, body=None, method=None, user="napat", headers=None, raw=None):
    h = {"X-ERP-User": user, "X-ERP-Company": "1", "X-Requested-With": "sERP"}
    h.update(headers or {})
    data = None
    if raw is not None:
        data, h["Content-Type"] = raw, "application/octet-stream"
    elif body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(BASE + "/api/" + path, data=data, headers=h, method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond, info=""):
    ok_all.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"\n        {info}"))


s, r = call("status")
check("server and database are up", s == 200 and r.get("db") == "ok" and r.get("schema"), r)

# --- invoices: VAT from the approved rule in force ---
s, r = call("invoices", {"doc_type": "AR", "partner_id": "C002", "doc_date": "2026-11-10", "vat_code": "VAT_STD",
                         "lines": [{"description": "Diagnostic training", "qty": 2, "unit_price": "12500.50", "account_code": "4110"}]})
check("sales invoice: VAT 7% of 25,001.00 = 1,750.07", s == 200 and abs(r["vat"] - 1750.07) < .001, r)
inv = r.get("doc_no")
s, r = call("invoices/" + str(inv))
check("invoice records the VAT rule version used", s == 200 and r["vat_rule"] == "VAT_STD" and r["vat_version"] == 2, r)
s, r = call("invoices", {"doc_type": "AR", "partner_id": "C002", "doc_date": "2026-10-15", "lines": [{"description": "x", "qty": 1, "unit_price": 1, "account_code": "4110"}]})
check("invoice dated in closed October is refused", s == 409 and "closed" in r.get("error", ""), r)

# --- payments: withholding from the approved rule + threshold ---
s, r = call("payments/preview", {"doc_no": inv, "payment_date": "2026-11-12", "wht_category": "service"})
check("payment preview: 3% service WHT on the pre-VAT amount (750.03)", s == 200 and abs(r["wht"] - 750.03) < .001, r)
s, r = call("invoices", {"doc_type": "AP", "partner_id": "V001", "doc_date": "2026-11-11", "vat_code": "VAT_STD",
                         "lines": [{"description": "Cable", "qty": 1, "unit_price": 900, "account_code": "6230"}]})
bill = r.get("doc_no")
s, r = call("payments/preview", {"doc_no": bill, "payment_date": "2026-11-12", "wht_category": "service"})
check("payment below the 1,000 threshold: no withholding", s == 200 and r["wht"] == 0, r)
s, r = call("payments", {"doc_no": inv, "payment_date": "2026-11-12", "wht_category": "service", "settle": 10000})
check("partial payment recorded", s == 200 and r.get("payment_no"), r)
s, r = call("invoices/" + str(inv))
check("invoice becomes 'partial' with the right outstanding", r["status"] == "partial" and abs(r["outstanding"] - 16751.07) < .001, r)

# --- journal rules ---
s, r = call("journal", {"entry_date": "2026-11-12", "description": "bad", "post": True,
                        "lines": [{"account_code": "6230", "debit": 100}, {"account_code": "1120", "credit": 90}]})
check("unbalanced entry cannot be posted", s == 400 and "Not balanced" in r.get("error", ""), r)
s, r = call("journal", {"entry_date": "2026-11-12", "description": "header account", "post": True,
                        "lines": [{"account_code": "6000", "debit": 100}, {"account_code": "1120", "credit": 100}]})
check("posting to a header account is refused", s == 400 and "postable" in r.get("error", ""), r)
s, r = call("journal", {"entry_date": "2026-11-12", "description": "draft test", "post": False,
                        "lines": [{"account_code": "6230", "debit": 100}, {"account_code": "1120", "credit": 100}]})
je = r.get("je_no")
s, r = call(f"journal/{je}/post", {})
check("draft entry posts", s == 200, r)
s, r = call(f"journal/{je}/reverse", {"date": "2026-11-13"})
check("posted entry reverses with a new entry", s == 200 and "reversed by" in r.get("message", ""), r)
s, r = call(f"journal/{je}/delete", {})
check("posted entry cannot be deleted", s == 409, r)

# --- roles and maker-checker ---
s, r = call("employees/E005/profile", {"tax_year": 2026, "allowances": {"parents_count": 1}}, method="PUT", user="somchai")
check("MD without payroll role cannot edit tax profiles", s == 403, r)
s, r = call("employees/E005/profile", {"tax_year": 2026, "allowances": {"parents_count": 1}}, method="PUT")
check("payroll user saves and approves a tax profile", s == 200, r)
s, r = call("tax/policies", {"kind": "approval", "category": "VAT", "required_approvals": 1, "approver_roles": ["md"]}, method="PUT")
check("tax admin cannot change approval policies", s == 403, r)

# --- import checks ---
s, r = call("tax/import?name=bad.json", raw=b'{"format":"nope"}')
check("invalid rule file is rejected with schema errors", s == 200 and r["errors"] and not r["applied"], r)
s, r = call("tax/import?name=bad.txt", raw=b"hello")
check("wrong file type refused", s == 400, r)

# --- payroll reflects the new profile ---
s, r = call("payroll/PR-2611/recalc", {})
s, r = call("payroll/PR-2611")
e5 = next((l for l in r.get("lines", []) if l["employee_id"] == "E005"), {})
check("recalculated payroll uses E005's new approved profile", (e5.get("calc_trace") or {}).get("profile_status") == "approved", e5)

# --- request hygiene ---
s, r = call("journal", {"entry_date": "2026-11-12"}, headers={"X-Requested-With": ""})
check("write without X-Requested-With header is refused", s == 403, r)
s, r = call("meta", headers={"Origin": "http://evil.example"})
check("cross-origin request is refused", s == 403, r)
s, r = call("dashboard?as_of=2026-11-13")
check("books still balance after all of the above", s == 200 and r["balanced"], r)

print(f"\n{sum(ok_all)}/{len(ok_all)} checks passed")
sys.exit(0 if all(ok_all) else 1)
