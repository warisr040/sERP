# Tax Rule Engine: Requirements Analysis
**Project:** simply handle IT ERP · **Version:** draft 0.2 (decisions from Pack added) · **Date:** 2026-11-13 · **Author:** Pack

---

## 1. Purpose and problem statement

The current draft schema (`erp_sample.sql`) has tax logic **hard-coded or hand-entered**:

| Where | Problem today |
|---|---|
| `invoice.vat_rate DEFAULT 7.00` | The VAT rate lives in the schema. A rate change means a database migration. |
| `payroll_line.wht_pnd1` | The income tax is typed in by hand. No formula, no brackets, no audit trail. |
| `payroll_line.sso_employee` | The 5% rate and the 15,000 wage cap exist only as a code comment. |
| WHT on rent and services (5% / 3%) | The amount is put directly on journal lines, with no rule behind it. |

**Goal:** treat every tax rule as **versioned data with effective dates**, not as code. Then:
1. A user with the right role can revise a rule (rate, cap, bracket, allowance) **without a developer**.
2. The full rule set can be **exported as one readable file**, handed to Claude with a change request ("update for the 2027 PIT changes"), and **imported back** after validation and approval.
3. Past transactions keep the exact rule version they were calculated with.

**In scope for phase 1:** several companies and several countries (Thailand is the first rule set), employee tax profiles (personal allowances), Excel and JSON export/import, and approval and back-dating policies that admins can configure.

**Out of scope (for now):** filing to the Revenue Department e-Filing system, and corporate income tax (PND50/51).

---

## 2. Stakeholders and roles

| Role | Needs | Permissions |
|---|---|---|
| **Tax admin / Senior accountant** | Edit rules when the law changes | Create and edit drafts, run tests, submit for approval |
| **Approver (Finance manager / MD)** | Control over what goes live | Approve or reject rule versions (the editor cannot approve their own work). How many approvers are needed is a setting (FR-13a) |
| **System admin** | Set up companies, countries and policies | Manage companies, countries, approval policies and back-dating policies |
| **Accountant / Payroll officer** | Correct tax on invoices and payslips automatically | Read-only. Sees which rule version was applied |
| **Auditor** | Proof of which rule applied, when, and who changed it | Read-only access to the history and change log |
| **Claude (external updater)** | A complete, self-describing file to edit | No system access. Works only on the exported file |
| **Developer** | A stable engine API and a stable file format | Changes rule *types* and the engine, never the rule *values* |

---

## 3. Tax rules in scope (Thailand)

| Code | Rule | Rule type | Current values (from the sample data) |
|---|---|---|---|
| `VAT_STD` | Standard VAT | Flat rate | 7% (calculated on the subtotal before VAT) |
| `VAT_ZERO` / `VAT_EXEMPT` | Zero-rated / exempt | Flat rate | 0% / not applicable |
| `WHT_RENT` | WHT on rent (PND3/53) | Rate by income type | 5% |
| `WHT_SERVICE` | WHT on services | Rate by income type | 3% |
| `WHT_ADVERTISING` | WHT on advertising | Rate by income type | 2% |
| `WHT_TRANSPORT` | WHT on transport | Rate by income type | 1% |
| `WHT_MIN_THRESHOLD` | No WHT below a payment amount | Threshold | 1,000 THB |
| `SSO` | Social security, employee and employer | Rate with floor and cap | 5% of wage, base 1,650 to 15,000 (max 750) |
| `PIT_EXPENSE` | Employment expense deduction | Rate with cap | 50%, max 100,000 per year |
| `PIT_ALLOW_PERSONAL` | Personal allowance | Fixed amount | 60,000 |
| `PIT_ALLOW_*` | Spouse, child, insurance, provident fund, etc. | Fixed amount or cap per employee | Configurable |
| `PIT_BRACKETS` | Progressive income tax | Bracket table | 0 / 5 / 10 / 15 / 20 / 25 / 30 / 35% |
| `PND1_METHOD` | Monthly withholding method | Formula | (estimated annual tax − tax already withheld this year) ÷ months remaining |

The values above are the ones used in the sample data. Before go-live, a tax admin must check each one against the current Revenue Department rules.

---

## 4. Functional requirements

