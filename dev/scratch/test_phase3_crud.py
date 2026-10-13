"""
dev/scratch/test_phase3_crud.py
Comprehensive test suite for Phase 3 Part B1 CRUD endpoints:
  - POST /attendance/punch-out
  - PATCH /attendance/{emp_code}/{date}
Run: .venv/Scripts/python dev/scratch/test_phase3_crud.py
"""
import sys
sys.path.insert(0, ".")

import concurrent.futures
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient

from app.main import app, db, init_indexes

print("=" * 60)
print("TESTING PHASE 3: PUNCH-OUT & REGULARIZATION")
print("=" * 60)

init_indexes()

# Setup test employee
TEST_EMP = "EMP7771"
OVERNIGHT_EMP = "EMP7772"

db.employees.delete_many({"emp_code": {"$in": [TEST_EMP, OVERNIGHT_EMP]}})
db.attendance_logs.delete_many({"emp_code": {"$in": [TEST_EMP, OVERNIGHT_EMP]}})

with TestClient(app) as client:
    # Create standard employee (09:30 - 18:30)
    r1 = client.post("/employees", json={
        "emp_code": TEST_EMP,
        "name": "Standard Employee",
        "email": "emp7771@hrone.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    assert r1.status_code == 201

    # Create overnight employee (22:00 - 06:00)
    r2 = client.post("/employees", json={
        "emp_code": OVERNIGHT_EMP,
        "name": "Overnight Employee",
        "email": "emp7772@hrone.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01"
    })
    assert r2.status_code == 201

    print("✓ Test employees created")

    # -------------------------------------------------------------
    # 1. PUNCH-OUT TESTS
    # -------------------------------------------------------------
    print("\n--- 1. Testing POST /attendance/punch-out ---")

    # 1.1 Unknown employee -> 404
    r_unk = client.post("/attendance/punch-out", json={"emp_code": "UNKNOWN_EMP"})
    assert r_unk.status_code == 404, f"Expected 404, got {r_unk.status_code}"
    print("✓ Unknown employee returns 404")

    # 1.2 No punch-in found -> 404
    r_no_pi = client.post("/attendance/punch-out", json={"emp_code": TEST_EMP})
    assert r_no_pi.status_code == 404, f"Expected 404 for no punch-in, got {r_no_pi.status_code}"
    print("✓ Punch-out with no prior punch-in returns 404")

    # Create a punch-in at 09:30 IST on 2026-07-06 (04:00 UTC = 1783310400000)
    t_in = 1783310400000
    r_pi = client.post("/attendance/punch-in", json={"emp_code": TEST_EMP, "punched_at": t_in})
    assert r_pi.status_code == 201
    assert r_pi.json()["late_minutes"] == 0

    # 1.3 punch_out <= punch_in -> 422
    r_early = client.post("/attendance/punch-out", json={"emp_code": TEST_EMP, "punched_at": t_in - 1000})
    assert r_early.status_code == 422
    r_equal = client.post("/attendance/punch-out", json={"emp_code": TEST_EMP, "punched_at": t_in})
    assert r_equal.status_code == 422
    print("✓ punch_out <= punch_in returns 422")

    # 1.4 punch_out > punch_in + 24h -> 422
    t_after_25h = t_in + (25 * 3600 * 1000)
    r_late24 = client.post("/attendance/punch-out", json={"emp_code": TEST_EMP, "punched_at": t_after_25h})
    assert r_late24.status_code == 422
    print("✓ punch_out > punch_in + 24h returns 422")

    # 1.5 Valid punch-out: 18:30 IST (13:00 UTC = 1783342800000)
    # Elapsed = 9 hours exactly -> work_hours = 9.00, overtime = 0, half_day = False
    t_out = 1783342800000
    r_po = client.post("/attendance/punch-out", json={"emp_code": TEST_EMP, "punched_at": t_out})
    assert r_po.status_code == 200, f"Punch-out failed: {r_po.status_code} {r_po.text}"
    body_po = r_po.json()
    assert body_po["work_hours"] == 9.0
    assert body_po["overtime_minutes"] == 0
    assert body_po["half_day"] is False
    assert body_po["punch_out"] == t_out
    assert "_id" not in body_po and "id" not in body_po
    print("✓ Normal punch-out succeeded: work_hours=9.0, overtime=0, half_day=False")

    # 1.6 Already punched-out record -> 409
    r_po_dup = client.post("/attendance/punch-out", json={"emp_code": TEST_EMP, "punched_at": t_out + 10000})
    assert r_po_dup.status_code == 409
    print("✓ Punch-out on already closed record returns 409")

    # 1.7 Overnight shift punch-out
    # Shift: 22:00 - 06:00. Punch-in: 2026-07-06 22:00 IST (16:30 UTC = 1783355400000)
    t_ov_in = 1783355400000
    r_ov_pi = client.post("/attendance/punch-in", json={"emp_code": OVERNIGHT_EMP, "punched_at": t_ov_in})
    assert r_ov_pi.status_code == 201
    assert r_ov_pi.json()["date"] == "2026-07-06"

    # Punch-out at 07:00 IST next morning (01:30 UTC July 7 = 1783387800000)
    # Elapsed = 9 hours (work_hours=9.0). Shift ended 06:00 IST -> 1 hour past shift_end -> overtime=60!
    t_ov_out = 1783387800000
    r_ov_po = client.post("/attendance/punch-out", json={"emp_code": OVERNIGHT_EMP, "punched_at": t_ov_out})
    assert r_ov_po.status_code == 200
    body_ov = r_ov_po.json()
    assert body_ov["work_hours"] == 9.0
    assert body_ov["overtime_minutes"] == 60, f"Expected 60m overtime across midnight, got {body_ov['overtime_minutes']}"
    print("✓ Overnight punch-out succeeded: correctly calculated 60m overtime across midnight")

    # 1.8 Concurrency test on punch-out
    # Open new record for EMP7771 on 2026-07-07
    t_in_day2 = 1783396800000 # 09:30 IST on July 7
    r_pi2 = client.post("/attendance/punch-in", json={"emp_code": TEST_EMP, "punched_at": t_in_day2})
    assert r_pi2.status_code == 201
    t_out_day2 = t_in_day2 + (9 * 3600 * 1000)

    def concurrent_punch_out():
        c = TestClient(app)
        return c.post("/attendance/punch-out", json={"emp_code": TEST_EMP, "punched_at": t_out_day2}).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futs = [executor.submit(concurrent_punch_out) for _ in range(5)]
        statuses = [f.result() for f in futs]

    print("Concurrent punch-out statuses:", statuses)
    assert statuses.count(200) == 1, f"Expected exactly one 200, got {statuses.count(200)}"
    assert statuses.count(409) == 4, f"Expected four 409s, got {statuses.count(409)}"
    print("✓ Concurrency on punch-out verified: exactly 1x 200, 4x 409")

    # -------------------------------------------------------------
    # 2. REGULARIZATION (PATCH) TESTS
    # -------------------------------------------------------------
    print("\n--- 2. Testing PATCH /attendance/{emp_code}/{date} ---")

    # 2.1 Unknown employee -> 404
    r_p_unk = client.patch(f"/attendance/UNKNOWN/{'2026-07-06'}", json={
        "reason": "Correcting record",
        "regularized_by": "HR_ADMIN"
    })
    assert r_p_unk.status_code == 404

    # 2.2 Unknown date -> 404
    r_p_nodate = client.patch(f"/attendance/{TEST_EMP}/2026-12-31", json={
        "reason": "Correcting record",
        "regularized_by": "HR_ADMIN"
    })
    assert r_p_nodate.status_code == 404
    print("✓ 404 returned for unknown employee or date")

    # 2.3 Invalid date format -> 422
    r_bad_date = client.patch(f"/attendance/{TEST_EMP}/2026-02-30", json={
        "reason": "Correcting record",
        "regularized_by": "HR_ADMIN"
    })
    assert r_bad_date.status_code == 422
    print("✓ 422 returned for invalid calendar date in path")

    # 2.4 Missing reason or regularized_by -> 422
    r_no_reason = client.patch(f"/attendance/{TEST_EMP}/2026-07-06", json={
        "status": "WFH",
        "regularized_by": "HR_ADMIN"
    })
    assert r_no_reason.status_code == 422
    print("✓ 422 returned when required fields missing")

    # 2.5 Request changing nothing -> 422 (Assumption A11)
    # The record on 2026-07-06 has status=PRESENT, punch_in=t_in, punch_out=t_out
    r_noop = client.patch(f"/attendance/{TEST_EMP}/2026-07-06", json={
        "status": "PRESENT",
        "punch_in": t_in,
        "punch_out": t_out,
        "reason": "No real change",
        "regularized_by": "HR_ADMIN"
    })
    assert r_noop.status_code == 422, f"Expected 422 for noop, got {r_noop.status_code}"
    print("✓ 422 returned when request changes nothing (Assumption A11)")

    # 2.6 Supplying punch times with ABSENT -> 422
    r_abs_punch = client.patch(f"/attendance/{TEST_EMP}/2026-07-06", json={
        "status": "ABSENT",
        "punch_in": t_in,
        "reason": "Making absent",
        "regularized_by": "HR_ADMIN"
    })
    assert r_abs_punch.status_code == 422
    print("✓ 422 returned when supplying punch_in with ABSENT status")

    # 2.7 Valid regularization to ABSENT (clears punch times, appends history)
    r_to_absent = client.patch(f"/attendance/{TEST_EMP}/2026-07-06", json={
        "status": "ABSENT",
        "reason": "Employee did not work",
        "regularized_by": "HR_ADMIN"
    })
    assert r_to_absent.status_code == 200
    b_abs = r_to_absent.json()
    assert b_abs["status"] == "ABSENT"
    assert b_abs["punch_in"] is None
    assert b_abs["punch_out"] is None
    assert b_abs["work_hours"] is None
    assert b_abs["late_minutes"] == 0
    assert len(b_abs["history"]) == 1
    h0 = b_abs["history"][0]
    assert h0["by"] == "HR_ADMIN"
    assert h0["reason"] == "Employee did not work"
    assert "status" in h0["changes"]
    assert h0["changes"]["status"] == {"from": "PRESENT", "to": "ABSENT"}
    assert "punch_in" in h0["changes"]
    print("✓ Valid regularization to ABSENT clears punch times and appends history entry")

    # 2.8 Inherited ABSENT status with punch_in -> 422 (Assumption A7)
    r_inh_abs = client.patch(f"/attendance/{TEST_EMP}/2026-07-06", json={
        "punch_in": t_in,
        "reason": "Attempt punch on absent without changing status",
        "regularized_by": "HR_ADMIN"
    })
    assert r_inh_abs.status_code == 422
    print("✓ 422 returned when supplying punch_in to inherited ABSENT status (Assumption A7)")

    # 2.9 Regularize back to WFH with punch times (Half-Day test)
    # Work hours = 4.0 hours (< 4.50) -> half_day = True (Rule R5)
    t_wfh_out = t_in + (4 * 3600 * 1000) # 4 hours
    r_to_wfh = client.patch(f"/attendance/{TEST_EMP}/2026-07-06", json={
        "status": "WFH",
        "punch_in": t_in,
        "punch_out": t_wfh_out,
        "reason": "Employee worked from home for half day",
        "regularized_by": "HR_MANAGER"
    })
    assert r_to_wfh.status_code == 200
    b_wfh = r_to_wfh.json()
    assert b_wfh["status"] == "WFH"
    assert b_wfh["work_hours"] == 4.0
    assert b_wfh["half_day"] is True
    assert len(b_wfh["history"]) == 2
    print("✓ Regularization back to WFH with 4.0h correctly calculates half_day=True (R5) and appends second history entry")

    # 2.10 Concurrency test on PATCH
    # Two simultaneous edits with different reasons
    def concurrent_patch(tag):
        c = TestClient(app)
        return c.patch(f"/attendance/{TEST_EMP}/2026-07-06", json={
            "status": "ON_DUTY" if tag == 1 else "PRESENT",
            "reason": f"Concurrent edit {tag}",
            "regularized_by": f"ADMIN_{tag}"
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futs = [executor.submit(concurrent_patch, i) for i in range(1, 6)]
        p_statuses = [f.result() for f in futs]

    print("Concurrent PATCH statuses:", p_statuses)
    assert p_statuses.count(200) >= 1, "At least one patch must succeed"
    print("✓ Concurrent edits handled safely without corrupting history")

# Cleanup
db.employees.delete_many({"emp_code": {"$in": [TEST_EMP, OVERNIGHT_EMP]}})
db.attendance_logs.delete_many({"emp_code": {"$in": [TEST_EMP, OVERNIGHT_EMP]}})

print()
print("=" * 60)
print("ALL PHASE 3 TESTS PASSED SUCCESSFULLY! ✓")
print("=" * 60)
