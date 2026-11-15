#!/usr/bin/env python3
"""
simply handle IT ERP - tax rule file tool  (schema 1.0.0)

One rule definition, two file formats (JSON and XLSX), identical checks.

  python taxrules.py validate  FILE [--base BASE_FILE] [--merge-anyway]
  python taxrules.py test      FILE                     run test cases only
  python taxrules.py to-xlsx   IN.json OUT.xlsx
  python taxrules.py to-json   IN.xlsx OUT.json
  python taxrules.py diff      OLD_FILE NEW_FILE        readable list of changes
  python taxrules.py checksum  FILE
  python taxrules.py stamp     FILE [OUT]               set base_checksum + exported_at (system export step)

FILE may be .json or .xlsx everywhere.
Exit code: 0 = OK, 1 = errors found, 2 = usage / file problem.
"""
from __future__ import annotations

import argparse
import ast
import copy
import datetime as dt
import hashlib
import json
import sys
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN, ROUND_UP
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("tax_rules.schema.json")
ACTIVE_STATUSES = {"draft", "submitted", "approved"}      # take part in date coverage
FORMULA_FUNCS = {"min", "max", "round", "floor", "ceil", "rule", "bracket_tax", "capped"}


# =====================================================================
# Normalisation + checksum
# =====================================================================
def normalize(doc: dict) -> dict:
    """Fill optional fields with null so JSON and XLSX produce the same canonical form."""
    d = copy.deepcopy(doc)
    d.setdefault("company_id", None)
    d.setdefault("exported_by", None)
    for r in d.get("rules", []):
        r.setdefault("name_th", None)
        r.setdefault("description", None)
        for v in r.get("versions", []):
            for k in ("company_id", "source_url", "notes"):
                v.setdefault(k, None)
            p = v.get("params", {})
            for c in p.get("categories", []) if isinstance(p, dict) else []:
                c.setdefault("label_en", None)
                c.setdefault("label_th", None)
    for t in d.get("test_cases", []):
        t.setdefault("company_id", None)
    return d


def _num_canon(o):
    """Make 0.1 and 0.10, 5 and 5.0 hash identically."""
    if isinstance(o, bool) or o is None or isinstance(o, str):
        return o
    if isinstance(o, (int, float)):
        q = Decimal(str(o)).normalize()
        return format(q, "f")
    if isinstance(o, list):
        return [_num_canon(x) for x in o]
    if isinstance(o, dict):
        return {k: _num_canon(v) for k, v in o.items()}
    return o


