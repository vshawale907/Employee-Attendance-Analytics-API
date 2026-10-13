# 03_ANALYTICS_AND_AGGREGATIONS.md — MongoDB Aggregations for Attendance Analytics

> Phase 4 teaching document. Details the four analytics aggregation pipelines in `app/main.py`:
> 1. Employee Monthly Summary
> 2. Cross-Collection Department Summary
> 3. Late-Comers Leaderboard with Competition Ranking ($rank)
> 4. Department Trend with Gap-Filling ($densify) and Moving Average

---

## 1. Overview of Analytics Architecture

The core challenge of this system is that operational attendance records are written individually, but business questions require aggregates:
- Who is late?
- Which departments are slipping?
- What does the daily attendance trend look like?

### Why MongoDB Aggregations?
In a system with 100,000+ attendance records, fetching logs into Python memory and looping with `for log in logs:` is banned:
1. **Network bottleneck:** Transferring 100,000 documents consumes hundreds of megabytes of bandwidth.
2. **Memory consumption:** Storing 100,000 Python dictionary objects causes heavy RAM usage and GC pauses.
3. **Execution time:** MongoDB runs aggregation stages in C++ close to the stored data, utilizing index scans (`IXSCAN`).

---

## 2. The Four Endpoints in Teaching Format

### 2.1 `GET /analytics/employees/{emp_code}/monthly`
- **WHAT:** Aggregates a single employee's monthly attendance: working days, present days (Mon-Fri only, half-day=0.5), leave days, late count, total late minutes, total overtime minutes, and attendance percentage.
- **WHERE:** `app/main.py: employee_monthly` endpoint.
- **WHY (Cite Rules):**
  - **Rule R7:** `working_days` counts Mon-Fri calendar days from `max(month_start, joined_on)` to `month_end`.
  - **Rule R6 & R7:** `present_days` counts Mon-Fri presence-status logs only; weekend logs do not count towards present days.
  - **Rule R5:** `half_day = True` counts as 0.5 present days.
  - **Rule R8:** `attendance_pct` = `present_days / working_days * 100` rounded 2 decimals half-up.
  - **Assumption A6:** If employee joined after the requested month, returns 200 with 0s and `attendance_pct = null`.
- **HOW TO TEST:** Run for standard employee with known logs, and test with mid-month joiner and post-month joiner.
- **WHAT BREAKS:** Counting weekend presence towards `present_days` inflates attendance percentage over 100%.

---

### 2.2 `GET /analytics/departments/summary`
- **WHAT:** Cross-collection aggregation summarizing monthly attendance per department: headcount, total present days, mean work hours, late count, total late minutes, leave count, and on-duty count.
- **WHERE:** `app/main.py: department_summary` endpoint.
- **WHY (Cite Rules):**
  - **Rule R9:** Headcount includes all employees with `joined_on <= month_end`, including those with zero attendance logs!
  - **Trap #13:** Running `$lookup` from logs to employees would omit employees with no logs. Instead, we aggregate from `employees` grouped by `department`, then perform `$lookup` to `attendance_logs`.
  - **Trap #20:** `avg_work_hours` averages all presence-status records that have a `work_hours` value, including weekend records. Average of records, not average of employee averages.
- **HOW TO TEST:** Query across departments with seed data; verify employees with no logs are counted in headcount.
- **WHAT BREAKS:** If starting from `attendance_logs`, employees with 0 logs are excluded from headcount, violating Rule R9.

---

### 2.3 `GET /analytics/leaderboard/late`
- **WHAT:** Ranks employees with late minutes > 0 by total late minutes descending, using standard competition ranking (ties share rank, next rank skipped).
- **WHERE:** `app/main.py: late_leaderboard` endpoint.
- **WHY (Cite Rules):**
  - **Competition Ranking ($rank):** Ties share rank (1, 2, 2, 4). Using array position or `$denseRank` violates contract.
  - **Post-Ranking Limit:** `limit` filters on `rank <= limit`. If three employees tie at rank 10, all three are returned.
  - **Tie-Breaker:** Rows ordered by `total_late_minutes` descending, then `emp_code` ascending.
- **HOW TO TEST:** Seed records with identical late minutes; verify ties share rank and `rank <= limit` returns tied records.
- **WHAT BREAKS:** Using MongoDB `$limit` before ranking or using array index as rank misranks tied employees.

---

### 2.4 `GET /analytics/departments/{department}/trend`
- **WHAT:** Returns exactly one row per calendar day between `from` and `to` inclusive, filling gap days with `$densify`, calculating daily headcount, present count, late count, attendance rate, and a 7-day moving average.
- **WHERE:** `app/main.py: department_trend` endpoint.
- **WHY (Cite Rules):**
  - **Trap #17:** Must return exactly `(to - from + 1)` rows.
  - **Rule R9:** Daily headcount counts department employees with `joined_on <= that_day`.
  - **Rule R8:** `attendance_rate` = `present_count / headcount` (4 decimals half-up) on working days with headcount > 0; `null` otherwise.
  - **Assumption A1:** 7-day moving average averages non-null reported 4-decimal rates over current row and up to 6 preceding rows inside the range.
- **HOW TO TEST:** Query 10-day range with missing logs; verify `$densify` produces all 10 dates with correct gap defaults.
- **WHAT BREAKS:** A Python date loop fails explain plan audit; missing gap filling causes discontinuous charts.
