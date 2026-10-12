# 01_STARTER_REVIEW_AND_FIXES.md — Deep-Dive on Starter Defects and Fixes

> Phase 2 teaching document. Explains every defect found in `app/main.py`, citing rules, explaining consequences, and demonstrating testing.

---

## 1. Helper Functions Defects

### 1.1 `compute_late_minutes`
- **WHERE:** `app/main.py:33-37`
- **WHAT:** 
  1. Truncation order bug: Computes `int((punch_in - start).total_seconds() / 60)` before comparing with 10.
  2. Timezone bug: `.replace(hour=h, minute=m, ...)` anchors the shift time directly on the timestamp's time zone (UTC), effectively shifting the shift start by 5h30m.
  3. Overnight bug: For overnight shifts, punch-ins before `shift_end` belong to yesterday's shift.
- **WHY (Cite Rule):** **Rule R2** states: *"Grace period 10 minutes. Late only if the punch-in is strictly more than 10:00 after shift_start; then late_minutes = whole minutes (floor) since shift_start, not since the grace period ended. Shift 09:30: 09:40:00 gives 0; 09:40:01 gives 10; 10:15:59 gives 45."* **Rule R1** mandates IST calendar alignment.
- **HOW TO TEST:** Pass punch-in at `09:40:01 IST` (601 elapsed seconds). Starter produces `0` because `int(601/60) = 10` and `10 > 10` is False. The fixed version checks `elapsed_seconds > 600` and returns `601 // 60 = 10`.
- **WHAT BREAKS:** Employees arriving at 09:40:01 are marked on-time instead of 10 minutes late. Grader tests for 09:40:01 fail.

---

### 1.2 `compute_work_hours`
- **WHERE:** `app/main.py:40-41`
- **WHAT:** Uses Python `round((punch_out - punch_in).total_seconds() / 3600, 2)`.
- **WHY (Cite Rule):** **Rule R4** and **Rule R8** state: *"rounded to 2 decimals half-up"*. Python's built-in `round()` uses round-half-to-even (banker's rounding). For example, `0.125` rounds to `0.12` in Python, but under round-half-up it must round to `0.13`.
- **HOW TO TEST:** Calculate work hours for 450 seconds (`0.125` hours). Python `round(0.125, 2)` gives `0.12`. `Decimal('0.125').quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)` gives `0.13`.
- **WHAT BREAKS:** Cumulative work hours across pay periods drift downwards; half-day threshold comparisons at boundaries misclassify shifts.

---

### 1.3 `compute_overtime`
- **WHERE:** `app/main.py:44-48`
- **WHAT:** 
  1. Missing minimum threshold: Counts all minutes after shift end without checking the 30-minute minimum.
  2. Naive/aware timezone mismatch: `end` is given `tzinfo=IST` while `punch_out` might be offset-naive UTC, raising `TypeError`.
  3. Overnight shift end: For overnight shifts, `shift_end` is on `date + 1 day`, not `date_str`.
- **WHY (Cite Rule):** **Rule R3** states: *"Whole minutes (floor) from shift_end to punch_out, counted only if that is at least 30; otherwise 0."*
- **HOW TO TEST:** Punch-out 29 minutes after shift end. Starter returns `29`. Fixed version returns `0`.
- **WHAT BREAKS:** Employees leaving 10 or 25 minutes late get unearned overtime recorded, failing analytics and payroll calculations.

---

## 2. Endpoint Defects

### 2.1 `/health`
- **WHERE:** `app/main.py:72-74`
- **WHAT:** Returns `{"status": "ok"}` statically without querying MongoDB.
- **WHY (Cite Rule):** Section 7 and contract specify: *"200 only when MongoDB answers; else 503."*
- **HOW TO TEST:** Stop MongoDB or provide invalid connection string. Starter returns 200; fixed endpoint pings MongoDB with timeout and returns 503.
- **WHAT BREAKS:** Monitoring tools and container orchestrators report the service healthy when the database is unreachable.

---

### 2.2 `POST /employees`
- **WHERE:** `app/main.py:77-86`
- **WHAT:** 
  1. Check-then-insert race condition: `find_one` followed by `insert_one`.
  2. Naive local time: `datetime.now()` instead of UTC datetime.
  3. Missing input validations: No regex checks on `emp_code`, `email`, `shift_start`, `shift_end`, `joined_on`.
- **WHY (Cite Rule):** Cross-Cutting Trap #5: Check-then-insert races. Hard constraint: Instants are BSON UTC datetimes in DB and epoch millis in API.
- **HOW TO TEST:** Send two concurrent requests creating the same `emp_code`. Without unique index, both succeed or corrupt data.
- **WHAT BREAKS:** Grader concurrency test fails; duplicate employees created; invalid date formats crash subsequent queries.

---

### 2.3 `GET /employees`
- **WHERE:** `app/main.py:88-96`
- **WHAT:** 
  1. Off-by-one pagination: `skip = page * page_size` skips the first page (`page=1` skips 20 items).
  2. Unstable sort: No `.sort("emp_code", 1)` specified.
  3. Total count ignores filter: `count_documents({})` counts entire collection instead of `q`.
  4. Empty string department filter dropped: `if department:` evaluates `""` to False.
- **WHY (Cite Rule):** **Rule R10**, Assumption A8, Trap #11.
- **HOW TO TEST:** Query `page=1, page_size=5`. Starter returns items 6-10. Fixed returns items 1-5.
- **WHAT BREAKS:** First page of employees is completely missing; department filtering displays wrong total count.

---

### 2.4 `POST /attendance/punch-in`
- **WHERE:** `app/main.py:99-122`
- **WHAT:** 
  1. No check for employee existence: If `find_one` is None, `emp["shift_start"]` throws `TypeError` (HTTP 500) instead of 404.
  2. Race condition: Check-then-insert on `attendance_logs`.
  3. Leaking `id`: Injects `doc["id"] = str(res.inserted_id)`.
  4. Naive local timestamp & ignoring IST / overnight rule.
  5. Returning datetimes instead of epoch milliseconds.
- **WHY (Cite Rule):** **Rule R1**, Hard Constraints: No `_id` or `id` accepted or returned; instants are epoch millis; 500 is a failed test.
- **HOW TO TEST:** Punch in with non-existent `emp_code="UNKNOWN"`. Starter throws 500; fixed returns 404.
- **WHAT BREAKS:** Grader tests with unknown employees get 500 instead of 404; contract validator rejects response due to unauthorized `id` field.

---

### 2.5 `GET /attendance`
- **WHERE:** `app/main.py:124-150`
- **WHAT:** 
  1. Loads all documents into memory via `list(db.attendance_logs.find(q))` and sorts in Python.
  2. Unstable sort: `docs.sort(key=lambda d: d["date"], reverse=True)` lacks a tie-breaker.
  3. Leaking `id` and datetime serialization failure.
- **WHY (Cite Rule):** Hard Constraint: No fetch-and-loop in Python; queries must be fast at 100k docs; no `id` in response.
- **HOW TO TEST:** Measure latency or query plan on large collection.
- **WHAT BREAKS:** Memory exhaustion on large datasets; slow responses (>10s); invalid response format.