def checksum(doc: dict) -> str:
    d = normalize(doc)
    payload = {k: d.get(k) for k in ("jurisdiction", "company_id", "jurisdiction_info", "rules", "test_cases", "policies")}
    blob = json.dumps(_num_canon(payload), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# =====================================================================
# Date helpers
# =====================================================================
def _d(s):
    return dt.date.fromisoformat(s) if s else None


def _covers(v, day: dt.date) -> bool:
    f, t = _d(v["effective_from"]), _d(v["effective_to"])
    return f <= day and (t is None or day <= t)


def vkey(rule_code, v):
    return (rule_code, v.get("company_id"), v["version"])


def fmt_key(k):
    code, comp, ver = k
    return f"{code} v{ver}" + (f" [company {comp}]" if comp else "")


# =====================================================================
# Calculation engine (pure functions, Decimal)
# =====================================================================
class EngineError(Exception):
    pass


def D(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if isinstance(x, bool):
        return Decimal(int(x))
    return Decimal(str(x))


def apply_rounding(x: Decimal, rounding: str) -> Decimal:
    if rounding == "none" or rounding is None:
        return x
    mode, prec = rounding.split(":")
    q = Decimal(prec)
    return x.quantize(q, rounding={"half_up": ROUND_HALF_UP, "down": ROUND_DOWN, "up": ROUND_UP}[mode])


class Engine:
    def __init__(self, doc: dict, company_id=None, statuses=None):
        """statuses: which versions count. Validation/tests use draft+submitted+approved (what WILL apply);
        real calculations (payroll, invoices) must pass {"approved"}."""
        self.rules = {r["code"]: r for r in doc["rules"]}
        self.company_id = company_id
        self.statuses = set(statuses) if statuses else ACTIVE_STATUSES

    def version(self, code: str, day: dt.date):
        if code not in self.rules:
            raise EngineError(f"unknown rule {code}")
        cands = [v for v in self.rules[code]["versions"] if v["status"] in self.statuses and _covers(v, day)]
        own = [v for v in cands if self.company_id and v.get("company_id") == self.company_id]
        base = [v for v in cands if v.get("company_id") is None]
        pick = own or base
        if not pick:
            raise EngineError(f"no version of {code} in force on {day}")
        if len(pick) > 1:
            raise EngineError(f"{len(pick)} overlapping versions of {code} on {day}")
        return pick[0]

    def calc(self, code: str, day: dt.date, inputs: dict, _depth=0) -> dict:
        if _depth > 20:
            raise EngineError("formula nesting too deep (cycle?)")
        rule = self.rules[code]
        v = self.version(code, day)
        p, rnd, rt = v["params"], v["rounding"], rule["rule_type"]

        def need(k):
            if k not in inputs:
                raise EngineError(f"{code}: missing input '{k}'")
            return inputs[k]

        if rt == "flat_rate":
            return {"tax": apply_rounding(D(need("amount")) * D(p["rate"]), rnd)}
        if rt == "rate_by_category":
            cat = need("category")
            rates = {c["key"]: c["rate"] for c in p["categories"]}
            if cat not in rates:
                raise EngineError(f"{code}: unknown category '{cat}'")
            return {"tax": apply_rounding(D(need("amount")) * D(rates[cat]), rnd)}
        if rt == "rate_with_cap":
            return {"amount": self._capped(p, D(need("base")), rnd)}
        if rt == "fixed_amount":
            amt, kind = D(p["amount"]), p.get("input_kind", "flag")
            if not p.get("input_field"):
                val = amt
            else:
                x = inputs.get("value", 0)
                if kind == "flag":
                    val = amt if x else Decimal(0)
                elif kind == "count":
                    n = D(x)
                    if p.get("max_count") is not None:
                        n = min(n, D(p["max_count"]))
                    val = amt * n
                else:
                    val = min(D(x), amt)
            return {"allowance": apply_rounding(val, rnd)}
        if rt == "bracket_table":
            return {"tax": apply_rounding(self._brackets(p, D(need("amount"))), rnd)}
        if rt == "threshold":
            return {"applies": D(need("amount")) >= D(p["min_amount"])}
        if rt == "formula":
            env = {k: D(need(k)) for k in p["inputs"]}
            val = self._eval(ast.parse(p["expression"], mode="eval").body, env, day, _depth)
            return {p["output"]: apply_rounding(D(val), rnd)}
        raise EngineError(f"unsupported rule_type {rt}")

    @staticmethod
    def _capped(p, base, rnd):
        b = base
        if p.get("base_cap") is not None:
            b = min(b, D(p["base_cap"]))
        if p.get("base_floor") is not None:
            b = max(b, D(p["base_floor"]))
        amt = b * D(p["rate"])
        if p.get("max_amount") is not None:
            amt = min(amt, D(p["max_amount"]))
        return apply_rounding(amt, rnd)

    @staticmethod
    def _brackets(p, x):
        if p.get("basis") == "flat_on_total":
            for b in p["brackets"]:
                if x >= D(b["from"]) and (b["to"] is None or x <= D(b["to"])):
                    return x * D(b["rate"])
            return Decimal(0)
        tax = Decimal(0)
        for b in p["brackets"]:
            lo, hi = D(b["from"]), (D(b["to"]) if b["to"] is not None else None)
            if x > lo:
                tax += ((min(x, hi) if hi is not None else x) - lo) * D(b["rate"])
        return tax

    def _eval(self, n, env, day, depth):
        if isinstance(n, ast.Constant):
            if isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
                return D(n.value)
            return n.value
        if isinstance(n, ast.Name):
            return env[n.id]
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -self._eval(n.operand, env, day, depth)
        if isinstance(n, ast.BinOp):
            a, b = self._eval(n.left, env, day, depth), self._eval(n.right, env, day, depth)
            if isinstance(n.op, ast.Add): return a + b
            if isinstance(n.op, ast.Sub): return a - b
            if isinstance(n.op, ast.Mult): return a * b
            if isinstance(n.op, ast.Div):
                if b == 0:
                    raise EngineError("division by zero")
                return a / b
        if isinstance(n, ast.Call):
            f = n.func.id
            args = [self._eval(a, env, day, depth) for a in n.args]
            if f == "min": return min(args)
            if f == "max": return max(args)
            if f == "floor": return D(args[0]).to_integral_value(ROUND_DOWN)
            if f == "ceil": return D(args[0]).to_integral_value(ROUND_UP)
            if f == "round":
                digits = int(args[1]) if len(args) > 1 else 0
                return D(args[0]).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
            if f == "rule":
                v = self.version(args[0], day)
                if args[1] not in v["params"]:
                    raise EngineError(f"rule('{args[0]}','{args[1]}'): no such param")
                return D(v["params"][args[1]])
            if f == "bracket_tax":
                return self.calc(args[0], day, {"amount": args[1]}, depth + 1)["tax"]
            if f == "capped":
                return self.calc(args[0], day, {"base": args[1]}, depth + 1)["amount"]
        raise EngineError(f"unsupported expression element {type(n).__name__}")


def run_tests(doc: dict):
    """Returns list of (test_id, ok, message)."""
    results = []
    for t in doc.get("test_cases", []):
        eng = Engine(doc, t.get("company_id"))
        try:
            got = eng.calc(t["rule"], _d(t["as_of"]), t["inputs"])
        except (EngineError, KeyError, TypeError, ValueError, ArithmeticError) as e:
            results.append((t["id"], False, f"error: {e}"))
            continue
        bad = []
        for k, exp in t["expected"].items():
            if k not in got:
                bad.append(f"{k}: rule gives {sorted(got)}, not '{k}'")
                continue
            g = got[k]
            if isinstance(exp, bool) or isinstance(g, bool):
                ok = g == exp
            else:
                ok = D(g).quantize(Decimal("0.01")) == D(exp).quantize(Decimal("0.01"))
            if not ok:
                bad.append(f"{k}: expected {exp}, got {g}")
        results.append((t["id"], not bad, "; ".join(bad) or "ok"))
    return results


# =====================================================================
# Validation
# =====================================================================
def _formula_problems(code, p, rules_by_code):
    errs, refs = [], set()
    try:
        tree = ast.parse(p["expression"], mode="eval")
    except SyntaxError as e:
        return [f"{code}: formula syntax error: {e.msg}"], refs
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Call, ast.Load,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub)
    func_nodes = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for n in ast.walk(tree):
        if not isinstance(n, allowed):
            errs.append(f"{code}: '{type(n).__name__}' is not allowed in formulas")
        if isinstance(n, ast.Name) and id(n) not in func_nodes and n.id not in p["inputs"]:
            errs.append(f"{code}: name '{n.id}' is not in inputs {p['inputs']}")
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name) or n.func.id not in FORMULA_FUNCS:
                errs.append(f"{code}: function not allowed: {ast.unparse(n.func)}")
                continue
            if n.func.id in ("rule", "bracket_tax", "capped"):
                a0 = n.args[0] if n.args else None
                if not (isinstance(a0, ast.Constant) and isinstance(a0.value, str)):
                    errs.append(f"{code}: {n.func.id}() needs a rule code in quotes as first argument")
                    continue
                ref = a0.value
                refs.add(ref)
                want = {"bracket_tax": "bracket_table", "capped": "rate_with_cap"}.get(n.func.id)
                if ref not in rules_by_code:
                    errs.append(f"{code}: references unknown rule '{ref}'")
                elif want and rules_by_code[ref]["rule_type"] != want:
                    errs.append(f"{code}: {n.func.id}('{ref}') needs a {want} rule")
    return errs, refs


