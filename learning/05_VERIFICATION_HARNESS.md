# 05_VERIFICATION_HARNESS.md — Automated Verification & Testing Strategy

> Phase 6 teaching document. Explains the testing harness, test fixtures, concurrency race simulations, and business rule coverage across all 12 API endpoints.

---

## 1. Testing Philosophy for Database-Centric APIs

In a high-throughput attendance and analytics system, unit tests with mocked databases hide critical bugs:
- They miss index boundary violations (such as `COLLSCAN` vs `IXSCAN`).
- They hide database-level unique constraint violations and race conditions.
- They cannot verify aggregation pipeline operators like `$densify`, `$setWindowFields`, or `$lookup`.

Therefore, our verification harness tests against **real, isolated MongoDB storage** (`attendance_test`) using `FastAPI`'s `TestClient` (Starlette / HTTPX), ensuring that every constraint, pipeline, and transaction behaves identically to production.

---

## 2. Test Suite Structure (`tests/`)

The test suite is organized into focused domain modules:

| Test File | Target Scope | Key Assertions & Scenarios |
|---|---|---|
| `conftest.py` | Fixtures & Config | Sets `MONGO_DB=attendance_test`, provides `test_client`, manages DB cleanup. |
| `test_health.py` | System Readiness | Verifies `GET /health` returns 200 `status: ok` when MongoDB answers. |
| `test_employees.py` | Employee Management | 201 creation, 409 unique conflict, 422 regex validation, pagination offset/limit. |
| `test_punch.py` | Punch Clock Lifecycle | On-time vs late (R2 grace period), overnight shifts (R1), duplicate punches (409), concurrent punch-in/out races. |
| `test_regularize.py` | Regularization & Auditing | Status updates, clearing punch times on ABSENT, recalculating work hours (R5), audit history entries, 422 guardrails. |
| `test_attendance_list.py` | Query & Pagination | Date range filters, status filters, date descending sort, `date_from > date_to` (422). |
| `test_analytics.py` | Aggregations (R6–R9) | Working days calculation, mid-month joiner prorating, weekend exclusion, `$rank` ties, `$densify` continuity, 7-day moving averages. |
| `test_explain.py` | Performance Audit | Verifies all 5 endpoints return `executionStats`, use `IXSCAN`, and contain 0 `COLLSCAN`. |

---

## 3. Concurrency Race Testing

Two critical concurrency scenarios were stressed using `concurrent.futures.ThreadPoolExecutor(max_workers=5)`:

### Scenario A: Simultaneous Punch-Ins
When two identical punch-in requests arrive at the same millisecond:
1. Both attempt insertion into `attendance_logs`.
2. The compound unique index `("emp_code", 1), ("date", 1)` admits exactly one write.
3. The second request raises `DuplicateKeyError` (code 11000).
4. Our endpoint catches it and returns HTTP 409 Conflict.
5. **Test Assertion:** Exactly one HTTP 201, four HTTP 409s.

### Scenario B: Simultaneous Punch-Outs
When two punch-out requests hit an open record:
1. Both attempt atomic `find_one_and_update` filtering on `{"punch_out": None}`.
2. Exactly one query matches and updates the document.
3. The second query finds no matching open record and returns HTTP 409 Conflict.
4. **Test Assertion:** Exactly one HTTP 200, four HTTP 409s.

---

## 4. How to Run the Verification Suite

```bash
# Run entire test suite with verbose output
.venv\Scripts\python -m pytest tests -v

# Run a specific domain module
.venv\Scripts\python -m pytest tests/test_punch.py -v
.venv\Scripts\python -m pytest tests/test_analytics.py -v
```
All 22 test cases pass in ~2 seconds.
