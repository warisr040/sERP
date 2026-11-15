#!/usr/bin/env python3
"""
sERP — simply handle IT ERP · application server  (version 1.0)

Serves the single-page app (web/index.html) and a JSON API on 127.0.0.1, backed by PostgreSQL.
Tax amounts (VAT, withholding, social security, salary tax) always come from the approved tax rules.

  python serp_server.py              start and open the app window
  python serp_server.py --no-window  start the server only (Docker, testing)

Settings: serp.config.json next to this file (created by the first-run screen) or env SERP_DSN / SERP_PORT.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import urllib.parse
import webbrowser
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "engine"))

import psycopg                      # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo   # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

import taxdb as TD                  # noqa: E402
import taxrules as TR               # noqa: E402

VERSION = "1.0.0"
CONFIG = HERE / "serp.config.json"
WEB = HERE / "web"
DEFAULT_PORT = 47394


# =====================================================================
# Config / connection
# =====================================================================
def load_config():
    cfg = {"dsn": None, "port": DEFAULT_PORT}
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
        except ValueError:
            pass
    if os.environ.get("SERP_DSN"):
        cfg["dsn"] = os.environ["SERP_DSN"]
    if os.environ.get("SERP_PORT"):
        cfg["port"] = int(os.environ["SERP_PORT"])
    return cfg


CFG = load_config()


def set_dsn(dsn):
    CFG["dsn"] = dsn
    os.environ["ERP_DSN"] = dsn or ""


set_dsn(CFG["dsn"])


def connect():
    if not CFG["dsn"]:
        raise ApiError(503, "Database not configured", code="setup")
    return psycopg.connect(CFG["dsn"], options="-c search_path=erp,public", connect_timeout=5)


class ApiError(Exception):
    def __init__(self, status, message, code=None, details=None):
        super().__init__(message)
        self.status, self.message, self.code, self.details = status, message, code, details


def jdefault(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    if isinstance(o, (set, tuple)):
        return list(o)
    raise TypeError(type(o).__name__)


def rows(cur):
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def one(cur):
    r = rows(cur)
    return r[0] if r else None


def D(x):
    return Decimal(str(x or 0))


def q2(x):
    return D(x).quantize(Decimal("0.01"))


def parse_date(s, name="date"):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        raise ApiError(400, f"Invalid {name}: {s!r}")


# =====================================================================
# Context helpers
# =====================================================================
class Ctx:
    def __init__(self, handler, query, body):
        self.h, self.q, self.body = handler, query, body
        self.user = handler.headers.get("X-ERP-User") or "napat"
        try:
            self.company_id = int(handler.headers.get("X-ERP-Company") or query.get("company") or 1)
        except ValueError:
            self.company_id = 1

    def arg(self, k, default=None):
        return self.q.get(k, default)


def company_info(cur, cid):
    cur.execute("SELECT company_id, company_code, name_en, name_th, tax_id, base_currency, jurisdiction_code, address "
                "FROM company WHERE company_id=%s", (cid,))
    c = one(cur)
    if not c:
        raise ApiError(404, f"Company {cid} not found")
    return c


def check_user(cur, user, need_roles=None):
    cur.execute("SELECT username, full_name, roles FROM erp_user WHERE username=%s AND is_active", (user,))
    u = one(cur)
    if not u:
        raise ApiError(403, f"Unknown user {user}")
    if need_roles and not (set(u["roles"]) & set(need_roles)):
        raise ApiError(403, f"{u['full_name']} needs one of the roles: {', '.join(need_roles)}")
    return u


def set_ctx(cur, ctx: Ctx, reason=None):
    TD.ctx(cur, ctx.user, reason, company_id=ctx.company_id)


def period_of(cur, cid, day):
    cur.execute("SELECT period_id, status FROM fiscal_period WHERE company_id=%s AND %s BETWEEN start_date AND end_date",
                (cid, day))
    p = cur.fetchone()
    if not p:
        raise ApiError(400, f"No accounting period covers {day}. Create it under Settings → Periods.")
    return p


def require_open(cur, cid, day):
    pid, st = period_of(cur, cid, day)
    if st != "open":
        raise ApiError(409, f"Period {pid} is {st}. Reopen it in Settings → Periods to post on {day}.")
    return pid


def next_no(cur, table, col, prefix):
    cur.execute(f"SELECT {col} FROM {table} WHERE {col} LIKE %s", (prefix + "%",))
    n = 0
    for (v,) in cur.fetchall():
        m = re.search(r"(\d+)$", v)
        if m:
            n = max(n, int(m.group(1)))
    return f"{prefix}{n + 1:03d}"


def engine_for(cur, cid):
    c = company_info(cur, cid)
    doc, vid = TD.db_to_doc(cur, c["jurisdiction_code"], c["company_code"], calc=True)
    return TR.Engine(doc, c["company_code"], statuses={"approved"}), doc, vid, c


def insert_je(cur, cid, je_no, day, period, jtype, desc, source, status, user, lines):
    cur.execute("""INSERT INTO journal_entry (je_no, entry_date, period_id, journal_type, description, source_doc,
                     status, created_by, company_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (je_no, day, period, jtype, desc, source, status, user, cid))
    n = 0
    for l in lines:
        dr, cr = q2(l.get("debit")), q2(l.get("credit"))
        if dr == 0 and cr == 0:
            continue
        n += 1
        cur.execute("""INSERT INTO journal_line (je_no, line_no, account_code, partner_id, employee_id, debit, credit,
                         memo, tax_rule_version_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (je_no, n, l["account_code"], l.get("partner_id"), l.get("employee_id"), dr, cr,
                     l.get("memo"), l.get("tax_rule_version_id")))
    return n


# =====================================================================
# API: status / setup
# =====================================================================
def dsn_display(dsn):
    if not dsn:
        return None
    d = conninfo_to_dict(dsn)
    d.pop("password", None)
    return " ".join(f"{k}={v}" for k, v in d.items())


def api_status(ctx):
    out = {"version": VERSION, "configured": bool(CFG["dsn"]), "dsn": dsn_display(CFG["dsn"]),
           "db": "none", "schema": False, "server_date": dt.date.today().isoformat()}
    if not CFG["dsn"]:
        return out
    try:
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT version()")
            out["db_version"] = cur.fetchone()[0].split(",")[0]
            cur.execute("SELECT to_regclass('erp.tax_rule') IS NOT NULL")
            out["schema"] = cur.fetchone()[0]
            out["db"] = "ok"
    except psycopg.Error as e:
        out["db"] = "error"
        out["error"] = str(e).strip().splitlines()[0]
        out["tips"] = db_error_tips(str(e), CFG["dsn"])
    return out


def db_error_tips(msg, dsn=None):
    """Turn a PostgreSQL connection error into plain next steps for the setup screen."""
    m = (msg or "").lower()
    d = conninfo_to_dict(dsn) if dsn else {}
    host, port = d.get("host") or "localhost", d.get("port") or "5432"
    local = host in ("localhost", "127.0.0.1", "::1") or host.startswith("/")
    if "timeout expired" in m or "connection refused" in m or "could not connect" in m or "is the server running" in m:
        tips = []
        if local:
            tips += ["PostgreSQL may not be installed on this computer. Install it (version 16 or newer) - see the quick guide, section 'Install PostgreSQL'.",
                     "If it is installed, the service may be stopped: press Win+R, type services.msc, find 'postgresql-x64-NN' and click Start (set Startup type to Automatic)."]
        else:
            tips += [f"Check that the server '{host}' is switched on and reachable from this computer (try: ping {host}).",
                     "On the database server, PostgreSQL must listen on the network (listen_addresses in postgresql.conf) and allow this computer in pg_hba.conf."]
        tips += [f"Check the port: PostgreSQL's default is 5432; you entered {port}. A second PostgreSQL install often uses 5433.",
                 "Try 127.0.0.1 instead of localhost as the host.",
                 "A firewall or antivirus may block the port - allow PostgreSQL or the port in Windows Defender Firewall."]
        return tips
    if "password authentication failed" in m or "no password supplied" in m:
        return ["The password is wrong or empty. Use the password you chose for the 'postgres' user when installing PostgreSQL.",
                "Forgot it? Reinstalling is easiest on a new computer; otherwise reset it with pgAdmin (Login/Group Roles -> postgres -> Definition)."]
    if "role" in m and "does not exist" in m:
        return ["That user does not exist. Use 'postgres' (created by the installer) or a user your administrator created."]
    if "could not translate host name" in m or "name or service not known" in m or "nodename nor servname" in m:
        return [f"The host name '{host}' was not found. Check the spelling, or use localhost for this computer."]
    if "permission denied to create database" in m:
        return ["This user may not create databases. Create the database in pgAdmin first, or use the 'postgres' user."]
    if "does not exist" in m and "database" in m:
        return ["The database does not exist yet - click 'Create sample database' to create it."]
    return ["Check host, port, user and password, and that PostgreSQL is running.",
            "The quick guide (section 'Install PostgreSQL' and Troubleshooting) has step-by-step help."]


def api_setup(ctx):
    b = ctx.body or {}
    if b.get("dsn"):
        dsn = b["dsn"]
    else:
        parts = {k: b.get(k) for k in ("host", "port", "dbname", "user", "password") if b.get(k)}
        dsn = make_conninfo(**parts)
    action = b.get("action", "test")
    target = conninfo_to_dict(dsn).get("dbname") or "postgres"
    msgs = []
    try:
        try:
            con = psycopg.connect(dsn, connect_timeout=5, autocommit=True)
        except psycopg.OperationalError as e:
            if "does not exist" in str(e) and action == "test":
                admin = make_conninfo(dsn, dbname="postgres")
                with psycopg.connect(admin, connect_timeout=5):
                    pass
                return {"ok": True, "schema": False,
                        "message": f"Server reachable. Database '{target}' does not exist yet - 'Create sample database' will create it."}
            if "does not exist" in str(e) and action == "create_sample":
                admin = make_conninfo(dsn, dbname="postgres")
                with psycopg.connect(admin, connect_timeout=5, autocommit=True) as a:
                    a.execute(f'CREATE DATABASE "{target}"')
                msgs.append(f"Created database {target}")
                con = psycopg.connect(dsn, connect_timeout=5, autocommit=True)
            else:
                raise
        with con:
            cur = con.cursor()
            cur.execute("SELECT to_regclass('erp.tax_rule') IS NOT NULL")
            has_schema = cur.fetchone()[0]
            if action == "test":
                return {"ok": True, "schema": has_schema,
                        "message": "Connected. " + ("sERP data found." if has_schema else "Database is empty - create the sample.")}
            if action == "create_sample":
                if has_schema and not b.get("replace"):
                    raise ApiError(409, "This database already has sERP data. Tick 'replace' to rebuild it.")
                if has_schema:
                    cur.execute("DROP SCHEMA erp CASCADE")
                sql = (HERE / "engine" / "erp_sample_v2.sql").read_text(encoding="utf-8")
                sql = "\n".join(l for l in sql.splitlines() if not l.startswith("\\"))   # psql-only lines
                cur.execute(sql)
                msgs.append("Loaded sample company, Thai tax rules and payroll")
                cur.execute("SELECT to_regclass('erp.tax_rule') IS NOT NULL")
                has_schema = cur.fetchone()[0]
            if action in ("save", "create_sample"):
                if not has_schema:
                    raise ApiError(409, "This database has no sERP data yet - use 'Create sample database'.")
                CFG["dsn"] = dsn
                CONFIG.write_text(json.dumps({"dsn": dsn, "port": CFG["port"]}, indent=2), encoding="utf-8")
                set_dsn(dsn)
                msgs.append("Settings saved")
            return {"ok": True, "schema": has_schema, "message": ". ".join(msgs)}
    except psycopg.Error as e:
        msg = str(e).strip().splitlines()[0]
        low = str(e).lower()
        if "timeout expired" in low or "connection refused" in low or "is the server running" in low:
            d = conninfo_to_dict(dsn)
            why = "connection timeout expired" if "timeout" in low else "connection refused"
            msg = f"PostgreSQL did not answer at {d.get('host') or 'localhost'}:{d.get('port') or 5432} ({why})"
        raise ApiError(400, msg, code="db", details={"tips": db_error_tips(str(e), dsn)})


# =====================================================================
# API: meta / dashboard
# =====================================================================
def api_meta(ctx):
    with connect() as con, con.cursor() as cur:
        c = company_info(cur, ctx.company_id)
        cur.execute("SELECT company_id, company_code, name_en FROM company ORDER BY company_id")
        companies = rows(cur)
        cur.execute("SELECT username, full_name, roles FROM erp_user WHERE is_active ORDER BY username")
        users = rows(cur)
        cur.execute("SELECT period_id, start_date, end_date, status FROM fiscal_period WHERE company_id=%s "
                    "ORDER BY start_date", (ctx.company_id,))
        periods = rows(cur)
        cur.execute("SELECT account_code, name_en, name_th, account_type, normal_side, parent_code, is_postable "
                    "FROM account WHERE is_active ORDER BY account_code")
        accounts = rows(cur)
        cur.execute("SELECT partner_id, partner_type, name, tax_id, payment_terms_days FROM partner "
                    "WHERE company_id=%s ORDER BY partner_id", (ctx.company_id,))
        partners = rows(cur)
        cur.execute("SELECT employee_id, first_name, last_name, dept_code, position, monthly_salary, is_active "
                    "FROM employee WHERE company_id=%s ORDER BY employee_id", (ctx.company_id,))
        employees = rows(cur)
    return {"company": c, "companies": companies, "users": users, "periods": periods, "accounts": accounts,
            "partners": partners, "employees": employees, "version": VERSION}


def _balances(cur, cid, upto, accounts=None, since=None):
    q = """SELECT l.account_code, sum(l.debit) d, sum(l.credit) c FROM journal_line l JOIN journal_entry e USING (je_no)
           WHERE e.status='posted' AND e.company_id=%s AND e.entry_date <= %s"""
    a = [cid, upto]
    if since:
        q += " AND e.entry_date >= %s"
        a.append(since)
    if accounts:
        q += " AND l.account_code = ANY(%s)"
        a.append(accounts)
    cur.execute(q + " GROUP BY 1", a)
    return {k: (D(d), D(c)) for k, d, c in cur.fetchall()}


def _open_docs(cur, cid, doc_type, as_of):
    cur.execute("""SELECT i.doc_no, i.doc_type, i.partner_id, p.name partner, i.doc_date, i.due_date, i.subtotal,
                          i.vat_amount, i.total, i.status, i.je_no, i.vat_rate,
                          COALESCE((SELECT sum(amount) FROM payment_allocation pa WHERE pa.doc_no=i.doc_no),0) settled
                   FROM invoice i JOIN partner p USING (partner_id)
                   WHERE i.company_id=%s AND i.doc_type = ANY(%s) AND i.status <> 'void'
                   ORDER BY i.doc_date DESC, i.doc_no DESC""", (cid, doc_type))
    out = rows(cur)
    for r in out:
        r["outstanding"] = D(r["total"]) - D(r["settled"])
        r["days_overdue"] = max((as_of - r["due_date"]).days, 0) if r["outstanding"] > 0 and r["status"] != "draft" else 0
    return out


def api_dashboard(ctx):
    as_of = parse_date(ctx.arg("as_of") or dt.date.today().isoformat(), "as_of")
    with connect() as con, con.cursor() as cur:
        cid = ctx.company_id
        pid, _ = period_of(cur, cid, as_of)
        cur.execute("SELECT start_date, end_date FROM fiscal_period WHERE company_id=%s AND period_id=%s", (cid, pid))
        p_start, p_end = cur.fetchone()
        bal = _balances(cur, cid, as_of)
        accts = {}
        cur.execute("SELECT account_code, account_type, normal_side FROM account")
        for k, t, ns in cur.fetchall():
            accts[k] = (t, ns)

        def b(code):
            d, c = bal.get(code, (Decimal(0), Decimal(0)))
            return d - c if accts.get(code, ("", "D"))[1] == "D" else c - d
        cash = b("1110") + b("1120") + b("1130")
        ar = [r for r in _open_docs(cur, cid, ["AR"], as_of) if r["outstanding"] > 0 and r["status"] != "draft"]
        ap = [r for r in _open_docs(cur, cid, ["AP"], as_of) if r["outstanding"] > 0 and r["status"] != "draft"]
        per = _balances(cur, cid, p_end, since=p_start)
        rev = sum((c - d for k, (d, c) in per.items() if accts.get(k, ("",))[0] == "revenue"), Decimal(0))
        exp = sum((d - c for k, (d, c) in per.items() if accts.get(k, ("",))[0] == "expense"), Decimal(0))
        tax_due = {"VAT (PP30)": b("2120") - b("1220"), "PND1 salary tax": b("2130"),
                   "PND3/53 withholding": b("2131"), "Social security": b("2140")}
        # monthly series
        cur.execute("SELECT period_id, start_date, end_date, status FROM fiscal_period WHERE company_id=%s ORDER BY start_date",
                    (cid,))
        series = []
        for ppid, s, e, st in cur.fetchall():
            pb = _balances(cur, cid, e, since=s)
            r_ = sum((c - d for k, (d, c) in pb.items() if accts.get(k, ("",))[0] == "revenue"), Decimal(0))
            e_ = sum((d - c for k, (d, c) in pb.items() if accts.get(k, ("",))[0] == "expense"), Decimal(0))
            series.append({"period": ppid, "status": st, "revenue": r_, "expense": e_})
        cur.execute("""SELECT e.je_no, e.entry_date, e.journal_type, e.description, e.status,
                              COALESCE(sum(l.debit),0) amount
                       FROM journal_entry e LEFT JOIN journal_line l USING (je_no)
                       WHERE e.company_id=%s GROUP BY e.je_no ORDER BY e.entry_date DESC, e.je_no DESC LIMIT 8""", (cid,))
        recent = rows(cur)
        cur.execute("""SELECT r.rule_code, v.version_no, v.status, r.category, v.effective_from,
                              (SELECT count(*) FROM tax_rule_approval a WHERE a.rule_version_id=v.rule_version_id
                                 AND a.decision='approve') approvals,
                              (tax_approval_policy_for(v.rule_version_id)).required_approvals required
                       FROM tax_rule_version v JOIN tax_rule r USING (rule_id)
                       WHERE v.status IN ('draft','submitted') ORDER BY v.status DESC, r.rule_code""")
        pending_rules = rows(cur)
        cur.execute("SELECT run_id, pay_date, status FROM payroll_run WHERE company_id=%s AND status='draft' ORDER BY pay_date",
                    (cid,))
        pending_payroll = rows(cur)
        cur.execute("SELECT count(*) FROM journal_entry WHERE company_id=%s AND status='draft'", (cid,))
        draft_je = cur.fetchone()[0]
        cur.execute("SELECT sum(debit) = sum(credit) FROM journal_line l JOIN journal_entry e USING (je_no) "
                    "WHERE e.status='posted' AND e.company_id=%s", (cid,))
        balanced = cur.fetchone()[0]
    return {"as_of": as_of, "period": pid, "period_start": p_start, "period_end": p_end,
            "kpi": {"cash": cash, "ar": sum((r["outstanding"] for r in ar), Decimal(0)),
                    "ap": sum((r["outstanding"] for r in ap), Decimal(0)),
                    "revenue": rev, "expense": exp, "profit": rev - exp,
                    "ar_overdue": sum((r["outstanding"] for r in ar if r["days_overdue"] > 0), Decimal(0))},
            "tax_due": tax_due, "series": series, "recent": recent, "open_ar": ar[:6], "open_ap": ap[:6],
            "pending_rules": pending_rules, "pending_payroll": pending_payroll, "draft_je": draft_je,
            "balanced": balanced}


# =====================================================================
# API: journal
# =====================================================================
def api_journal_list(ctx):
    with connect() as con, con.cursor() as cur:
        q = """SELECT e.je_no, e.entry_date, e.period_id, e.journal_type, e.description, e.source_doc, e.status,
                      e.created_by, COALESCE(sum(l.debit),0) debit, COALESCE(sum(l.credit),0) credit, count(l.*) n_lines
               FROM journal_entry e LEFT JOIN journal_line l USING (je_no) WHERE e.company_id=%s"""
        a = [ctx.company_id]
        if ctx.arg("period"):
            q += " AND e.period_id=%s"
            a.append(ctx.arg("period"))
        if ctx.arg("type"):
            q += " AND e.journal_type=%s"
            a.append(ctx.arg("type"))
        if ctx.arg("status"):
            q += " AND e.status=%s"
            a.append(ctx.arg("status"))
        if ctx.arg("q"):
            q += " AND (e.je_no ILIKE %s OR e.description ILIKE %s OR e.source_doc ILIKE %s)"
            a += [f"%{ctx.arg('q')}%"] * 3
        cur.execute(q + " GROUP BY e.je_no ORDER BY e.entry_date DESC, e.je_no DESC", a)
        return {"entries": rows(cur)}


def api_journal_get(ctx, je_no):
    with connect() as con, con.cursor() as cur:
        cur.execute("SELECT * FROM journal_entry WHERE je_no=%s AND company_id=%s", (je_no, ctx.company_id))
        h = one(cur)
        if not h:
            raise ApiError(404, f"{je_no} not found")
        cur.execute("""SELECT l.line_no, l.account_code, a.name_en account_name, l.partner_id, p.name partner,
                              l.employee_id, l.debit, l.credit, l.memo,
                              r.rule_code tax_rule, v.version_no tax_version
                       FROM journal_line l JOIN account a USING (account_code)
                       LEFT JOIN partner p ON p.partner_id = l.partner_id
                       LEFT JOIN tax_rule_version v ON v.rule_version_id = l.tax_rule_version_id
                       LEFT JOIN tax_rule r ON r.rule_id = v.rule_id
                       WHERE l.je_no=%s ORDER BY l.line_no""", (je_no,))
        h["lines"] = rows(cur)
        cur.execute("SELECT je_no FROM journal_entry WHERE description = %s AND company_id=%s",
                    (f"Reversal of {je_no}", ctx.company_id))
        r = cur.fetchone()
        h["reversed_by"] = r[0] if r else None
        return h


def api_journal_create(ctx):
    b = ctx.body or {}
    day = parse_date(b.get("entry_date"), "entry_date")
    lines = [l for l in b.get("lines", []) if l.get("account_code") and (D(l.get("debit")) or D(l.get("credit")))]
    if len(lines) < 2:
        raise ApiError(400, "A journal entry needs at least two lines with amounts")
    for l in lines:
        if D(l.get("debit")) < 0 or D(l.get("credit")) < 0:
            raise ApiError(400, "Amounts cannot be negative")
        if D(l.get("debit")) and D(l.get("credit")):
            raise ApiError(400, f"Line {l['account_code']}: use either debit or credit, not both")
    dr = sum(q2(l.get("debit")) for l in lines)
    cr = sum(q2(l.get("credit")) for l in lines)
    status = "posted" if b.get("post") else "draft"
    if status == "posted" and dr != cr:
        raise ApiError(400, f"Not balanced: debit {dr:,.2f} vs credit {cr:,.2f}")
    if not (b.get("description") or "").strip():
        raise ApiError(400, "Description is required")
    with connect() as con, con.cursor() as cur:
        set_ctx(cur, ctx, "journal entry")
        check_user(cur, ctx.user, ["accountant", "tax_admin", "payroll", "finance_manager", "md"])
        pid = require_open(cur, ctx.company_id, day) if status == "posted" else period_of(cur, ctx.company_id, day)[0]
        cur.execute("SELECT account_code FROM account WHERE account_code = ANY(%s) AND is_postable",
                    ([l["account_code"] for l in lines],))
        ok = {r[0] for r in cur.fetchall()}
        bad = [l["account_code"] for l in lines if l["account_code"] not in ok]
        if bad:
            raise ApiError(400, f"Not a postable account: {', '.join(bad)}")
        je = next_no(cur, "journal_entry", "je_no", f"JE-{day:%y%m}-")
        insert_je(cur, ctx.company_id, je, day, pid, b.get("journal_type") or "GJ", b["description"].strip(),
                  b.get("source_doc"), status, ctx.user, lines)
        con.commit()
    return {"je_no": je, "status": status}


def api_journal_action(ctx, je_no, action):
    with connect() as con, con.cursor() as cur:
        set_ctx(cur, ctx, f"journal {action}")
        check_user(cur, ctx.user, ["accountant", "tax_admin", "payroll", "finance_manager", "md"])
        cur.execute("SELECT * FROM journal_entry WHERE je_no=%s AND company_id=%s FOR UPDATE", (je_no, ctx.company_id))
        h = one(cur)
        if not h:
            raise ApiError(404, f"{je_no} not found")
        if action == "post":
            if h["status"] != "draft":
                raise ApiError(409, f"{je_no} is {h['status']}")
            require_open(cur, ctx.company_id, h["entry_date"])
            cur.execute("UPDATE journal_entry SET status='posted' WHERE je_no=%s", (je_no,))
            msg = f"{je_no} posted"
        elif action == "delete":
            if h["status"] != "draft":
                raise ApiError(409, "Only drafts can be deleted - reverse a posted entry instead")
            cur.execute("DELETE FROM journal_entry WHERE je_no=%s", (je_no,))
            msg = f"{je_no} deleted"
        elif action == "reverse":
            if h["status"] != "posted":
                raise ApiError(409, "Only posted entries can be reversed")
            day = parse_date((ctx.body or {}).get("date") or dt.date.today().isoformat())
            pid = require_open(cur, ctx.company_id, day)
            cur.execute("SELECT 1 FROM journal_entry WHERE description=%s AND company_id=%s",
                        (f"Reversal of {je_no}", ctx.company_id))
            if cur.fetchone():
                raise ApiError(409, f"{je_no} is already reversed")
            cur.execute("SELECT account_code, partner_id, employee_id, debit, credit, memo, tax_rule_version_id "
                        "FROM journal_line WHERE je_no=%s ORDER BY line_no", (je_no,))
            lines = [{"account_code": a, "partner_id": p, "employee_id": e, "debit": c, "credit": d, "memo": m,
                      "tax_rule_version_id": t} for a, p, e, d, c, m, t in cur.fetchall()]
            new = next_no(cur, "journal_entry", "je_no", f"JE-{day:%y%m}-")
            insert_je(cur, ctx.company_id, new, day, pid, "ADJ", f"Reversal of {je_no}", je_no, "posted", ctx.user, lines)
            msg = f"{je_no} reversed by {new}"
        else:
            raise ApiError(404, "Unknown action")
        con.commit()
    return {"message": msg}


# =====================================================================
# API: invoices + payments (VAT and WHT from the rule engine)
# =====================================================================
def api_invoices(ctx):
    as_of = parse_date(ctx.arg("as_of") or dt.date.today().isoformat())
    t = ctx.arg("type")
    with connect() as con, con.cursor() as cur:
        docs = _open_docs(cur, ctx.company_id, [t] if t else ["AR", "AP"], as_of)
        cur.execute("""SELECT p.payment_no, p.direction, p.payment_date, p.partner_id, pt.name partner, p.amount,
                              p.wht_amount, p.method, p.reference, p.je_no,
                              (SELECT string_agg(doc_no, ', ') FROM payment_allocation pa WHERE pa.payment_no=p.payment_no) docs
                       FROM payment p LEFT JOIN partner pt USING (partner_id)
                       WHERE p.company_id=%s ORDER BY p.payment_date DESC, p.payment_no DESC""", (ctx.company_id,))
        pays = rows(cur)
    return {"invoices": docs, "payments": pays}


def api_invoice_get(ctx, doc_no):
    with connect() as con, con.cursor() as cur:
        cur.execute("""SELECT i.*, p.name partner, p.tax_id partner_tax_id, r.rule_code vat_rule, v.version_no vat_version,
                              v.legal_reference vat_legal
                       FROM invoice i JOIN partner p USING (partner_id)
                       LEFT JOIN tax_rule_version v ON v.rule_version_id = i.vat_rule_version_id
                       LEFT JOIN tax_rule r ON r.rule_id = v.rule_id
                       WHERE i.doc_no=%s AND i.company_id=%s""", (doc_no, ctx.company_id))
        h = one(cur)
        if not h:
            raise ApiError(404, f"{doc_no} not found")
        cur.execute("SELECT line_no, description, qty, unit_price, amount, account_code FROM invoice_line "
                    "WHERE doc_no=%s ORDER BY line_no", (doc_no,))
        h["lines"] = rows(cur)
        cur.execute("""SELECT pa.payment_no, pa.amount settled, p.payment_date, p.amount cash, p.wht_amount, p.method
                       FROM payment_allocation pa JOIN payment p USING (payment_no) WHERE pa.doc_no=%s
                       ORDER BY p.payment_date""", (doc_no,))
        h["payments"] = rows(cur)
        h["outstanding"] = D(h["total"]) - sum((D(x["settled"]) for x in h["payments"]), Decimal(0))
        return h


def api_tax_options(ctx):
    """VAT choices and WHT categories in force on a date, for invoice and payment forms."""
    day = parse_date(ctx.arg("date") or dt.date.today().isoformat())
    with connect() as con, con.cursor() as cur:
        eng, doc, vid, c = engine_for(cur, ctx.company_id)
        vat = []
        for code in ("VAT_STD", "VAT_ZERO"):
            try:
                v = eng.version(code, day)
                vat.append({"code": code, "rate": v["params"]["rate"], "version": v["version"],
                            "name": eng.rules[code]["name_en"]})
            except TR.EngineError:
                pass
        wht = []
        try:
            v = eng.version("WHT_PND3_53", day)
            wht = [{"key": x["key"], "label": x.get("label_en") or x["key"], "label_th": x.get("label_th"),
                    "rate": x["rate"]} for x in v["params"]["categories"]]
        except TR.EngineError:
            pass
        try:
            threshold = eng.version("WHT_MIN_THRESHOLD", day)["params"]["min_amount"]
        except TR.EngineError:
            threshold = 0
        fields = []
        for r in doc["rules"]:
            if r["category"] == "PIT" and r["rule_type"] == "fixed_amount":
                try:
                    v = eng.version(r["code"], day)
                except TR.EngineError:
                    continue
                p = v["params"]
                if p.get("input_field"):
                    fields.append({"field": p["input_field"], "kind": p.get("input_kind", "flag"), "rule": r["code"],
                                   "label": r["name_en"], "label_th": r["name_th"], "amount": p["amount"],
                                   "per": p["per"], "max_count": p.get("max_count"),
                                   "evidence": p.get("evidence_required", False)})
    return {"date": day, "vat": vat, "wht": wht, "wht_threshold": threshold, "allowance_fields": fields}


def api_invoice_create(ctx):
    b = ctx.body or {}
    doc_type = b.get("doc_type")
    if doc_type not in ("AR", "AP"):
        raise ApiError(400, "doc_type must be AR (sales invoice) or AP (purchase bill)")
    day = parse_date(b.get("doc_date"), "doc_date")
    lines = [l for l in b.get("lines", []) if (l.get("description") or "").strip() and D(l.get("qty")) and l.get("account_code")]
    if not lines:
        raise ApiError(400, "Add at least one line with description, quantity, price and account")
    with connect() as con, con.cursor() as cur:
        set_ctx(cur, ctx, f"create {doc_type} invoice")
        check_user(cur, ctx.user, ["accountant", "tax_admin", "finance_manager", "md"])
        pid = require_open(cur, ctx.company_id, day)
        cur.execute("SELECT partner_id, name, payment_terms_days FROM partner WHERE partner_id=%s AND company_id=%s",
                    (b.get("partner_id"), ctx.company_id))
        p = cur.fetchone()
        if not p:
            raise ApiError(400, "Choose a customer / vendor")
        eng, doc, vid, c = engine_for(cur, ctx.company_id)
        subtotal = Decimal(0)
        for l in lines:
            l["amount"] = q2(D(l["qty"]) * D(l["unit_price"]))
            subtotal += l["amount"]
        vat_code = b.get("vat_code") or "VAT_STD"
        vat_amt, vat_rate, vat_vid = Decimal(0), Decimal(0), None
        if vat_code != "NONE":
            try:
                v = eng.version(vat_code, day)
            except TR.EngineError as e:
                raise ApiError(409, f"No approved VAT rule in force: {e}")
            vat_amt = eng.calc(vat_code, day, {"amount": subtotal})["tax"]
            vat_rate = D(v["params"]["rate"]) * 100
            vat_vid = vid.get((vat_code, v["company_id"], v["version"]))
        total = subtotal + vat_amt
        prefix = "INV" if doc_type == "AR" else "BILL"
        doc_no = next_no(cur, "invoice", "doc_no", f"{prefix}-{day:%y%m}-")
        due = parse_date(b["due_date"]) if b.get("due_date") else day + dt.timedelta(days=p[2] or 0)
        je = next_no(cur, "journal_entry", "je_no", f"JE-{day:%y%m}-")
        jl = []
        if doc_type == "AR":
            jl.append({"account_code": "1210", "partner_id": p[0], "debit": total, "memo": doc_no})
            for l in lines:
                jl.append({"account_code": l["account_code"], "partner_id": p[0], "credit": l["amount"], "memo": l["description"][:80]})
            if vat_amt:
                jl.append({"account_code": "2120", "partner_id": p[0], "credit": vat_amt, "memo": f"Output VAT {vat_rate:.0f}%",
                           "tax_rule_version_id": vat_vid})
        else:
            for l in lines:
                jl.append({"account_code": l["account_code"], "partner_id": p[0], "debit": l["amount"], "memo": l["description"][:80]})
            if vat_amt:
                jl.append({"account_code": "1220", "partner_id": p[0], "debit": vat_amt, "memo": f"Input VAT {vat_rate:.0f}%",
                           "tax_rule_version_id": vat_vid})
            jl.append({"account_code": "2110", "partner_id": p[0], "credit": total, "memo": doc_no})
        insert_je(cur, ctx.company_id, je, day, pid, "SALES" if doc_type == "AR" else "PURCHASE",
                  (b.get("description") or f"{'Invoice' if doc_type == 'AR' else 'Bill'} {doc_no} - {p[1]}")[:200],
                  doc_no, "posted", ctx.user, jl)
        cur.execute("""INSERT INTO invoice (doc_no, doc_type, partner_id, doc_date, due_date, subtotal, vat_rate, vat_amount,
                         total, status, je_no, company_id, vat_rule_version_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',%s,%s,%s)""",
                    (doc_no, doc_type, p[0], day, due, subtotal, vat_rate, vat_amt, total, je, ctx.company_id, vat_vid))
        for i, l in enumerate(lines, start=1):
            cur.execute("INSERT INTO invoice_line VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (doc_no, i, l["description"].strip(), D(l["qty"]), q2(l["unit_price"]), l["amount"], l["account_code"]))
        con.commit()
    return {"doc_no": doc_no, "je_no": je, "subtotal": subtotal, "vat": vat_amt, "total": total}


def _wht_for(eng, day, base, category):
    if not category:
        return Decimal(0), None
    try:
        thr = eng.calc("WHT_MIN_THRESHOLD", day, {"amount": base})["applies"]
    except TR.EngineError:
        thr = True
    if not thr:
        return Decimal(0), None
    v = eng.version("WHT_PND3_53", day)
    return eng.calc("WHT_PND3_53", day, {"amount": base, "category": category})["tax"], v


def api_payment_preview_or_create(ctx, create):
    b = ctx.body or {}
    day = parse_date(b.get("payment_date"), "payment_date")
    with connect() as con, con.cursor() as cur:
        set_ctx(cur, ctx, "payment")
        cur.execute("SELECT * FROM invoice WHERE doc_no=%s AND company_id=%s FOR UPDATE", (b.get("doc_no"), ctx.company_id))
        inv = one(cur)
        if not inv:
            raise ApiError(404, "Invoice not found")
        cur.execute("SELECT COALESCE(sum(amount),0) FROM payment_allocation WHERE doc_no=%s", (inv["doc_no"],))
        outstanding = D(inv["total"]) - D(cur.fetchone()[0])
        if outstanding <= 0:
            raise ApiError(409, f"{inv['doc_no']} is already fully settled")
        settle = q2(b.get("settle") or outstanding)
        if settle <= 0 or settle > outstanding:
            raise ApiError(400, f"Amount must be between 0 and the outstanding {outstanding:,.2f}")
        eng, doc, vid, c = engine_for(cur, ctx.company_id)
        base = q2(settle * D(inv["subtotal"]) / D(inv["total"])) if D(inv["total"]) else settle
        wht, wv = _wht_for(eng, day, base, b.get("wht_category"))
        cash = settle - wht
        res = {"doc_no": inv["doc_no"], "settle": settle, "wht_base": base, "wht": wht, "cash": cash,
               "outstanding_after": outstanding - settle,
               "wht_rule": f"WHT_PND3_53 v{wv['version']}" if wv else None}
        if not create:
            return res
        check_user(cur, ctx.user, ["accountant", "tax_admin", "finance_manager", "md"])
        pid = require_open(cur, ctx.company_id, day)
        bank = b.get("bank_account") or "1120"
        wvid = vid.get(("WHT_PND3_53", wv["company_id"], wv["version"])) if wv else None
        incoming = inv["doc_type"] == "AR"
        pay_no = next_no(cur, "payment", "payment_no", f"{'RCV' if incoming else 'PAY'}-{day:%y%m}-")
        je = next_no(cur, "journal_entry", "je_no", f"JE-{day:%y%m}-")
        if incoming:
            jl = [{"account_code": bank, "partner_id": inv["partner_id"], "debit": cash, "memo": pay_no},
                  {"account_code": "1230", "partner_id": inv["partner_id"], "debit": wht, "memo": "Customer withheld tax",
                   "tax_rule_version_id": wvid},
                  {"account_code": "1210", "partner_id": inv["partner_id"], "credit": settle, "memo": f"Settle {inv['doc_no']}"}]
        else:
            jl = [{"account_code": "2110", "partner_id": inv["partner_id"], "debit": settle, "memo": f"Settle {inv['doc_no']}"},
                  {"account_code": "2131", "partner_id": inv["partner_id"], "credit": wht, "memo": "WHT PND3/53",
                   "tax_rule_version_id": wvid},
                  {"account_code": bank, "partner_id": inv["partner_id"], "credit": cash, "memo": pay_no}]
        insert_je(cur, ctx.company_id, je, day, pid, "RECEIPT" if incoming else "PAYMENT",
                  f"{'Receipt' if incoming else 'Payment'} {pay_no} for {inv['doc_no']}", pay_no, "posted", ctx.user, jl)
        cur.execute("""INSERT INTO payment (payment_no, direction, payment_date, partner_id, bank_account, amount, wht_amount,
                         method, reference, je_no, company_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (pay_no, "in" if incoming else "out", day, inv["partner_id"], bank, cash, wht,
                     b.get("method") or "transfer", b.get("reference"), je, ctx.company_id))
        cur.execute("INSERT INTO payment_allocation VALUES (%s,%s,%s)", (pay_no, inv["doc_no"], settle))
        cur.execute("UPDATE invoice SET status=%s WHERE doc_no=%s",
                    ("paid" if outstanding - settle == 0 else "partial", inv["doc_no"]))
        con.commit()
        res.update({"payment_no": pay_no, "je_no": je})
        return res


# =====================================================================
# API: payroll + employees
# =====================================================================
def api_payroll_list(ctx):
    with connect() as con, con.cursor() as cur:
        cur.execute("""SELECT pr.run_id, pr.period_id, pr.pay_date, pr.status, pr.je_no, count(pl.*) employees,
                              COALESCE(sum(pl.gross_pay),0) gross, COALESCE(sum(pl.sso_employee),0) sso,
                              COALESCE(sum(pl.wht_pnd1),0) pnd1, COALESCE(sum(pl.net_pay),0) net
                       FROM payroll_run pr LEFT JOIN payroll_line pl USING (run_id)
                       WHERE pr.company_id=%s GROUP BY pr.run_id ORDER BY pr.pay_date DESC""", (ctx.company_id,))
        return {"runs": rows(cur)}


def api_payroll_get(ctx, run_id):
    with connect() as con, con.cursor() as cur:
        cur.execute("SELECT * FROM payroll_run WHERE run_id=%s AND company_id=%s", (run_id, ctx.company_id))
        r = one(cur)
        if not r:
            raise ApiError(404, f"{run_id} not found")
        cur.execute("""SELECT pl.*, e.first_name || ' ' || e.last_name employee_name, e.position
                       FROM payroll_line pl JOIN employee e USING (employee_id) WHERE run_id=%s ORDER BY employee_id""",
                    (run_id,))
        r["lines"] = rows(cur)
        return r


def api_payroll_calc(ctx, post=False, run_id=None):
    b = ctx.body or {}
    with connect() as con, con.cursor() as cur:
        c = company_info(cur, ctx.company_id)
        check_user(cur, ctx.user, ["payroll", "tax_admin", "accountant"])
        inputs = b.get("inputs")
        if run_id:
            cur.execute("SELECT pay_date, status FROM payroll_run WHERE run_id=%s AND company_id=%s", (run_id, ctx.company_id))
            r = cur.fetchone()
            if not r:
                raise ApiError(404, f"{run_id} not found")
            pay_date = r[0]
            if inputs is None:
                cur.execute("SELECT employee_id, overtime, commission, allowance, other_deduction FROM payroll_line "
                            "WHERE run_id=%s", (run_id,))
                inputs = {e: {"overtime": o, "commission": cm, "allowance": a, "other_deduction": od}
                          for e, o, cm, a, od in cur.fetchall()}
        else:
            pay_date = parse_date(b.get("pay_date"), "pay_date")
            run_id = b.get("run_id") or f"PR-{pay_date:%y%m}"
        je = None
        if post:
            je = next_no(cur, "journal_entry", "je_no", f"JE-{pay_date:%y%m}-")
    inputs = {k: {f: float(D(x)) for f, x in v.items()} for k, v in (inputs or {}).items()}
    out = []
    try:
        res = TD.do_payroll(run_id, pay_date.isoformat(), c["company_code"], None, ctx.user, post, je, out=out, inputs=inputs)
    except TD.Fail as e:
        raise ApiError(409, str(e))
    res["log"] = out
    return res


def api_employees(ctx):
    year = int(ctx.arg("year") or dt.date.today().year)
    with connect() as con, con.cursor() as cur:
        cur.execute("""SELECT e.employee_id, e.first_name, e.last_name, e.dept_code, e.position, e.hire_date,
                              e.monthly_salary, e.bank_name, e.bank_account_masked, e.is_active,
                              p.allowances, p.status profile_status, p.approved_by, p.updated_at,
                              o.ytd_income, o.ytd_withheld, o.ytd_sso, o.as_of opening_as_of
                       FROM employee e
                       LEFT JOIN employee_tax_profile p ON p.employee_id=e.employee_id AND p.tax_year=%s
                       LEFT JOIN employee_tax_opening o ON o.employee_id=e.employee_id AND o.tax_year=%s
                       WHERE e.company_id=%s ORDER BY e.employee_id""", (year, year, ctx.company_id))
        emps = rows(cur)
        cur.execute("""SELECT employee_id, allowance_key, file_ref FROM employee_tax_evidence WHERE tax_year=%s""", (year,))
        ev = {}
        for e, k, f in cur.fetchall():
            ev.setdefault(e, []).append({"key": k, "file": f})
        for e in emps:
            e["evidence"] = ev.get(e["employee_id"], [])
    return {"year": year, "employees": emps}


def api_profile_save(ctx, emp):
    b = ctx.body or {}
    year = int(b.get("tax_year") or dt.date.today().year)
    alw = b.get("allowances") or {}
    with connect() as con, con.cursor() as cur:
        set_ctx(cur, ctx, f"tax profile {emp} {year}")
        check_user(cur, ctx.user, ["payroll", "tax_admin"])
        cur.execute("SELECT 1 FROM employee WHERE employee_id=%s AND company_id=%s", (emp, ctx.company_id))
        if not cur.fetchone():
            raise ApiError(404, "Employee not found")
        clean = {}
        for k, v in alw.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", k):
                continue
            if isinstance(v, bool):
                clean[k] = v
            elif v in (None, "", 0, "0"):
                continue
            else:
                clean[k] = float(D(v)) if not float(D(v)).is_integer() else int(D(v))
        cur.execute("""INSERT INTO employee_tax_profile (employee_id, tax_year, allowances, status, approved_by, updated_at)
                       VALUES (%s,%s,%s,'approved',%s,now())
                       ON CONFLICT (employee_id, tax_year) DO UPDATE SET allowances=EXCLUDED.allowances,
                         status='approved', approved_by=EXCLUDED.approved_by, updated_at=now()""",
                    (emp, year, Jsonb(clean), ctx.user))
        con.commit()
    return {"message": f"Tax profile {emp} {year} saved", "allowances": clean}


# =====================================================================
# API: tax rules
# =====================================================================
def api_tax_rules(ctx):
    day = parse_date(ctx.arg("date") or dt.date.today().isoformat())
    with connect() as con, con.cursor() as cur:
        c = company_info(cur, ctx.company_id)
        doc, vid = TD.db_to_doc(cur, c["jurisdiction_code"], c["company_code"])
        cur.execute("""SELECT v.rule_version_id, v.created_by, v.submitted_by, v.submitted_at, v.approved_at,
                              v.pending_effective_to, (tax_approval_policy_for(v.rule_version_id)).required_approvals req,
                              (tax_approval_policy_for(v.rule_version_id)).approver_roles roles
                       FROM tax_rule_version v""")
        meta = {r["rule_version_id"]: r for r in rows(cur)}
        cur.execute("SELECT rule_version_id, approver, decision, comment, decided_at FROM tax_rule_approval ORDER BY decided_at")
        appr = {}
        for r in rows(cur):
            appr.setdefault(r["rule_version_id"], []).append(r)
        cur.execute("SELECT rule_version_id FROM tax_rules_in_force(%s, %s::smallint)", (day, ctx.company_id))
        in_force = {r[0] for r in cur.fetchall()}
        tests = TR.run_tests(doc)              # validate the clean document before adding screen-only fields
        errs, warns = TR.validate(doc)
        for r in doc["rules"]:
            for v in r["versions"]:
                i = vid.get((r["code"], v["company_id"], v["version"]))
                m = meta.get(i, {})
                v.update({"id": i, "in_force": i in in_force, "created_by": m.get("created_by"),
                          "submitted_by": m.get("submitted_by"), "approved_at": m.get("approved_at"),
                          "pending_close": m.get("pending_effective_to"), "required_approvals": m.get("req"),
                          "approver_roles": m.get("roles"), "approvals": appr.get(i, [])})
    return {"date": day, "company": c, "rules": doc["rules"], "test_cases": doc["test_cases"],
            "tests": [{"id": t, "ok": ok, "message": m} for t, ok, m in tests],
            "validation": {"errors": errs, "warnings": warns}, "base_checksum": doc["base_checksum"]}


def api_tax_export(ctx):
    fmt = ctx.arg("fmt", "json")
    with connect() as con, con.cursor() as cur:
        c = company_info(cur, ctx.company_id)
        doc, _ = TD.db_to_doc(cur, c["jurisdiction_code"], None, ctx.user)
    stamp = dt.date.today().strftime("%Y%m%d")
    name = f"tax_rules_{c['jurisdiction_code']}_{stamp}.{fmt}"
    if fmt == "xlsx":
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / name
            TR.write_xlsx(doc, str(p))
            data = p.read_bytes()
        return ("file", name, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", data)
    data = json.dumps(TR.normalize(doc), ensure_ascii=False, indent=2).encode("utf-8")
    return ("file", name, "application/json", data)


def api_tax_import(ctx, raw):
    name = Path(ctx.arg("name") or "upload.json").name
    if not name.lower().endswith((".json", ".xlsx")):
        raise ApiError(400, "Upload a .json or .xlsx rule file")
    apply = ctx.arg("apply") == "1"
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / name
        p.write_bytes(raw)
        try:
            new = TR.load(str(p))
        except (ValueError, KeyError) as e:
            raise ApiError(400, f"Cannot read the file: {e}")
        with connect() as con, con.cursor() as cur:
            check_user(cur, ctx.user, ["tax_admin"])
            base, _ = TD.db_to_doc(cur, new.get("jurisdiction") or "TH", new.get("company_id"), ctx.user)
        errs, warns = TR.validate(new, base)
        broken = any(e.startswith("schema:") for e in errs)       # structure invalid: a diff would be meaningless
        changes = [] if broken else TR.diff(base, new)
        res = {"file": name, "errors": errs, "warnings": warns, "changes": changes,
               "change_summary": new.get("change_summary"), "applied": False}
        if apply and not errs:
            try:
                r = TD.do_import(str(p), ctx.user, out=[], file_name=name)
            except TD.Fail as e:
                raise ApiError(409, str(e), details=getattr(e, "details", None))
            res.update({"applied": True, "batch": r["batch"]})
        return res


def api_tax_action(ctx):
    b = ctx.body or {}
    code, ver, action = b.get("rule"), int(b.get("version") or 0), b.get("action")
    comp = b.get("company")
    try:
        if action == "submit":
            msg = TD.do_submit(code, ver, comp, ctx.user, out=[])
        elif action in ("approve", "reject"):
            if action == "reject" and not (b.get("comment") or "").strip():
                raise ApiError(400, "Give a reason when rejecting")
            msg = TD.do_decide(code, ver, comp, ctx.user, action, b.get("comment"), out=[])
        elif action == "recalc":
            out = []
            TD.do_recalc_report(code, ver, comp, ctx.user, out=out)
            msg = "\n".join(out)
        else:
            raise ApiError(400, "Unknown action")
    except TD.Fail as e:
        raise ApiError(409, str(e))
    return {"message": msg}


def api_recalcs(ctx):
    with connect() as con, con.cursor() as cur:
        cur.execute("""SELECT rr.recalc_id, r.rule_code, v.version_no, rr.mode, rr.period_from, rr.period_to, rr.status,
                              rr.total_difference, rr.report, rr.created_by, rr.created_at, rr.confirmed_by, rr.confirmed_at
                       FROM tax_recalc_run rr JOIN tax_rule_version v ON v.rule_version_id = rr.trigger_version_id
                       JOIN tax_rule r USING (rule_id) WHERE rr.company_id=%s ORDER BY rr.recalc_id DESC""",
                    (ctx.company_id,))
        return {"recalcs": rows(cur)}


def api_recalc_confirm(ctx, rid):
    try:
        return {"message": TD.do_confirm_recalc(int(rid), ctx.user, out=[])}
    except TD.Fail as e:
        raise ApiError(409, str(e))


def api_tax_log(ctx):
    with connect() as con, con.cursor() as cur:
        cur.execute("""SELECT l.log_id, l.object_type, l.object_id, l.action, l.actor, l.reason, l.at,
                              l.old_value, l.new_value FROM tax_change_log l ORDER BY l.log_id DESC LIMIT %s""",
                    (int(ctx.arg("limit") or 150),))
        out = rows(cur)
        cur.execute("""SELECT batch_id, file_name, file_format, imported_by, imported_at, result, change_summary
                       FROM tax_import_batch ORDER BY batch_id DESC LIMIT 30""")
        return {"log": out, "imports": rows(cur)}


def api_policies(ctx):
    with connect() as con, con.cursor() as cur:
        if ctx.h.command == "PUT":
            b = ctx.body or {}
            set_ctx(cur, ctx, "policy change")
            check_user(cur, ctx.user, ["md", "finance_manager"])
            if b.get("kind") == "approval":
                cur.execute("""INSERT INTO approval_policy (company_id, category, required_approvals, approver_roles,
                                 escalate_after_days) VALUES (%s,%s,%s,%s,%s)
                               ON CONFLICT (company_id, category) DO UPDATE SET required_approvals=EXCLUDED.required_approvals,
                                 approver_roles=EXCLUDED.approver_roles, escalate_after_days=EXCLUDED.escalate_after_days""",
                            (ctx.company_id, b["category"], int(b["required_approvals"]), b["approver_roles"],
                             b.get("escalate_after_days") or None))
            elif b.get("kind") == "retro":
                cur.execute("""INSERT INTO retro_policy (company_id, category, mode) VALUES (%s,%s,%s)
                               ON CONFLICT (company_id, category) DO UPDATE SET mode=EXCLUDED.mode""",
                            (ctx.company_id, b["category"], b["mode"]))
            else:
                raise ApiError(400, "kind must be approval or retro")
            con.commit()
        out = {}
        for cat in ("VAT", "WHT", "PIT", "SSO", "OTHER"):
            cur.execute("""SELECT company_id IS NOT NULL company_specific, required_approvals, approver_roles,
                                  escalate_after_days FROM approval_policy
                           WHERE category=%s AND (company_id=%s OR company_id IS NULL)
                           ORDER BY company_id NULLS LAST LIMIT 1""", (cat, ctx.company_id))
            a = one(cur)
            cur.execute("""SELECT company_id IS NOT NULL company_specific, mode FROM retro_policy
                           WHERE category=%s AND (company_id=%s OR company_id IS NULL)
                           ORDER BY company_id NULLS LAST LIMIT 1""", (cat, ctx.company_id))
            r = one(cur)
            out[cat] = {"approval": a, "retro": r}
        return {"policies": out}


# =====================================================================
# API: reports + periods
# =====================================================================
def api_report(ctx, kind):
    with connect() as con, con.cursor() as cur:
        cid = ctx.company_id
        cur.execute("SELECT account_code, name_en, name_th, account_type, normal_side, is_postable FROM account ORDER BY account_code")
        accts = rows(cur)
        if kind == "tb":
            upto = parse_date(ctx.arg("date") or dt.date.today().isoformat())
            bal = _balances(cur, cid, upto)
            out = []
            for a in accts:
                if a["account_code"] not in bal:
                    continue
                d, c = bal[a["account_code"]]
                net = d - c
                out.append({**a, "debit": d, "credit": c,
                            "dr_balance": net if net > 0 else Decimal(0), "cr_balance": -net if net < 0 else Decimal(0)})
            return {"date": upto, "rows": out}
        if kind in ("pl", "bs"):
            d_from = parse_date(ctx.arg("from") or "2000-01-01")
            d_to = parse_date(ctx.arg("to") or dt.date.today().isoformat())
            bal = _balances(cur, cid, d_to, since=d_from if kind == "pl" else None)
            out = []
            for a in accts:
                if a["account_code"] not in bal:
                    continue
                d, c = bal[a["account_code"]]
                amt = d - c if a["normal_side"] == "D" else c - d
                out.append({**a, "amount": amt})
            if kind == "bs":
                # current earnings = revenue - expense to date
                earn = sum((r["amount"] if r["account_type"] == "revenue" else -r["amount"])
                           for r in out if r["account_type"] in ("revenue", "expense"))
                out = [r for r in out if r["account_type"] in ("asset", "liability", "equity")]
                out.append({"account_code": "3990", "name_en": "Current period earnings", "name_th": "กำไร(ขาดทุน)งวดปัจจุบัน",
                            "account_type": "equity", "normal_side": "C", "amount": earn, "is_postable": False})
            return {"from": d_from, "to": d_to, "rows": out}
        if kind == "tax":
            cur.execute("SELECT period_id, start_date, end_date, status FROM fiscal_period WHERE company_id=%s ORDER BY start_date",
                        (cid,))
            out = []
            for pid, s, e, st in cur.fetchall():
                pb = _balances(cur, cid, e, since=s)

                def cr(k):
                    d, c = pb.get(k, (Decimal(0), Decimal(0)))
                    return c - d

                def dr(k):
                    d, c = pb.get(k, (Decimal(0), Decimal(0)))
                    return d - c
                cur.execute("""SELECT COALESCE(sum(l.credit),0) FROM journal_line l JOIN journal_entry e USING (je_no)
                               WHERE e.company_id=%s AND e.status='posted' AND e.entry_date BETWEEN %s AND %s
                                 AND l.account_code=%s AND e.journal_type <> 'PAYMENT'""", (cid, s, e, "2120"))
                out_vat = cur.fetchone()[0]
                cur.execute("""SELECT COALESCE(sum(l.debit),0) FROM journal_line l JOIN journal_entry e USING (je_no)
                               WHERE e.company_id=%s AND e.status='posted' AND e.entry_date BETWEEN %s AND %s
                                 AND l.account_code=%s""", (cid, s, e, "1220"))
                in_vat = cur.fetchone()[0]
                cur.execute("""SELECT COALESCE(sum(pl.wht_pnd1),0), COALESCE(sum(pl.sso_employee+pl.sso_employer),0),
                                      COALESCE(sum(pl.gross_pay),0)
                               FROM payroll_line pl JOIN payroll_run pr USING (run_id)
                               WHERE pr.company_id=%s AND pr.period_id=%s AND pr.status IN ('approved','paid')""", (cid, pid))
                pnd1, sso, gross = cur.fetchone()
                cur.execute("""SELECT COALESCE(sum(l.credit),0) FROM journal_line l JOIN journal_entry e USING (je_no)
                               WHERE e.company_id=%s AND e.status='posted' AND e.entry_date BETWEEN %s AND %s
                                 AND l.account_code='2131'""", (cid, s, e))
                pnd3 = cur.fetchone()[0]
                cur.execute("""SELECT COALESCE(sum(l.debit),0) FROM journal_line l JOIN journal_entry e USING (je_no)
                               WHERE e.company_id=%s AND e.status='posted' AND e.entry_date BETWEEN %s AND %s
                                 AND l.account_code='1230'""", (cid, s, e))
                wht_suffered = cur.fetchone()[0]
                out.append({"period": pid, "status": st, "output_vat": out_vat, "input_vat": in_vat,
                            "vat_payable": D(out_vat) - D(in_vat), "pnd1": pnd1, "pnd3_53": pnd3, "sso": sso,
                            "gross_salary": gross, "wht_suffered": wht_suffered})
            return {"rows": out}
    raise ApiError(404, "Unknown report")


def api_periods(ctx):
    b = ctx.body or {}
    with connect() as con, con.cursor() as cur:
        set_ctx(cur, ctx, "period change")
        check_user(cur, ctx.user, ["finance_manager", "md", "accountant"])
        if b.get("action") == "create":
            cur.execute("SELECT max(end_date) FROM fiscal_period WHERE company_id=%s", (ctx.company_id,))
            last = cur.fetchone()[0]
            start = last + dt.timedelta(days=1)
            end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
            cur.execute("INSERT INTO fiscal_period VALUES (%s,%s,%s,%s,'open')", (f"{start:%Y-%m}", ctx.company_id, start, end))
            msg = f"Created period {start:%Y-%m}"
        elif b.get("action") in ("close", "open"):
            st = "closed" if b["action"] == "close" else "open"
            cur.execute("UPDATE fiscal_period SET status=%s WHERE period_id=%s AND company_id=%s AND status <> 'locked'",
                        (st, b.get("period_id"), ctx.company_id))
            if not cur.rowcount:
                raise ApiError(404, "Period not found or locked")
            msg = f"Period {b.get('period_id')} {st}"
        else:
            raise ApiError(400, "Unknown action")
        con.commit()
    return {"message": msg}


# =====================================================================
# HTTP
# =====================================================================
ROUTES = [
    ("GET", r"/api/status", api_status),
    ("POST", r"/api/setup", api_setup),
    ("GET", r"/api/meta", api_meta),
    ("GET", r"/api/dashboard", api_dashboard),
    ("GET", r"/api/journal", api_journal_list),
    ("POST", r"/api/journal", api_journal_create),
    ("GET", r"/api/journal/([^/]+)", api_journal_get),
    ("POST", r"/api/journal/([^/]+)/(post|delete|reverse)", api_journal_action),
    ("GET", r"/api/invoices", api_invoices),
    ("POST", r"/api/invoices", api_invoice_create),
    ("GET", r"/api/invoices/([^/]+)", api_invoice_get),
    ("GET", r"/api/tax-options", api_tax_options),
    ("POST", r"/api/payments/preview", lambda c: api_payment_preview_or_create(c, False)),
    ("POST", r"/api/payments", lambda c: api_payment_preview_or_create(c, True)),
    ("GET", r"/api/payroll", api_payroll_list),
    ("POST", r"/api/payroll", lambda c: api_payroll_calc(c, False)),
    ("GET", r"/api/payroll/([^/]+)", api_payroll_get),
    ("POST", r"/api/payroll/([^/]+)/recalc", lambda c, r: api_payroll_calc(c, False, r)),
    ("POST", r"/api/payroll/([^/]+)/post", lambda c, r: api_payroll_calc(c, True, r)),
    ("GET", r"/api/employees", api_employees),
    ("PUT", r"/api/employees/([^/]+)/profile", api_profile_save),
    ("GET", r"/api/tax/rules", api_tax_rules),
    ("GET", r"/api/tax/export", api_tax_export),
    ("POST", r"/api/tax/action", api_tax_action),
    ("GET", r"/api/tax/recalcs", api_recalcs),
    ("POST", r"/api/tax/recalcs/(\d+)/confirm", api_recalc_confirm),
    ("GET", r"/api/tax/log", api_tax_log),
    ("GET", r"/api/tax/policies", api_policies),
    ("PUT", r"/api/tax/policies", api_policies),
    ("GET", r"/api/reports/(tb|pl|bs|tax)", api_report),
    ("POST", r"/api/periods", api_periods),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "sERP/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("SERP_QUIET"):
            return
        sys.stderr.write("%s %s\n" % (dt.datetime.now().strftime("%H:%M:%S"), fmt % args))

    def _send(self, status, body: bytes, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, default=jdefault, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _handle(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        if not path.startswith("/api/"):
            return self._static(path)
        # only accept API calls from our own page (blocks other sites in the browser from calling the API)
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            return self._json(403, {"error": "Cross-origin request refused"})
        if self.command != "GET" and self.headers.get("X-Requested-With") != "sERP":
            return self._json(403, {"error": "Missing X-Requested-With header"})
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        for method, pat, fn in ROUTES:
            if method != self.command:
                continue
            m = re.fullmatch(pat, path)
            if not m:
                continue
            try:
                body = None
                ctype = self.headers.get("Content-Type", "")
                if raw and "json" in ctype:
                    body = json.loads(raw.decode("utf-8"))
                ctx = Ctx(self, query, body)
                res = fn(ctx, *m.groups())
                if isinstance(res, tuple) and res and res[0] == "file":
                    _, name, mime, data = res
                    return self._send(200, data, mime, {"Content-Disposition": f'attachment; filename="{name}"'})
                return self._json(200, res)
            except ApiError as e:
                return self._json(e.status, {"error": e.message, "code": e.code, "details": e.details})
            except psycopg.Error as e:
                msg = e.diag.message_primary if getattr(e, "diag", None) and e.diag.message_primary else str(e)
                status = 409 if isinstance(e, (psycopg.errors.RaiseException, psycopg.errors.IntegrityError)) else 500
                if isinstance(e, psycopg.OperationalError):
                    return self._json(503, {"error": "Database unavailable: " + str(e).strip().splitlines()[0], "code": "db"})
                return self._json(status, {"error": msg})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if path == "/api/tax/import" and self.command == "POST":
            try:
                return self._json(200, api_tax_import(Ctx(self, query, None), raw))
            except ApiError as e:
                return self._json(e.status, {"error": e.message, "details": e.details})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        return self._json(404, {"error": f"No route {self.command} {path}"})

    def _static(self, path):
        if path in ("/", "/index.html"):
            f = WEB / "index.html"
        elif path.endswith("/") and (WEB / path.strip("/") / "index.html").is_file():
            f = (WEB / path.strip("/") / "index.html").resolve()
        else:
            f = (WEB / path.lstrip("/")).resolve()
            if WEB.resolve() not in f.parents or not f.is_file():
                return self._send(404, b"Not found", "text/plain")
        mime = {".html": "text/html; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                ".ico": "image/x-icon", ".js": "text/javascript", ".css": "text/css", ".jpg": "image/jpeg"}.get(f.suffix, "application/octet-stream")
        extra = {}
        if f.suffix == ".html":
            extra["Content-Security-Policy"] = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                                                "connect-src 'self'; frame-ancestors 'none'")
        self._send(200, f.read_bytes(), mime, extra)

    do_GET = do_POST = do_PUT = do_DELETE = _handle


def open_window(url):
    """Open as an app window (Edge/Chrome --app) when possible, else the default browser."""
    if os.environ.get("SERP_NO_WINDOW"):
        return
    cands = []
    if sys.platform == "win32":
        pf = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        for base in pf:
            cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe", Path(base) / "Google/Chrome/Application/chrome.exe"]
    elif sys.platform == "darwin":
        cands += [Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                  Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    else:
        for n in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
            p = shutil.which(n)
            if p:
                cands.append(Path(p))
    for c in cands:
        if c.exists():
            profile = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "sERP" / "window"
            try:
                subprocess.Popen([str(c), f"--app={url}", f"--user-data-dir={profile}", "--window-size=1440,900",
                                  "--no-first-run", "--no-default-browser-check"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue
    webbrowser.open(url)


def init_sample_if_empty():
    """Docker / unattended first start: wait for the database, then load the sample if it is empty."""
    import time
    for _ in range(60):
        try:
            with psycopg.connect(CFG["dsn"], connect_timeout=3, autocommit=True) as con:
                cur = con.cursor()
                cur.execute("SELECT to_regclass('erp.tax_rule') IS NOT NULL")
                if cur.fetchone()[0]:
                    print("Database already has sERP data.")
                    return
                sql = (HERE / "engine" / "erp_sample_v2.sql").read_text(encoding="utf-8")
                cur.execute("\n".join(l for l in sql.splitlines() if not l.startswith("\\")))
                print("Loaded the sample company and Thai tax rules.")
                return
        except psycopg.OperationalError:
            time.sleep(1)
    print("WARNING: database not reachable - open the app to set it up.")


def main():
    ap = argparse.ArgumentParser(description="sERP application server")
    ap.add_argument("--port", type=int, default=CFG["port"])
    ap.add_argument("--host", default=os.environ.get("SERP_HOST", "127.0.0.1"))
    ap.add_argument("--no-window", action="store_true")
    a = ap.parse_args()
    try:
        srv = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError:
        url = f"http://127.0.0.1:{a.port}/"
        print(f"sERP is already running - opening {url}")
        if not a.no_window:
            open_window(url)
        return
    if os.environ.get("SERP_INIT_SAMPLE") and CFG["dsn"]:
        init_sample_if_empty()
    url = f"http://127.0.0.1:{a.port}/"
    print(f"sERP {VERSION} running at {url}  (database: {dsn_display(CFG['dsn']) or 'not configured yet'})")
    print("Close this window or press Ctrl+C to stop.")
    if not a.no_window:
        threading.Timer(0.6, open_window, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
