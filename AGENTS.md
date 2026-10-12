# AGENTS.md — Rules Digest for Context Resets

> Re-read this file at the start of every phase. It is a compressed version of the mission rules
> that survive a context window reset. The full spec is in the original prompt.

---

## Working Rules (Section 2 of prompt)

1. **Phase-gated**: build → stop → post evidence → wait for `continue` (or `AUTO` to run all remaining).
2. **Teaching format** for every non-trivial function: WHAT / WHERE / WHY (cite rule) / HOW TO TEST / WHAT BREAKS.
3. **Evidence over claims**: never write "works" unless you ran it and show output.
4. **Don't trust memory for MongoDB behaviour**: test in `dev/scratch/` before relying on operators.
5. **Decisions**: default to Assumption Register; record new ones in `dev/PROGRESS.md`.
6. **Git**: commit at end of every phase with clear message.
7. **Safety**: only write to `attendance_db` (dev) or `attendance_test` (generated). Never drop other databases.
8. **Chat replies**: compact (<600 words). Long explanations go in `learning/` files.
9. **Continuity**: re-read `AGENTS.md` and `dev/PROGRESS.md` at start of every phase.

---

## Hard Constraints (Section 3 of prompt)

- Python 3.11+, FastAPI, **sync PyMongo** (Motor deprecated May 2025).
- **ALL app code in ONE file**: `app/main.py`.
- Starts with `uvicorn app.main:app --port 8000`; `/health` 200 within 20s of start.
- `MONGO_URI` + `MONGO_DB` from env vars; no hard-coded defaults; real env wins over `.env`.
- All indexes created at startup, idempotently.
- No network except MongoDB. No Dockerfile. No secrets. No `.env` committed.
- Analytics = MongoDB aggregation pipelines. No fetch-and-loop in Python.
- `_id` never accepted or returned. Instants = epoch milliseconds (int) in API, BSON datetime in MongoDB.
- 500 = failed test. Use 404/409/422/503 as the contract says.

---

## The Cross-Cutting Trap List (Section 5.13)

1. Naive datetimes / server-local time — bug hides in IST, shows in UTC.
2. Python `round()` / MongoDB `$round` are half-even. **Banned** for reported numbers.
3. Float averages for rates/windows — use scaled integers or Decimal.
4. `date.fromisoformat` too lenient — also require regex `^\d{4}-\d{2}-\d{2}$`.
5. Check-then-insert races — use unique indexes.
6. Read-modify-write without guarding on what you read — lost updates.
7. Legacy docs missing fields — `$ifNull` / `.get(default)`.
8. Leaking `_id` / `id`.
9. Pydantic lax coercion of strings/floats/booleans into ints.
10. Huge `page` → overflow → 500.
11. Sorting without a unique tie-breaker → unstable pages.
12. `$group` output is unordered — sort afterwards.
13. After `$unwind` employee counted once per log — compute per-employee partials first.
14. Array index as rank; `limit` applied before ranking.
15. "No record" ≠ "ABSENT".
16. Mid-month joiners: working days start at `joined_on`; headcount follows R9.
17. Trend must return exactly `(to - from + 1)` rows.
18. Explain must be built by the real builders.
19. Stored derived values are authoritative; analytics never recompute from punch times.
20. Weekend records count toward late/overtime/leave/on-duty totals but NOT `present_days`; `avg_work_hours` includes weekend present records.

---

## Assumption Register (Section 7 defaults)

| ID | Assumption |
|----|-----------|
| A1 | Moving average uses **reported, 4-decimal-rounded** `attendance_rate` values. |
| A2 | Range limit: reject when inclusive day count > 92. |
| A3 | Records dated before `joined_on` counted literally; `working_days` starts at `joined_on`. |
| A4 | Punch-out check order: 404 → 409 → 422. |
| A5 | Punch-in on a date with any existing record (even seeded ABSENT/LEAVE) → 409. |
| A6 | Employee joined after requested month → 200 with zeros and null percentage. |
| A7 | PATCH to ABSENT/LEAVE when status inherited and punch time supplied → 422. |
| A8 | Empty-string `department` query → treat as exact filter on empty string. |
| A9 | 422 evaluated before 404. |
| A10 | Overnight rule applies only when `shift_end <= shift_start`. |

---

## Precedence

`openapi.yaml` > `DATA_MODEL.md` > `PROBLEM_STATEMENT.docx` > prompt body
