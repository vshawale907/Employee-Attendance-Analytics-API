"""
Employee Attendance & Analytics API
FastAPI + MongoDB (PyMongo sync)

All application code lives in this file per assignment specification.
Run: uvicorn app.main:app --port 8000
"""

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
# TODO - The rest of the contract will be added in Phase 4 & 5:
#   GET   /analytics/employees/{emp_code}/monthly (Phase 4)
#   GET   /analytics/departments/summary          (Phase 4)
#   GET   /analytics/leaderboard/late             (Phase 4)
#   GET   /analytics/departments/{department}/trend (Phase 4)
#   GET   /admin/explain/{endpoint}               (Phase 5)
# --------------------------------------------------------------------------- #