def validate(doc: dict, base: dict | None = None, merge_anyway=False):
    errors, warnings = [], []
    doc = normalize(doc)

    # 1. JSON Schema
    try:
        import jsonschema
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        v = jsonschema.Draft202012Validator(schema)
        for e in sorted(v.iter_errors(doc), key=lambda e: list(e.absolute_path)):
            path = "/".join(str(x) for x in e.absolute_path) or "(root)"
            errors.append(f"schema: {path}: {e.message}")
    except ImportError:
        warnings.append("jsonschema not installed - schema check skipped")
    if errors:
        return errors, warnings          # structure broken; deeper checks would be noise

    rules = doc["rules"]
    by_code = {}
    for r in rules:
        if r["code"] in by_code:
            errors.append(f"duplicate rule code {r['code']}")
        by_code[r["code"]] = r

    top_comp = doc.get("company_id")
    graph = {}
    for r in rules:
        code, seen = r["code"], set()
        for v in r["versions"]:
            k = vkey(code, v)
            if k in seen:
                errors.append(f"duplicate version {fmt_key(k)}")
            seen.add(k)
            if v["effective_to"] and _d(v["effective_to"]) < _d(v["effective_from"]):
                errors.append(f"{fmt_key(k)}: effective_to is before effective_from")
            if top_comp is None and v.get("company_id"):
                errors.append(f"{fmt_key(k)}: company override inside a country-level file")
            if top_comp and v.get("company_id") not in (None, top_comp):
                errors.append(f"{fmt_key(k)}: belongs to another company")
            p = v["params"]
            if r["rule_type"] == "rate_by_category":
                keys = [c["key"] for c in p["categories"]]
                if len(keys) != len(set(keys)):
                    errors.append(f"{fmt_key(k)}: duplicate category keys")
            if r["rule_type"] == "bracket_table":
                b = p["brackets"]
                if b[0]["from"] != 0:
                    errors.append(f"{fmt_key(k)}: first bracket must start at 0")
                for i in range(len(b) - 1):
                    if b[i]["to"] is None or b[i]["to"] != b[i + 1]["from"]:
                        errors.append(f"{fmt_key(k)}: bracket {i + 1} 'to' must equal bracket {i + 2} 'from'")
                if b[-1]["to"] is not None:
                    warnings.append(f"{fmt_key(k)}: last bracket has an upper limit; amounts above it are untaxed")
            if r["rule_type"] == "rate_with_cap" and p.get("base_floor") is not None and p.get("base_cap") is not None \
                    and p["base_floor"] > p["base_cap"]:
                errors.append(f"{fmt_key(k)}: base_floor is above base_cap")
            if r["rule_type"] == "formula":
                e, refs = _formula_problems(code, p, by_code)
                errors += [f"{fmt_key(k)}: {x}" for x in e]
                graph.setdefault(code, set()).update(refs)

        # 2. date coverage per (company) stream
        for comp in {v.get("company_id") for v in r["versions"]}:
            act = sorted([v for v in r["versions"] if v.get("company_id") == comp and v["status"] in ACTIVE_STATUSES],
                         key=lambda v: v["effective_from"])
            for a, b in zip(act, act[1:]):
                a_to = _d(a["effective_to"])
                if a_to is None or a_to >= _d(b["effective_from"]):
                    errors.append(f"{code}: v{a['version']} and v{b['version']} overlap "
                                  f"(close v{a['version']} with effective_to = day before {b['effective_from']})")
                elif r["is_mandatory"] and comp is None and (a_to + dt.timedelta(days=1)) != _d(b["effective_from"]):
                    errors.append(f"{code}: gap between v{a['version']} (to {a['effective_to']}) and v{b['version']} "
                                  f"(from {b['effective_from']}) - mandatory rule")
            if r["is_mandatory"] and comp is None and act and act[-1]["effective_to"]:
                warnings.append(f"{code}: no version in force after {act[-1]['effective_to']} - add a successor before then")
            if any(v["status"] == "draft" for v in r["versions"] if v.get("company_id") == comp):
                warnings.append(f"{code}: has draft version(s) - tests below run against them")

    # 3. formula reference cycles
    def cyc(node, stack):
        for nxt in graph.get(node, ()):
            if nxt in stack:
                return stack[stack.index(nxt):] + [nxt]
            c = cyc(nxt, stack + [nxt])
            if c:
                return c
        return None
    for code in graph:
        c = cyc(code, [code])
        if c:
            errors.append("formula cycle: " + " -> ".join(c))
            break

    # 4. test cases
    ids = [t["id"] for t in doc["test_cases"]]
    for i in {x for x in ids if ids.count(x) > 1}:
        errors.append(f"duplicate test id {i}")
    tested = set()
    for t in doc["test_cases"]:
        if t["rule"] not in by_code:
            errors.append(f"{t['id']}: unknown rule {t['rule']}")
        tested.add(t["rule"])
    for code in by_code:
        if code not in tested:
            warnings.append(f"{code}: no test case")
    if not errors:
        for tid, ok, msg in run_tests(doc):
            if not ok:
                errors.append(f"test {tid} FAILED: {msg}")

    # 5. comparison with the live rule set (import check)
    if base is not None:
        base = normalize(base)
        live = checksum(base)
        if doc["base_checksum"] != live and not merge_anyway:
            errors.append(f"base_checksum {doc['base_checksum'][:19]}... does not match the current rule set "
                          f"{live[:19]}... - the file was exported from an older/different rule set "
                          f"(use --merge-anyway to review conflicts)")
        if doc["jurisdiction"] != base["jurisdiction"]:
            errors.append(f"jurisdiction {doc['jurisdiction']} does not match current {base['jurisdiction']}")
        new_v = {vkey(r["code"], v): (r, v) for r in doc["rules"] for v in r["versions"]}
        old_v = {vkey(r["code"], v): (r, v) for r in base["rules"] for v in r["versions"]}
        for k, (r, ov) in old_v.items():
            if ov["status"] != "approved":
                continue
            if k not in new_v:
                errors.append(f"{fmt_key(k)} is approved and was removed - approved versions cannot be deleted")
                continue
            nv = new_v[k][1]
            for f in sorted(set(ov) | set(nv)):
                if ov.get(f) == nv.get(f):
                    continue
                if f == "effective_to" and ov[f] is None and nv[f] is not None:
                    continue                                   # closing an open-ended version is allowed
                errors.append(f"{fmt_key(k)} is approved - '{f}' cannot change "
                              f"({json.dumps(ov.get(f), ensure_ascii=False)[:60]} -> "
                              f"{json.dumps(nv.get(f), ensure_ascii=False)[:60]})")
        for k, (r, nv) in new_v.items():
            if k not in old_v and nv["status"] != "draft":
                errors.append(f"{fmt_key(k)} is new - it must have status 'draft' (it is '{nv['status']}')")
        oldr = {r["code"]: r for r in base["rules"]}
        for code, r in by_code.items():
            if code in oldr and any(v["status"] == "approved" for v in oldr[code]["versions"]):
                for f in ("category", "rule_type"):
                    if r[f] != oldr[code][f]:
                        errors.append(f"{code}: '{f}' cannot change once a version is approved")
        if not doc.get("change_summary", "").strip():
            errors.append("change_summary is empty - describe what changed and why")
    return errors, warnings


