# 00_UNDERSTANDING.md — System Understanding Report

> Phase 1 deliverable. No code here — this is purely analysis.
> Written before touching any endpoint logic.

---

## 1. The System in Plain Words

**What it is:** A REST API that lets an HR system record employee attendance and run reports on it.

**Two actors:** A *punch clock* (hardware or app) that calls punch-in/punch-out, and an *HR admin* who queries reports and corrects records manually (regularization).

**Two collections in MongoDB:**
- `employees` — one document per person, with their shift times and join date.
- `attendance_logs` — one document per (employee, calendar-day). Created at punch-in; enriched at punch-out; optionally corrected via PATCH. Analytics queries both.

**Lifecycle of one working day:**
1. Employee arrives → `POST /attendance/punch-in` → a new log doc is created with `late_minutes` computed, everything else at defaults.
2. Employee leaves → `POST /attendance/punch-out` → the open doc is updated with `work_hours`, `overtime_minutes`, `half_day`.
3. HR corrects it next day → `PATCH /attendance/{emp_code}/{date}` → derived fields recomputed, one entry appended to `history`.
4. Manager runs a report → `GET /analytics/...` → MongoDB aggregation pipelines read stored values (never recompute).

**Why one file?** The grader clones the repo and runs `uvicorn app.main:app --port 8000`. All code must be in one place so the grader doesn't have to chase imports.

---

## 2. Request Lifecycle Diagram

```
Client (curl / test / HR app)
        │
        │  HTTP request (JSON body or query params)
        ▼
┌───────────────────────────────┐
│      uvicorn (ASGI server)    │  ← listens on port 8000
│  handles HTTP framing,        │
│  spawns a thread per request  │
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│    FastAPI routing layer      │  ← matches URL + method to endpoint function
│    (@app.get, @app.post …)    │
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│  Pydantic validation          │  ← checks types, patterns, ranges
│  (request body + query params)│    fails → 422, never reaches endpoint fn
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│   Endpoint function (def)     │  ← plain Python function run in thread pool
│   (business logic in          │    can call blocking I/O safely
│    app/main.py)               │
└──────────────┬────────────────┘
               │  PyMongo (sync, blocking)
               ▼
┌───────────────────────────────┐
│   MongoDB (Docker container)  │  ← stores documents, runs aggregations
│   attendance_db               │    returns BSON docs with UTC datetimes
└──────────────┬────────────────┘
               │  Python dicts
               ▼
┌───────────────────────────────┐
│  Serializer (serialize_record)│  ← strips _id, converts datetimes → ms
│  Pydantic response model      │    FastAPI encodes as JSON
└──────────────┬────────────────┘
               │
               ▼
        HTTP response (JSON)
        back to client
```

**Key insight:** FastAPI runs `def` (sync) endpoints in a **thread pool** — multiple requests run simultaneously in different threads. PyMongo is thread-safe. That's why we can use simple synchronous PyMongo and still handle concurrent requests correctly.

---

## 3. All 12 Endpoints

| # | Method | Path | Tags | Key status codes | What it does |
|---|--------|------|------|-----------------|--------------|
| 1 | GET | `/health` | system | 200, 503 | Pings MongoDB; 503 if down |
| 2 | POST | `/employees` | employees | 201, 409, 422 | Create employee; unique emp_code |
| 3 | GET | `/employees` | employees | 200, 422 | List employees, paged, sorted by emp_code |
| 4 | POST | `/attendance/punch-in` | attendance | 201, 404, 409, 422 | Open a day's attendance record |
| 5 | GET | `/attendance` | attendance | 200, 422 | List attendance records, paged |
| 6 | POST | `/attendance/punch-out` | attendance | 200, 404, 409, 422 | Close record, compute work_hours etc. |
| 7 | PATCH | `/attendance/{emp_code}/{date}` | attendance | 200, 404, 409, 422 | Regularize: correct fields, append history |
| 8 | GET | `/analytics/employees/{emp_code}/monthly` | analytics | 200, 404, 422 | Monthly summary for one employee |
| 9 | GET | `/analytics/departments/summary` | analytics | 200, 422 | Cross-collection department summary |
| 10 | GET | `/analytics/leaderboard/late` | analytics | 200, 422 | Late-comers leaderboard with $rank |
| 11 | GET | `/analytics/departments/{department}/trend` | analytics | 200, 404, 422 | Daily trend with gap-fill ($densify) + 7-day MA |
| 12 | GET | `/admin/explain/{endpoint}` | admin | 200, 422 | MongoDB explain plan for 5 named endpoints |