### 4.1 Rule definition
- **FR-01** Every rule has: `code`, Thai and English name, `rule_type`, `parameters`, `effective_from`, `effective_to` (empty = open-ended), `legal_reference` (e.g. "Revenue Code s.40(1)" or a Royal Decree number), and free-text `notes`.
- **FR-02** The engine supports a **fixed set of rule types**: `flat_rate`, `rate_by_category`, `rate_with_cap` (floor, cap, max amount), `fixed_amount`, `bracket_table`, `threshold`, and `formula`. A new type needs a developer. New *values* never do.
- **FR-03** `formula` rules use a **limited, safe expression language**: arithmetic, `min`, `max`, `round`, and references to other rules and inputs. No loops, no code, no database access.
- **FR-04** Rules can reference each other (e.g. `PND1_METHOD` uses `PIT_BRACKETS`, `PIT_EXPENSE`, and `SSO`). The system must detect and reject circular references.
- **FR-05** Rounding is configurable per rule (`half_up` or `down`, to 0.01 or 1 THB). The default is half-up to the satang (0.01 THB).

### 4.2 Versioning and effective dates
- **FR-06** A rule change creates a **new version**. Existing versions are never edited in place once they are live.
- **FR-07** Exactly one approved version of a rule may cover any given date. The system rejects gaps or overlaps for rules marked "mandatory".
  *Built:* the database enforces no overlap between **approved** versions. Closing an approved open-ended version is stored as a *pending* end date. It takes effect only when the successor is approved, and is cancelled if the successor is rejected, so a rejected change never leaves a gap.
- **FR-08** Every calculated tax amount (invoice line, payroll line, journal line) stores the **`rule_version_id`** it used. Reprinting an old payslip gives the same numbers.
- **FR-09** Retroactive changes (an `effective_from` date in a closed accounting period) follow a **configurable back-dating policy**, set per company and per rule category:

  | Policy | Behaviour |
  |---|---|
  | `block` (default) | Retroactive versions can't be approved. |
  | `adjust_next_period` | Posted entries stay unchanged. The system calculates the differences and creates **adjustment entries** (and payroll adjustment lines) in the first open period, linked to the originals. |
  | `recalculate_reopen` | The approver reopens the affected periods. The system reverses the affected entries and posts them again with the new rules, with a reversal pair for each change. This needs the period-reopen permission. |

  Whatever the policy, the system **never silently overwrites** a posted entry. It always produces a recalculation report first (old amount → new amount for each document and employee), and someone must confirm it.
- **FR-09a** The policy can be **overridden for a single change** by an approver who holds the `tax.retro_override` permission. The override and its reason are logged.

### 4.3 Editing interface
- **FR-10** Show all rules, with filters by category (VAT, WHT, PIT, SSO), status, and effective date.
- **FR-11** Use a form editor suited to each rule type: rate fields, a bracket table grid with add/remove rows, a formula field with syntax checking.
- **FR-12** Show a **before-and-after preview** of a change, with sample calculations, before it is saved. For example: "Somchai's October payslip: PND1 6,100.00 → 5,850.00".
- **FR-13** Workflow: `draft → submitted → approved → active`, and `rejected` returns it to draft. The person who submits cannot approve (maker-checker).
- **FR-13a** **Approval policy is configurable** per company and per rule category (e.g. VAT, WHT, PIT, SSO):
  - `required_approvals`: 1 to n (e.g. 1 for WHT, 2 for PIT).
  - `approver_roles`: which roles may approve (e.g. Finance manager, MD).
  - `distinct_approvers`: always true. The same person can't count twice, and the maker can't count at all.
  - An optional `escalate_after_days` sends a reminder to the next approver role.
  - A rejection by any approver sends the version back to draft.
- **FR-13b** Changing an approval or back-dating policy is itself logged, and needs the approval of the current policy's approvers.

### 4.4 Test cases (golden examples)
- **FR-14** Each rule set includes **test cases**: given inputs → expected outputs. Example: salary 80,000/month, no extra allowances → PND1 6,100.00/month.
- **FR-15** All test cases must pass before a version can be submitted. A test whose expected value changes on purpose must be updated in the same change.
- **FR-16** Run a **regression check** on approval: recalculate the last N payroll runs and invoices with the new rules and list the differences.

