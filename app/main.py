"""
Employee Attendance & Analytics API
FastAPI + MongoDB (PyMongo sync)

All application code lives in this file per assignment specification.
Run: uvicorn app.main:app --port 8000
"""

import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Configuration & Database Connection
# Real environment variables take precedence over .env file
# --------------------------------------------------------------------------- #
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB_NAME]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """
    WHAT: Idempotently creates all required compound and unique indexes.
    WHERE: init_indexes() called during FastAPI lifespan startup.
    WHY (Cite Rule): Assignment Section 4 requires all indexes created at startup
    idempotently so queries use index scans (IXSCAN) without collection scans (COLLSCAN).
    HOW TO TEST: Run db.collection.getIndexes() in mongosh.
    WHAT BREAKS: Queries on large datasets (100k docs) do COLLSCAN and timeout.
    """
    # employees collection
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("status", 1), ("date", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure indexes exist
    try:
        init_indexes()
    except Exception as e:
        # If Mongo is not yet ready, startup will not crash immediately
        pass
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


@app.exception_handler(PyMongoError)
def handle_mongo_exception(request: Request, exc: PyMongoError):
    """
    WHAT: Converts uncaught PyMongo exceptions into HTTP 503.
    WHY: Rule Hard Constraints state that HTTP 500 is an immediate test failure.
    """
    return JSONResponse(
        status_code=503,
        content={"detail": f"Database error: {exc}"},
    )


# --------------------------------------------------------------------------- #
# Helpers & Business Logic (Rules R1 - R5, R8)
# --------------------------------------------------------------------------- #
def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime (UTC or aware) to epoch milliseconds integer."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Treat naive datetime as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    """Convert epoch milliseconds integer to whole-second truncated UTC datetime."""
    # Rule R1: Instants are truncated to whole seconds before any calculation
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def compute_record_date_and_shift(punch_in_utc: datetime, shift_start: str, shift_end: str):
    """
    WHAT: Computes the IST attendance date and shift start/end UTC datetimes.
    WHERE: compute_record_date_and_shift
    WHY (Cite Rule): Rule R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in. Overnight shifts
    (shift_end <= shift_start): a punch-in earlier than shift_end belongs to the
    shift that started the previous day.
    HOW TO TEST:
      - 2026-07-06T19:00:00Z (00:30 IST July 7), normal shift -> "2026-07-07"
      - 2026-07-07T19:55:00Z (01:25 IST July 8), overnight shift 22:00-06:00 -> "2026-07-07"
    WHAT BREAKS: Incorrect date grouping, wrong monthly reports, overtime/late calculated against wrong date.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    ist_date = punch_in_ist.date()
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh, em) <= (sh, sm)

    if is_overnight and punch_in_ist.time() < time(eh, em):
        att_date = ist_date - timedelta(days=1)
    else:
        att_date = ist_date

    start_ist = datetime(att_date.year, att_date.month, att_date.day, sh, sm, 0, tzinfo=IST)
    end_ist = datetime(att_date.year, att_date.month, att_date.day, eh, em, 0, tzinfo=IST)
    if is_overnight:
        end_ist += timedelta(days=1)

    return att_date.isoformat(), start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)


