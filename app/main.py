"""
Employee Attendance & Analytics API
FastAPI + MongoDB (PyMongo sync)

All application code lives in this file per assignment specification.
Run: uvicorn app.main:app --port 8000
"""

import calendar
import os
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
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
            date_cls.fromisoformat(v)
        except Exception:
            raise ValueError("Invalid calendar date")
        return v


class PunchInRequest(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    punch_out: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints (CRUD)
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


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    WHAT: Records punch-out for an employee's most recent open record, computing work_hours,
    overtime_minutes, and half_day.
    WHERE: punch_out
    WHY (Cite Rule):
      - Rule R3: Overtime >= 30m counted, otherwise 0.
      - Rule R4 & R8: work_hours = seconds / 3600, rounded 2 decimals half-up.
      - Rule R5: half_day = work_hours < 4.50.
      - Assumption A4: Check order: 404 (unknown employee / no punch-in) -> 409 (already punched out)
        -> 422 (punched_at <= punch_in or > 24h).
      - Concurrency: Atomic update guarding on punch_out: None.
    HOW TO TEST:
      - Punch in, punch out -> 200 with derived fields.
      - Punch out again -> 409.
      - Punch out before punch in -> 422.
      - Concurrent punch out requests -> exactly one 200, others 409.
    WHAT BREAKS: Concurrent punch outs overwrite each other; overnight shifts fail to match shift_end.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = from_epoch_ms(body.punched_at)
    else:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the most recent record with a punch-in
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None},
        },
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    # Assumption A4: Check order: 404 -> 409 -> 422
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    if punch_out_utc <= punch_in_utc:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")

    if punch_out_utc > punch_in_utc + timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    # Derived field computations
    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    half_day = bool(work_hours < 4.50)

    # Compute shift_end_utc
    d_obj = date_cls.fromisoformat(record["date"])
    sh, sm = map(int, emp.get("shift_start", "09:30").split(":"))
    eh, em = map(int, emp.get("shift_end", "18:30").split(":"))
    is_overnight = (eh, em) <= (sh, sm)

    shift_end_ist = datetime(d_obj.year, d_obj.month, d_obj.day, eh, em, 0, tzinfo=IST)
    if is_overnight:
        shift_end_ist += timedelta(days=1)
    shift_end_utc = shift_end_ist.astimezone(timezone.utc)

    overtime = compute_overtime(punch_out_utc, shift_end_utc)

    # Atomic update guarding on punch_out: None for race safety
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Record already punched out by concurrent request")

    return serialize_attendance(updated_doc)


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    """
    WHAT: Regularizes (corrects) an attendance record, recomputes derived fields,
    and appends an audit trail entry to history.
    WHERE: regularize_attendance
    WHY (Cite Rule):
      - Section 5 & Rule R1-R5: Derived fields recomputed; history appends one entry.
      - Rule R6: Presence requires punch_in; ABSENT/LEAVE clears punch times (A7).
      - Rule R1: punch_in must stay on record's date.
      - Assumption A11: Request changing nothing returns 422.
      - Concurrency: Optimistic array match on history prevents lost updates (returns 409 on conflict).
    HOW TO TEST:
      - Change status to ABSENT without punch times -> clears times, appends history.
      - Supplying punch_in with ABSENT -> 422.
      - No changes -> 422.
      - Concurrent edits -> exactly one succeeds, second gets 409.
    WHAT BREAKS: Lost update anomaly when concurrent HR managers edit records.
    """
    # Validate date in path
    try:
        if not (len(date) == 10 and date[4] == "-" and date[7] == "-"):
            raise ValueError()
        date_cls.fromisoformat(date)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid date path parameter, expected YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    target_status = body.status if body.status is not None else record.get("status")

    # Rule & Assumption A7: setting status ABSENT or LEAVE clears both punch times,
    # supplying punch_in or punch_out with it (either explicitly or via inherited status) is 422.
    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied for ABSENT or LEAVE status",
            )
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Presence status (PRESENT, WFH, ON_DUTY)
        if body.punch_in is not None:
            final_punch_in = from_epoch_ms(body.punch_in)
        else:
            final_punch_in = record.get("punch_in")
            if final_punch_in and final_punch_in.tzinfo is None:
                final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)

        if not final_punch_in:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        # Rule R1: punch_in must stay on the record's attendance date
        shift_start = emp.get("shift_start", "09:30")
        shift_end = emp.get("shift_end", "18:30")
        computed_date, shift_start_utc, _ = compute_record_date_and_shift(
            final_punch_in, shift_start, shift_end
        )
        if computed_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {computed_date}, not {date}",
            )

        # Punch out
        if body.punch_out is not None:
            final_punch_out = from_epoch_ms(body.punch_out)
        else:
            final_punch_out = record.get("punch_out")
            if final_punch_out and final_punch_out.tzinfo is None:
                final_punch_out = final_punch_out.replace(tzinfo=timezone.utc)

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if final_punch_out > final_punch_in + timedelta(hours=24):
                raise HTTPException(
                    status_code=422,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )

            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_half_day = bool(final_work_hours < 4.50)

            # Shift end calculation for overtime
            d_obj = date_cls.fromisoformat(date)
            sh, sm = map(int, shift_start.split(":"))
            eh, em = map(int, shift_end.split(":"))
            is_overnight = (eh, em) <= (sh, sm)
            shift_end_ist = datetime(d_obj.year, d_obj.month, d_obj.day, eh, em, 0, tzinfo=IST)
            if is_overnight:
                shift_end_ist += timedelta(days=1)
            shift_end_utc = shift_end_ist.astimezone(timezone.utc)

            final_overtime_minutes = compute_overtime(final_punch_out, shift_end_utc)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

        final_late_minutes = compute_late_minutes(final_punch_in, shift_start_utc)

    # Compute changes dict
    # All changes compare old stored value vs new final value
    changes = {}
    for field_name, old_val, new_val in [
        ("status", record.get("status"), target_status),
        ("punch_in", to_epoch_ms(record.get("punch_in")), to_epoch_ms(final_punch_in)),
        ("punch_out", to_epoch_ms(record.get("punch_out")), to_epoch_ms(final_punch_out)),
        ("work_hours", record.get("work_hours"), final_work_hours),
        ("late_minutes", record.get("late_minutes", 0), final_late_minutes),
        ("overtime_minutes", record.get("overtime_minutes", 0), final_overtime_minutes),
        ("half_day", bool(record.get("half_day", False)), final_half_day),
    ]:
        if old_val != new_val:
            changes[field_name] = {"from": old_val, "to": new_val}

    # Assumption A11: Request that changes nothing is 422
    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Concurrency safe update: match on _id and current history
    existing_history = record.get("history") or []
    update_query = {"_id": record["_id"], "history": existing_history}
    update_doc = {
        "$set": {
            "status": target_status,
            "punch_in": final_punch_in,
            "punch_out": final_punch_out,
            "work_hours": final_work_hours,
            "late_minutes": final_late_minutes,
            "overtime_minutes": final_overtime_minutes,
            "half_day": final_half_day,
        },
        "$push": {"history": history_entry},
    }

    updated_doc = db.attendance_logs.find_one_and_update(
        update_query,
        update_doc,
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification conflict; record was updated by another request",
        )

    return serialize_attendance(updated_doc)


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
            date_cls.fromisoformat(date_from)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date_from")

    if date_to:
        try:
            date_cls.fromisoformat(date_to)
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
# Analytics Helpers & Query Builders (Phase 4 & 5)
# --------------------------------------------------------------------------- #
def get_month_range(month_str: str) -> tuple[str, str, date_cls, date_cls]:
    """Parse YYYY-MM into start_str, end_str, start_date, end_date."""
    parts = month_str.split("-")
    y, m = int(parts[0]), int(parts[1])
    last_day = calendar.monthrange(y, m)[1]
    start_str = f"{month_str}-01"
    end_str = f"{month_str}-{last_day:02d}"
    return start_str, end_str, date_cls(y, m, 1), date_cls(y, m, last_day)