### 4.5 Export (to give to Claude)
- **FR-17** Export everything in one click: the whole rule set, or selected categories, as **one file** (see section 5).
- **FR-18** The export is **self-describing**. It includes the schema version, every rule with all its versions, the test cases, the list of supported rule types and formula functions, and a short "instructions for updater" header. Claude can then edit it correctly without seeing the codebase.
- **FR-19** The export contains **no personal data**. Test cases use made-up people and amounts only. Employee names, tax IDs, and salaries from the live database are never included.
- **FR-20** **Excel export (must have):** the same content as `.xlsx`, with this layout:

  | Sheet | Content |
  |---|---|
  | `README` | Instructions for the updater (same text as the JSON header), schema version, base checksum, export date, and a cell for the change summary |
  | `Rules` | One row per rule: code, names (Thai and English), category, type, mandatory, description |
  | `Versions` | One row per version: rule_code, company_id, version, status, effective from/to, rounding, legal reference, source URL, notes |
  | `Params` | Single-value settings (rate, cap, amount, threshold…): one row per setting (`rule_code`, `company_id`, `version`, `param`, `value`) |
  | `Categories` | WHT rates by income type: `key`, labels, `rate` |
  | `Brackets` | Bracket tables: `from`, `to`, `rate` |
  | `Formulas` | `output`, `inputs`, `expression` |
  | `TestCases` + `TestValues` | Test header, plus one row per input or expected value (`io`, `key`, `value`). This replaces "one column per input" so tests with different inputs fit one sheet |
  | `ApprovalPolicy` / `RetroPolicy` | Only in company-level exports |
  | `Lists` | Allowed values for the drop-down menus (rule types, categories, statuses, rounding, back-dating modes) |

  *Built in schema 1.0.0. See `tax_rules.schema.json` and `taxrules.py`.*

  Cells for approved versions are **locked**, and drop-down lists enforce valid types, categories and statuses, so editing in Excel stays safe.
- **FR-20a** **Excel import:** a returned `.xlsx` is converted to the same internal JSON and goes through **exactly the same checks, list of changes and approval** as a JSON import (FR-21 to FR-25). One rule set, two file formats, and no difference in rules.

### 4.6 Import (Claude's update comes back)
- **FR-21** Import checks the file against the JSON Schema. Invalid files are rejected, with errors listed by line or path.
- **FR-22** Import shows a **readable list of changes**: added, changed, and removed rules and versions, with old and new values side by side.
- **FR-23** Imported changes are always created as **drafts**. They go through the normal test → submit → approve workflow. Nothing goes live directly from a file.
- **FR-24** Import rejects files whose `base_checksum` doesn't match the current live rule set. This stops someone importing an update based on an old export. Users can choose to "merge anyway" with conflicts shown.
- **FR-25** Each import is logged with the file name, who imported it, a file hash, and the `change_summary` text from the file.

### 4.7 Audit
- **FR-26** Keep a full change log: who, when, what changed (old → new), why (a required comment), and the approver.
- **FR-27** Report: "Rules in force on date X". Auditors and the payslip reprint feature use it.

### 4.8 Multiple companies and multiple countries
- **FR-28** A **country rule set** (e.g. `TH`, later `JP`, `VN`, …) holds all the rules for one country. Each rule set has its own base currency, rounding defaults and income categories.
- **FR-29** Each **company** belongs to one country rule set, and inherits its rules automatically.
- **FR-30** A company may have **company-level overrides** for selected rules, for example a BOI-promoted company with special rates, or a different WHT setting. An override is a rule version marked with that company. Lookup order: *company override → country rule*.
- **FR-31** Export and import can cover: one country, one company's overrides, or everything. Each file states its `jurisdiction` and, if it has one, `company_id`.
- **FR-32** Rule codes are unique **within** a country (`TH.SSO`, `VN.SSO`). The engine never applies one country's rule to a company in another country.
- **FR-33** Reports, accounting periods and approval policies are set per company. Users only see the companies they are assigned to.

### 4.9 Employee tax profile
- **FR-34** Each employee has a **tax profile for each tax year**, with the allowances the PIT rules use: spouse, number of children (with birth years where the law depends on them), parent care, life and health insurance premiums, provident fund / RMF / SSF / ThaiESG contributions, home-loan interest, donations, and any other allowance the country rule set defines.
- **FR-35** Which allowance fields exist comes from the **country rule set** (each `PIT_ALLOW_*` rule declares its input field, cap, and evidence requirement). A new allowance type therefore needs only a rule change, not a developer.
- **FR-36** A profile change during the year triggers a **PND1 recalculation** for the remaining months, using tax already withheld this year (`ytd_withheld`). Past payslips are not changed.
- **FR-37** Profile changes are logged (who and when). If the rule says so, a field can require an uploaded document (e.g. an insurance certificate) before HR approves it.
- **FR-38** If an employee has no profile, the system uses the defaults (personal allowance, employment expense deduction, social security only). The payslip is flagged "default tax profile".
- **FR-39** Profile data is **personal data**. It is never exported in rule files (FR-19), and only HR/Payroll roles can see it.

---

## 5. Export and import file format

**Format: JSON** (or YAML as an option): readable by people, easy for Claude to edit correctly, and checkable against a JSON Schema. One file, UTF-8, Thai text allowed.

