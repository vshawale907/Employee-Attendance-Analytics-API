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
from enum import Enum
import json
from typing import Optional

from bson import json_util
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
    Creates compound and unique indexes idempotently across employees
    and attendance_logs collections to optimize query performance and
    enforce storage-level data integrity.
    """
    # employees collection
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("status", 1), ("date", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])



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
    """Translates uncaught database errors into HTTP 503 Service Unavailable."""
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
    Computes attendance date in Indian Standard Time (IST, UTC+05:30) and converts
    shift start/end boundary times to UTC. Handles overnight shifts where shifts
    ending early morning belong to the prior calendar day.
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
    Calculates arrival delay in minutes. Applies a 10-minute grace period
    where delays <= 10 minutes incur 0 penalty, while delays exceeding 10 minutes
    count the full duration from scheduled shift start.
    """
    elapsed_seconds = int((punch_in_utc - shift_start_utc).total_seconds())
    if elapsed_seconds > 600:
        return elapsed_seconds // 60
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    Computes total elapsed working hours rounded half-up to 2 decimal places
    using Decimal arithmetic to prevent floating-point drift.
    """
    elapsed_seconds = int((punch_out_utc - punch_in_utc).total_seconds())
    if elapsed_seconds <= 0:
        return 0.0
    val = (Decimal(elapsed_seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(val)


def compute_overtime(punch_out_utc: datetime, shift_end_utc: datetime) -> int:
    """
    Calculates overtime in minutes past shift end. Enforces a 30-minute minimum
    qualification threshold.
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
    """Verifies database connectivity via MongoDB ping command."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Registers a new employee record. Uniqueness of emp_code is enforced
    at the database layer to prevent check-then-insert race conditions.
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
    Returns a paginated list of employees sorted by emp_code ascending.
    Supports filtering by exact department match.
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
    Records an employee punch-in. Computes the attendance date in IST
    (handling overnight shifts) and calculates initial arrival delay.
    Guarded by a unique compound index on (emp_code, date) to prevent duplicate punches.
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
    Records an employee punch-out for their open attendance record.
    Calculates total work hours, overtime, and half-day status.
    Uses atomic find_one_and_update to guarantee concurrency safety.
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
    Manually regularizes an attendance record, recalculating derived fields
    (work hours, late minutes, overtime, half day) and appending an audit
    trail entry to the history array.
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
    Returns a paginated list of attendance logs sorted by date descending,
    with emp_code ascending as a secondary tie-breaker. Supports filtering
    by employee, date range, and status.
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
    Computes monthly attendance summary for an employee. Prorates working days
    based on joined_on date, excludes weekends from present days, and calculates
    financial-grade attendance percentage rounded half-up to 2 decimal places.
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
    Computes a cross-collection department-level monthly attendance summary.
    Counts active headcount from employees collection (including zero-log employees),
    and aggregates weekday present days and overall average working hours.
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
    Generates a monthly late arrival leaderboard using standard competition ranking ($rank).
    Employees tied on late minutes share rank with subsequent ranks skipped.
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
    Generates a continuous daily attendance trend for a department up to 92 days.
    Fills calendar gaps using $densify and calculates 7-day moving averages.
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

    # Calculate 7-day moving average using reported rates
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
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
class ExplainEndpoint(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: ExplainEndpoint = Path(..., description="Target endpoint name"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Executes MongoDB explain with executionStats verbosity for the primary query
    or aggregation pipeline of the named endpoint, confirming index utilization.
    """
    if endpoint == ExplainEndpoint.attendance_list:
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

        skip = (page - 1) * page_size
        collection_name = "attendance_logs"
        explain_raw = db.command(
            "explain",
            {
                "find": collection_name,
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

    elif endpoint == ExplainEndpoint.employee_monthly:
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")

        month_start, month_end, _, _ = get_month_range(month)
        pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
        collection_name = "attendance_logs"
        explain_raw = db.command(
            "explain",
            {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    elif endpoint == ExplainEndpoint.department_summary:
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        month_start, month_end, _, _ = get_month_range(month)
        pipeline = build_department_summary_pipeline(month_start, month_end, department)
        collection_name = "employees"
        explain_raw = db.command(
            "explain",
            {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    elif endpoint == ExplainEndpoint.late_leaderboard:
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        effective_limit = limit if limit is not None else 10
        month_start, month_end, _, _ = get_month_range(month)
        pipeline = build_late_leaderboard_pipeline(month_start, month_end, effective_limit, department)
        collection_name = "attendance_logs"
        explain_raw = db.command(
            "explain",
            {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    elif endpoint == ExplainEndpoint.department_trend:
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")

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
        emp_codes = [e["emp_code"] for e in dept_employees]
        from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
        to_dt_excl = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc) + timedelta(days=1)

        pipeline = build_department_trend_pipeline(
            emp_codes, from_date, to_date, from_dt, to_dt_excl, dept_employees
        )
        collection_name = "attendance_logs"
        explain_raw = db.command(
            "explain",
            {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    clean_explain = json.loads(json_util.dumps(explain_raw))
    return {
        "endpoint": endpoint.value,
        "collection": collection_name,
        "explain": clean_explain,
    }