# =====================================================================
# Diff
# =====================================================================
def _flat(o, prefix=""):
    out = {}
    if isinstance(o, dict):
        for k, v in o.items():
            out.update(_flat(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            out.update(_flat(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = o
    return out


def diff(old: dict, new: dict):
    old, new = normalize(old), normalize(new)
    lines = []
    ov = {vkey(r["code"], v): v for r in old["rules"] for v in r["versions"]}
    nv = {vkey(r["code"], v): v for r in new["rules"] for v in r["versions"]}
    orules = {r["code"]: r for r in old["rules"]}
    nrules = {r["code"]: r for r in new["rules"]}
    for c in nrules.keys() - orules.keys():
        lines.append(f"+ RULE  {c}  ({nrules[c]['rule_type']}, {nrules[c]['category']}) {nrules[c]['name_en']}")
    for c in orules.keys() - nrules.keys():
        lines.append(f"- RULE  {c}")
    for c in sorted(nrules.keys() & orules.keys()):
        for f in ("name_en", "name_th", "description", "is_mandatory", "category", "rule_type"):
            if orules[c][f] != nrules[c][f]:
                lines.append(f"~ RULE  {c}.{f}: {orules[c][f]!r} -> {nrules[c][f]!r}")
    for k in sorted(nv.keys() - ov.keys(), key=str):
        v = nv[k]
        lines.append(f"+ VERSION {fmt_key(k)}  [{v['status']}]  {v['effective_from']} .. {v['effective_to'] or 'open'}")
        for p, val in _flat(v["params"]).items():
            lines.append(f"      {p} = {val}")
        lines.append(f"      legal_reference = {v['legal_reference']}")
    for k in sorted(ov.keys() - nv.keys(), key=str):
        lines.append(f"- VERSION {fmt_key(k)}  [{ov[k]['status']}]")
    for k in sorted(ov.keys() & nv.keys(), key=str):
        a, b = _flat(ov[k]), _flat(nv[k])
        for p in sorted(a.keys() | b.keys()):
            if a.get(p) != b.get(p):
                lines.append(f"~ VERSION {fmt_key(k)}  {p}: {a.get(p)!r} -> {b.get(p)!r}")
    ot = {t["id"]: t for t in old["test_cases"]}
    nt = {t["id"]: t for t in new["test_cases"]}
    for i in sorted(nt.keys() - ot.keys()):
        lines.append(f"+ TEST  {i}  {nt[i]['rule']} @ {nt[i]['as_of']}: {nt[i]['description']} -> {nt[i]['expected']}")
    for i in sorted(ot.keys() - nt.keys()):
        lines.append(f"- TEST  {i}")
    for i in sorted(ot.keys() & nt.keys()):
        a, b = _flat(ot[i]), _flat(nt[i])
        for p in sorted(a.keys() | b.keys()):
            if a.get(p) != b.get(p):
                lines.append(f"~ TEST  {i}  {p}: {a.get(p)!r} -> {b.get(p)!r}")
    if old.get("policies") != new.get("policies"):
        lines.append("~ POLICIES changed")
    return lines


# =====================================================================
# XLSX  <->  JSON
# =====================================================================
LISTS = {
    "rule_type": ["flat_rate", "rate_by_category", "rate_with_cap", "fixed_amount", "bracket_table", "threshold", "formula"],
    "category": ["VAT", "WHT", "PIT", "SSO", "OTHER"],
    "status": ["draft", "submitted", "approved", "rejected", "retired"],
    "rounding": ["half_up:0.01", "half_up:1", "down:0.01", "down:1", "up:1", "none"],
    "boolean": ["TRUE", "FALSE"],
    "io": ["input", "expected"],
    "retro_mode": ["block", "adjust_next_period", "recalculate_reopen"],
}
SHEETS = {
    "Rules":      ["code", "name_en", "name_th", "category", "rule_type", "is_mandatory", "description"],
    "Versions":   ["rule_code", "company_id", "version", "status", "effective_from", "effective_to",
                   "rounding", "legal_reference", "source_url", "notes"],
    "Params":     ["rule_code", "company_id", "version", "param", "value"],
    "Categories": ["rule_code", "company_id", "version", "key", "label_en", "label_th", "rate"],
    "Brackets":   ["rule_code", "company_id", "version", "from", "to", "rate"],
    "Formulas":   ["rule_code", "company_id", "version", "output", "inputs", "expression"],
    "TestCases":  ["id", "rule", "company_id", "as_of", "description"],
    "TestValues": ["test_id", "io", "key", "value"],
    "ApprovalPolicy": ["category", "required_approvals", "approver_roles", "escalate_after_days"],
    "RetroPolicy":    ["category", "mode"],
}
README_KEYS = ["format", "schema_version", "jurisdiction", "company_id", "exported_at", "exported_by",
               "base_checksum", "jurisdiction_name", "base_currency", "default_rounding",
               "change_summary", "updater_instructions", "supported_rule_types", "formula_functions"]
DATE_COLS = {"effective_from", "effective_to", "as_of"}
EXTRA_ROWS = 300     # empty unlocked rows below the data for additions


def write_xlsx(doc: dict, path: str):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill, Protection, Border, Side
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.comments import Comment

    doc = normalize(doc)
    wb = Workbook()
    F = "Arial"
    hdr_font, hdr_fill = Font(name=F, bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F3864")
    body = Font(name=F, size=10)
    lock_fill = PatternFill("solid", fgColor="D9D9D9")
    edit_fill = PatternFill("solid", fgColor="FFF2CC")
    thin = Side(style="thin", color="BFBFBF")
    border = Border(bottom=thin)

    # ---------- README ----------
    ws = wb.active
    ws.title = "README"
    ws["A1"] = "simply handle IT ERP - Tax rule set"
    ws["A1"].font = Font(name=F, bold=True, size=14, color="1F3864")
    ji = doc["jurisdiction_info"]
    vals = {
        "format": doc["format"], "schema_version": doc["schema_version"], "jurisdiction": doc["jurisdiction"],
        "company_id": doc.get("company_id"), "exported_at": doc["exported_at"], "exported_by": doc.get("exported_by"),
        "base_checksum": doc["base_checksum"], "jurisdiction_name": ji["name"], "base_currency": ji["base_currency"],
        "default_rounding": ji["default_rounding"], "change_summary": doc.get("change_summary", ""),
        "updater_instructions": doc["updater_instructions"],
        "supported_rule_types": ", ".join(doc.get("supported_rule_types", [])),
        "formula_functions": " | ".join(doc.get("formula_functions", [])),
    }
    ws["A3"], ws["B3"] = "key", "value"
    for c in (ws["A3"], ws["B3"]):
        c.font, c.fill = hdr_font, hdr_fill
    for i, k in enumerate(README_KEYS, start=4):
        ws.cell(i, 1, k).font = Font(name=F, bold=True, size=10)
        c = ws.cell(i, 2, vals[k])
        c.font, c.alignment = body, Alignment(wrap_text=True, vertical="top")
        c.number_format = "@"
        c.protection = Protection(locked=(k != "change_summary"))
        if k == "change_summary":
            c.fill = edit_fill
            ws.row_dimensions[i].height = 60
        if k == "updater_instructions":
            ws.row_dimensions[i].height = 120
    r0 = 4 + len(README_KEYS) + 1
    legend = [
        ("How to use this workbook", None),
        ("Grey rows", "approved versions - locked. Only an empty effective_to may be filled (to close the version)."),
        ("White rows", "editable. Add new rows at the bottom of each sheet."),
        ("Yellow cell", "change_summary - required when sending changes back."),
        ("Change a rule", "add a row in Versions with the next version number, status = draft, dates and legal_reference; "
                          "add its values in Params / Categories / Brackets / Formulas with the same rule_code + version."),
        ("Tests", "add rows in TestCases + TestValues (io = input / expected) for every changed rule."),
        ("Rates", "fractions: 0.07 = 7%. Dates: YYYY-MM-DD. company_id empty = country rule."),
        ("Do not", "rename sheets or columns, or change base_checksum."),
    ]
    for j, (a, b) in enumerate(legend):
        ws.cell(r0 + j, 1, a).font = Font(name=F, bold=True, size=11 if b is None else 10,
                                         color="1F3864" if b is None else "000000")
        if b:
            c = ws.cell(r0 + j, 2, b)
            c.font, c.alignment = body, Alignment(wrap_text=True)
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 110
    ws.protection.sheet = True

    # ---------- Lists ----------
    wl = wb.create_sheet("Lists")
    list_ref = {}
    for ci, (name, items) in enumerate(LISTS.items(), start=1):
        col = wl.cell(1, ci).column_letter
        wl.cell(1, ci, name).font = hdr_font
        wl.cell(1, ci).fill = hdr_fill
        for ri, it in enumerate(items, start=2):
            wl.cell(ri, ci, it).font = body
        list_ref[name] = f"=Lists!${col}$2:${col}${len(items) + 1}"
        wl.column_dimensions[col].width = 22
    wl.protection.sheet = True

    # ---------- rows ----------
    approved = {vkey(r["code"], v) for r in doc["rules"] for v in r["versions"] if v["status"] == "approved"}
    rules_locked = {r["code"] for r in doc["rules"] if any(v["status"] == "approved" for v in r["versions"])}
    rows = {s: [] for s in SHEETS}      # (values, locked_flag, open_effective_to)
    for r in doc["rules"]:
        rows["Rules"].append(([r["code"], r["name_en"], r["name_th"], r["category"], r["rule_type"],
                               r["is_mandatory"], r["description"]], r["code"] in rules_locked, False))
        for v in r["versions"]:
            k = vkey(r["code"], v)
            lk = k in approved
            ident = [r["code"], v.get("company_id"), v["version"]]
            rows["Versions"].append((ident + [v["status"], v["effective_from"], v["effective_to"], v["rounding"],
                                              v["legal_reference"], v["source_url"], v["notes"]],
                                     lk, v["effective_to"] is None))
            p = v["params"]
            rt = r["rule_type"]
            if rt == "rate_by_category":
                for c in p["categories"]:
                    rows["Categories"].append((ident + [c["key"], c["label_en"], c["label_th"], c["rate"]], lk, False))
            elif rt == "bracket_table":
                for b in p["brackets"]:
                    rows["Brackets"].append((ident + [b["from"], b["to"], b["rate"]], lk, False))
                if "basis" in p:
                    rows["Params"].append((ident + ["basis", p["basis"]], lk, False))
            elif rt == "formula":
                rows["Formulas"].append((ident + [p["output"], ", ".join(p["inputs"]), p["expression"]], lk, False))
            else:
                for pk, pv in p.items():
                    rows["Params"].append((ident + [pk, pv], lk, False))
    for t in doc["test_cases"]:
        rows["TestCases"].append(([t["id"], t["rule"], t.get("company_id"), t["as_of"], t["description"]], False, False))
        for io, dct in (("input", t["inputs"]), ("expected", t["expected"])):
            for kk, vv in dct.items():
                rows["TestValues"].append(([t["id"], io, kk, vv], False, False))
    pol = doc.get("policies") or {}
    for a in pol.get("approval", []):
        rows["ApprovalPolicy"].append(([a["category"], a["required_approvals"], ", ".join(a["approver_roles"]),
                                        a.get("escalate_after_days")], False, False))
    for a in pol.get("retro", []):
        rows["RetroPolicy"].append(([a["category"], a["mode"]], False, False))

    widths = {"name_en": 44, "name_th": 30, "description": 50, "legal_reference": 48, "source_url": 30, "notes": 40,
              "expression": 120, "inputs": 60, "label_en": 26, "label_th": 22, "rule_code": 24, "code": 24,
              "rule": 18, "id": 14, "test_id": 14, "key": 20, "param": 18, "value": 16, "approver_roles": 36}
    dv_map = {("Rules", "category"): "category", ("Rules", "rule_type"): "rule_type",
              ("Rules", "is_mandatory"): "boolean", ("Versions", "status"): "status",
              ("Versions", "rounding"): "rounding", ("TestValues", "io"): "io",
              ("ApprovalPolicy", "category"): "category", ("RetroPolicy", "category"): "category",
              ("RetroPolicy", "mode"): "retro_mode"}
    pct_cols = {("Categories", "rate"), ("Brackets", "rate")}

    for sname, cols in SHEETS.items():
        if sname in ("ApprovalPolicy", "RetroPolicy") and not doc.get("policies"):
            continue
        ws = wb.create_sheet(sname, index=len(wb.sheetnames) - 1)    # keep Lists last
        for ci, h in enumerate(cols, start=1):
            c = ws.cell(1, ci, h)
            c.font, c.fill, c.alignment = hdr_font, hdr_fill, Alignment(horizontal="center")
            ws.column_dimensions[c.column_letter].width = widths.get(h, 14)
        data = rows[sname]
        last = 1 + len(data) + EXTRA_ROWS
        for ri in range(2, last + 1):
            rec = data[ri - 2] if ri - 2 < len(data) else None
            for ci, h in enumerate(cols, start=1):
                c = ws.cell(ri, ci)
                if rec:
                    val = rec[0][ci - 1]
                    c.value = val
                c.font, c.border = body, border
                if h in DATE_COLS or h in ("company_id", "code", "rule_code", "id", "test_id", "rule"):
                    c.number_format = "@"
                elif (sname, h) in pct_cols:
                    c.number_format = "0.00%"
                if h in ("expression", "description", "legal_reference", "notes"):
                    c.alignment = Alignment(wrap_text=False, vertical="top")
                locked = bool(rec and rec[1])
                if sname == "Rules" and rec and rec[1] and h in ("name_en", "name_th", "description"):
                    locked = False
                if sname == "Versions" and rec and rec[1] and h == "effective_to" and rec[2]:
                    locked = False         # may close an open-ended approved version
                    c.fill = edit_fill
                elif locked:
                    c.fill = lock_fill
                c.protection = Protection(locked=locked)
        for ci, h in enumerate(cols, start=1):
            key = dv_map.get((sname, h))
            if key:
                col = ws.cell(1, ci).column_letter
                dv = DataValidation(type="list", formula1=list_ref[key], allow_blank=True,
                                    showErrorMessage=True, errorTitle="Invalid value",
                                    error=f"Choose a value from the list ({key}).")
                dv.add(f"{col}2:{col}{last}")
                ws.add_data_validation(dv)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{ws.cell(1, len(cols)).column_letter}{1 + len(data)}"
        ws.protection.sheet = True
        ws.protection.autoFilter = False
        ws.protection.sort = False
        ws.protection.formatColumns = False
    if "Params" in wb.sheetnames:
        wb["Params"]["D1"].comment = Comment(
            "flat_rate: rate | rate_with_cap: rate, base_floor, base_cap, max_amount | "
            "fixed_amount: amount, per, input_field, input_kind, max_count, evidence_required | "
            "threshold: min_amount, applies_to | bracket_table: basis", "ERP")
    wb.save(path)


def _cell(v, col):
    if isinstance(v, (dt.datetime, dt.date)):
        return v.date().isoformat() if isinstance(v, dt.datetime) else v.isoformat()
    if isinstance(v, str):
        v = v.strip()
        if v == "":
            return None
        if col in ("is_mandatory", "evidence_required") or col == "value":
            if v.upper() in ("TRUE", "FALSE"):
                return v.upper() == "TRUE"
    if isinstance(v, float) and v.is_integer() and col not in ("rate",):
        return int(v)
    return v


def read_xlsx(path: str) -> dict:
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True)
    problems = []

    def table(name, required=True):
        if name not in wb.sheetnames:
            if required:
                problems.append(f"missing sheet '{name}'")
            return []
        ws = wb[name]
        hdr = [c.value for c in ws[1]][:len(SHEETS[name])]
        if hdr != SHEETS[name]:
            problems.append(f"sheet '{name}': columns must be {SHEETS[name]} (found {hdr})")
            return []
        out = []
        for ri, row in enumerate(ws.iter_rows(min_row=2, max_col=len(hdr), values_only=True), start=2):
            rec = {h: _cell(v, h) for h, v in zip(hdr, row)}
            if all(v is None for v in rec.values()):
                continue
            rec["_where"] = f"{name}!row {ri}"
            out.append(rec)
        return out

    # README
    meta = {}
    if "README" in wb.sheetnames:
        for row in wb["README"].iter_rows(min_row=4, max_col=2, values_only=True):
            if row[0] in README_KEYS:
                meta[row[0]] = row[1]
    else:
        problems.append("missing sheet 'README'")

    rules_t, vers_t = table("Rules"), table("Versions")
    params_t, cats_t, br_t, fx_t = table("Params"), table("Categories"), table("Brackets"), table("Formulas")
    tc_t, tv_t = table("TestCases"), table("TestValues")
    ap_t, rp_t = table("ApprovalPolicy", False), table("RetroPolicy", False)
    if problems:
        raise ValueError("XLSX structure problems:\n  " + "\n  ".join(problems))

    def ident(rec):
        return (rec["rule_code"], rec["company_id"], rec["version"])

    rules, rule_by = [], {}
    for r in rules_t:
        obj = {"code": r["code"], "name_en": r["name_en"], "name_th": r["name_th"], "category": r["category"],
               "rule_type": r["rule_type"], "is_mandatory": bool(r["is_mandatory"]),
               "description": r["description"], "versions": []}
        rules.append(obj)
        rule_by[r["code"]] = obj
    vers_by = {}
    for v in vers_t:
        if v["rule_code"] not in rule_by:
            raise ValueError(f"{v['_where']}: rule_code '{v['rule_code']}' not in Rules sheet")
        obj = {"version": v["version"], "company_id": v["company_id"], "status": v["status"],
               "effective_from": v["effective_from"], "effective_to": v["effective_to"], "params": {},
               "rounding": v["rounding"], "legal_reference": v["legal_reference"],
               "source_url": v["source_url"], "notes": v["notes"]}
        rule_by[v["rule_code"]]["versions"].append(obj)
        vers_by[ident(v)] = (rule_by[v["rule_code"]], obj)

    def target(rec):
        k = ident(rec)
        if k not in vers_by:
            raise ValueError(f"{rec['_where']}: no Versions row for {k[0]} company={k[1]} v{k[2]}")
        return vers_by[k]

    for p in params_t:
        _, v = target(p)
        v["params"][p["param"]] = p["value"]
    for c in cats_t:
        _, v = target(c)
        v["params"].setdefault("categories", []).append(
            {"key": c["key"], "label_en": c["label_en"], "label_th": c["label_th"], "rate": c["rate"]})
    for b in br_t:
        _, v = target(b)
        v["params"].setdefault("brackets", []).append({"from": b["from"], "to": b["to"], "rate": b["rate"]})
    for f in fx_t:
        _, v = target(f)
        v["params"].update({"expression": f["expression"], "output": f["output"],
                            "inputs": [x.strip() for x in (f["inputs"] or "").split(",") if x.strip()]})

    tests, tby = [], {}
    for t in tc_t:
        obj = {"id": t["id"], "rule": t["rule"], "company_id": t["company_id"], "as_of": t["as_of"],
               "description": t["description"] or "", "inputs": {}, "expected": {}}
        tests.append(obj)
        tby[t["id"]] = obj
    for tv in tv_t:
        if tv["test_id"] not in tby:
            raise ValueError(f"{tv['_where']}: test_id '{tv['test_id']}' not in TestCases sheet")
        bucket = "inputs" if tv["io"] == "input" else "expected"
        tby[tv["test_id"]][bucket][tv["key"]] = tv["value"]

    def splitlist(s):
        return [x.strip() for x in str(s or "").replace("|", ",").split(",") if x.strip()]

    doc = {
        "format": meta.get("format"), "schema_version": meta.get("schema_version"),
        "jurisdiction": meta.get("jurisdiction"), "company_id": _cell(meta.get("company_id"), ""),
        "exported_at": meta.get("exported_at"), "exported_by": _cell(meta.get("exported_by"), ""),
        "base_checksum": meta.get("base_checksum"),
        "updater_instructions": meta.get("updater_instructions") or "",
        "change_summary": meta.get("change_summary") or "",
        "supported_rule_types": splitlist(meta.get("supported_rule_types")),
        "formula_functions": [x.strip() for x in str(meta.get("formula_functions") or "").split(" | ") if x.strip()],
        "jurisdiction_info": {"code": meta.get("jurisdiction"), "name": meta.get("jurisdiction_name"),
                              "base_currency": meta.get("base_currency"),
                              "default_rounding": meta.get("default_rounding")},
        "rules": rules, "test_cases": tests,
    }
    if ap_t or rp_t:
        doc["policies"] = {
            "approval": [{"category": a["category"], "required_approvals": a["required_approvals"],
                          "approver_roles": splitlist(a["approver_roles"]),
                          "escalate_after_days": a["escalate_after_days"]} for a in ap_t],
            "retro": [{"category": a["category"], "mode": a["mode"]} for a in rp_t],
        }
    return normalize(doc)


# =====================================================================
# IO + CLI
# =====================================================================
def load(path: str) -> dict:
    p = Path(path)
    if p.suffix.lower() == ".xlsx":
        return read_xlsx(path)
    return normalize(json.loads(p.read_text(encoding="utf-8")))


def save_json(doc: dict, path: str):
    Path(path).write_text(json.dumps(normalize(doc), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("validate"); s.add_argument("file"); s.add_argument("--base"); s.add_argument("--merge-anyway", action="store_true")
    s = sub.add_parser("test"); s.add_argument("file")
    s = sub.add_parser("to-xlsx"); s.add_argument("src"); s.add_argument("dst")
    s = sub.add_parser("to-json"); s.add_argument("src"); s.add_argument("dst")
    s = sub.add_parser("diff"); s.add_argument("old"); s.add_argument("new")
    s = sub.add_parser("checksum"); s.add_argument("file")
    s = sub.add_parser("stamp"); s.add_argument("file"); s.add_argument("out", nargs="?")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "validate":
            doc = load(a.file)
            base = load(a.base) if a.base else None
            errs, warns = validate(doc, base, a.merge_anyway)
            for w in warns:
                print(f"WARN   {w}")
            for e in errs:
                print(f"ERROR  {e}")
            n = len(doc.get("test_cases", []))
            print(f"\n{'FAILED' if errs else 'OK'}: {len(errs)} error(s), {len(warns)} warning(s), {n} test case(s)")
            if base is not None:
                print("\nChanges vs current rule set:")
                for line in diff(base, doc) or ["(no changes)"]:
                    print("  " + line)
            return 1 if errs else 0
        if a.cmd == "test":
            res = run_tests(load(a.file))
            for tid, ok, msg in res:
                print(f"{'PASS' if ok else 'FAIL'}  {tid}  {'' if ok else msg}")
            bad = sum(1 for _, ok, _ in res if not ok)
            print(f"\n{len(res) - bad}/{len(res)} passed")
            return 1 if bad else 0
        if a.cmd == "to-xlsx":
            write_xlsx(load(a.src), a.dst); print(f"wrote {a.dst}"); return 0
        if a.cmd == "to-json":
            save_json(load(a.src), a.dst); print(f"wrote {a.dst}"); return 0
        if a.cmd == "diff":
            for line in diff(load(a.old), load(a.new)) or ["(no changes)"]:
                print(line)
            return 0
        if a.cmd == "checksum":
            print(checksum(load(a.file))); return 0
        if a.cmd == "stamp":
            doc = load(a.file)
            doc["base_checksum"] = checksum(doc)
            doc["exported_at"] = dt.datetime.now(dt.timezone(dt.timedelta(hours=7))).isoformat(timespec="seconds")
            out = a.out or a.file
            (write_xlsx if out.lower().endswith(".xlsx") else save_json)(doc, out)
            print(f"{out}: base_checksum = {doc['base_checksum']}"); return 0
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"ERROR  {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
