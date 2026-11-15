#!/usr/bin/env python3
"""
simply handle IT ERP - tax rules <-> database, approval workflow, payroll.

Connection: env ERP_DSN (default: host=/tmp/pg port=5544 user=postgres dbname=erpdev)
Acting user: --user or env ERP_USER (recorded in the change log; checked by maker-checker).

  Rules
    taxdb.py export  OUT.json|OUT.xlsx [--jurisdiction TH] [--company DEMO01]
    taxdb.py import  FILE [--bootstrap]               validate vs live rules, then apply as drafts
    taxdb.py submit  RULE_CODE VERSION [--company C]
    taxdb.py approve RULE_CODE VERSION [--company C] [--comment ..]
    taxdb.py reject  RULE_CODE VERSION [--company C] --comment ..
    taxdb.py in-force DATE [--company DEMO01]
  Back-dating (FR-09)
    taxdb.py recalc-report  RULE_CODE VERSION [--company C]   old vs new for closed periods
    taxdb.py confirm-recalc RECALC_ID
  Payroll
    taxdb.py payroll RUN_ID --pay-date YYYY-MM-DD [--company DEMO01] [--inputs inputs.json]
                     [--post --je JE_NO]
    taxdb.py remit   --period YYYY-MM --date YYYY-MM-DD --je JE_NO [--company DEMO01]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

import taxrules as TR

DEFAULT_DSN = "host=/tmp/pg port=5544 user=postgres dbname=erpdev"
TZ = dt.timezone(dt.timedelta(hours=7))


class Fail(Exception):
    pass


def connect():
    return psycopg.connect(os.environ.get("ERP_DSN", DEFAULT_DSN), options="-c search_path=erp,public",
                           autocommit=False)


def _say(out, s):
    """Collect messages for the API (out = list) or print them for the CLI (out = None)."""
    if out is None:
        print(s)
    else:
        out.append(s)


def ctx(cur, user, reason=None, bootstrap=False, company_id=None):
    cur.execute("SELECT set_config('erp.user', %s, true), set_config('erp.reason', %s, true),"
                " set_config('erp.bootstrap', %s, true), set_config('erp.company_id', %s, true)",
                (user, reason or "", "on" if bootstrap else "", str(company_id or "")))


def company_row(cur, code):
    cur.execute("SELECT company_id, company_code, jurisdiction_code FROM company WHERE company_code = %s", (code,))
    r = cur.fetchone()
    if not r:
        raise Fail(f"unknown company {code}")
    return r


def _jsonable(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    return o


# =====================================================================
# DB  ->  document (same shape as the export file)
# =====================================================================
def db_to_doc(cur, jurisdiction: str, company_code: str | None = None, user="system", calc=False):
    """calc=False: the PLANNED view (pending closes shown as effective_to) - used for export, import, validation.
    calc=True: the view that applies today (pending closes ignored) - used for payroll and invoices."""
    cur.execute("SELECT jurisdiction_code, name, base_currency, default_rounding FROM jurisdiction "
                "WHERE jurisdiction_code = %s", (jurisdiction,))
    j = cur.fetchone()
    if not j:
        raise Fail(f"unknown jurisdiction {jurisdiction}")
    comp_id = company_row(cur, company_code)[0] if company_code else None
    cur.execute("SELECT company_id, company_code FROM company")
    code_of = dict(cur.fetchall())

    cur.execute("""SELECT rule_id, rule_code, name_en, name_th, category, rule_type, is_mandatory, description
                   FROM tax_rule WHERE jurisdiction_code = %s ORDER BY sort_order, rule_id""", (jurisdiction,))
    rules, by_id = [], {}
    for rid, code, ne, nt, cat, rt, mand, desc in cur.fetchall():
        r = {"code": code, "name_en": ne, "name_th": nt, "category": cat, "rule_type": rt,
             "is_mandatory": mand, "description": desc, "versions": []}
        rules.append(r)
        by_id[rid] = r
    vid = {}
    cur.execute("""SELECT v.rule_version_id, v.rule_id, v.company_id, v.version_no, v.status, v.effective_from,
                          COALESCE(v.effective_to, CASE WHEN NOT %s THEN v.pending_effective_to END),
                          v.params, v.rounding, v.legal_reference, v.source_url, v.notes
                   FROM tax_rule_version v JOIN tax_rule r USING (rule_id)
                   WHERE r.jurisdiction_code = %s AND (v.company_id IS NULL OR v.company_id = %s)
                   ORDER BY v.rule_id, v.company_id NULLS FIRST, v.version_no""", (calc, jurisdiction, comp_id))
    for vid_, rid, cid, vno, st, ef, et, params, rnd, legal, url, notes in cur.fetchall():
        r = by_id[rid]
        v = {"version": vno, "company_id": code_of.get(cid) if cid else None, "status": st,
             "effective_from": ef.isoformat(), "effective_to": et.isoformat() if et else None,
             "params": params, "rounding": rnd, "legal_reference": legal, "source_url": url, "notes": notes}
        r["versions"].append(v)
        vid[(r["code"], v["company_id"], vno)] = vid_
    rules = [r for r in rules if r["versions"]]

    cur.execute("""SELECT t.test_id, r.rule_code, t.company_id, t.as_of, t.description, t.inputs, t.expected
                   FROM tax_test_case t JOIN tax_rule r USING (rule_id)
                   WHERE t.jurisdiction_code = %s AND (t.company_id IS NULL OR t.company_id = %s)
                   ORDER BY t.sort_order, t.test_id""", (jurisdiction, comp_id))
    tests = [{"id": a, "rule": b, "company_id": code_of.get(c) if c else None, "as_of": d.isoformat(),
              "description": e, "inputs": f, "expected": g} for a, b, c, d, e, f, g in cur.fetchall()]

    template = json.loads(Path(__file__).with_name("tax_rules_TH.json").read_text(encoding="utf-8"))
    doc = {
        "format": "simplyhandleit-erp-tax-rules", "schema_version": "1.0.0",
        "jurisdiction": jurisdiction, "company_id": company_code,
        "exported_at": dt.datetime.now(TZ).isoformat(timespec="seconds"), "exported_by": user,
        "base_checksum": "sha256:" + "0" * 64,
        "updater_instructions": template["updater_instructions"], "change_summary": "",
        "supported_rule_types": template["supported_rule_types"], "formula_functions": template["formula_functions"],
        "jurisdiction_info": {"code": j[0], "name": j[1], "base_currency": j[2], "default_rounding": j[3]},
        "rules": rules, "test_cases": tests,
    }
    if company_code:
        cur.execute("SELECT category, required_approvals, approver_roles, escalate_after_days FROM approval_policy "
                    "WHERE company_id = %s ORDER BY category", (comp_id,))
        ap = [{"category": a, "required_approvals": b, "approver_roles": c, "escalate_after_days": d}
              for a, b, c, d in cur.fetchall()]
        cur.execute("SELECT category, mode FROM retro_policy WHERE company_id = %s ORDER BY category", (comp_id,))
        rp = [{"category": a, "mode": b} for a, b in cur.fetchall()]
        if ap or rp:
            doc["policies"] = {"approval": ap, "retro": rp}
    doc = TR.normalize(doc)
    doc["base_checksum"] = TR.checksum(doc)
    return doc, vid


# =====================================================================
# Import (FR-21..25)
# =====================================================================
def do_import(path, user, bootstrap=False, out=None, file_name=None):
    raw = Path(path).read_bytes()
    file_name = file_name or Path(path).name
    new = TR.load(path)
    fmt = "xlsx" if path.lower().endswith(".xlsx") else "json"
    with connect() as con, con.cursor() as cur:
        ctx(cur, user, reason=f"import {file_name}", bootstrap=bootstrap)
        jur, comp_code = new["jurisdiction"], new.get("company_id")
        comp_id = company_row(cur, comp_code)[0] if comp_code else None
        cur.execute("SELECT count(*) FROM tax_rule WHERE jurisdiction_code = %s", (jur,))
        has_rules = cur.fetchone()[0] > 0

        if bootstrap:
            if has_rules:
                raise Fail("--bootstrap is only for an empty rule set")
            errs, warns = TR.validate(new)
            base = None
        else:
            base, _ = db_to_doc(cur, jur, comp_code, user)
            errs, warns = TR.validate(new, base)
        changes = TR.diff(base, new) if base else [f"bootstrap: {len(new['rules'])} rules"]
        report = "\n".join([f"ERROR {e}" for e in errs] + [f"WARN {w}" for w in warns] + changes)
        cur.execute("""INSERT INTO tax_import_batch (file_name, file_format, file_sha256, base_checksum,
                         jurisdiction_code, company_id, change_summary, imported_by, result, report)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING batch_id""",
                    (file_name, fmt, hashlib.sha256(raw).hexdigest(), new["base_checksum"], jur, comp_id,
                     new.get("change_summary"), user,
                     "rejected" if errs else ("bootstrap" if bootstrap else "applied"), report))
        batch = cur.fetchone()[0]
        if errs:
            con.commit()                                    # keep the rejected batch in the log (FR-25)
            e = Fail("import rejected (batch %d):\n  " % batch + "\n  ".join(errs))
            e.details = {"batch": batch, "errors": errs, "warnings": warns, "changes": changes}
            raise e

        cur.execute("SELECT company_code, company_id FROM company")
        cid_of = dict(cur.fetchall())
        cur.execute("SELECT rule_code, rule_id FROM tax_rule WHERE jurisdiction_code = %s", (jur,))
        rid_of = dict(cur.fetchall())
        for i, r in enumerate(new["rules"]):
            if r["code"] in rid_of:
                cur.execute("""UPDATE tax_rule SET name_en=%s, name_th=%s, description=%s, is_mandatory=%s,
                               sort_order=%s WHERE rule_id=%s""",
                            (r["name_en"], r["name_th"], r["description"], r["is_mandatory"], i, rid_of[r["code"]]))
            else:
                cur.execute("""INSERT INTO tax_rule (jurisdiction_code, rule_code, name_en, name_th, category,
                                 rule_type, is_mandatory, description, sort_order)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING rule_id""",
                            (jur, r["code"], r["name_en"], r["name_th"], r["category"], r["rule_type"],
                             r["is_mandatory"], r["description"], i))
                rid_of[r["code"]] = cur.fetchone()[0]

        old_v = {}
        if base:
            old_v = {TR.vkey(r["code"], v): v for r in base["rules"] for v in r["versions"]}
        new_v = {TR.vkey(r["code"], v): v for r in new["rules"] for v in r["versions"]}
        for k, v in new_v.items():
            code, comp, vno = k
            rid, cid = rid_of[code], cid_of.get(comp) if comp else None
            if k not in old_v:
                cur.execute("""INSERT INTO tax_rule_version (rule_id, company_id, version_no, status, effective_from,
                                 effective_to, params, rounding, legal_reference, source_url, notes, created_by,
                                 approved_at, import_batch_id)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (rid, cid, vno, v["status"], v["effective_from"], v["effective_to"], Jsonb(v["params"]),
                             v["rounding"], v["legal_reference"], v["source_url"], v["notes"], user,
                             dt.datetime.now(TZ) if v["status"] == "approved" else None, batch))
            elif old_v[k]["status"] == "approved":
                if old_v[k]["effective_to"] is None and v["effective_to"]:   # only change the validator allows
                    cur.execute("UPDATE tax_rule_version SET pending_effective_to=%s WHERE rule_id=%s "
                                "AND company_id IS NOT DISTINCT FROM %s AND version_no=%s",
                                (v["effective_to"], rid, cid, vno))
            elif old_v[k] != v:
                cur.execute("""UPDATE tax_rule_version SET effective_from=%s, effective_to=%s, params=%s, rounding=%s,
                                 legal_reference=%s, source_url=%s, notes=%s, import_batch_id=%s,
                                 status = CASE WHEN status = 'rejected' THEN 'draft' ELSE status END
                               WHERE rule_id=%s AND company_id IS NOT DISTINCT FROM %s AND version_no=%s""",
                            (v["effective_from"], v["effective_to"], Jsonb(v["params"]), v["rounding"],
                             v["legal_reference"], v["source_url"], v["notes"], batch, rid, cid, vno))
        for k in old_v.keys() - new_v.keys():               # validator already blocked approved removals
            code, comp, vno = k
            if old_v[k]["status"] == "draft":
                cur.execute("DELETE FROM tax_rule_version WHERE rule_id=%s AND company_id IS NOT DISTINCT FROM %s "
                            "AND version_no=%s", (rid_of[code], cid_of.get(comp) if comp else None, vno))
            else:   # rejected / retired versions stay for the audit trail
                changes.append(f"  (kept {TR.fmt_key(k)} [{old_v[k]['status']}] for audit - it is not applied)")

        cur.execute("DELETE FROM tax_test_case WHERE jurisdiction_code=%s AND company_id IS NOT DISTINCT FROM %s",
                    (jur, comp_id))
        if comp_id is not None:      # company file also carries country tests (read-only); keep them as they are
            pass
        for i, t in enumerate(new["test_cases"]):
            tc = cid_of.get(t["company_id"]) if t["company_id"] else None
            if tc != comp_id:
                continue
            cur.execute("""INSERT INTO tax_test_case (jurisdiction_code, test_id, rule_id, company_id, as_of,
                             description, inputs, expected, sort_order) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (jur, t["id"], rid_of[t["rule"]], tc, t["as_of"], t["description"],
                         Jsonb(t["inputs"]), Jsonb(t["expected"]), i))
        if new.get("policies") and comp_id:
            for a in new["policies"].get("approval", []):
                cur.execute("""INSERT INTO approval_policy (company_id, category, required_approvals, approver_roles,
                                 escalate_after_days) VALUES (%s,%s,%s,%s,%s)
                               ON CONFLICT (company_id, category) DO UPDATE SET required_approvals=EXCLUDED.required_approvals,
                                 approver_roles=EXCLUDED.approver_roles, escalate_after_days=EXCLUDED.escalate_after_days""",
                            (comp_id, a["category"], a["required_approvals"], a["approver_roles"], a["escalate_after_days"]))
            for a in new["policies"].get("retro", []):
                cur.execute("""INSERT INTO retro_policy (company_id, category, mode) VALUES (%s,%s,%s)
                               ON CONFLICT (company_id, category) DO UPDATE SET mode=EXCLUDED.mode""",
                            (comp_id, a["category"], a["mode"]))
        con.commit()
    _say(out, f"import batch {batch}: {'bootstrap' if bootstrap else 'applied'}")
    for w in warns:
        _say(out, f"  WARN {w}")
    for c in changes:
        _say(out, f"  {c}")
    return {"batch": batch, "warnings": warns, "changes": changes}


# =====================================================================
# Workflow
# =====================================================================
def _version_id(cur, code, vno, company_code, jur="TH"):
    cur.execute("""SELECT v.rule_version_id, v.status FROM tax_rule_version v JOIN tax_rule r USING (rule_id)
                   LEFT JOIN company c ON c.company_id = v.company_id
                   WHERE r.jurisdiction_code=%s AND r.rule_code=%s AND v.version_no=%s
                     AND c.company_code IS NOT DISTINCT FROM %s""", (jur, code, vno, company_code))
    r = cur.fetchone()
    if not r:
        raise Fail(f"no version {code} v{vno}" + (f" for {company_code}" if company_code else ""))
    return r


def do_submit(code, vno, company, user, out=None):
    with connect() as con, con.cursor() as cur:
        ctx(cur, user, f"submit {code} v{vno}")
        vid, st = _version_id(cur, code, vno, company)
        # run tests against what will apply before allowing submission (FR-15)
        doc, _ = db_to_doc(cur, "TH", company, user)
        errs, _ = TR.validate(doc)
        if errs:
            raise Fail("cannot submit, rule set has errors:\n  " + "\n  ".join(errs))
        cur.execute("UPDATE tax_rule_version SET status='submitted', submitted_by=%s, submitted_at=now() "
                    "WHERE rule_version_id=%s", (user, vid))
        con.commit()
    _say(out, f"{code} v{vno} submitted by {user}")
    return f"{code} v{vno} submitted"


def do_decide(code, vno, company, user, decision, comment, out=None):
    with connect() as con, con.cursor() as cur:
        ctx(cur, user, f"{decision} {code} v{vno}: {comment or ''}")
        vid, st = _version_id(cur, code, vno, company)
        cur.execute("INSERT INTO tax_rule_approval (rule_version_id, approver, decision, comment) VALUES (%s,%s,%s,%s)",
                    (vid, user, decision, comment))
        if decision == "reject":
            cur.execute("UPDATE tax_rule_version SET status='rejected' WHERE rule_version_id=%s", (vid,))
            msg = "rejected - back to the maker"
        else:
            cur.execute("SELECT (tax_approval_policy_for(%s)).required_approvals", (vid,))
            need = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM tax_rule_approval WHERE rule_version_id=%s AND decision='approve'", (vid,))
            have = cur.fetchone()[0]
            if have >= need:
                cur.execute("UPDATE tax_rule_version SET status='approved' WHERE rule_version_id=%s", (vid,))
                msg = f"APPROVED ({have}/{need})"
            else:
                msg = f"approval recorded ({have}/{need}) - waiting for {need - have} more"
        con.commit()
    _say(out, f"{code} v{vno}: {msg}")
    return f"{code} v{vno}: {msg}"


def do_in_force(day, company):
    with connect() as con, con.cursor() as cur:
        cur.execute("SELECT company_id FROM company WHERE company_code=%s", (company,))
        cid = cur.fetchone()[0]
        cur.execute("SELECT rule_code, version_no, source, effective_from, effective_to, params "
                    "FROM tax_rules_in_force(%s, %s::smallint) ORDER BY category, rule_code", (day, cid))
        for code, v, src, f, t, p in cur.fetchall():
            short = json.dumps(p, ensure_ascii=False)
            print(f"{code:20} v{v} {src:8} {f}..{t or 'open':10}  {short[:70]}")


# =====================================================================
# Payroll (calculated from approved rules only)
# =====================================================================
def _payroll_calc(cur, company_code, pay_date: dt.date, run_id, inputs, doc, vid, rule_overrides=None,
                  employees=None):
    """Return list of computed lines. rule_overrides: doc to use instead (for what-if recalculation)."""
    comp_id = company_row(cur, company_code)[0]
    eng = TR.Engine(rule_overrides or doc, company_code, statuses={"approved"} if not rule_overrides else None)
    year, month = pay_date.year, pay_date.month
    months_remaining = 12 - month + 1
    pit_allow = [r for r in doc["rules"] if r["category"] == "PIT" and r["rule_type"] == "fixed_amount"]

    q = "SELECT employee_id, monthly_salary FROM employee WHERE company_id=%s AND is_active"
    args = [comp_id]
    if employees:
        q += " AND employee_id = ANY(%s)"
        args.append(list(employees))
    cur.execute(q + " ORDER BY employee_id", args)
    out = []
    for emp, salary in cur.fetchall():
        inp = (inputs or {}).get(emp, {})
        base = Decimal(str(inp.get("base_salary", salary)))
        ot, comm, allw, oded = (Decimal(str(inp.get(k, 0))) for k in ("overtime", "commission", "allowance",
                                                                       "other_deduction"))
        gross = base + ot + comm + allw

        cur.execute("SELECT ytd_income, ytd_withheld, ytd_sso FROM employee_tax_opening "
                    "WHERE employee_id=%s AND tax_year=%s", (emp, year))
        o = cur.fetchone() or (Decimal(0), Decimal(0), Decimal(0))
        cur.execute("""SELECT COALESCE(sum(pl.gross_pay),0), COALESCE(sum(pl.wht_pnd1),0), COALESCE(sum(pl.sso_employee),0)
                       FROM payroll_line pl JOIN payroll_run pr USING (run_id)
                       WHERE pl.employee_id=%s AND pr.status IN ('approved','paid') AND pr.run_id <> %s
                         AND EXTRACT(year FROM pr.pay_date)=%s AND pr.pay_date < %s""", (emp, run_id, year, pay_date))
        p = cur.fetchone()
        ytd_inc, ytd_wht, ytd_sso = o[0] + p[0], o[1] + p[1], o[2] + p[2]

        cur.execute("SELECT allowances, status FROM employee_tax_profile WHERE employee_id=%s AND tax_year=%s",
                    (emp, year))
        prof = cur.fetchone()
        allowances, prof_status = (prof[0], prof[1]) if prof and prof[1] == "approved" else ({}, "default")

        sso_ver = eng.version("SSO", pay_date)
        sso = eng.calc("SSO", pay_date, {"base": gross})["amount"]
        allow_detail, other_allow = {}, Decimal(0)
        for r in pit_allow:
            v_in_force = eng.version(r["code"], pay_date)
            fld = v_in_force["params"].get("input_field")
            if not fld:
                continue                              # e.g. personal allowance: part of the formula itself
            val = allowances.get(fld, 0)
            a = eng.calc(r["code"], pay_date, {"value": val})["allowance"]
            if a:
                allow_detail[r["code"]] = float(a)
                other_allow += a
        annual_sso = ytd_sso + sso * months_remaining
        pnd1_in = {"monthly_income": gross, "months_remaining": months_remaining, "ytd_income": ytd_inc,
                   "ytd_withheld": ytd_wht, "annual_sso": annual_sso, "other_allowances": other_allow}
        pnd1_ver = eng.version("PND1_MONTHLY", pay_date)
        pnd1 = eng.calc("PND1_MONTHLY", pay_date, pnd1_in)["monthly_wht"]

        def vref(code, v):
            return vid.get((code, v["company_id"], v["version"]))
        trace = {"as_of": pay_date.isoformat(), "profile_status": prof_status, "allowances": allow_detail,
                 "pnd1_inputs": {k: float(v) for k, v in pnd1_in.items()},
                 "rule_versions": {"SSO": sso_ver["version"], "PND1_MONTHLY": pnd1_ver["version"],
                                   **{c: eng.version(c, pay_date)["version"] for c in
                                      ("PIT_BRACKETS", "PIT_EXPENSE", "PIT_ALLOW_PERSONAL")}}}
        out.append({"employee_id": emp, "base_salary": base, "overtime": ot, "commission": comm, "allowance": allw,
                    "sso_employee": sso, "sso_employer": sso, "wht_pnd1": pnd1, "other_deduction": oded,
                    "sso_rule_version_id": vref("SSO", sso_ver), "pnd1_rule_version_id": vref("PND1_MONTHLY", pnd1_ver),
                    "calc_trace": trace, "gross": gross, "net": gross - sso - pnd1 - oded})
    return out


def do_payroll(run_id, pay_date, company, inputs_path, user, post, je_no, out=None, inputs=None):
    pay_date = dt.date.fromisoformat(pay_date)
    if inputs is None:
        inputs = json.loads(Path(inputs_path).read_text()) if inputs_path else {}
    with connect() as con, con.cursor() as cur:
        comp_id, _, jur = company_row(cur, company)
        ctx(cur, user, f"payroll {run_id}", company_id=comp_id)
        period = pay_date.strftime("%Y-%m")
        cur.execute("SELECT status FROM fiscal_period WHERE period_id=%s AND company_id=%s", (period, comp_id))
        pst = cur.fetchone()
        if not pst:
            raise Fail(f"no fiscal period {period}")
        cur.execute("SELECT status FROM payroll_run WHERE run_id=%s", (run_id,))
        r = cur.fetchone()
        if r and r[0] in ("approved", "paid"):
            raise Fail(f"{run_id} is {r[0]} - it cannot be recalculated")
        doc, vid = db_to_doc(cur, jur, company, user, calc=True)
        lines = _payroll_calc(cur, company, pay_date, run_id, inputs, doc, vid)
        if r:
            cur.execute("DELETE FROM payroll_line WHERE run_id=%s", (run_id,))
            cur.execute("UPDATE payroll_run SET pay_date=%s, period_id=%s WHERE run_id=%s", (pay_date, period, run_id))
        else:
            cur.execute("INSERT INTO payroll_run (run_id, period_id, pay_date, status, je_no, company_id) "
                        "VALUES (%s,%s,%s,'draft',NULL,%s)", (run_id, period, pay_date, comp_id))
        for l in lines:
            cur.execute("""INSERT INTO payroll_line (run_id, employee_id, base_salary, overtime, commission, allowance,
                             sso_employee, sso_employer, wht_pnd1, other_deduction, sso_rule_version_id,
                             pnd1_rule_version_id, calc_trace) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (run_id, l["employee_id"], l["base_salary"], l["overtime"], l["commission"], l["allowance"],
                         l["sso_employee"], l["sso_employer"], l["wht_pnd1"], l["other_deduction"],
                         l["sso_rule_version_id"], l["pnd1_rule_version_id"], Jsonb(l["calc_trace"])))
        tot = {k: sum((l[k] for l in lines), Decimal(0))
               for k in ("gross", "sso_employee", "sso_employer", "wht_pnd1", "other_deduction", "net")}
        if post:
            if pst[0] != "open":
                raise Fail(f"period {period} is {pst[0]} - cannot post payroll")
            if not je_no:
                raise Fail("--je is required with --post")
            sso_v = lines[0]["sso_rule_version_id"]
            pnd_v = lines[0]["pnd1_rule_version_id"]
            cur.execute("""INSERT INTO journal_entry (je_no, entry_date, period_id, journal_type, description,
                             source_doc, status, created_by, company_id)
                           VALUES (%s,%s,%s,'PAYROLL',%s,%s,'posted',%s,%s)""",
                        (je_no, pay_date, period, f"Salary {pay_date:%B %Y} (calculated from tax rules)", run_id,
                         user, comp_id))
            jl = [("6110", tot["gross"], 0, "Gross salary + OT + commission + allowance", None),
                  ("6120", tot["sso_employer"], 0, f"Employer SSO ({len(lines)} employees)", sso_v),
                  ("2130", 0, tot["wht_pnd1"], "PND1 withheld", pnd_v),
                  ("2140", 0, tot["sso_employee"] + tot["sso_employer"], "SSO employee + employer", sso_v),
                  ("2160", 0, tot["other_deduction"], "Other employee deductions", None),
                  ("1120", 0, tot["net"], "Net pay transferred", None)]
            n = 0
            for acc, dr, cr, memo, tv in jl:
                if dr == 0 and cr == 0:
                    continue
                n += 1
                cur.execute("""INSERT INTO journal_line (je_no, line_no, account_code, debit, credit, memo,
                                 tax_rule_version_id) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                            (je_no, n, acc, dr, cr, memo, tv))
            cur.execute("UPDATE payroll_run SET status='paid', je_no=%s WHERE run_id=%s", (je_no, run_id))
        con.commit()
    _say(out, f"{run_id} ({pay_date}): {len(lines)} employees  gross {tot['gross']:,.2f}  SSO {tot['sso_employee']:,.2f} x2  "
          f"PND1 {tot['wht_pnd1']:,.2f}  net {tot['net']:,.2f}  -> {'posted ' + je_no if post else 'draft'}")
    for l in lines:
        _say(out, f"   {l['employee_id']}  gross {l['gross']:>10,.2f}  sso {l['sso_employee']:>7,.2f}  "
              f"pnd1 {l['wht_pnd1']:>9,.2f}  net {l['net']:>10,.2f}  [{l['calc_trace']['profile_status']}]")
    return {"run_id": run_id, "posted": bool(post), "je_no": je_no if post else None,
            "totals": {k: float(v) for k, v in tot.items()}}


def do_remit(period, date, je_no, company, user, out=None):
    with connect() as con, con.cursor() as cur:
        comp_id = company_row(cur, company)[0]
        ctx(cur, user, f"remit {period}", company_id=comp_id)
        cur.execute("SELECT end_date FROM fiscal_period WHERE period_id=%s AND company_id=%s", (period, comp_id))
        end = cur.fetchone()[0]
        d = dt.date.fromisoformat(date)
        cur.execute("""SELECT l.account_code, sum(l.credit - l.debit)
                       FROM journal_line l JOIN journal_entry e USING (je_no)
                       WHERE e.status='posted' AND e.company_id=%s AND e.entry_date <= %s
                         AND l.account_code IN ('2130','2131','2140')
                       GROUP BY 1 HAVING sum(l.credit - l.debit) <> 0 ORDER BY 1""", (comp_id, end))
        bal = cur.fetchall()
        if not bal:
            raise Fail("nothing to remit")
        cur.execute("""INSERT INTO journal_entry (je_no, entry_date, period_id, journal_type, description, status,
                         created_by, company_id) VALUES (%s,%s,%s,'PAYMENT',%s,'posted',%s,%s)""",
                    (je_no, d, d.strftime("%Y-%m"), f"Remit SSO + PND1 + PND3 for {period}", user, comp_id))
        memo = {"2130": "PND1", "2131": "PND3/53", "2140": "SSO"}
        total = Decimal(0)
        for i, (acc, amt) in enumerate(bal, start=1):
            cur.execute("INSERT INTO journal_line (je_no, line_no, account_code, debit, credit, memo) "
                        "VALUES (%s,%s,%s,%s,0,%s)", (je_no, i, acc, amt, f"{memo[acc]} {period}"))
            total += amt
        cur.execute("INSERT INTO journal_line (je_no, line_no, account_code, debit, credit, memo) "
                    "VALUES (%s,%s,'1120',0,%s,'Bank')", (je_no, len(bal) + 1, total))
        con.commit()
    _say(out, f"{je_no}: remitted {total:,.2f} " + ", ".join(f"{memo[a]} {v:,.2f}" for a, v in bal))
    return {"je_no": je_no, "total": float(total)}


# =====================================================================
# Back-dating report (FR-09)
# =====================================================================
def do_recalc_report(code, vno, company, user, out=None):
    with connect() as con, con.cursor() as cur:
        vid_, st = _version_id(cur, code, vno, company)
        cur.execute("""SELECT r.category, v.effective_from, c.company_id, c.company_code, c.jurisdiction_code
                       FROM tax_rule_version v JOIN tax_rule r USING (rule_id)
                       JOIN company c ON c.jurisdiction_code = r.jurisdiction_code
                       WHERE v.rule_version_id=%s AND (v.company_id IS NULL OR c.company_id = v.company_id)""", (vid_,))
        targets = cur.fetchall()
        ids = []
        for cat, eff, comp_id, comp_code, jur in targets:
            ctx(cur, user, f"recalc report {code} v{vno}", company_id=comp_id)
            cur.execute("SELECT max(end_date) FROM fiscal_period WHERE company_id=%s AND status IN ('closed','locked')",
                        (comp_id,))
            closed_end = cur.fetchone()[0]
            if not closed_end or eff > closed_end:
                _say(out, f"{comp_code}: {code} v{vno} starts {eff}, after the last closed period - no report needed")
                continue
            cur.execute("SELECT mode FROM retro_policy WHERE category=%s AND (company_id=%s OR company_id IS NULL) "
                        "ORDER BY company_id NULLS LAST LIMIT 1", (cat, comp_id))
            mode = (cur.fetchone() or ["block"])[0]
            if mode == "block":
                raise Fail(f"{comp_code}: back-dating policy for {cat} is 'block' - this version can't be approved")
            doc, vid = db_to_doc(cur, jur, comp_code, user, calc=True)
            planned, _ = db_to_doc(cur, jur, comp_code, user)
            # what-if rule set: approved versions (with planned closes) + this candidate
            whatif = json.loads(json.dumps(planned))
            for r in whatif["rules"]:
                r["versions"] = [v for v in r["versions"] if v["status"] == "approved"
                                 or (r["code"] == code and v["version"] == vno)]
            report, diff_total = [], Decimal(0)
            if cat in ("SSO", "PIT"):
                cur.execute("""SELECT pr.run_id, pr.pay_date, array_agg(pl.employee_id ORDER BY pl.employee_id)
                               FROM payroll_run pr JOIN payroll_line pl USING (run_id)
                               WHERE pr.company_id=%s AND pr.status IN ('approved','paid')
                                 AND pr.pay_date BETWEEN %s AND %s GROUP BY 1,2 ORDER BY 2""", (comp_id, eff, closed_end))
                for run_id, pdate, emps in cur.fetchall():
                    cur.execute("""SELECT employee_id, base_salary, overtime, commission, allowance, other_deduction,
                                          sso_employee, wht_pnd1 FROM payroll_line WHERE run_id=%s""", (run_id,))
                    old = {e: row for e, *row in cur.fetchall()}
                    inputs = {e: {"base_salary": r_[0], "overtime": r_[1], "commission": r_[2], "allowance": r_[3],
                                  "other_deduction": r_[4]} for e, r_ in old.items()}
                    new_lines = _payroll_calc(cur, comp_code, pdate, run_id + "#whatif", inputs, doc, vid,
                                              rule_overrides=whatif, employees=emps)
                    for l in new_lines:
                        o = old[l["employee_id"]]
                        d_sso, d_pnd = l["sso_employee"] - o[5], l["wht_pnd1"] - o[6]
                        if d_sso or d_pnd:
                            report.append({"run_id": run_id, "employee_id": l["employee_id"],
                                           "sso_old": float(o[5]), "sso_new": float(l["sso_employee"]),
                                           "pnd1_old": float(o[6]), "pnd1_new": float(l["wht_pnd1"])})
                            diff_total += d_sso * 2 + d_pnd
            cur.execute("""INSERT INTO tax_recalc_run (trigger_version_id, company_id, mode, period_from, period_to,
                             report, total_difference, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                           RETURNING recalc_id""",
                        (vid_, comp_id, mode, eff, closed_end, Jsonb(report), diff_total, user))
            rid = cur.fetchone()[0]
            ids.append(rid)
            _say(out, f"recalc report {rid} [{comp_code}, mode {mode}] {code} v{vno}: {len(report)} line(s) change, "
                  f"net difference {diff_total:,.2f}")
            for x in report:
                _say(out, f"   {x['run_id']} {x['employee_id']}: SSO {x['sso_old']:,.2f} -> {x['sso_new']:,.2f}  "
                      f"PND1 {x['pnd1_old']:,.2f} -> {x['pnd1_new']:,.2f}")
        con.commit()
    return ids


def do_confirm_recalc(recalc_id, user, out=None):
    with connect() as con, con.cursor() as cur:
        ctx(cur, user, f"confirm recalc {recalc_id}")
        cur.execute("""UPDATE tax_recalc_run SET status='confirmed', confirmed_by=%s, confirmed_at=now()
                       WHERE recalc_id=%s AND status='pending' AND created_by <> %s RETURNING recalc_id""",
                    (user, recalc_id, user))
        if not cur.fetchone():
            raise Fail("recalc report not found, not pending, or you created it (needs a second person)")
        con.commit()
    _say(out, f"recalc report {recalc_id} confirmed by {user}")
    return f"recalc report {recalc_id} confirmed"


# =====================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", default=os.environ.get("ERP_USER", "napat"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("export"); s.add_argument("out"); s.add_argument("--jurisdiction", default="TH"); s.add_argument("--company")
    s = sub.add_parser("import"); s.add_argument("file"); s.add_argument("--bootstrap", action="store_true")
    for name in ("submit", "approve", "reject", "recalc-report"):
        s = sub.add_parser(name); s.add_argument("rule"); s.add_argument("version", type=int)
        s.add_argument("--company"); s.add_argument("--comment")
    s = sub.add_parser("confirm-recalc"); s.add_argument("recalc_id", type=int)
    s = sub.add_parser("in-force"); s.add_argument("date"); s.add_argument("--company", default="DEMO01")
    s = sub.add_parser("payroll"); s.add_argument("run_id"); s.add_argument("--pay-date", required=True)
    s.add_argument("--company", default="DEMO01"); s.add_argument("--inputs"); s.add_argument("--post", action="store_true")
    s.add_argument("--je")
    s = sub.add_parser("remit"); s.add_argument("--period", required=True); s.add_argument("--date", required=True)
    s.add_argument("--je", required=True); s.add_argument("--company", default="DEMO01")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "export":
            with connect() as con, con.cursor() as cur:
                doc, _ = db_to_doc(cur, a.jurisdiction, a.company, a.user)
            (TR.write_xlsx if a.out.lower().endswith(".xlsx") else TR.save_json)(doc, a.out)
            print(f"wrote {a.out}  ({len(doc['rules'])} rules, base_checksum {doc['base_checksum'][:19]}...)")
        elif a.cmd == "import":
            do_import(a.file, a.user, a.bootstrap)
        elif a.cmd == "submit":
            do_submit(a.rule, a.version, a.company, a.user)
        elif a.cmd in ("approve", "reject"):
            if a.cmd == "reject" and not a.comment:
                raise Fail("--comment is required to reject")
            do_decide(a.rule, a.version, a.company, a.user, a.cmd, a.comment)
        elif a.cmd == "recalc-report":
            do_recalc_report(a.rule, a.version, a.company, a.user)
        elif a.cmd == "confirm-recalc":
            do_confirm_recalc(a.recalc_id, a.user)
        elif a.cmd == "in-force":
            do_in_force(a.date, a.company)
        elif a.cmd == "payroll":
            do_payroll(a.run_id, a.pay_date, a.company, a.inputs, a.user, a.post, a.je)
        elif a.cmd == "remit":
            do_remit(a.period, a.date, a.je, a.company, a.user)
        return 0
    except Fail as e:
        print(f"ERROR  {e}")
        return 1
    except psycopg.Error as e:
        msg = (e.diag.message_primary if getattr(e, "diag", None) and e.diag.message_primary else str(e))
        print(f"ERROR  (database) {msg}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