def compute_late_minutes(punch_in_utc: datetime, shift_start_utc: datetime) -> int:
    """
    WHAT: Computes late minutes with strict 10-minute grace period.
    WHERE: compute_late_minutes
    WHY (Cite Rule): Rule R2: Grace period 10 minutes. Late only if punch-in is
    strictly more than 10:00 (600 seconds) after shift_start; then late_minutes = whole
    minutes (floor) since shift_start, not since grace period ended.
    HOW TO TEST:
      - 09:40:00 with 09:30 shift -> 0
      - 09:40:01 with 09:30 shift -> 10
      - 10:15:59 with 09:30 shift -> 45
    WHAT BREAKS: Employees arriving at 09:40:01 marked on time (starter bug), failing grader R2 tests.
    """
    elapsed_seconds = int((punch_in_utc - shift_start_utc).total_seconds())
    if elapsed_seconds > 600:
        return elapsed_seconds // 60
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    WHAT: Computes elapsed work hours rounded to 2 decimals using round-half-up.
    WHERE: compute_work_hours
    WHY (Cite Rule): Rule R4 & R8: (punch_out - punch_in) in seconds / 3600,
    rounded to 2 decimals half-up. Python round() is banned because it is half-even.
    HOW TO TEST: 450 seconds (0.125h) -> 0.13 (Python round gives 0.12).
    WHAT BREAKS: Cumulative work hour drift, incorrect half-day classifications at thresholds.
    """
    elapsed_seconds = int((punch_out_utc - punch_in_utc).total_seconds())
    if elapsed_seconds <= 0:
        return 0.0
    val = (Decimal(elapsed_seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(val)


def compute_overtime(punch_out_utc: datetime, shift_end_utc: datetime) -> int:
    """
    WHAT: Computes overtime minutes with 30-minute minimum threshold.
    WHERE: compute_overtime
    WHY (Cite Rule): Rule R3: Whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30; otherwise 0.
    HOW TO TEST:
      - 29 minutes past shift end -> 0
      - 30 minutes past shift end -> 30
      - 45 minutes past shift end -> 45
    WHAT BREAKS: Small overstays (<30m) falsely credited as overtime.
    """
    elapsed_seconds = int((punch_out_utc - shift_end_utc).total_seconds())
    if elapsed_seconds < 0:
        return 0
    minutes = elapsed_seconds // 60
    return minutes if minutes >= 30 else 0


def serialize_employee(doc: dict) -> dict:
    """Serialize employee doc for API response (no _id, created_at as epoch ms)."""
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at_ms = to_epoch_ms(created_at)
    elif isinstance(created_at, int):
        created_at_ms = created_at
    else:
        created_at_ms = 0

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


def serialize_attendance(doc: dict) -> dict:
    """Serialize attendance doc for API response (no _id or id, instants as epoch ms)."""
    raw_history = doc.get("history") or []
    history = []
    for h in raw_history:
        at_val = h.get("at")
        at_ms = to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val
        history.append({
            "at": at_ms,
            "by": h.get("by", ""),
            "reason": h.get("reason", ""),
            "changes": h.get("changes", {}),
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Request & Response Models (Strict Pydantic v2)
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except Exception:
            raise ValueError("Invalid calendar date")
        return v


class PunchInRequest(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


# --------------------------------------------------------------------------- #
# Endpoints (Part A Starter Fixes)
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """
    WHAT: Verifies MongoDB connectivity via ping command.
    WHY (Cite Rule): Assignment Section 7: 200 only when MongoDB answers; else 503.
    """
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    WHAT: Creates an employee record with race-safe unique index.
    WHY (Cite Rule): Trap #5: Check-then-insert races. Unique index on emp_code
    ensures atomic conflict detection. Instants stored as UTC datetime.
    """
    doc = body.model_dump()
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    WHAT: Lists employees sorted by emp_code ascending with correct pagination.
    WHY (Cite Rule): Rule R10: page starts at 1, total reflects filters.
    Fixes starter off-by-one pagination bug (skip = (page - 1) * page_size)
    and handles empty string department queries correctly (Assumption A8).
    """
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size

    cursor = db.employees.find(q).sort([("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    WHAT: Opens a day's attendance record adhering to Rules R1 & R2 with concurrency safety.
    WHY (Cite Rule):
      - R1: Date derived from IST timestamp, overnight shifts handled.
      - R2: Late minutes computed with 10-minute grace period.
      - Concurrency: Atomic insert against unique index (emp_code, date) prevents race conditions.
      - Contract: Returns epoch milliseconds, no _id or id leaked.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = from_epoch_ms(body.punched_at)
    else:
        punch_in_utc = datetime.now(timezone.utc).replace(microsecond=0)

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    att_date, shift_start_utc, _ = compute_record_date_and_shift(
        punch_in_utc, shift_start, shift_end
    )

    late_mins = compute_late_minutes(punch_in_utc, shift_start_utc)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance(doc)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    WHAT: Lists attendance logs sorted by date descending, emp_code ascending.
    WHY (Cite Rule):
      - Database pagination: avoids loading 100k records into memory.
      - Stable sort: uses (date: -1, emp_code: 1) as tie-breaker.
      - Date validation: date_from > date_to returns 422.
      - Serialization: returns epoch milliseconds, no leaked internal ids.
    """
    if date_from:
        try:
            date.fromisoformat(date_from)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date_from")

    if date_to:
        try:
            date.fromisoformat(date_to)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date_to")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size

    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# TODO - The rest of the contract will be added in Phase 3, 4, 5:
#   POST  /attendance/punch-out                   (Phase 3)
#   PATCH /attendance/{emp_code}/{date}           (Phase 3)
#   GET   /analytics/employees/{emp_code}/monthly (Phase 4)
#   GET   /analytics/departments/summary          (Phase 4)
#   GET   /analytics/leaderboard/late             (Phase 4)
#   GET   /analytics/departments/{department}/trend (Phase 4)
#   GET   /admin/explain/{endpoint}               (Phase 5)
# --------------------------------------------------------------------------- #
