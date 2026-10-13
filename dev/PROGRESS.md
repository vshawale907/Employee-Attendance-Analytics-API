# dev/PROGRESS.md — Phase Status + Assumption Register

Re-read this file at the start of every phase alongside AGENTS.md.

---

## Phase Status

| Phase | Status | Notes |
|-------|--------|-------|
| 0 — Setup | ✅ DONE | Environment, Mongo 8.0, 15 docs |
| 1 — Understanding report | ✅ DONE | learning/00_UNDERSTANDING.md |
| 2 — Part A review + fix | ✅ DONE | REVIEW.md, app/main.py, learning/01_STARTER_REVIEW_AND_FIXES.md |
| 3 — Part B1 foundation + CRUD | ✅ DONE | punch-out, regularize (PATCH), learning/02_CRUD_AND_PUNCH_LIFECYCLE.md |
| 4 — Part B2 analytics | ✅ DONE | 4 aggregation pipelines, $rank, $densify, learning/03_ANALYTICS_AND_AGGREGATIONS.md |
| 5 — Part C explain + DECISIONS.md | ✅ DONE | /admin/explain/{endpoint}, index tuning, DECISIONS.md, learning/04_EXPLAIN_AND_INDEXES.md |
| 6 — Verification harness | ✅ DONE | 22 pytest tests in tests/ covering all 12 endpoints, rules, traps, and concurrency |
| 7 — Learning pack + README + final audit | ⬜ TODO | |

---

## Assumption Register

| ID | Assumption | Source | Status |
|----|-----------|--------|--------|
| A1 | Moving average input = reported (4-dp-rounded) attendance_rate values | Prompt §7 | ACTIVE |
| A2 | Range limit: reject inclusive day count > 92 | Prompt §7 | ACTIVE |
| A3 | Records before joined_on counted literally; working_days starts at joined_on | Prompt §7 | ACTIVE |
| A4 | Punch-out check order: 404 → 409 → 422 | Prompt §7 | ACTIVE |
| A5 | Punch-in on date with any existing record → 409 | Prompt §7 | ACTIVE |
| A6 | Employee joined after requested month → 200 zeros + null pct | Prompt §7 | ACTIVE |
| A7 | PATCH to ABSENT/LEAVE with punch time supplied → 422 | Prompt §7 | ACTIVE |
| A8 | Empty-string department → exact filter on "" | Prompt §7 | ACTIVE |
| A9 | 422 evaluated before 404 | Prompt §7 | ACTIVE |
| A10 | Overnight rule only when shift_end <= shift_start | Prompt §7 | ACTIVE |
| A11 | PATCH with identical final state → 422 "nothing changed" | Phase 1 analysis | ACTIVE |
| A12 | Trend `present_count` includes half-days as 0.5, weekends included | openapi.yaml | ACTIVE |
| A13 | `avg_work_hours` averages ALL present-status records incl. weekends | §5.9 note 20 | ACTIVE |

---

## Open Questions / Contradictions Found

1. `openapi.yaml` says `EpochMillis` minimum is `100000000000` (13 digits = seconds-range guard). The constraint is 
   `ge=100_000_000_000` which correctly rejects seconds-looking integers. ✅ Consistent.

2. `attendance_logs` `punch_in` schema is `EpochMillisNullable` — so null is allowed (ABSENT/LEAVE). ✅ Consistent with DATA_MODEL.

3. `RegularizeRequest` requires BOTH `reason` AND `regularized_by` (both in `required` list in openapi.yaml line 532-533). 
   This means a PATCH without either is 422. ✅ Clear.

4. `DepartmentSummary` response: `present_days` = Mon-Fri only, but `avg_work_hours` includes weekend present records.
   This comes from rule R7 + note in §5.9. **Potential confusion point** — document carefully.

5. `department_trend` says "more than 92 calendar days inclusive → 422". That means day_count > 92, where 
   day_count = (to - from).days + 1. Exactly 92 is allowed. ✅ (A2)

6. The `$densify` upper bound is **exclusive** per MongoDB docs. So bound = [from_dt, to_dt + 1 day). 
   Test this explicitly before shipping.

---

## Key File Locations

| File | Purpose |
|------|---------|
| `app/main.py` | ALL app code (graded) |
| `REVIEW.md` | Part A defect list (graded) |
| `DECISIONS.md` | Part C 5 questions (graded) |
| `README.md` | Short, honest overview |
| `AGENTS.md` | Rules digest (this project's memory) |
| `dev/PROGRESS.md` | This file — phase status |
| `dev/requirements-dev.txt` | Test tools (not in root requirements.txt) |
| `dev/scratch/` | One-off MongoDB test scripts |
| `learning/` | Teaching files for student |

---

## Environment

- Python: 3.11.3
- MongoDB: 7.x (Docker container `hrone-mongo`, port 27017)
- MONGO_URI: mongodb://localhost:27017
- MONGO_DB (dev): attendance_db
- MONGO_DB (test): attendance_test