```json
{
  "format": "simplyhandleit-erp-tax-rules",
  "schema_version": "1.0",
  "jurisdiction": "TH",
  "company_id": null,
  "exported_at": "2026-11-13T21:40:00+07:00",
  "exported_by": "napat",
  "base_checksum": "sha256:9f2c...",
  "updater_instructions": "Edit values only within supported rule_types. Add a NEW version with effective_from for any change; never edit an approved version. Update or add test_cases for every change. Fill change_summary and legal_reference.",
  "change_summary": "",
  "supported_rule_types": ["flat_rate","rate_by_category","rate_with_cap","fixed_amount","bracket_table","threshold","formula"],
  "formula_functions": ["min","max","round","floor","ceil","rule","input"],

  "rules": [
    {
      "code": "SSO",
      "name_en": "Social security contribution",
      "name_th": "เงินสมทบประกันสังคม",
      "category": "SSO",
      "rule_type": "rate_with_cap",
      "versions": [
        {
          "version": 1,
          "status": "approved",
          "effective_from": "2024-01-01",
          "effective_to": null,
          "params": { "rate": 0.05, "base_floor": 1650, "base_cap": 15000, "rounding": "half_up_1" },
          "legal_reference": "Social Security Act B.E. 2533",
          "notes": "Applies to both employee and employer share"
        }
      ]
    },
    {
      "code": "PIT_BRACKETS",
      "category": "PIT",
      "rule_type": "bracket_table",
      "versions": [
        {
          "version": 1, "status": "approved", "effective_from": "2017-01-01", "effective_to": null,
          "params": { "brackets": [
            { "from": 0,       "to": 150000,  "rate": 0.00 },
            { "from": 150000,  "to": 300000,  "rate": 0.05 },
            { "from": 300000,  "to": 500000,  "rate": 0.10 },
            { "from": 500000,  "to": 750000,  "rate": 0.15 },
            { "from": 750000,  "to": 1000000, "rate": 0.20 },
            { "from": 1000000, "to": 2000000, "rate": 0.25 },
            { "from": 2000000, "to": 5000000, "rate": 0.30 },
            { "from": 5000000, "to": null,    "rate": 0.35 }
          ]}
        }
      ]
    }
  ],

  "test_cases": [
    {
      "id": "TC-PND1-001",
      "description": "Monthly salary 80,000, no extra allowances, January start",
      "rule": "PND1_METHOD",
      "inputs": { "monthly_income": 80000, "months_remaining": 12, "ytd_withheld": 0 },
      "expected": { "monthly_wht": 6100.00 }
    },
    {
      "id": "TC-SSO-001",
      "description": "Wage above cap",
      "rule": "SSO",
      "inputs": { "wage": 45000 },
      "expected": { "contribution": 750 }
    }
  ]
}
```

**Round trip with Claude:**
1. Export → `tax_rules_2026-11-13.json`
2. Give Claude the file and the change, e.g. "Royal Decree X changes the personal allowance to 80,000 from 2027-01-01. Here is the source link."
3. Claude returns the file with a **new version** added (old versions kept), test cases updated, and `change_summary` and `legal_reference` filled in.
4. Import → list of changes → tests run automatically → submit → approve → live on `effective_from`.

---

## 6. Data model changes (draft)

```
-- Countries and companies
jurisdiction        (jurisdiction_code PK 'TH', name, base_currency, default_rounding)
company             (+ jurisdiction_code FK)                      -- existing table, new column

-- Rules
tax_rule            (rule_id PK, jurisdiction_code FK, rule_code, name_en, name_th,
                     category, rule_type, is_mandatory)
                     UNIQUE (jurisdiction_code, rule_code)
tax_rule_version    (rule_version_id PK, rule_id FK, company_id FK NULL,   -- NULL = country rule, set = company override
                     version_no, status, effective_from, effective_to,
                     params JSONB, rounding, legal_reference, notes, created_by)
                     UNIQUE (rule_id, company_id, version_no)
                     EXCLUDE overlapping approved date ranges per (rule_id, company_id)  (btree_gist)
tax_rule_approval   (rule_version_id FK, approver, decision, comment, at)  -- one row per approver
tax_test_case       (test_id PK, rule_id FK, company_id FK NULL, description,
                     inputs JSONB, expected JSONB)

-- Policies (configurable)
approval_policy     (company_id FK, category, required_approvals, approver_roles TEXT[],
                     escalate_after_days)
retro_policy        (company_id FK, category,
                     mode CHECK IN ('block','adjust_next_period','recalculate_reopen'))

-- Audit and import
tax_change_log      (log_id PK, object_type, object_id, action, old_value JSONB,
                     new_value JSONB, reason, actor, at)
tax_import_batch    (batch_id PK, file_name, file_format CHECK IN ('json','xlsx'),
                     file_sha256, base_checksum, jurisdiction_code, company_id,
                     change_summary, imported_by, imported_at, result)
tax_recalc_run      (recalc_id PK, trigger_version_id FK, mode, period_from, period_to,
                     status, report JSONB, confirmed_by)      -- back-dating report and its result

-- Employee tax profile
employee_tax_profile (employee_id FK, tax_year, allowances JSONB,  -- keys defined by PIT_ALLOW_* rules
                      status, approved_by, updated_at)
                      PRIMARY KEY (employee_id, tax_year)
employee_tax_evidence (employee_id, tax_year, allowance_key, file_ref, verified_by)
```

