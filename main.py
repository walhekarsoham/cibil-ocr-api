"""
CIBIL Report Parser — FastAPI
==============================
Endpoints:
  POST /parse          → Upload PDF, get structured JSON + saved to DB
  GET  /reports        → List all parsed reports
  GET  /reports/{id}   → Full structured data for one report
  GET  /reports/{id}/score    → Score summary only
  GET  /reports/{id}/accounts → Accounts + payment history
  GET  /reports/{id}/enquiries → Enquiries
  DELETE /reports/{id} → Delete a report

Run:
  pip install fastapi uvicorn python-multipart
  uvicorn main:app --reload --port 8000

Then open: http://localhost:8000/docs  (auto-generated interactive UI)
"""

import os, json, shutil, tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, UploadFile, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import sqlite3

# ── Import your parser ────────────────────────────────────────────────────────
# Make sure cibil_parser_v2.py is in the same folder
from cibil_parser_v2 import parse_cibil_pdf, init_db, save_to_db

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH      = "cibil.db"
UPLOAD_FOLDER = "uploads"
Path(UPLOAD_FOLDER).mkdir(exist_ok=True)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="CIBIL Report Parser API",
    description="Upload a CIBIL PDF → get fully structured JSON data stored in database",
    version="2.0.0"
)

# Allow requests from your website frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # Change to your domain in production e.g. ["https://yoursite.com"]
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── DB Helper ─────────────────────────────────────────────────────────────────
def get_db():
    conn = init_db(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# POST /parse  — Main upload endpoint
# ══════════════════════════════════════════════════════════════════════════════
@app.post("/parse", summary="Upload CIBIL PDF and parse it")
async def parse_report(file: UploadFile = File(...)):
    """
    Upload a CIBIL PDF file.
    Returns the full structured JSON and saves to the database.
    """
    # Validate file type
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    # Save uploaded file temporarily
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        # Parse PDF → structured dict
        data = parse_cibil_pdf(tmp_path)

        # Save to DB
        conn = init_db(DB_PATH)
        conn.row_factory = sqlite3.Row
        report_id = save_to_db(conn, data)
        conn.close()

        return {
            "status":    "success",
            "report_id": report_id,
            "message":   f"Report parsed and saved. report_id = {report_id}",
            "data":      data
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Parsing failed: {str(e)}")
    finally:
        os.unlink(tmp_path)


# ══════════════════════════════════════════════════════════════════════════════
# GET /reports  — List all reports
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/reports", summary="List all parsed reports")
def list_reports(conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute("""
        SELECT r.id, r.control_number, r.report_date, r.created_at,
               s.cibil_score, p.full_name
        FROM report_metadata r
        LEFT JOIN score_summary    s ON s.report_id = r.id
        LEFT JOIN personal_details p ON p.report_id = r.id
        ORDER BY r.created_at DESC
    """).fetchall()

    return [dict(row) for row in rows]


# ══════════════════════════════════════════════════════════════════════════════
# GET /reports/{id}  — Full report
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/reports/{report_id}", summary="Get full structured data for a report")
def get_report(report_id: int, conn: sqlite3.Connection = Depends(get_db)):
    meta = conn.execute("SELECT * FROM report_metadata WHERE id=?", (report_id,)).fetchone()
    if not meta:
        raise HTTPException(status_code=404, detail="Report not found")

    score   = conn.execute("SELECT * FROM score_summary WHERE report_id=?",   (report_id,)).fetchone()
    person  = conn.execute("SELECT * FROM personal_details WHERE report_id=?", (report_id,)).fetchone()
    ids     = conn.execute("SELECT * FROM identification_details WHERE report_id=?", (report_id,)).fetchall()
    addrs   = conn.execute("SELECT * FROM address_details WHERE report_id=?",  (report_id,)).fetchall()
    contact = conn.execute("SELECT * FROM contact_details WHERE report_id=?",  (report_id,)).fetchone()
    employ  = conn.execute("SELECT * FROM employment_details WHERE report_id=?",(report_id,)).fetchone()
    enqs    = conn.execute("SELECT * FROM enquiries WHERE report_id=?",         (report_id,)).fetchall()

    # Accounts + payment history
    accounts_rows = conn.execute("SELECT * FROM accounts WHERE report_id=?", (report_id,)).fetchall()
    open_accounts, closed_accounts = [], []
    for acc in accounts_rows:
        acc_dict = dict(acc)
        ph = conn.execute("SELECT month, dpd FROM payment_history WHERE account_id=? ORDER BY month DESC",
                          (acc_dict["id"],)).fetchall()
        acc_dict["payment_history"] = [dict(r) for r in ph]
        # Parse contact JSON fields
        if acc_dict["is_open"]:
            open_accounts.append(acc_dict)
        else:
            closed_accounts.append(acc_dict)

    # Parse JSON strings in contact
    contact_dict = dict(contact) if contact else {}
    for field in ("phone_numbers", "emails"):
        if field in contact_dict and isinstance(contact_dict[field], str):
            contact_dict[field] = json.loads(contact_dict[field])

    return {
        "report_metadata":       dict(meta),
        "score_summary":         dict(score) if score else None,
        "personal_details":      dict(person) if person else None,
        "identification_details": [dict(r) for r in ids],
        "address_details":       [dict(r) for r in addrs],
        "contact_details":       contact_dict,
        "employment_details":    dict(employ) if employ else None,
        "accounts": {
            "open_accounts":   open_accounts,
            "closed_accounts": closed_accounts,
        },
        "enquiries": [dict(r) for r in enqs],
    }


# ══════════════════════════════════════════════════════════════════════════════
# GET /reports/{id}/score
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/reports/{report_id}/score", summary="Get CIBIL score summary")
def get_score(report_id: int, conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute(
        "SELECT * FROM score_summary WHERE report_id=?", (report_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Score not found")
    return dict(row)


# ══════════════════════════════════════════════════════════════════════════════
# GET /reports/{id}/accounts
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/reports/{report_id}/accounts", summary="Get accounts with payment history")
def get_accounts(report_id: int, conn: sqlite3.Connection = Depends(get_db)):
    accounts_rows = conn.execute(
        "SELECT * FROM accounts WHERE report_id=?", (report_id,)
    ).fetchall()

    if not accounts_rows:
        raise HTTPException(status_code=404, detail="No accounts found")

    open_accounts, closed_accounts = [], []
    for acc in accounts_rows:
        acc_dict = dict(acc)
        ph = conn.execute(
            "SELECT month, dpd FROM payment_history WHERE account_id=? ORDER BY month DESC",
            (acc_dict["id"],)
        ).fetchall()
        acc_dict["payment_history"] = [dict(r) for r in ph]
        if acc_dict["is_open"]:
            open_accounts.append(acc_dict)
        else:
            closed_accounts.append(acc_dict)

    return {
        "open_accounts":   open_accounts,
        "closed_accounts": closed_accounts,
        "summary": {
            "total_open":         len(open_accounts),
            "total_closed":       len(closed_accounts),
            "total_overdue":      sum(a.get("amount_overdue") or 0 for a in open_accounts),
            "total_balance":      sum(a.get("current_balance") or 0 for a in open_accounts),
            "accounts_with_dpd":  sum(
                1 for a in open_accounts + closed_accounts
                if any((p["dpd"] or 0) > 0 for p in a["payment_history"])
            ),
        }
    }


# ══════════════════════════════════════════════════════════════════════════════
# GET /reports/{id}/enquiries
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/reports/{report_id}/enquiries", summary="Get credit enquiries")
def get_enquiries(report_id: int, conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        "SELECT * FROM enquiries WHERE report_id=? ORDER BY enquiry_date DESC",
        (report_id,)
    ).fetchall()
    return {
        "total": len(rows),
        "enquiries": [dict(r) for r in rows]
    }


# ══════════════════════════════════════════════════════════════════════════════
# DELETE /reports/{id}
# ══════════════════════════════════════════════════════════════════════════════
@app.delete("/reports/{report_id}", summary="Delete a report and all its data")
def delete_report(report_id: int, conn: sqlite3.Connection = Depends(get_db)):
    meta = conn.execute("SELECT id FROM report_metadata WHERE id=?", (report_id,)).fetchone()
    if not meta:
        raise HTTPException(status_code=404, detail="Report not found")

    # Delete payment history first (child of accounts)
    acc_ids = [r[0] for r in conn.execute(
        "SELECT id FROM accounts WHERE report_id=?", (report_id,)
    ).fetchall()]
    for aid in acc_ids:
        conn.execute("DELETE FROM payment_history WHERE account_id=?", (aid,))

    tables = ["accounts","enquiries","employment_details","contact_details",
              "address_details","identification_details","personal_details",
              "score_summary","report_metadata"]
    for table in tables:
        conn.execute(f"DELETE FROM {table} WHERE {'id' if table=='report_metadata' else 'report_id'}=?",
                     (report_id,))
    conn.commit()
    return {"status": "deleted", "report_id": report_id}


# ══════════════════════════════════════════════════════════════════════════════
# GET /health
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/health", summary="Health check")
def health():
    return {"status": "ok", "db": DB_PATH}
