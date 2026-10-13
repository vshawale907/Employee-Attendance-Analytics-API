# 02_CRUD_AND_PUNCH_LIFECYCLE.md — Attendance CRUD & Regularization

> Phase 3 deliverable. Comprehensive teaching guide on the complete lifecycle of an attendance record:
> Punch-In -> Punch-Out -> Manual Regularization (PATCH).

---

## 1. The Attendance Lifecycle

An attendance record transitions through three main states:

```
[Day Starts]
     │
     ▼
POST /attendance/punch-in
     │  - Creates record for (emp_code, date)
     │  - Calculates date (R1) & late_minutes (R2)
     │  - punch_out: null, work_hours: null, overtime: 0, half_day: false, history: []
     ▼
[OPEN RECORD]
     │
     ▼
POST /attendance/punch-out
     │  - Finds most recent open record (punch_in <= punched_at)
     │  - Computes work_hours (R4), overtime (R3), half_day (R5)
     │  - Atomic guard ensures double punch-out returns 409
     ▼
[CLOSED RECORD]
     │
     ▼
PATCH /attendance/{emp_code}/{date} (Regularization)
     │  - Modifies status, punch_in, and/or punch_out
     │  - Validates business constraints (presence vs absent/leave)
     │  - Recomputes all derived fields
     │  - Appends audit entry to history with before/after changes
     ▼
[REGULARIZED RECORD]
```

---

## 2. Non-Trivial Functions Teaching Format

### 2.1 `POST /attendance/punch-out`
- **WHAT:** Closes an open attendance record by recording `punch_out` timestamp and computing `work_hours`, `overtime_minutes`, and `half_day`.
- **WHERE:** `app/main.py: punch_out` endpoint.
- **WHY (Cite Rules):**
  - **Rule R3 (Overtime):** Whole minutes from shift_end to punch_out, minimum 30 minutes.
  - **Rule R4 & R8 (Work Hours):** `(punch_out - punch_in) / 3600`, rounded 2 decimals half-up.
  - **Rule R5 (Half Day):** `work_hours < 4.50` flags `half_day = True`.
  - **Assumption A4:** Validation evaluation order: 404 (employee/punch-in not found) -> 409 (already punched out) -> 422 (invalid punched_at <= punch_in or > 24h).
- **HOW TO TEST:**
  1. Punch in at 09:30, punch out at 18:30 (9.00 work hours, 0 overtime, half_day=False).
  2. Punch out again on the same record -> 409 Conflict.
  3. Punch out with timestamp earlier than punch_in -> 422 Unprocessable Entity.
  4. Concurrent test: 5 simultaneous punch-outs -> exactly 1x 200, 4x 409.
- **WHAT BREAKS:** If double punch-out isn't guarded atomically with `punch_out: None`, concurrent requests overwrite hours or generate inconsistent reports.

---

### 2.2 `PATCH /attendance/{emp_code}/{date}` (Regularization)
- **WHAT:** Manually corrects a day's attendance record, recomputes all derived values, and appends an audit trail to `history`.
- **WHERE:** `app/main.py: regularize_attendance` endpoint.
- **WHY (Cite Rules):**
  - **Section 5 (History):** Audit trail of manual corrections only. Never modified or deleted. Records before and after values of changed fields.
  - **Rule R1:** Corrected `punch_in` must remain on the record's attendance date.
  - **Rule R6 & Section 5:** `ABSENT` / `LEAVE` cannot have punch times. Supplying them with punch times is 422.
  - **Assumption A11:** A PATCH that results in no changes to any stored fields must return 422.
  - **Concurrency:** Optimistic concurrency guard on `history` array prevents lost updates; loser receives 409.
- **HOW TO TEST:**
  1. Change status from `PRESENT` to `ABSENT` without punch times -> clears punch times, 0 late/overtime, appends history.
  2. Attempt to PATCH status `ABSENT` while supplying `punch_in` -> 422.
  3. Attempt to submit identical values -> 422 ("Request changes nothing").
  4. Run concurrent PATCH requests -> exactly one succeeds, second gets 409.
- **WHAT BREAKS:** Uncontrolled read-modify-write causes lost audit history entries when two admins edit a record concurrently.