**Missing 500:** Every endpoint must convert `PyMongoError` → 503. A bare 500 is always a test failure.

---

## 4. Rules R1-R10 — In My Own Words + 2 Numeric Examples Each

### R1 — Time zone and attendance date
**What it means:** Everything stored as a UTC datetime. But the HR world runs on IST (UTC+05:30). When you punch in, we look at what time it is in IST and use that IST calendar date as the `date` field. Special case: overnight shifts like 22:00-06:00 — if you punch in at 01:00 IST, that's the tail end of yesterday's shift, so `date` = yesterday.

The rule: if `shift_end <= shift_start` (overnight), and the IST time of `punch_in` is **earlier than** `shift_end`, then `date = IST_date - 1 day`. Otherwise `date = IST_date`.

Also: before anything else, truncate the timestamp to whole seconds. `09:40:00.900` → `09:40:00`.

**Example 1** (normal shift 09:30-18:30):
- `punch_in = 2026-07-06T19:00:00Z` → IST = `2026-07-07T00:30:00+05:30`
- IST date = **2026-07-07** (not the 6th)
- Not overnight, so no subtraction. `date = "2026-07-07"`

**Example 2** (overnight shift 22:00-06:00):
- `punch_in = 2026-07-07T19:55:00Z` → IST = `2026-07-08T01:25:00+05:30`
- IST time 01:25, `shift_end = 06:00`. Is 01:25 < 06:00? YES.
- So subtract one day: `date = "2026-07-07"` (the shift started the previous evening)

---

### R2 — Late minutes
**What it means:** You get a 10-minute grace period. Late only if you arrive **strictly more than 10 minutes** after shift start. If late, `late_minutes` = total minutes since shift start (not since grace period ended). The comparison is done on **seconds** before flooring to minutes — this catches the 09:40:01 case.

**Example 1** (shift 09:30):
- Punch-in 09:40:00 → seconds since shift start = 600. Is 600 > 600? **NO** → `late_minutes = 0`
- Punch-in 09:40:01 → seconds since shift start = 601. Is 601 > 600? **YES** → `late_minutes = 601 // 60 = 10`

**Example 2** (shift 09:30):
- Punch-in 10:15:59 → seconds = 2759. Is 2759 > 600? YES → `late_minutes = 2759 // 60 = 45`
- Punch-in 10:15:00 → seconds = 2700. `late_minutes = 2700 // 60 = 45`

---

### R3 — Overtime minutes
**What it means:** Only counts if you stay at least 30 minutes after shift end. Then `overtime_minutes` = whole minutes between shift end and punch-out.

**Example 1** (shift end 18:30):
- Punch-out 18:59:00 → 29 minutes. Is 29 >= 30? **NO** → `overtime_minutes = 0`
- Punch-out 18:59:59 → 29 minutes (floor). **NO** → `overtime_minutes = 0`

**Example 2** (shift end 18:30):
- Punch-out 19:00:00 → 30 minutes. Is 30 >= 30? **YES** → `overtime_minutes = 30`
- Punch-out 19:10:45 → 40 minutes (floor of 40.75). **YES** → `overtime_minutes = 40`

---

