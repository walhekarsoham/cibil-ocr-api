"""
CIBIL Score Report Parser v2
============================
Parses CIBIL PDFs via OCR and outputs a fully structured JSON + SQLite DB
matching the required 9-section schema.

Usage:
    python cibil_parser_v2.py --pdf Cibil_report.pdf
    python cibil_parser_v2.py --pdf Cibil_report.pdf --db cibil_v2.db --json-out result.json

Requirements:
    pip install pdf2image pytesseract pillow
    + Tesseract OCR + Poppler installed on system
"""

import re, json, sqlite3, argparse
from datetime import datetime
from pathlib import Path
import pytesseract
  # Windows: set to r"D:\Release-25.12.0-0\poppler-25.12.0\Library\bin"

# ═══════════════════════════════════════════════════════════════════════════════
# OCR
# ═══════════════════════════════════════════════════════════════════════════════
def ocr_pdf(pdf_path: str) -> list[str]:
    from pdf2image import convert_from_path
    import pytesseract

    # Cloud-safe version (no Windows paths)
    images = convert_from_path(pdf_path, dpi=200)

    return [pytesseract.image_to_string(img) for img in images]

# ═══════════════════════════════════════════════════════════════════════════════
# DATE NORMALISER  DD/MM/YYYY → YYYY-MM-DD
# ═══════════════════════════════════════════════════════════════════════════════
def norm_date(raw: str) -> str | None:
    if not raw or raw.strip() in ("-", "", "None"):
        return None
    raw = raw.strip()
    m = re.match(r"(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})", raw)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None

def norm_float(raw) -> float | None:
    if raw is None:
        return None
    s = re.sub(r"[^\d.]", "", str(raw))
    return float(s) if s else None

def find(pat, text, g=1, default=None):
    m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
    return m.group(g).strip() if m else default

def find_all(pat, text):
    return re.findall(pat, text, re.IGNORECASE)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. REPORT METADATA
