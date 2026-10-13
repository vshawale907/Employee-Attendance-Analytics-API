# 04_EXPLAIN_AND_INDEXES.md — Query Optimization, Index Strategy & MongoDB Explain

> Phase 5 teaching document. Explains the architecture of `GET /admin/explain/{endpoint}`, compound index design, and how to guarantee `IXSCAN` while eliminating `COLLSCAN` on 100k+ records.

---

## 1. Why `explain` with `executionStats` Matters

When an application runs against a small seed database (15-50 records), every query finishes in less than 5 milliseconds—even a disastrous full collection scan (`COLLSCAN`). But when the grader or a production load test runs against **100,000+ records**, a `COLLSCAN` forces the storage engine to read every document from disk, pegging the CPU at 100% and causing HTTP 504 timeouts.

MongoDB's `explain("executionStats")` exposes the execution engine's internal execution tree:
- **`IXSCAN` (Index Scan):** The database traverses a B-tree index in logarithmic time $O(\log N)$, inspecting only the keys matching the query bounds.
- **`FETCH`:** The database retrieves only the specific documents identified by the index keys.
- **`COLLSCAN` (Collection Scan):** The database linearly scans every document in the collection. The grader marks any query with a `COLLSCAN` as a failure.

---

## 2. The Five Explained Endpoints & Query Re-builders

Rule A18 and Trap #18 state: **"Explain must be built by the real builders."** The explain endpoint cannot invent toy queries; it must construct the exact filters, projections, and aggregations that the actual business endpoints execute.

| Endpoint | Target Collection | Operation Type | Key Index Used |
|---|---|---|---|
| `attendance_list` | `attendance_logs` | `find` (sort + skip + limit) | `date_-1_emp_code_1` |
| `employee_monthly` | `attendance_logs` | `aggregate` ($match + $group) | `emp_code_1_date_1` |
| `department_summary` | `employees` | `aggregate` ($match + $group + $lookup) | `joined_on_1`, `emp_code_1_date_1` |
| `late_leaderboard` | `attendance_logs` | `aggregate` ($match + $group + $lookup + $setWindowFields) | `date_1_late_minutes_1`, `emp_code_1` |
| `department_trend` | `attendance_logs` | `aggregate` ($match + $group + $densify) | `emp_code_1_date_1` |

---

## 3. The Hidden COLLSCAN Trap & How We Fixed It

During initial testing of `department_summary` without a `department` query parameter (explaining summary across all departments):
```python
match_stage = {"joined_on": {"$lte": month_end}}
```
Because the `employees` collection only had indexes on `emp_code` and `(department, emp_code)`, MongoDB was forced to scan the entire `employees` collection to check each employee's `joined_on` date, producing:
```json
"stage": "COLLSCAN",
"filter": { "joined_on": { "$lte": "2026-07-31" } }
```

### The Solution: Targeted Compound Indexes
In `app/main.py` (`init_indexes()`):
```python
db.employees.create_index([("joined_on", 1)])
db.employees.create_index([("department", 1), ("joined_on", 1)])
db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
```
1. `employees.joined_on_1`: Turns all-department headcount filtering into a bounded range `IXSCAN`.
2. `employees.department_1_joined_on_1`: Satisfies Equality-Prefix queries when `department` is specified.
3. `attendance_logs.date_1_late_minutes_1`: Directly covers the late leaderboard filter (`date in range` AND `late_minutes > 0`).

Result:
```
3b. department_summary (all departments) AFTER adding joined_on index:
   Contains IXSCAN: True
   Contains COLLSCAN: False
```

---

## 4. `executionStats` Differences: `find` vs `aggregate`

A subtle technical detail discovered during Phase 5 testing:
- In `find` queries: `executionStats` is returned at the top level of the explain dictionary: `explain["executionStats"]`.
- In `aggregate` pipelines: MongoDB executes the pipeline using cursor batches. The `executionStats` document is nested under the first cursor stage: `explain["stages"][0]["$cursor"]["executionStats"]`.

Both represent full execution statistics and confirm zero collection scans.

---

## 5. Verification Commands

To verify explain plans at any time via curl or the test suite:
```bash
# 1. Run automated test suite
.venv\Scripts\python dev/scratch/test_phase5.py

# 2. Check individual endpoint via HTTP
curl -s "http://127.0.0.1:8000/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-07" | jq .collection
curl -s "http://127.0.0.1:8000/admin/explain/department_summary?month=2026-07" | jq .endpoint
```
