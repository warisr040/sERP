# simply handle IT ERP: tax rules in the database (step 2)

| File | Purpose |
|---|---|
| `01_core.sql` | Core schema: accounts, journal, invoices, payments, payroll tables, and the sample transactions *except payroll* |
| `02_tax_rules.sql` | Tax rule tables, constraints, triggers (immutability, maker-checker, back-dating), policies, employee tax profiles, row-level security |
| `taxrules.py` | Rule engine, validator and JSON ↔ Excel converter (same as the step 1 package, plus approved-only mode for calculations) |
| `taxdb.py` | Database tool: export/import, submit/approve/reject, rules-in-force report, back-dating report, payroll, tax remittance |
| `tax_rules_TH.json` + `tax_rules.schema.json` | Thai rule set and the file definition |
| `build_sample.py` | Builds the whole sample database from scratch and writes `erp_sample_v2.sql` |
| `erp_sample_v2.sql` | **Ready-made dump.** `psql -d <empty db> -f erp_sample_v2.sql` (PostgreSQL 16, needs the `btree_gist` extension) |
| `verify.py` | 36 end-to-end checks on a throw-away copy of the dump |

## How payroll is calculated now
`taxdb.py payroll` uses **approved rule versions only**, with this data:
- the employee's approved tax profile for the year (spouse, children, parents, life insurance…). No approved profile means defaults are used, and the payslip shows `default`;
- year-to-date income, tax withheld and social security: opening balances carried over from the old system (`employee_tax_opening`) plus payroll runs already paid;
- this month's inputs (overtime, commission, allowance, deductions) from a JSON file.

Each payroll line stores the rule versions it used and a `calc_trace` (all inputs), so reprints are exact (FR-08).

## Rule change workflow
```
taxdb.py export rules.json              # or .xlsx; hand it to Claude with the law change
taxdb.py import rules_updated.json      # checked against the live rules, applied as DRAFT
taxdb.py submit  RULE V                 # tests must pass
taxdb.py approve RULE V --user malee    # maker-checker; approvers needed per policy (PIT/SSO: 2)
# back-dated into a closed period (policy adjust_next_period / recalculate_reopen):
taxdb.py recalc-report RULE V           # old vs new per payslip, stored
taxdb.py confirm-recalc ID --user malee # second person confirms, then approval can complete
```
Closing an approved version stays *pending* until its successor is approved. If the successor is rejected, the close is cancelled, so the rule never has a gap.

## The database enforces these rules, even for direct SQL
- New versions must be drafts.
- Approved versions can't be edited or deleted. The only allowed change is closing an open end date (or retiring the version).
- No two approved versions of a rule can cover the same day (exclusion constraint).
- Status changes follow draft → submitted → approved / rejected.
- The approver must be a different person from the maker and submitter, and must hold an approver role from the policy.
- A version that starts inside a closed period is blocked, or needs a confirmed recalculation report first, depending on the policy.
- Every change is written to `tax_change_log`.
- Row-level security keeps each company's data separate when the app connects as `erp_app` and sets `erp.company_id`.

## Not done yet (next steps)
- Posting the **adjustment entries** from a confirmed recalculation report (`adjust_next_period`), and the reverse-and-repost flow (`recalculate_reopen`).
- Calculating VAT and withholding on invoices through the engine. Today they're linked to the rule version, but the amounts are still entered.
- FR-13b (changing a policy needs approval). Today it's only logged.
- Encrypting tax profile data (NFR-10, e.g. pgcrypto), and screens / an API on top of `taxdb.py`.
