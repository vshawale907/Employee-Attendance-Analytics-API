# Employee Attendance & Analytics API

A high-performance, concurrency-safe REST API built with **FastAPI** and **MongoDB (PyMongo sync)** for employee lifecycle management, attendance tracking, administrative regularization, and deep aggregation analytics.

Designed strictly against the OpenAPI 3.1 contract, adhering to financial-grade rounding rules, atomic concurrency boundaries, and optimized index execution plans.

---

## Architecture Highlights

- **Single-File Delivery (`app/main.py`):** The entire application codebase is organized in `app/main.py` for direct, self-contained deployment with `uvicorn app.main:app --port 8000`.
- **Sync PyMongo Driver:** Built using the stable, synchronous `pymongo` driver (anticipating Motor deprecation in May 2025), running inside FastAPI threadpool workers.
- **Pure MongoDB Aggregations:** Analytics computations (monthly summary, department metrics, leaderboard rankings, daily trends) execute entirely inside MongoDB using `$match`, `$group`, `$lookup`, `$densify`, and `$setWindowFields` ($rank). Zero in-memory fetch-and-loop.
- **Financial-Grade Rounding:** Implements deterministic `Decimal` arithmetic with `ROUND_HALF_UP` (Rule R8), avoiding IEEE 754 floating-point drift and Python's half-even banker's rounding.
- **Storage-Level Concurrency Safety:** Unique compound indexes (`(emp_code, date)`) and atomic `find_one_and_update` guards eliminate race conditions on simultaneous punch-ins, punch-outs, and PATCH regularizations.
- **Index-Backed Performance (`IXSCAN`):** Compound indexes idempotently initialized at startup ensure all 12 endpoints execute via index scans (`IXSCAN`) with zero full collection scans (`COLLSCAN`) on 100k+ records.

---

## Quick Start

### 1. Prerequisites
- **Python:** 3.11+
- **MongoDB:** 6.0+ (Tested against MongoDB 7.0 / 8.0)

### 2. Environment Setup
```bash
# Clone the repository
git clone https://github.com/vshawale907/Employee-Attendance-Analytics-API.git
cd Employee-Attendance-Analytics-API

# Create and activate virtual environment
python -m venv .venv
# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
# On Linux / macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Database Configuration
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Default configuration:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
*(Real environment variables take precedence over `.env`)*

### 4. Seed Database & Start Server
```bash
# Seed initial data (optional sample seed)
python sample_seed.py

# Launch the FastAPI application
uvicorn app.main:app --port 8000 --reload
```
The interactive API documentation is available at `http://localhost:8000/docs`.

---

## API Reference

### System & Health
| Method | Path | Summary | Success Status |
|---|---|---|---|
| `GET` | `/health` | Health check verifying MongoDB ping | `200` |

### Employees
| Method | Path | Summary | Success Status |
|---|---|---|---|
| `POST` | `/employees` | Register new employee | `201` |
| `GET` | `/employees` | List employees with pagination and department filter | `200` |

### Attendance & Lifecycle
| Method | Path | Summary | Success Status |
|---|---|---|---|
| `POST` | `/attendance/punch-in` | Punch-in recording (calculates R1 date and R2 late minutes) | `201` |
| `POST` | `/attendance/punch-out` | Punch-out recording (calculates R3/R4 work hours and overtime) | `200` |
| `GET` | `/attendance` | List attendance records with date filters and pagination | `200` |
| `PATCH` | `/attendance/{emp_code}/{date}` | Administrative regularization with audit history tracking | `200` |

### Analytics & Aggregation
| Method | Path | Summary | Success Status |
|---|---|---|---|
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee attendance summary and percentages | `200` |
| `GET` | `/analytics/departments/summary` | Department-level monthly summary and active headcount | `200` |
| `GET` | `/analytics/leaderboard/late` | Late attendance leaderboard using competition ranking (`$rank`) | `200` |
| `GET` | `/analytics/departments/{department}/trend` | Continuous calendar trend series with `$densify` & 7-day MA | `200` |

### Admin & Performance Audit
| Method | Path | Summary | Success Status |
|---|---|---|---|
| `GET` | `/admin/explain/{endpoint}` | Explain plan with `executionStats` verifying `IXSCAN` usage | `200` |

---

## Business Rules Implemented

- **R1 (Shifts & Calendar Dates):** Shift times and calendar dates are evaluated in Indian Standard Time (IST, UTC+05:30). Overnight shifts (`shift_end <= shift_start`) assign punch-ins before `shift_end` to the previous calendar day.
- **R2 (Late Arrival):** 10-minute grace period. Punching in $\le 10$ minutes late incurs 0 late minutes. Punching in $> 10$ minutes late records the full delay from `shift_start`.
- **R3 (Overtime):** 30-minute minimum threshold. Working $< 30$ minutes beyond `shift_end` yields 0 overtime; $\ge 30$ minutes counts the full excess duration.
- **R4 (Work Hours):** Whole-second truncation prior to duration calculation. Hours computed as $(\text{punch\_out} - \text{punch\_in}) / 3600$, rounded half-up to 2 decimals.
- **R5 (Half Day):** Work hours $< 4.5$ hours automatically marks `half_day = true`.
- **R6 (Leaderboard Ranking):** Standard competition ranking via `$rank` in MongoDB `$setWindowFields`. Ties share rank (1, 1, 3); limits apply after ranking.
- **R7 (Working Days):** Monday through Friday are working days. Weekends count toward late/overtime totals but do not add to `present_days`.
- **R8 (Financial Rounding):** `Decimal` with `ROUND_HALF_UP` for all percentages (2 decimals) and rates (4 decimals).
- **R9 (Headcount Calculation):** Anchored on `employees` matching `joined_on <= period_end` so employees with zero attendance logs are accurately counted.
- **R10 (Pagination):** 1-based page numbering, configurable `page_size`, and stable compound tie-breaking sorts.

---

## Testing & Verification

The test harness runs against an isolated test database (`attendance_test`), leaving production and seed data untouched:

```bash
# Run the entire test suite
pytest tests -v
```

### Test Suite Structure (`tests/`)
- `tests/test_health.py`: MongoDB ping and health status.
- `tests/test_employees.py`: Validation, unique constraints, and pagination offsets.
- `tests/test_punch.py`: Late calculations, overnight shifts, duration validation, and concurrent punch race safety.
- `tests/test_regularize.py`: Audit history entries, punch clearance on ABSENT, half-day recalculation, and optimistic lock protection.
- `tests/test_attendance_list.py`: Filtering, stable sort, and date range checks.
- `tests/test_analytics.py`: Monthly employee rollups, mid-month joiners, department summaries, leaderboard ranking, and continuous trend series.
- `tests/test_explain.py`: Validates `executionStats`, `IXSCAN` presence, and `COLLSCAN` absence across all 5 explained queries.

---

## Project Documentation

- **[DECISIONS.md](DECISIONS.md):** Architectural decisions covering index selections, punch race handling, tie breakers, headcount logic, and 100x scalability.
- **[REVIEW.md](REVIEW.md):** Catalog of all starter defects identified and resolved during initial review.
- **[learning/](learning/):** In-depth educational guides explaining system concepts, CRUD lifecycles, aggregations, query plans, and test architectures.