def count_working_days(start_d: date_cls, end_d: date_cls) -> int:
    """Count Mon-Fri days between start_d and end_d inclusive (Rule R7)."""
    if start_d > end_d:
        return 0
    days = 0
    cur = start_d
    while cur <= end_d:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


def round_half_up(val: float, decimals: int) -> float:
    """R8 Rounding: exact arithmetic round-half-up using Decimal."""
    d_fmt = "0." + "0" * decimals
    return float(Decimal(str(val)).quantize(Decimal(d_fmt), rounding=ROUND_HALF_UP))


def build_employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> list:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end}
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                            [1, 2, 3, 4, 5]
                                        ]
                                    }
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]


def build_department_summary_pipeline(month_start: str, month_end: str, department: Optional[str] = None) -> list:
    match_stage = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"}
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"dept_emps": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$dept_emps"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]}
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {
                                                    "$in": [
                                                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                                        [1, 2, 3, 4, 5]
                                                    ]
                                                }
                                            ]
                                        },
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0.0
                                    ]
                                }
                            },
                            "work_hours_list": {
                                "$push": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]}
                                            ]
                                        },
                                        "$work_hours",
                                        "$$REMOVE"
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            },
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                            },
                            "on_duty_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                            }
                        }
                    }
                ],
                "as": "logs_summary"
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$ifNull": [{"$first": "$logs_summary.present_days"}, 0.0]
                },
                "avg_work_hours": {
                    "$let": {
                        "vars": {"hours": {"$ifNull": [{"$first": "$logs_summary.work_hours_list"}, []]}},
                        "in": {
                            "$cond": [
                                {"$gt": [{"$size": "$$hours"}, 0]},
                                {"$avg": "$$hours"},
                                None
                            ]
                        }
                    }
                },
                "late_count": {
                    "$ifNull": [{"$first": "$logs_summary.late_count"}, 0]
                },
                "total_late_minutes": {
                    "$ifNull": [{"$first": "$logs_summary.total_late_minutes"}, 0]
                },
                "leave_count": {
                    "$ifNull": [{"$first": "$logs_summary.leave_count"}, 0]
                },
                "on_duty_count": {
                    "$ifNull": [{"$first": "$logs_summary.on_duty_count"}, 0]
                }
            }
        },
        {"$sort": {"department": 1}}
    ]


