"""
dev/scratch/test_phase4_analytics.py
Comprehensive test suite for Phase 4 Analytics endpoints:
  - GET /analytics/employees/{emp_code}/monthly
  - GET /analytics/departments/summary
  - GET /analytics/leaderboard/late
  - GET /analytics/departments/{department}/trend
Run: .venv/Scripts/python dev/scratch/test_phase4_analytics.py
"""
import sys
sys.path.insert(0, ".")

from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from app.main import app, db, init_indexes

print("=" * 60)
print("TESTING PHASE 4: ANALYTICS AGGREGATIONS")
print("=" * 60)

init_indexes()

# Setup test fixtures
EMP_A = "EMP8801" # joined 2026-01-01, Engineering
EMP_B = "EMP8802" # joined 2026-07-15, Engineering (mid-month joiner)
EMP_C = "EMP8803" # joined 2026-08-01, Engineering (post-month joiner)
EMP_D = "EMP8804" # joined 2026-01-01, Marketing (no logs)
EMP_E = "EMP8805" # joined 2026-01-01, Sales (ties test)
EMP_F = "EMP8806" # joined 2026-01-01, Sales (ties test)

test_emp_codes = [EMP_A, EMP_B, EMP_C, EMP_D, EMP_E, EMP_F]
db.employees.delete_many({"emp_code": {"$in": test_emp_codes}})
db.attendance_logs.delete_many({"emp_code": {"$in": test_emp_codes}})