# ═══════════════════════════════════════════════════════════════════════════════
def parse_report_metadata(pages: list[str]) -> dict:
    text = pages[0] if pages else ""
    control = find(r"Control Number\s*[:\-]\s*([\d,\s]+)", text)
    rdate   = find(r"Date\s*[:\-]\s*([\d/]+)", text)
    return {
        "control_number": control.replace(",", "").replace(" ", "") if control else None,
        "report_date":    norm_date(rdate),
        "report_version": "1.0"
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 2. SCORE SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════
def parse_score_summary(pages: list[str]) -> dict:
    text = pages[0] if pages else ""
    score = find(r"CIBIL Score is\s+(\d{3})", text)
    sdate = find(r"as of Date\s*[:\-]\s*([\d/]+)", text)
    return {
        "cibil_score":     int(score) if score else None,
        "score_date":      norm_date(sdate),
        "score_range_min": 300,
        "score_range_max": 900
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 3. PERSONAL DETAILS
# ═══════════════════════════════════════════════════════════════════════════════
def parse_personal_details(pages: list[str]) -> dict:
    text = "\n".join(pages[:2])
    name   = find(r"Hello,\s+([A-Z][A-Z\s]+?)(?:\n|Your)", text) or \
             find(r"Name\s*\n([A-Z][^\n]+)", text)
    dob    = find(r"Date Of Birth\s*[,\n\s]+([\d/]+)", text)
    gender = find(r"Gender\s+(Male|Female|Transgender|Other)", text)
    return {
        "full_name":     name.strip() if name else None,
        "date_of_birth": norm_date(dob),
        "gender":        gender
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 4. IDENTIFICATION DETAILS
# ═══════════════════════════════════════════════════════════════════════════════
def parse_identification(pages: list[str]) -> list[dict]:
    text = "\n".join(pages[:3])
    ids = []

    # PAN
    pan = find(r"(?:Income Tax ID|PAN)[^\n]*\n+([A-Z]{5}\d{4}[A-Z])", text)
    ids.append({
        "id_type":    "PAN",
        "id_number":  pan,
        "issue_date": None,
        "expiry_date": None
    })

    # Passport
    pp_num    = find(r"Passport Number\s*\n+([A-Z]\d{7,8})", text)
    pp_issue  = find(r"Passport Number[\s\S]{1,200}?(\d{2}/\d{2}/\d{4})", text)
    pp_expiry_all = re.findall(r"(\d{2}/\d{2}/\d{4})", text)
    pp_expiry = pp_expiry_all[1] if len(pp_expiry_all) > 1 else None
    if pp_num or pp_issue:
        ids.append({
            "id_type":    "Passport",
            "id_number":  pp_num,
            "issue_date": norm_date(pp_issue),
            "expiry_date": norm_date(pp_expiry)
        })

    return ids


# ═══════════════════════════════════════════════════════════════════════════════
# 5. ADDRESS DETAILS
# ═══════════════════════════════════════════════════════════════════════════════
def parse_addresses(pages: list[str]) -> list[dict]:
    text = "\n".join(pages[:3])
    results = []

    # Split on "Address" keyword occurrences
    chunks = re.split(r"\nAddress\s*\n", text)
    for chunk in chunks[1:]:           # skip text before first "Address"
        lines = [l.strip() for l in chunk.strip().split("\n") if l.strip()]
        if not lines:
            continue
        # First non-empty lines until "Category" = the address string
        addr_lines = []
        for l in lines:
            if re.match(r"^(Category|Residence Code|Date Reported)", l, re.IGNORECASE):
                break
            addr_lines.append(l)
        address_str = " ".join(addr_lines).strip()
        if len(address_str) < 10:
            continue

        category      = find(r"Category\s*\n([^\n]+)", chunk)
        residence_code = find(r"Residence Code\s*\n([^\n]+)", chunk)
        date_rep       = find(r"Date Reported\s*\n([\d/]+)", chunk)

        addr_type = "Residence"
        if "office" in (category or "").lower() or "office" in address_str.lower():
            addr_type = "Office"

        results.append({
            "address_type":  addr_type,
            "address":       address_str,
            "category":      category or residence_code,
            "date_reported": norm_date(date_rep)
        })

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# 6. CONTACT DETAILS
# ═══════════════════════════════════════════════════════════════════════════════
def parse_contact(pages: list[str]) -> dict:
    text = "\n".join(pages[:4])
    phones = list(set(re.findall(r"\b[6-9]\d{9}\b", text)))
    emails = list(set(re.findall(r"[\w.\-]+@[\w.\-]+\.[a-zA-Z]{2,}", text)))
    # OCR often garbles emails — clean obvious junk
    emails = [e for e in emails if len(e) > 6 and "." in e.split("@")[-1]]
    return {
        "phone_numbers": phones,
        "emails":        emails
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 7. EMPLOYMENT DETAILS
# ═══════════════════════════════════════════════════════════════════════════════
def parse_employment(pages: list[str]) -> dict:
    text = "\n".join(pages[:4])
    section = find(r"EMPLOYMENT DETAILS([\s\S]{1,600}?)(?=ALL ACCOUNTS|ENQUIRY|$)", text) or ""
    acct_type  = find(r"Account Type\s*\n([^\n]+)", section)
    occupation = find(r"Occupation\s*\n([^\n]+)", section)
    income_raw = find(r"Income\s*\n([\d,]+)", section)
    indicator  = find(r"Monthly / Annual Income Indicator\s*\n([^\n]+)", section)
    net_gross  = find(r"Net / Gross Income Indicator\s*\n([^\n]+)", section)
    return {
        "account_type": acct_type,
        "occupation":   occupation,
        "income":       norm_float(income_raw),
        "income_type":  (indicator or "").strip() or None,
        "net_gross":    (net_gross or "").strip() or None
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 8. ACCOUNTS  (most complex section)
# ═══════════════════════════════════════════════════════════════════════════════
MONTH_MAP = {
    "Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06",
    "Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12"
}

def parse_payment_history(chunk: str) -> list[dict]:
    """Extract month-by-month DPD from an account chunk."""
    history = []
    # Pattern: "Jan 2026\nSTD" or "Jan 2026   0" etc
    months = re.findall(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})",
        chunk
    )
    # Try to find DPD values alongside months
    # OCR renders STD as 0, numbers as actual DPD, XXX as None
    dpd_tokens = re.findall(
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s*(STD|XXX|SMA|\d+|0)?",
        chunk
    )
    for i, (mon, yr) in enumerate(months):
        raw_dpd = dpd_tokens[i] if i < len(dpd_tokens) else None
        if raw_dpd in (None, "STD", "", "0"):
            dpd = 0
        elif raw_dpd == "XXX":
            dpd = None
        else:
            try:
                dpd = int(raw_dpd)
            except ValueError:
                dpd = 0
        history.append({
            "month": f"{yr}-{MONTH_MAP[mon]}",
            "dpd":   dpd
        })
    return history


def parse_single_account(chunk: str) -> dict:
    """Parse one account block into the required structure."""

    def get(label, fallback=None):
        patterns = [
            rf"{re.escape(label)}\s+([^\n]+)",
            rf"{re.escape(label)}\s*\n([^\n]+)"
        ]
        for p in patterns:
            v = find(p, chunk)
            if v and v.strip() not in ("-","","None"):
                return v.strip()
        return fallback

    member      = find(r"Member Name\s*\n([^\n]+)", chunk)
    acct_type   = find(r"Account Type\s*\n([^\n]+)", chunk)
    ownership   = find(r"Ownership\s*\n([^\n]+)", chunk)
    acct_num    = find(r"Account Number\s*\n([^\n]+)", chunk)
    # Clean OCR noise from account number
    if acct_num and re.search(r"[a-z]{3,}|Ownership", acct_num, re.IGNORECASE):
        acct_num = None

    date_opened   = norm_date(get("Date Opened / Disbursed"))
    date_closed   = norm_date(get("Date Closed"))
    last_payment  = norm_date(get("Date of Last Payment"))

    # Numeric fields — OCR sometimes captures them, sometimes redacted
    def money(label):
        v = get(label)
        return norm_float(v) if v and re.search(r'\d', str(v)) else None

    roi_raw = get("Rate of Interest")
    roi = norm_float(roi_raw.replace("%","")) if roi_raw else None

    tenure_raw = get("Repayment Tenure")
    tenure = int(float(tenure_raw)) if tenure_raw and re.match(r"^\d+", tenure_raw or "") else None

    suit_raw = get("Suit - Filed / Wilful Default")
    suit_flag = bool(suit_raw and suit_raw not in ("-","None",""))

    written_off_raw = get("Written-off Amount (Total)")
    written_off = norm_float(written_off_raw) if written_off_raw and re.search(r'\d', str(written_off_raw)) else 0

    settlement_raw = get("Settlement Amount")
    settlement = norm_float(settlement_raw) if settlement_raw and re.search(r'\d', str(settlement_raw)) else 0

    account_details = {
        "credit_limit":       money("Credit Limit"),
        "high_credit":        money("High Credit"),
        "sanctioned_amount":  money("Sanctioned Amount"),
        "current_balance":    money("Current Balance"),
        "cash_limit":         money("Cash Limit"),
        "amount_overdue":     money("Amount Overdue"),
        "rate_of_interest":   roi,
        "repayment_tenure":   tenure,
        "emi_amount":         money("EMI Amount"),
        "payment_frequency":  get("Payment Frequency"),
        "date_opened":        date_opened,
        "date_closed":        date_closed,
        "date_last_payment":  last_payment,
        "written_off_amount": written_off,
        "settlement_amount":  settlement,
        "suit_filed_flag":    suit_flag
    }

    payment_history = parse_payment_history(chunk)

    return {
        "member_name":            member,
        "account_type":           acct_type,
        "ownership":              ownership,
        "account_number_masked":  acct_num,
        "account_details":        account_details,
        "payment_history":        payment_history
    }


def parse_accounts(pages: list[str]) -> dict:
    full_text = "\n".join(pages)

    # Find the accounts section
    acc_section = find(r"ALL ACCOUNTS([\s\S]+?)(?=ENQUIRY DETAILS|End of report|$)", full_text, g=1) or ""

    # Split into individual account chunks on "Member Name"
    raw_chunks = re.split(r"(?=Member Name\s*\n)", acc_section)

    open_accounts   = []
    closed_accounts = []

    for chunk in raw_chunks:
        if "Account Type" not in chunk:
            continue
        acc = parse_single_account(chunk)
        if not acc.get("member_name"):
            continue
        # Determine open vs closed
        if acc["account_details"].get("date_closed"):
            closed_accounts.append(acc)
        else:
            open_accounts.append(acc)

    return {
        "open_accounts":   open_accounts,
        "closed_accounts": closed_accounts
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 9. ENQUIRIES
# ═══════════════════════════════════════════════════════════════════════════════
def parse_enquiries(pages: list[str]) -> list[dict]:
    full_text = "\n".join(pages)
    section = find(r"ENQUIRY DETAILS([\s\S]+?)(?=End of report|$)", full_text, g=1) or ""

    if "No Enquiry Information Reported" in section:
        return []

    enquiries = []
    # Pattern: date  member  account_type  purpose on consecutive lines
    rows = re.findall(
        r"(\d{2}/\d{2}/\d{4})\s*\n([A-Z][^\n]{2,50})\s*\n([^\n]{3,50})\s*\n([^\n]{3,50})",
        section
    )
    for dt, member, acct_type, purpose in rows:
        # Filter out garbage rows (addresses, labels)
        if any(x in member.upper() for x in ["NAGAR","CITY","TALUKA","ROAD","PLOT","WING"]):
            continue
        amount_m = re.search(r"(\d{5,})", purpose)
        enquiries.append({
            "member_name":    member.strip(),
            "enquiry_date":   norm_date(dt),
            "enquiry_amount": int(amount_m.group(1)) if amount_m else None,
            "enquiry_type":   acct_type.strip()
        })

    return enquiries


# ═══════════════════════════════════════════════════════════════════════════════
# FULL STRUCTURED OUTPUT
# ═══════════════════════════════════════════════════════════════════════════════
def parse_cibil_pdf(pdf_path: str) -> dict:
    print(f"📄  OCR-ing {pdf_path} ...")
    pages = ocr_pdf(pdf_path)
    print(f"✅  {len(pages)} pages extracted. Parsing ...")

    result = {
        "report_metadata":      parse_report_metadata(pages),
        "score_summary":        parse_score_summary(pages),
        "personal_details":     parse_personal_details(pages),
        "identification_details": parse_identification(pages),
        "address_details":      parse_addresses(pages),
        "contact_details":      parse_contact(pages),
        "employment_details":   parse_employment(pages),
        "accounts":             parse_accounts(pages),
        "enquiries":            parse_enquiries(pages),
    }
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE  (fully restructured schema)
# ═══════════════════════════════════════════════════════════════════════════════
def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS report_metadata (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        control_number  TEXT UNIQUE,
        report_date     TEXT,
        report_version  TEXT,
        created_at      TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS score_summary (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id       INTEGER REFERENCES report_metadata(id),
        cibil_score     INTEGER,
        score_date      TEXT,
        score_range_min INTEGER DEFAULT 300,
        score_range_max INTEGER DEFAULT 900
    );

    CREATE TABLE IF NOT EXISTS personal_details (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id       INTEGER REFERENCES report_metadata(id),
        full_name       TEXT,
        date_of_birth   TEXT,
        gender          TEXT
    );

    CREATE TABLE IF NOT EXISTS identification_details (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id   INTEGER REFERENCES report_metadata(id),
        id_type     TEXT,
        id_number   TEXT,
        issue_date  TEXT,
        expiry_date TEXT
    );

    CREATE TABLE IF NOT EXISTS address_details (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id       INTEGER REFERENCES report_metadata(id),
        address_type    TEXT,
        address         TEXT,
        category        TEXT,
        date_reported   TEXT
    );

    CREATE TABLE IF NOT EXISTS contact_details (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id       INTEGER REFERENCES report_metadata(id),
        phone_numbers   TEXT,   -- JSON array
        emails          TEXT    -- JSON array
    );

    CREATE TABLE IF NOT EXISTS employment_details (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id       INTEGER REFERENCES report_metadata(id),
        account_type    TEXT,
        occupation      TEXT,
        income          REAL,
        income_type     TEXT,
        net_gross       TEXT
    );

    CREATE TABLE IF NOT EXISTS accounts (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id               INTEGER REFERENCES report_metadata(id),
        is_open                 INTEGER,   -- 1=open, 0=closed
        member_name             TEXT,
        account_type            TEXT,
        ownership               TEXT,
        account_number_masked   TEXT,
        credit_limit            REAL,
        high_credit             REAL,
        sanctioned_amount       REAL,
        current_balance         REAL,
        cash_limit              REAL,
        amount_overdue          REAL,
        rate_of_interest        REAL,
        repayment_tenure        INTEGER,
        emi_amount              REAL,
        payment_frequency       TEXT,
        date_opened             TEXT,
        date_closed             TEXT,
        date_last_payment       TEXT,
        written_off_amount      REAL,
        settlement_amount       REAL,
        suit_filed_flag         INTEGER
    );

    CREATE TABLE IF NOT EXISTS payment_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id  INTEGER REFERENCES accounts(id),
        month       TEXT,
        dpd         INTEGER
    );

    CREATE TABLE IF NOT EXISTS enquiries (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id       INTEGER REFERENCES report_metadata(id),
        member_name     TEXT,
        enquiry_date    TEXT,
        enquiry_amount  REAL,
        enquiry_type    TEXT
    );
    """)
    conn.commit()
    return conn


def save_to_db(conn: sqlite3.Connection, data: dict) -> int:
    cur = conn.cursor()
    meta = data["report_metadata"]

    # 1. report_metadata
    cur.execute("""
        INSERT INTO report_metadata (control_number, report_date, report_version)
        VALUES (?,?,?)
        ON CONFLICT(control_number) DO UPDATE SET
            report_date=excluded.report_date, created_at=datetime('now')
    """, (meta["control_number"], meta["report_date"], meta["report_version"]))
    report_id = cur.lastrowid or cur.execute(
        "SELECT id FROM report_metadata WHERE control_number=?", (meta["control_number"],)
    ).fetchone()[0]

    # 2. score_summary
    s = data["score_summary"]
    cur.execute("INSERT INTO score_summary (report_id,cibil_score,score_date,score_range_min,score_range_max) VALUES (?,?,?,?,?)",
                (report_id, s["cibil_score"], s["score_date"], s["score_range_min"], s["score_range_max"]))

    # 3. personal_details
    p = data["personal_details"]
    cur.execute("INSERT INTO personal_details (report_id,full_name,date_of_birth,gender) VALUES (?,?,?,?)",
                (report_id, p["full_name"], p["date_of_birth"], p["gender"]))

    # 4. identification_details
    for id_item in data["identification_details"]:
        cur.execute("INSERT INTO identification_details (report_id,id_type,id_number,issue_date,expiry_date) VALUES (?,?,?,?,?)",
                    (report_id, id_item["id_type"], id_item["id_number"], id_item["issue_date"], id_item["expiry_date"]))

    # 5. address_details
    for addr in data["address_details"]:
        cur.execute("INSERT INTO address_details (report_id,address_type,address,category,date_reported) VALUES (?,?,?,?,?)",
                    (report_id, addr["address_type"], addr["address"], addr["category"], addr["date_reported"]))

    # 6. contact_details
    c = data["contact_details"]
    cur.execute("INSERT INTO contact_details (report_id,phone_numbers,emails) VALUES (?,?,?)",
                (report_id, json.dumps(c["phone_numbers"]), json.dumps(c["emails"])))

    # 7. employment_details
    e = data["employment_details"]
    cur.execute("INSERT INTO employment_details (report_id,account_type,occupation,income,income_type,net_gross) VALUES (?,?,?,?,?,?)",
                (report_id, e["account_type"], e["occupation"], e["income"], e["income_type"], e["net_gross"]))

    # 8. accounts + payment_history
    for is_open, accs in [(1, data["accounts"]["open_accounts"]), (0, data["accounts"]["closed_accounts"])]:
        for acc in accs:
            d = acc["account_details"]
            cur.execute("""
                INSERT INTO accounts
                    (report_id, is_open, member_name, account_type, ownership,
                     account_number_masked, credit_limit, high_credit, sanctioned_amount,
                     current_balance, cash_limit, amount_overdue, rate_of_interest,
                     repayment_tenure, emi_amount, payment_frequency, date_opened,
                     date_closed, date_last_payment, written_off_amount,
                     settlement_amount, suit_filed_flag)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (report_id, is_open,
                  acc["member_name"], acc["account_type"], acc["ownership"],
                  acc["account_number_masked"],
                  d["credit_limit"], d["high_credit"], d["sanctioned_amount"],
                  d["current_balance"], d["cash_limit"], d["amount_overdue"],
                  d["rate_of_interest"], d["repayment_tenure"], d["emi_amount"],
                  d["payment_frequency"], d["date_opened"], d["date_closed"],
                  d["date_last_payment"], d["written_off_amount"],
                  d["settlement_amount"], int(d["suit_filed_flag"])))
            acc_id = cur.lastrowid
            for ph in acc["payment_history"]:
                cur.execute("INSERT INTO payment_history (account_id,month,dpd) VALUES (?,?,?)",
                            (acc_id, ph["month"], ph["dpd"]))

    # 9. enquiries
    for enq in data["enquiries"]:
        cur.execute("INSERT INTO enquiries (report_id,member_name,enquiry_date,enquiry_amount,enquiry_type) VALUES (?,?,?,?,?)",
                    (report_id, enq["member_name"], enq["enquiry_date"], enq["enquiry_amount"], enq["enquiry_type"]))

    conn.commit()
    return report_id


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="CIBIL PDF → Structured JSON + DB (v2)")
    parser.add_argument("--pdf",      required=True, help="Path to CIBIL PDF")
    parser.add_argument("--db",       default="cibil_v2.db", help="SQLite DB path")
    parser.add_argument("--json-out", default="cibil_output.json", help="JSON output path")
    parser.add_argument("--poppler",  help=r"Poppler bin path (Windows). E.g. D:\poppler\bin")
    args = parser.parse_args()

    global POPPLER_PATH
    if args.poppler:
        POPPLER_PATH = args.poppler

    data = parse_cibil_pdf(args.pdf)

    # Save JSON
    Path(args.json_out).write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"📝  JSON saved → {args.json_out}")

    # Save DB
    conn = init_db(args.db)
    report_id = save_to_db(conn, data)
    conn.close()
    print(f"💾  DB saved   → {args.db}  (report_id={report_id})")

    # Summary
    print("\n📊  Extraction Summary:")
    print(f"    Score       : {data['score_summary']['cibil_score']}")
    print(f"    Name        : {data['personal_details']['full_name']}")
    print(f"    Open accts  : {len(data['accounts']['open_accounts'])}")
    print(f"    Closed accts: {len(data['accounts']['closed_accounts'])}")
    print(f"    Enquiries   : {len(data['enquiries'])}")
    total_ph = sum(len(a['payment_history']) for a in data['accounts']['open_accounts'] + data['accounts']['closed_accounts'])
    print(f"    Pay. history: {total_ph} month-entries")

if __name__ == "__main__":
    main()
