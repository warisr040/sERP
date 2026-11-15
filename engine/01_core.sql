-- =====================================================================
--  simply handle IT ERP — core schema + base sample data   (PostgreSQL 16)
--  Load order: 01_core.sql -> 02_tax_rules.sql -> taxdb.py import/payroll (see build_sample.py)
--  Fictional company, Thai tax rules simplified, currency THB
--  Period covered: Oct 2026 (full month) + early Nov 2026
--
--  Run:  psql -d your_db -f erp_sample.sql
--  Re-runnable: drops and rebuilds schema "erp".
-- =====================================================================

DROP SCHEMA IF EXISTS erp CASCADE;
CREATE SCHEMA erp;
SET search_path TO erp;

-- ---------------------------------------------------------------------
-- 1. MASTER DATA
-- ---------------------------------------------------------------------
CREATE TABLE company (
    company_id      SMALLINT PRIMARY KEY,
    name_en         TEXT NOT NULL,
    name_th         TEXT,
    tax_id          CHAR(13) NOT NULL,
    vat_registered  BOOLEAN NOT NULL DEFAULT TRUE,
    base_currency   CHAR(3) NOT NULL DEFAULT 'THB',
    address         TEXT
);

CREATE TABLE fiscal_period (
    period_id   TEXT PRIMARY KEY,               -- 'YYYY-MM'
    company_id  SMALLINT NOT NULL REFERENCES company,
    start_date  DATE NOT NULL,
    end_date    DATE NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','closed','locked')),
    CHECK (end_date >= start_date)
);