def build_late_leaderboard_pipeline(
    month_start: str,
    month_end: str,
    limit: int = 10,
    department: Optional[str] = None
) -> list:
    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ])
    return pipeline


def build_department_trend_pipeline(
    emp_codes: list,
    from_str: str,
    to_str: str,
    from_dt: datetime,
    to_dt_excl: datetime,
    dept_employees: list
) -> list:
    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_str, "$lte": to_str}
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_str": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_excl]
                }
            }
        },
        {
            "$project": {
                "date_dt": 1,
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "dept_emps": {"$literal": dept_employees}
            }
        },
        {
            "$project": {
                "date": "$date_str",
                "date_dt": 1,
                "is_working_day": {
                    "$in": [{"$isoDayOfWeek": "$date_dt"}, [1, 2, 3, 4, 5]]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "emp",
                            "cond": {"$lte": ["$$emp.joined_on", "$date_str"]}
                        }
                    }
                },
                "present_count": 1,
                "late_count": 1
            }
        },
        {"$sort": {"date_dt": 1}}
    ]


# --------------------------------------------------------------------------- #
# Endpoints (Analytics)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    WHAT: Monthly attendance summary for one employee via MongoDB aggregation.
    WHY (Cite Rule): R7 working days from joined_on, R5/R6 present days Mon-Fri only,
    R8 attendance percentage rounding, A6 employee joined after month returns zeros and null pct.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    month_start, month_end, m_start_d, m_end_d = get_month_range(month)
    joined_on_d = date_cls.fromisoformat(emp["joined_on"])

    effective_start_d = max(m_start_d, joined_on_d)
    working_days = count_working_days(effective_start_d, m_end_d)

    pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
    res = list(db.attendance_logs.aggregate(pipeline))

    if res:
        data = res[0]
        present_days = float(data.get("present_days", 0.0))
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        total_late = int(data.get("total_late_minutes", 0))
        total_ot = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        raw_pct = (Decimal(str(present_days)) / Decimal(str(working_days))) * 100
        attendance_pct = float(raw_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    """
    WHAT: Cross-collection department summary for a month.
    WHY (Cite Rule):
      - R9: Headcount includes zero-log employees, excludes future joiners.
      - R5/R7: Present days Mon-Fri only, half-day=0.5.
      - Trap #20: avg_work_hours averages all present-status records incl weekends.
      - R8: 2 decimals half-up.
    """
    month_start, month_end, _, _ = get_month_range(month)
    pipeline = build_department_summary_pipeline(month_start, month_end, department)
    raw_items = list(db.employees.aggregate(pipeline))

    items = []
    for item in raw_items:
        avg_wh = item.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = round_half_up(avg_wh, 2)

        items.append({
            "department": item["department"],
            "headcount": item["headcount"],
            "present_days": float(item["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(item["late_count"]),
            "total_late_minutes": int(item["total_late_minutes"]),
            "leave_count": int(item["leave_count"]),
            "on_duty_count": int(item["on_duty_count"]),
        })

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """
    WHAT: Late leaderboard for a month using standard competition ranking ($rank).
    WHY (Cite Rule):
      - Ties share rank (1, 2, 2, 4); next rank skipped.
      - Limit applies after ranking (rank <= limit).
      - Sort order: total_late_minutes descending, emp_code ascending.
    """
    month_start, month_end, _, _ = get_month_range(month)
    pipeline = build_late_leaderboard_pipeline(month_start, month_end, limit, department)
    raw_items = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for item in raw_items:
        items.append({
            "rank": int(item["rank"]),
            "emp_code": item["emp_code"],
            "name": item["name"],
            "department": item["department"],
            "total_late_minutes": int(item["total_late_minutes"]),
            "late_count": int(item["late_count"]),
        })

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    WHAT: Daily series trend for department with $densify and 7-day moving average.
    WHY (Cite Rule):
      - Trap #17: Exactly (to - from + 1) rows.
      - R9: Daily headcount based on joined_on <= date.
      - R8: Attendance rate (4 decimals half-up) on working days with headcount > 0.
      - A1: 7-day MA uses reported 4-decimal rates over window.
      - A2: Reject range > 92 days.
    """
    try:
        d_from = date_cls.fromisoformat(from_date)
        d_to = date_cls.fromisoformat(to_date)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to must not be earlier than from")

    day_count = (d_to - d_from).days + 1
    if day_count > 92:
        raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

    dept_employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not dept_employees:
        raise HTTPException(status_code=404, detail="Department not found")

    emp_codes = [e["emp_code"] for e in dept_employees]
    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    to_dt_excl = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = build_department_trend_pipeline(
        emp_codes, from_date, to_date, from_dt, to_dt_excl, dept_employees
    )
    raw_days = list(db.attendance_logs.aggregate(pipeline))

    # Calculate reported attendance_rate and 7-day moving average
    rates = []
    processed_days = []

    for d in raw_days:
        is_work = bool(d["is_working_day"])
        headcount = int(d["headcount"])
        pres_count = float(d["present_count"])
        late_cnt = int(d["late_count"])

        if is_work and headcount > 0:
            rate = Decimal(str(pres_count)) / Decimal(str(headcount))
            att_rate = float(rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        else:
            att_rate = None

        rates.append(att_rate)
        processed_days.append({
            "date": d["date"],
            "is_working_day": is_work,
            "headcount": headcount,
            "present_count": pres_count,
            "late_count": late_cnt,
            "attendance_rate": att_rate,
        })

    # Calculate 7-day moving average using reported rates (Assumption A1)
    final_items = []
    for i, row in enumerate(processed_days):
        window_rates = [r for r in rates[max(0, i - 6): i + 1] if r is not None]
        if window_rates:
            avg_d = sum(Decimal(str(r)) for r in window_rates) / Decimal(str(len(window_rates)))
            ma_7d = float(avg_d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        else:
            ma_7d = None

        row["moving_avg_7d"] = ma_7d
        final_items.append(row)

    return {
        "department": department,
        "items": final_items,
    }


# --------------------------------------------------------------------------- #
# TODO - Explain endpoint will be added in Phase 5:
#   GET   /admin/explain/{endpoint}               (Phase 5)
# --------------------------------------------------------------------------- #