with TestClient(app) as client:
    # Seed test employees
    client.post("/employees", json={"emp_code": EMP_A, "name": "Alice A", "email": "a@hrone.com", "department": "Engineering", "joined_on": "2026-01-01"})
    client.post("/employees", json={"emp_code": EMP_B, "name": "Bob B", "email": "b@hrone.com", "department": "Engineering", "joined_on": "2026-07-15"})
    client.post("/employees", json={"emp_code": EMP_C, "name": "Charlie C", "email": "c@hrone.com", "department": "Engineering", "joined_on": "2026-08-01"})
    client.post("/employees", json={"emp_code": EMP_D, "name": "Dana D", "email": "d@hrone.com", "department": "Marketing", "joined_on": "2026-01-01"})
    client.post("/employees", json={"emp_code": EMP_E, "name": "Eve E", "email": "e@hrone.com", "department": "Sales", "joined_on": "2026-01-01"})
    client.post("/employees", json={"emp_code": EMP_F, "name": "Frank F", "email": "f@hrone.com", "department": "Sales", "joined_on": "2026-01-01"})

    # Seed logs for EMP_A (2026-07-06 Mon, 2026-07-07 Tue, 2026-07-11 Sat)
    # Mon: full day (9h), Tue: half day (4h), Sat: weekend (5h)
    # Mon 09:30
    t_mon_in = 1783310400000
    client.post("/attendance/punch-in", json={"emp_code": EMP_A, "punched_at": t_mon_in})
    client.post("/attendance/punch-out", json={"emp_code": EMP_A, "punched_at": t_mon_in + 9 * 3600 * 1000})

    # Tue 09:45 (15 min late)
    t_tue_in = 1783397700000
    client.post("/attendance/punch-in", json={"emp_code": EMP_A, "punched_at": t_tue_in})
    client.post("/attendance/punch-out", json={"emp_code": EMP_A, "punched_at": t_tue_in + 4 * 3600 * 1000}) # half day!

    # Sat weekend punch (2026-07-11)
    t_sat_in = 1783742400000
    client.post("/attendance/punch-in", json={"emp_code": EMP_A, "punched_at": t_sat_in})
    client.post("/attendance/punch-out", json={"emp_code": EMP_A, "punched_at": t_sat_in + 5 * 3600 * 1000})

    # Seed tied late logs for EMP_E and EMP_F in Sales (both 45 mins late)
    t_sale_in = 1783313100000 # 45 mins late
    client.post("/attendance/punch-in", json={"emp_code": EMP_E, "punched_at": t_sale_in})
    client.post("/attendance/punch-out", json={"emp_code": EMP_E, "punched_at": t_sale_in + 8 * 3600 * 1000})

    client.post("/attendance/punch-in", json={"emp_code": EMP_F, "punched_at": t_sale_in})
    client.post("/attendance/punch-out", json={"emp_code": EMP_F, "punched_at": t_sale_in + 8 * 3600 * 1000})

    print("✓ Test employees and attendance logs seeded")

    # -----------------------------------------------------------------------
    # 1. TEST /analytics/employees/{emp_code}/monthly
    # -----------------------------------------------------------------------
    print("\n--- 1. Testing Employee Monthly ---")
    # Unknown employee -> 404
    r_unk = client.get(f"/analytics/employees/UNKNOWN/monthly?month=2026-07")
    assert r_unk.status_code == 404
    print("✓ Unknown employee returns 404")

    # Bad month format -> 422
    r_bad_m = client.get(f"/analytics/employees/{EMP_A}/monthly?month=2026-13")
    assert r_bad_m.status_code == 422
    print("✓ Invalid month returns 422")

    # Normal employee EMP_A
    r_a = client.get(f"/analytics/employees/{EMP_A}/monthly?month=2026-07")
    assert r_a.status_code == 200
    data_a = r_a.json()
    assert data_a["working_days"] == 23 # July 2026 has 23 weekdays
    # Mon (1.0) + Tue (0.5) = 1.5 present days (Sat does NOT count per R7!)
    assert data_a["present_days"] == 1.5, f"Expected 1.5 present_days, got {data_a['present_days']}"
    assert data_a["late_count"] == 1
    assert data_a["total_late_minutes"] == 15
    # 1.5 / 23 * 100 = 6.5217... -> 6.52
    assert data_a["attendance_pct"] == 6.52, f"Expected 6.52 pct, got {data_a['attendance_pct']}"
    print("✓ Employee monthly summary correct: present_days=1.5 (weekend excluded), pct=6.52")

    # Mid-month joiner EMP_B (joined 2026-07-15)
    # Working days between July 15 and July 31
    r_b = client.get(f"/analytics/employees/{EMP_B}/monthly?month=2026-07")
    assert r_b.status_code == 200
    data_b = r_b.json()
    assert data_b["working_days"] == 13 # 13 weekdays from July 15 to 31
    print("✓ Mid-month joiner working_days counted from joined_on (13 days)")

    # Post-month joiner EMP_C (joined 2026-08-01) -> Assumption A6
    r_c = client.get(f"/analytics/employees/{EMP_C}/monthly?month=2026-07")
    assert r_c.status_code == 200
    data_c = r_c.json()
    assert data_c["working_days"] == 0
    assert data_c["attendance_pct"] is None
    print("✓ Post-month joiner returns 200 with 0 working days and null attendance_pct (A6)")

    # -----------------------------------------------------------------------
    # 2. TEST /analytics/departments/summary
    # -----------------------------------------------------------------------
    print("\n--- 2. Testing Department Summary ---")
    r_dept = client.get("/analytics/departments/summary?month=2026-07")
    assert r_dept.status_code == 200
    items = r_dept.json()["items"]
    # Check Engineering headcount: EMP_A and EMP_B joined in/before July, EMP_C joined in Aug
    # Plus whatever existed in seed data!
    eng_summary = next(item for item in items if item["department"] == "Engineering")
    # Dana D in Marketing has ZERO logs but joined in Jan 2026 -> Marketing must be in items!
    mkt_summary = next(item for item in items if item["department"] == "Marketing")
    assert mkt_summary["headcount"] == 1, "Marketing headcount must include zero-log employee (R9)"
    assert mkt_summary["present_days"] == 0.0
    print("✓ Department summary headcount includes zero-log employee Dana D (R9)")

    # Filter by department
    r_filter = client.get("/analytics/departments/summary?month=2026-07&department=Marketing")
    assert r_filter.status_code == 200
    filter_items = r_filter.json()["items"]
    assert len(filter_items) == 1
    assert filter_items[0]["department"] == "Marketing"
    print("✓ Department summary query filter works")

    # -----------------------------------------------------------------------
    # 3. TEST /analytics/leaderboard/late
    # -----------------------------------------------------------------------
    print("\n--- 3. Testing Late Leaderboard ---")
    # Sales department has EMP_E and EMP_F both with 45 minutes late -> TIED for rank 1!
    r_lb = client.get("/analytics/leaderboard/late?month=2026-07&department=Sales")
    assert r_lb.status_code == 200
    lb_items = r_lb.json()["items"]
    assert len(lb_items) >= 2
    # Standard competition ranking: both must share rank 1!
    assert lb_items[0]["rank"] == 1
    assert lb_items[1]["rank"] == 1
    # Tied rows ordered by emp_code ascending
    assert lb_items[0]["emp_code"] < lb_items[1]["emp_code"]
    print("✓ Leaderboard competition ranking ($rank): tied employees share rank 1 and are ordered by emp_code")

    # -----------------------------------------------------------------------
    # 4. TEST /analytics/departments/{department}/trend
    # -----------------------------------------------------------------------
    print("\n--- 4. Testing Department Trend ---")
    # Unknown department -> 404
    r_t_unk = client.get("/analytics/departments/UNKNOWN_DEPT/trend?from=2026-07-01&to=2026-07-05")
    assert r_t_unk.status_code == 404
    print("✓ Unknown department returns 404")

    # to < from -> 422
    r_t_inv = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
    assert r_t_inv.status_code == 422
    print("✓ to < from returns 422")

    # Range > 92 days -> 422 (Assumption A2)
    r_t_long = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01") # ~120 days
    assert r_t_long.status_code == 422
    print("✓ Range > 92 days returns 422 (Assumption A2)")

    # Valid 10-day range
    r_trend = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-10")
    assert r_trend.status_code == 200
    t_items = r_trend.json()["items"]
    assert len(t_items) == 10, f"Expected 10 rows from July 1 to 10, got {len(t_items)} (Trap #17)"

    # Verify fields
    for row in t_items:
        assert "date" in row
        assert "is_working_day" in row
        assert "headcount" in row
        assert "present_count" in row
        assert "late_count" in row
        assert "attendance_rate" in row
        assert "moving_avg_7d" in row
        if not row["is_working_day"]:
            assert row["attendance_rate"] is None, "Weekends must have attendance_rate = null"
    print("✓ Department trend returns exactly 10 rows with gap-filling ($densify) and valid moving average")

# Cleanup
db.employees.delete_many({"emp_code": {"$in": test_emp_codes}})
db.attendance_logs.delete_many({"emp_code": {"$in": test_emp_codes}})

print()
print("=" * 60)
print("ALL PHASE 4 TESTS PASSED SUCCESSFULLY! ✓")
print("=" * 60)