-- Chart of accounts. normal_side tells reports which side is "positive".
CREATE TABLE account (
    account_code  TEXT PRIMARY KEY,
    name_en       TEXT NOT NULL,
    name_th       TEXT,
    account_type  TEXT NOT NULL
                  CHECK (account_type IN ('asset','liability','equity','revenue','expense')),
    normal_side   CHAR(1) NOT NULL CHECK (normal_side IN ('D','C')),
    parent_code   TEXT REFERENCES account,
    is_postable   BOOLEAN NOT NULL DEFAULT TRUE,   -- headers = false
    is_active     BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE partner (                              -- customers & vendors
    partner_id    TEXT PRIMARY KEY,
    partner_type  TEXT NOT NULL CHECK (partner_type IN ('customer','vendor','both')),
    name          TEXT NOT NULL,
    tax_id        CHAR(13),
    branch        TEXT DEFAULT '00000',
    payment_terms_days INT NOT NULL DEFAULT 30,
    email         TEXT,
    phone         TEXT
);

CREATE TABLE department (
    dept_code  TEXT PRIMARY KEY,
    name       TEXT NOT NULL
);

CREATE TABLE employee (
    employee_id     TEXT PRIMARY KEY,
    first_name      TEXT NOT NULL,
    last_name       TEXT NOT NULL,
    dept_code       TEXT NOT NULL REFERENCES department,
    position        TEXT NOT NULL,
    hire_date       DATE NOT NULL,
    monthly_salary  NUMERIC(15,2) NOT NULL CHECK (monthly_salary >= 0),
    bank_name       TEXT,
    bank_account_masked TEXT,                        -- never store full numbers in samples
    is_active       BOOLEAN NOT NULL DEFAULT TRUE
);

-- ---------------------------------------------------------------------
-- 2. GENERAL LEDGER (double entry)
-- ---------------------------------------------------------------------
CREATE TABLE journal_entry (
    je_no        TEXT PRIMARY KEY,
    entry_date   DATE NOT NULL,
    period_id    TEXT NOT NULL REFERENCES fiscal_period,
    journal_type TEXT NOT NULL
                 CHECK (journal_type IN ('GJ','SALES','PURCHASE','RECEIPT','PAYMENT','PAYROLL','ADJ')),
    description  TEXT NOT NULL,
    source_doc   TEXT,                               -- invoice / bill / payroll ref
    status       TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft','posted','void')),
    created_by   TEXT NOT NULL DEFAULT 'system',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE journal_line (
    je_no         TEXT NOT NULL REFERENCES journal_entry ON DELETE CASCADE,
    line_no       SMALLINT NOT NULL,
    account_code  TEXT NOT NULL REFERENCES account,
    partner_id    TEXT REFERENCES partner,
    employee_id   TEXT REFERENCES employee,
    debit         NUMERIC(15,2) NOT NULL DEFAULT 0 CHECK (debit  >= 0),
    credit        NUMERIC(15,2) NOT NULL DEFAULT 0 CHECK (credit >= 0),
    memo          TEXT,
    PRIMARY KEY (je_no, line_no),
    CHECK ((debit = 0) <> (credit = 0))              -- exactly one side per line
);

-- Posted entries must balance (checked at COMMIT so lines can be inserted one by one)
CREATE FUNCTION check_je_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE d NUMERIC; c NUMERIC; st TEXT; k TEXT := COALESCE(NEW.je_no, OLD.je_no);
BEGIN
    SELECT status INTO st FROM journal_entry WHERE je_no = k;
    IF st = 'posted' THEN
        SELECT COALESCE(SUM(debit),0), COALESCE(SUM(credit),0) INTO d, c
          FROM journal_line WHERE je_no = k;
        IF d <> c OR d = 0 THEN
            RAISE EXCEPTION 'Journal % is not balanced (Dr % / Cr %)', k, d, c;
        END IF;
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER trg_line_balanced
    AFTER INSERT OR UPDATE OR DELETE ON journal_line
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_je_balanced();
CREATE CONSTRAINT TRIGGER trg_header_balanced
    AFTER INSERT OR UPDATE ON journal_entry
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_je_balanced();

-- ---------------------------------------------------------------------
-- 3. SUB-LEDGERS: invoices, bills, payments
-- ---------------------------------------------------------------------
CREATE TABLE invoice (                              -- AR (sales) and AP (purchase bills)
    doc_no       TEXT PRIMARY KEY,
    doc_type     TEXT NOT NULL CHECK (doc_type IN ('AR','AP')),
    partner_id   TEXT NOT NULL REFERENCES partner,
    doc_date     DATE NOT NULL,
    due_date     DATE NOT NULL,
    subtotal     NUMERIC(15,2) NOT NULL,
    vat_rate     NUMERIC(5,2)  NOT NULL,              -- copied from the VAT rule version
    vat_amount   NUMERIC(15,2) NOT NULL,
    total        NUMERIC(15,2) NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open'
                 CHECK (status IN ('draft','open','partial','paid','void')),
    je_no        TEXT REFERENCES journal_entry,
    CHECK (total = subtotal + vat_amount)
);

CREATE TABLE invoice_line (
    doc_no        TEXT NOT NULL REFERENCES invoice ON DELETE CASCADE,
    line_no       SMALLINT NOT NULL,
    description   TEXT NOT NULL,
    qty           NUMERIC(12,3) NOT NULL,
    unit_price    NUMERIC(15,2) NOT NULL,
    amount        NUMERIC(15,2) NOT NULL,
    account_code  TEXT NOT NULL REFERENCES account,
    PRIMARY KEY (doc_no, line_no),
    CHECK (amount = ROUND(qty * unit_price, 2))
);

CREATE TABLE payment (
    payment_no    TEXT PRIMARY KEY,
    direction     TEXT NOT NULL CHECK (direction IN ('in','out')),
    payment_date  DATE NOT NULL,
    partner_id    TEXT REFERENCES partner,
    bank_account  TEXT NOT NULL REFERENCES account,
    amount        NUMERIC(15,2) NOT NULL CHECK (amount > 0),   -- cash moved
    wht_amount    NUMERIC(15,2) NOT NULL DEFAULT 0,             -- tax withheld
    method        TEXT NOT NULL CHECK (method IN ('transfer','cheque','cash','promptpay')),
    reference     TEXT,
    je_no         TEXT REFERENCES journal_entry
);

CREATE TABLE payment_allocation (
    payment_no  TEXT NOT NULL REFERENCES payment ON DELETE CASCADE,
    doc_no      TEXT NOT NULL REFERENCES invoice,
    amount      NUMERIC(15,2) NOT NULL CHECK (amount > 0),     -- settles invoice (cash + WHT)
    PRIMARY KEY (payment_no, doc_no)
);

-- ---------------------------------------------------------------------
-- 4. PAYROLL
-- ---------------------------------------------------------------------
CREATE TABLE payroll_run (
    run_id       TEXT PRIMARY KEY,
    period_id    TEXT NOT NULL REFERENCES fiscal_period,
    pay_date     DATE NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft','approved','paid')),
    je_no        TEXT REFERENCES journal_entry
);

CREATE TABLE payroll_line (
    run_id          TEXT NOT NULL REFERENCES payroll_run ON DELETE CASCADE,
    employee_id     TEXT NOT NULL REFERENCES employee,
    base_salary     NUMERIC(15,2) NOT NULL,
    overtime        NUMERIC(15,2) NOT NULL DEFAULT 0,
    commission      NUMERIC(15,2) NOT NULL DEFAULT 0,
    allowance       NUMERIC(15,2) NOT NULL DEFAULT 0,
    gross_pay       NUMERIC(15,2) GENERATED ALWAYS AS (base_salary + overtime + commission + allowance) STORED,
    sso_employee    NUMERIC(15,2) NOT NULL DEFAULT 0,   -- calculated from tax rule SSO
    sso_employer    NUMERIC(15,2) NOT NULL DEFAULT 0,
    wht_pnd1        NUMERIC(15,2) NOT NULL DEFAULT 0,   -- personal income tax withheld
    other_deduction NUMERIC(15,2) NOT NULL DEFAULT 0,
    net_pay         NUMERIC(15,2) GENERATED ALWAYS AS
                    (base_salary + overtime + commission + allowance
                     - sso_employee - wht_pnd1 - other_deduction) STORED,
    PRIMARY KEY (run_id, employee_id)
);

-- ---------------------------------------------------------------------
-- 5. REPORTING VIEWS
-- ---------------------------------------------------------------------
CREATE VIEW v_trial_balance AS
SELECT a.account_code, a.name_en, a.account_type,
       SUM(l.debit)  AS total_debit,
       SUM(l.credit) AS total_credit,
       CASE a.normal_side WHEN 'D' THEN SUM(l.debit - l.credit)
                          ELSE SUM(l.credit - l.debit) END AS balance
FROM journal_line l
JOIN journal_entry e USING (je_no)
JOIN account a USING (account_code)
WHERE e.status = 'posted'
GROUP BY a.account_code, a.name_en, a.account_type, a.normal_side
ORDER BY a.account_code;

CREATE VIEW v_ar_aging AS
SELECT i.doc_no, p.name AS customer, i.doc_date, i.due_date, i.total,
       COALESCE(SUM(pa.amount),0)                AS settled,
       i.total - COALESCE(SUM(pa.amount),0)      AS outstanding,
       GREATEST(CURRENT_DATE - i.due_date, 0)    AS days_overdue
FROM invoice i
JOIN partner p USING (partner_id)
LEFT JOIN payment_allocation pa USING (doc_no)
WHERE i.doc_type = 'AR' AND i.status <> 'void'
GROUP BY i.doc_no, p.name, i.doc_date, i.due_date, i.total;

CREATE VIEW v_income_statement AS
SELECT e.period_id, a.account_type, a.account_code, a.name_en,
       SUM(CASE a.normal_side WHEN 'C' THEN l.credit - l.debit
                              ELSE l.debit - l.credit END) AS amount
FROM journal_line l
JOIN journal_entry e USING (je_no)
JOIN account a USING (account_code)
WHERE e.status = 'posted' AND a.account_type IN ('revenue','expense')
GROUP BY e.period_id, a.account_type, a.account_code, a.name_en
ORDER BY e.period_id, a.account_type DESC, a.account_code;


-- =====================================================================
--                              SAMPLE DATA
-- =====================================================================
BEGIN;

INSERT INTO company VALUES
 (1, 'Simply Handle IT Demo Co., Ltd.', 'บริษัท ซิมพลี แฮนเดิล ไอที เดโม จำกัด',
  '0105569000001', TRUE, 'THB', '99/9 Sukhumvit Rd, Khlong Toei, Bangkok 10110');

INSERT INTO fiscal_period VALUES
 ('2026-10', 1, '2026-10-01', '2026-10-31', 'closed'),
 ('2026-11', 1, '2026-11-01', '2026-11-30', 'open'),
 ('2026-12', 1, '2026-12-01', '2026-12-31', 'open');

-- Chart of accounts (headers first, then postable accounts)
INSERT INTO account (account_code, name_en, name_th, account_type, normal_side, parent_code, is_postable) VALUES
 ('1000','Assets','สินทรัพย์','asset','D',NULL,FALSE),
 ('2000','Liabilities','หนี้สิน','liability','C',NULL,FALSE),
 ('3000','Equity','ส่วนของผู้ถือหุ้น','equity','C',NULL,FALSE),
 ('4000','Revenue','รายได้','revenue','C',NULL,FALSE),
 ('5000','Cost of Sales','ต้นทุนขาย','expense','D',NULL,FALSE),
 ('6000','Operating Expenses','ค่าใช้จ่ายในการดำเนินงาน','expense','D',NULL,FALSE),

 ('1110','Cash on hand','เงินสด','asset','D','1000',TRUE),
 ('1120','Bank - KBank current','เงินฝากกระแสรายวัน กสิกรไทย','asset','D','1000',TRUE),
 ('1130','Bank - SCB savings','เงินฝากออมทรัพย์ ไทยพาณิชย์','asset','D','1000',TRUE),
 ('1210','Accounts receivable','ลูกหนี้การค้า','asset','D','1000',TRUE),
 ('1220','Input VAT','ภาษีซื้อ','asset','D','1000',TRUE),
 ('1230','Prepaid withholding tax','ภาษีเงินได้ถูกหัก ณ ที่จ่าย','asset','D','1000',TRUE),
 ('1310','Inventory','สินค้าคงเหลือ','asset','D','1000',TRUE),
 ('1510','Office equipment','อุปกรณ์สำนักงาน','asset','D','1000',TRUE),
 ('1519','Accum. depreciation - equipment','ค่าเสื่อมราคาสะสม-อุปกรณ์','asset','C','1000',TRUE),

 ('2110','Accounts payable','เจ้าหนี้การค้า','liability','C','2000',TRUE),
 ('2120','Output VAT','ภาษีขาย','liability','C','2000',TRUE),
 ('2130','WHT payable - PND1 (salary)','ภาษีหัก ณ ที่จ่ายค้างจ่าย ภ.ง.ด.1','liability','C','2000',TRUE),
 ('2131','WHT payable - PND3/53','ภาษีหัก ณ ที่จ่ายค้างจ่าย ภ.ง.ด.3/53','liability','C','2000',TRUE),
 ('2140','Social security payable','เงินประกันสังคมค้างจ่าย','liability','C','2000',TRUE),
 ('2150','Accrued salaries','เงินเดือนค้างจ่าย','liability','C','2000',TRUE),
 ('2160','Employee deductions payable','เงินหักพนักงานค้างจ่าย','liability','C','2000',TRUE),

 ('3110','Share capital','ทุนจดทะเบียน','equity','C','3000',TRUE),
 ('3210','Retained earnings','กำไรสะสม','equity','C','3000',TRUE),

 ('4110','Service revenue','รายได้จากการให้บริการ','revenue','C','4000',TRUE),
 ('4120','Product sales','รายได้จากการขายสินค้า','revenue','C','4000',TRUE),
 ('4210','Interest income','ดอกเบี้ยรับ','revenue','C','4000',TRUE),

 ('5110','Cost of goods sold','ต้นทุนสินค้าที่ขาย','expense','D','5000',TRUE),

 ('6110','Salaries & wages','เงินเดือนและค่าจ้าง','expense','D','6000',TRUE),
 ('6120','Social security - employer','เงินสมทบประกันสังคม','expense','D','6000',TRUE),
 ('6210','Office rent','ค่าเช่าสำนักงาน','expense','D','6000',TRUE),
 ('6220','Utilities','ค่าน้ำ ค่าไฟ','expense','D','6000',TRUE),
 ('6230','Software & internet','ค่าซอฟต์แวร์และอินเทอร์เน็ต','expense','D','6000',TRUE),
 ('6310','Depreciation','ค่าเสื่อมราคา','expense','D','6000',TRUE),
 ('6410','Bank charges','ค่าธรรมเนียมธนาคาร','expense','D','6000',TRUE);

INSERT INTO partner VALUES
 ('C001','customer','Siam Auto Parts Co., Ltd.','0105560111111','00000',30,'ap@siamauto.example','02-111-1111'),
 ('C002','customer','Chiang Mai Motor Service Ltd.','0505561222222','00000',30,'acc@cmmotor.example','053-222-222'),
 ('C003','customer','Eastern Fleet Logistics Co., Ltd.','0205562333333','00001',45,'finance@efl.example','038-333-333'),
 ('V001','vendor','Bangkok Diagnostic Supply Co., Ltd.','0105563444444','00000',30,'sales@bkkdiag.example','02-444-4444'),
 ('V002','vendor','Ms. Ratree Office Rental (individual landlord)','3100500555555',NULL,0,NULL,'081-555-5555'),
 ('V003','vendor','Metropolitan Electricity Authority','0994000165510','00000',15,NULL,'1130'),
 ('V004','vendor','IT Equipment Center Co., Ltd.','0105564666666','00000',0,'shop@itec.example','02-666-6666');

INSERT INTO department VALUES
 ('MGT','Management'), ('ACC','Accounting & Finance'),
 ('ENG','Engineering'), ('SAL','Sales'), ('ADM','Administration');

INSERT INTO employee VALUES
 ('E001','Somchai','Prasert','MGT','Managing Director','2024-01-02',80000,'KBank','xxx-x-x1234-x',TRUE),
 ('E002','Napat','Wongsakul','ACC','Senior Accountant','2024-03-01',45000,'SCB','xxx-x-x2345-x',TRUE),
 ('E003','Kittipong','Srisuk','ENG','Software Engineer','2025-06-16',55000,'KBank','xxx-x-x3456-x',TRUE),
 ('E004','Pimchanok','Rattana','SAL','Sales Executive','2025-09-01',28000,'BBL','xxx-x-x4567-x',TRUE),
 ('E005','Arthit','Chaiyaporn','ADM','Admin Officer','2026-02-01',18000,'KTB','xxx-x-x5678-x',TRUE);

-- ---------------- October 2026 journal entries ----------------
INSERT INTO journal_entry (je_no, entry_date, period_id, journal_type, description, source_doc, status, created_by) VALUES
 ('JE-2610-001','2026-10-01','2026-10','GJ','Owner capital injection',NULL,'posted','napat'),
 ('JE-2610-002','2026-10-02','2026-10','PURCHASE','Office laptops x3 (paid immediately)','BILL-2610-001','posted','napat'),
 ('JE-2610-003','2026-10-05','2026-10','PAYMENT','October office rent, WHT 5%','BILL-2610-002','posted','napat'),
 ('JE-2610-004','2026-10-10','2026-10','SALES','CAN/UDS diagnostic service project','INV-2610-001','posted','napat'),
 ('JE-2610-005','2026-10-15','2026-10','PURCHASE','Diagnostic adapters for resale','BILL-2610-003','posted','napat'),
 ('JE-2610-006','2026-10-20','2026-10','SALES','Sale of diagnostic adapters + COGS','INV-2610-002','posted','napat'),
 ('JE-2610-007','2026-10-25','2026-10','RECEIPT','Receipt from Siam Auto Parts, 3% WHT','RCV-2610-001','posted','napat'),
 ('JE-2610-008','2026-10-28','2026-10','PAYMENT','Electricity October','BILL-2610-004','posted','napat'),
 ('JE-2610-010','2026-10-31','2026-10','ADJ','Depreciation equipment (5 yrs straight-line)',NULL,'posted','napat'),
 ('JE-2610-011','2026-10-31','2026-10','GJ','Bank transfer fee',NULL,'posted','napat');

INSERT INTO journal_line (je_no, line_no, account_code, partner_id, employee_id, debit, credit, memo) VALUES
 ('JE-2610-001',1,'1120',NULL,NULL,1000000,0,'Capital deposit'),
 ('JE-2610-001',2,'3110',NULL,NULL,0,1000000,NULL),

 ('JE-2610-002',1,'1510','V004',NULL,120000,0,'3 laptops @ 40,000'),
 ('JE-2610-002',2,'1220','V004',NULL,8400,0,'VAT 7%'),
 ('JE-2610-002',3,'1120','V004',NULL,0,128400,NULL),

 ('JE-2610-003',1,'6210','V002',NULL,30000,0,'Rent Oct'),
 ('JE-2610-003',2,'2131','V002',NULL,0,1500,'WHT 5% PND3'),
 ('JE-2610-003',3,'1120','V002',NULL,0,28500,NULL),

 ('JE-2610-004',1,'1210','C001',NULL,214000,0,NULL),
 ('JE-2610-004',2,'4110','C001',NULL,0,200000,'Diagnostic service'),
 ('JE-2610-004',3,'2120','C001',NULL,0,14000,'VAT 7%'),

 ('JE-2610-005',1,'1310','V001',NULL,50000,0,'20 adapters @ 2,500'),
 ('JE-2610-005',2,'1220','V001',NULL,3500,0,'VAT 7%'),
 ('JE-2610-005',3,'2110','V001',NULL,0,53500,NULL),

 ('JE-2610-006',1,'1210','C002',NULL,85600,0,NULL),
 ('JE-2610-006',2,'4120','C002',NULL,0,80000,'16 adapters @ 5,000'),
 ('JE-2610-006',3,'2120','C002',NULL,0,5600,'VAT 7%'),
 ('JE-2610-006',4,'5110',NULL,NULL,40000,0,'COGS 16 @ 2,500'),
 ('JE-2610-006',5,'1310',NULL,NULL,0,40000,NULL),

 ('JE-2610-007',1,'1120','C001',NULL,208000,0,NULL),
 ('JE-2610-007',2,'1230','C001',NULL,6000,0,'Customer withheld 3% of 200,000'),
 ('JE-2610-007',3,'1210','C001',NULL,0,214000,'Settle INV-2610-001'),

 ('JE-2610-008',1,'6220','V003',NULL,4500,0,NULL),
 ('JE-2610-008',2,'1220','V003',NULL,315,0,'VAT 7%'),
 ('JE-2610-008',3,'1120','V003',NULL,0,4815,NULL),

 ('JE-2610-010',1,'6310',NULL,NULL,2000,0,'120,000 / 60 months'),
 ('JE-2610-010',2,'1519',NULL,NULL,0,2000,NULL),

 ('JE-2610-011',1,'6410',NULL,NULL,25,0,NULL),
 ('JE-2610-011',2,'1120',NULL,NULL,0,25,NULL);

-- ---------------- November 2026 journal entries ----------------
INSERT INTO journal_entry (je_no, entry_date, period_id, journal_type, description, source_doc, status, created_by) VALUES
 ('JE-2611-001','2026-11-05','2026-11','PAYMENT','Pay Bangkok Diagnostic Supply','PAY-2611-001','posted','napat'),
 ('JE-2611-003','2026-11-13','2026-11','PAYMENT','VAT return PP30 October',NULL,'posted','napat'),
 ('JE-2611-004','2026-11-05','2026-11','PAYMENT','November office rent, WHT 5%','BILL-2611-001','draft','napat'),
 ('JE-2611-005','2026-11-12','2026-11','SALES','Fleet diagnostic retainer - Eastern Fleet','INV-2611-001','posted','napat');

INSERT INTO journal_line (je_no, line_no, account_code, partner_id, employee_id, debit, credit, memo) VALUES
 ('JE-2611-001',1,'2110','V001',NULL,53500,0,'Settle BILL-2610-003'),
 ('JE-2611-001',2,'1120','V001',NULL,0,53500,NULL),

 ('JE-2611-003',1,'2120',NULL,NULL,19600,0,'Output VAT Oct'),
 ('JE-2611-003',2,'1220',NULL,NULL,0,12215,'Input VAT Oct'),
 ('JE-2611-003',3,'1120',NULL,NULL,0,7385,'Net VAT paid'),

 ('JE-2611-004',1,'6210','V002',NULL,30000,0,'Rent Nov (draft, not yet approved)'),
 ('JE-2611-004',2,'2131','V002',NULL,0,1500,NULL),
 ('JE-2611-004',3,'1120','V002',NULL,0,28500,NULL),

 ('JE-2611-005',1,'1210','C003',NULL,96300,0,NULL),
 ('JE-2611-005',2,'4110','C003',NULL,0,90000,'Monthly retainer, 30 vehicles @ 3,000'),
 ('JE-2611-005',3,'2120','C003',NULL,0,6300,'VAT 7%');

-- ---------------- Invoices / bills ----------------
INSERT INTO invoice VALUES
 ('INV-2610-001','AR','C001','2026-10-10','2026-11-09',200000,7,14000,214000,'paid','JE-2610-004'),
 ('INV-2610-002','AR','C002','2026-10-20','2026-11-19', 80000,7, 5600, 85600,'open','JE-2610-006'),
 ('INV-2611-001','AR','C003','2026-11-12','2026-12-27', 90000,7, 6300, 96300,'open','JE-2611-005'),
 ('BILL-2610-001','AP','V004','2026-10-02','2026-10-02',120000,7, 8400,128400,'paid','JE-2610-002'),
 ('BILL-2610-002','AP','V002','2026-10-05','2026-10-05', 30000,0,    0, 30000,'paid','JE-2610-003'),
 ('BILL-2610-003','AP','V001','2026-10-15','2026-11-14', 50000,7, 3500, 53500,'paid','JE-2610-005'),
 ('BILL-2610-004','AP','V003','2026-10-28','2026-11-12',  4500,7,  315,  4815,'paid','JE-2610-008'),
 ('BILL-2611-001','AP','V002','2026-11-05','2026-11-05', 30000,0,    0, 30000,'draft','JE-2611-004');

INSERT INTO invoice_line VALUES
 ('INV-2610-001',1,'CAN bus / UDS diagnostic integration - phase 1',1,200000,200000,'4110'),
 ('INV-2610-002',1,'OBD-II / CAN diagnostic adapter',16,5000,80000,'4120'),
 ('INV-2611-001',1,'Fleet diagnostic retainer (per vehicle, Nov)',30,3000,90000,'4110'),
 ('BILL-2610-001',1,'Laptop 14" i7 / 32GB',3,40000,120000,'1510'),
 ('BILL-2610-002',1,'Office rent October 2026',1,30000,30000,'6210'),
 ('BILL-2610-003',1,'Diagnostic adapter (wholesale)',20,2500,50000,'1310'),
 ('BILL-2610-004',1,'Electricity October 2026',1,4500,4500,'6220'),
 ('BILL-2611-001',1,'Office rent November 2026',1,30000,30000,'6210');

INSERT INTO payment VALUES
 ('PAY-2610-001','out','2026-10-02','V004','1120',128400,   0,'transfer','KB-TRF-88120','JE-2610-002'),
 ('PAY-2610-002','out','2026-10-05','V002','1120', 28500,1500,'transfer','KB-TRF-88191','JE-2610-003'),
 ('RCV-2610-001','in' ,'2026-10-25','C001','1120',208000,6000,'transfer','KB-IN-55012','JE-2610-007'),
 ('PAY-2610-003','out','2026-10-28','V003','1120',  4815,   0,'promptpay','PP-77821','JE-2610-008'),
 ('PAY-2611-001','out','2026-11-05','V001','1120', 53500,   0,'transfer','KB-TRF-90011','JE-2611-001');

INSERT INTO payment_allocation VALUES
 ('PAY-2610-001','BILL-2610-001',128400),
 ('PAY-2610-002','BILL-2610-002', 30000),
 ('RCV-2610-001','INV-2610-001', 214000),
 ('PAY-2610-003','BILL-2610-004',  4815),
 ('PAY-2611-001','BILL-2610-003', 53500);

-- ---------------- Payroll ----------------
-- Payroll runs, payroll lines, the payroll journal (JE-2610-009) and the
-- SSO/PND1/PND3 remittance (JE-2611-002) are CALCULATED from the tax rules
-- by taxdb.py - see build_sample.py.

COMMIT;

-- =====================================================================
--  QUICK CHECKS (uncomment to run)
-- =====================================================================
-- SELECT * FROM v_trial_balance;
-- SELECT SUM(total_debit) dr, SUM(total_credit) cr FROM v_trial_balance;   -- must match
-- SELECT * FROM v_ar_aging WHERE outstanding > 0;
-- SELECT * FROM v_income_statement WHERE period_id = '2026-10';
-- SELECT employee_id, gross_pay, net_pay FROM payroll_line WHERE run_id = 'PR-2610';
