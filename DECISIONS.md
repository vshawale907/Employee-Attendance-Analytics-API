# DECISIONS.md

1. **Indexes.**
- `employees`: `emp_code` (unique, identity lookup); `(department, emp_code)` (filtered listing with stable sort); `joined_on` and `(department, joined_on)` (department summary headcount filtering without COLLSCAN).
- `attendance_logs`: `(emp_code, date)` (unique, one log per day guarantee, prevents race conditions); `(date, -1, emp_code, 1)` (paginated listing); `(status, date)` (status queries); `(date, late_minutes)` (late leaderboard date range and threshold scan).
- Rejected: `(late_minutes, -1)` standalone, because queries filter by bounded date first; an unbound index adds write overhead without index-covering range scans.

2. **Punch-in race.**
Both concurrent requests compute the IST date and attempt insertion. The unique index on `(emp_code, date)` acts as the atomic boundary in MongoDB. Exactly one write commits (201); the second triggers a `DuplicateKeyError` (code 11000), which our handler translates into a clean HTTP 409 Conflict.

3. **Ties.**
We use MongoDB `$rank` in `$setWindowFields` for standard competition ranking (tied records share rank, skipping subsequent ranks). The filter `rank <= limit` includes all tied candidates at the threshold. Results sort by `total_late_minutes` descending, using `emp_code` ascending as a deterministic tie-breaker.

4. **Headcount.**
The pipeline queries `employees` first (matching `joined_on <= month_end`), groups by department to capture total headcount, and `$lookup`s `attendance_logs`. Defaulting metrics via `$ifNull` to `0` guarantees employees with zero logs are fully counted in department headcount.

5. **One thing you would change if this had to serve 100x the data.**
Maintain incremental, pre-aggregated rollups in summary collections via MongoDB Change Streams on write, avoiding on-demand `$lookup` and `$densify` pipelines across hundreds of millions of raw logs.