### R4 — Work hours
**What it means:** `(punch_out - punch_in)` in seconds, divided by 3600, rounded to 2 decimal places using **half-up** rounding (not Python's built-in `round()` which uses half-even/banker's rounding).

**Example 1** (half-up matters):
- Seconds = 16200 → 16200 / 3600 = 4.5 exactly → `work_hours = 4.50`
- Seconds = 16199 → 16199 / 3600 = 4.49972... → half-up rounds to `4.50` (because at 2dp: `4.4997` rounds to `4.50`) 
  - Wait: 4.4997... the 3rd decimal is 9 so the 2nd decimal rounds up. `work_hours = 4.50`

**Example 2**:
- Seconds = 16170 → 16170 / 3600 = 4.491̄6̄... → `work_hours = 4.49`
- Python's `round(4.4916, 2) = 4.49` ✓ (no tie here, so half-up and half-even agree)

---

### R5 — Half day
**What it means:** After punch-out, look at the **rounded** `work_hours`. If it's less than 4.50, mark `half_day = True`. A half day counts as 0.5 of a present day for analytics.

**Example 1**:
- Seconds = 16199 → `work_hours = 4.50` (from R4 above) → 4.50 < 4.50? **NO** → `half_day = False`
- Seconds = 16150 → 16150 / 3600 = 4.4861... → `work_hours = 4.49` → 4.49 < 4.50? **YES** → `half_day = True`

**Example 2**:
- Seconds = 12600 (3.5 hours, like EMP0004 sample doc) → `work_hours = 3.50` → `half_day = True`
- Seconds = 18000 (5 hours) → `work_hours = 5.00` → `half_day = False`

---

### R6 — Presence
**What it means:** `PRESENT`, `WFH`, `ON_DUTY` count as "present" for analytics. `ABSENT` and `LEAVE` don't. A day with **no record at all** also doesn't count — that's not the same as ABSENT.

**Example 1**: EMP0003 on 2026-07-06 has `status="PRESENT"` → present. On 2026-07-08 has `status="LEAVE"` → not present.

**Example 2**: EMP0001 has no record on 2026-07-08 → not present, but also NOT "absent". The analytics simply have no document for that day — it just vanishes from totals.

---

### R7 — Working days
**What it means:** Mon-Fri only, no holiday calendar. For a mid-month joiner, start counting from `joined_on`. Weekend records still count toward late/overtime totals but NOT toward `present_days`.

**Example 1** (mid-month joiner):
- EMP0002 joined 2026-07-13 (a Monday). Month is July 2026.
- Working days for EMP0002 in July = Mon-Fri from the 13th to the 31st = 13 working days (Jul 13-17, 20-24, 27-31).
- An employee who joined the 1st would have Mon-Fri from Jul 1-31 = 23 working days.

**Example 2** (weekend records):
- EMP0005 punches in on Saturday 2026-07-05 (overnight shift). The record exists.
- Its `late_minutes` contributes to `total_late_minutes` but the record does NOT count toward `present_days`.

---

### R8 — Rounding of reported numbers
**What it means:** Financial-grade rounding. All reported numbers are half-up to 2 decimal places (rates: 4 decimal places). Python's `round()` is half-even (banker's rounding): `round(0.125, 2) = 0.12`, but half-up gives `0.13`. Use `Decimal` with `ROUND_HALF_UP`.

**Example 1**: `attendance_pct` where present_days=5, working_days=9:
- `5/9 * 100 = 55.555...%`
- half-up 2dp: `55.56`
- Python `round(55.555, 2)` might give `55.56` or `55.55` depending on float representation — **don't rely on it**

**Example 2**: `attendance_rate` (4dp) where present2=3, headcount=4:
- `3 / (2*4) = 0.375`
- half-up 4dp: `0.3750` (tie at 4th decimal: 0.3750 exactly)
- But for `0.33335` half-up gives `0.3334`; half-even gives `0.3333` — these differ

---