Changes to existing tables:
- `invoice.vat_rate DEFAULT 7.00` → replace with `vat_rule_version_id FK`. The rate is copied onto the invoice for display.
- `payroll_line` gets `sso_rule_version_id` and `pit_rule_version_id`. `wht_pnd1` and `sso_*` become **calculated by the engine**, not typed in.
- `journal_line` gets an optional `tax_rule_version_id` (for WHT and VAT lines).
- `payroll_line` and `journal_line` get an optional `adjusts_ref` that links an adjustment back to the original entry (back-dating mode `adjust_next_period`).
- `employee` and every transaction table carry a `company_id`, so each company's data stays separate.

---

## 7. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-01 | **Accuracy:** money is stored as `NUMERIC`, never floating point. Rounding happens only where a rule says so. |
| NFR-02 | **Traceability:** every tax amount can be traced to rule version → approver → legal reference. |
| NFR-03 | **Security:** role-based access, maker-checker approval, and no personal data in exports. |
| NFR-04 | **Performance:** a full payroll of 500 employees calculates in under 5 seconds. Rule lookup by date is indexed. |
| NFR-05 | **Format stability:** `schema_version` follows semver. The importer accepts the current and previous minor version. |
| NFR-06 | **Language:** all rule names and messages in Thai and English. |
| NFR-07 | **Testability:** the engine is a pure function (inputs + rule set → outputs), so it can be unit-tested without a database. |
| NFR-08 | **Multiple companies and countries in phase 1:** each company's data is kept separate (row-level security per `company_id`), and every rule belongs to a country. |
| NFR-09 | **Same result from either file format:** exporting to JSON and to xlsx, then importing each back unchanged, must give an identical rule set with an identical checksum. |
| NFR-10 | **Personal data protection (PDPA):** employee tax profiles and evidence documents are encrypted, and access to them is logged. |

---

## 8. Risks, open questions, next steps

**Risks**
- *Wrong rule goes live* → reduced by test cases, regression differences, and maker-checker approval.
- *Claude edits an approved version instead of adding a new one* → the importer rejects changes to approved versions.
- *The formula language grows into a programming language* → keep the function list small and fixed. Add new rule types through development instead.
- *Mid-year PIT changes* → the PND1 method must recalculate the remaining months using tax already withheld this year (`ytd_withheld`).

- *Excel edits break the structure* (renamed sheets, merged cells) → the importer checks sheet and column names strictly and points to the exact cell for each error.
- *A company override hides a later country-level change* → the list of changes and the "rules in force" report show overridden rules side by side with the country rule.

**Decisions (Pack, 2026-11-13)**

| # | Question | Decision |
|---|---|---|
| 1 | Number of approvers | **Configurable** per company and category (FR-13a) |
| 2 | Back-dated changes | **Configurable policy**: `block` / `adjust_next_period` / `recalculate_reopen`, with a logged override for a single change (FR-09, FR-09a) |
| 3 | Export format | **JSON and xlsx**, both with full export and import (FR-20, FR-20a) |
| 4 | Multiple companies / countries | **Both in phase 1** (section 4.8) |
| 5 | Employee allowances | **In phase 1**, with the allowance fields defined by the rules (section 4.9) |

**Suggested defaults when a new company is set up** (admins can change them):
- Approvals: 1 for VAT and WHT; 2 for PIT and SSO.
- Back-dating: `adjust_next_period` for payroll; `block` for VAT (VAT returns already filed shouldn't change silently).

**Next steps**
1. Write the JSON Schema v1.0 and the matching Excel template (one definition produces both).
2. Build the tables in section 6, then migrate the sample data's VAT, WHT, SSO, and PIT values into the `TH` rule set.
3. Build the calculation engine (a pure function) and the test-case runner.
4. Build the approval and back-dating policy features, with the recalculation report.
5. Build the editor screens, the export/import (JSON and xlsx) with the list of changes, and the employee tax profile screen.