### R9 — Headcount
**What it means:** For a department summary or trend day, count employees whose `joined_on` is on or before the end of the period. This ensures employees with zero logs still appear in headcount (the pipeline must start from `employees`, not from `attendance_logs`).

**Example 1** (month = July 2026):
- EMP0001 joined 2026-01-05: `joined_on <= 2026-07-31`? YES → counts
- A hypothetical EMP0099 joined 2026-08-01: `joined_on <= 2026-07-31`? NO → does NOT count

**Example 2** (trend for a single day 2026-07-10):
- EMP0002 joined 2026-07-13: `joined_on <= 2026-07-10`? NO → headcount=0 for EMP0002 on July 10
- EMP0001 joined 2026-01-05: YES → counts in headcount

---

### R10 — Pagination
**What it means:** Pages start at 1 (not 0). Default page size = 20. Max page size = 100. The `total` field in responses always reflects the filtered set — not the whole collection.

**Example 1**:
- 45 employees in Engineering. `GET /employees?department=Engineering&page=2&page_size=20`
- `total = 45`, `page = 2`, `page_size = 20`
- Returns employees at positions 21-40 (0-indexed: skip=20, limit=20)
- skip = (page - 1) * page_size = (2-1)*20 = 20 ✅ (starter had `page * page_size = 40` — off by one!)

**Example 2**:
- `page = 1, page_size = 20`, 5 matching records → returns all 5, `total = 5`, `page = 1`
- Huge page: `page = 10**18` → skip would be astronomically large. Must short-circuit to empty page, never overflow.

---

## 5. Ambiguities / Contradictions Found

| # | Where | What | Decision / Resolution |
|---|-------|------|----------------------|
| 1 | R1 + overnight rule | "earlier than shift_end" — is this IST time-of-day, not datetime? | YES — compare IST time components only, not absolute datetime |
| 2 | `openapi.yaml` `PunchOutRequest` | `punched_at` has no `required`, defaults to "now" | Confirmed: if omitted, use `datetime.now(UTC)` |
| 3 | `RegularizeRequest` | Both `reason` AND `regularized_by` are in `required` array | Always required; omitting either → 422 |
| 4 | PATCH: "ABSENT/LEAVE clears both punch times" | What if the record was already ABSENT with no punch times? Does it count as "no change"? | Only fields that truly change go in `changes`. If ABSENT→ABSENT with no punch times, likely `changes` is empty → 422 "nothing changed" |
| 5 | Trend `attendance_rate` | "null if not a working day or headcount=0" | `null` on weekends AND on working days with headcount=0 (e.g., no one has joined yet) |
| 6 | PATCH punch_in validation (A10) | "punch_in whose R1 date differs from record's date → 422" — what if the employee has an overnight shift? | The new punch_in's R1-computed date must equal the record's stored `date` field. Use the employee's shift to compute. |
| 7 | `EpochMillis` `nullable: true` in schema vs strict int | `punch_in` in PATCH body is `EpochMillis` (not Nullable) — client cannot null it explicitly | A PATCH that omits `punch_in` leaves it unchanged. Client cannot set punch_in to null via PATCH (they'd change status to ABSENT/LEAVE for that). |
| 8 | `$densify` upper bound | MongoDB docs say lower bound inclusive, upper bound **exclusive** | Use `[from_date, to_date + 1 day)` as bounds. Test explicitly. |

---

## 6. New Assumptions (beyond the register)

| ID | Assumption | Rationale |
|----|-----------|-----------|
| A11 | PATCH with identical final state → 422 "nothing changed" even if ABSENT→ABSENT | `changes` is empty → the guard catches it |
| A12 | Trend `present_count` includes half-days as 0.5 (weekend included) | openapi.yaml: "presence-status records that day (half day = 0.5), any weekday or weekend" |
| A13 | `avg_work_hours` in department summary = average over ALL present-status records with non-null `work_hours`, including weekends (rule §5.9 note 20) | Distinct from `present_days` which is Mon-Fri only |
