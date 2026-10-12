"""
dev/scratch/test_phase2_fixes.py
Comprehensive test suite for Phase 2 Part A fixes in app/main.py.
Run: .venv/Scripts/python dev/scratch/test_phase2_fixes.py
"""
import sys
sys.path.insert(0, ".")

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from fastapi.testclient import TestClient
from app.main import (
    app,
    db,
    init_indexes,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    compute_record_date_and_shift,
)

IST = timezone(timedelta(hours=5, minutes=30))

print("=" * 60)
print("TEST 1: Helper Functions & Rules R1-R4")
print("=" * 60)

# 1.1 R2 compute_late_minutes
# Shift: 09:30 IST = 04:00 UTC
shift_start_utc = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
# Punch 09:40:00 IST = 04:10:00 UTC (600s elapsed)
p_0940_00 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
assert compute_late_minutes(p_0940_00, shift_start_utc) == 0, "09:40:00 must be 0"

# Punch 09:40:01 IST = 04:10:01 UTC (601s elapsed)
p_0940_01 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
assert compute_late_minutes(p_0940_01, shift_start_utc) == 10, "09:40:01 must be 10"

# Punch 10:15:59 IST = 04:45:59 UTC (2759s elapsed)
p_1015_59 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
assert compute_late_minutes(p_1015_59, shift_start_utc) == 45, "10:15:59 must be 45"
print("✓ compute_late_minutes passes R2 test vectors (09:40:00 -> 0, 09:40:01 -> 10, 10:15:59 -> 45)")

# 1.2 R4 & R8 compute_work_hours (Round-half-up)
t0 = datetime(2026, 7, 6, 0, 0, 0, tzinfo=timezone.utc)
t1 = t0 + timedelta(seconds=450)  # 0.125h
assert compute_work_hours(t0, t1) == 0.13, f"Expected 0.13, got {compute_work_hours(t0, t1)}"

t2 = t0 + timedelta(seconds=9630) # 2.675h
assert compute_work_hours(t0, t2) == 2.68, f"Expected 2.68, got {compute_work_hours(t0, t2)}"
print("✓ compute_work_hours correctly implements round-half-up (0.125 -> 0.13, 2.675 -> 2.68)")

# 1.3 R3 compute_overtime (30m threshold)
shift_end_utc = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc) # 18:30 IST
p_out_29m = shift_end_utc + timedelta(minutes=29, seconds=59)
assert compute_overtime(p_out_29m, shift_end_utc) == 0, "29m must be 0 overtime"

p_out_30m = shift_end_utc + timedelta(minutes=30)
assert compute_overtime(p_out_30m, shift_end_utc) == 30, "30m must be 30 overtime"

p_out_45m = shift_end_utc + timedelta(minutes=45, seconds=30)
assert compute_overtime(p_out_45m, shift_end_utc) == 45, "45m must be 45 overtime"
print("✓ compute_overtime enforces 30m minimum (29m -> 0, 30m -> 30, 45m -> 45)")

# 1.4 R1 Overnight shifts
p_overnight = datetime(2026, 7, 7, 19, 55, 0, tzinfo=timezone.utc) # 01:25 IST on July 8
d_ov, s_ov, e_ov = compute_record_date_and_shift(p_overnight, "22:00", "06:00")
assert d_ov == "2026-07-07", f"Expected overnight date 2026-07-07, got {d_ov}"
print("✓ compute_record_date_and_shift handles overnight shifts across midnight")

print()
print("=" * 60)
print("TEST 2: FastAPI Endpoints & Concurrency Safety")
print("=" * 60)

# Ensure indexes exist
init_indexes()

# Clean up test records
db.employees.delete_many({"emp_code": {"$in": ["EMP9991", "EMP9992", "EMP9993"]}})
db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP9991", "EMP9992", "EMP9993"]}})

with TestClient(app) as client:
    # 2.1 Health check
    r = client.get("/health")
    assert r.status_code == 200, f"Health check failed: {r.status_code} {r.text}"
    assert r.json() == {"status": "ok"}
    print("✓ GET /health returns 200 ok when MongoDB is answering")

    # 2.2 POST /employees validation
    # Bad email
    r_bad_email = client.post("/employees", json={
        "emp_code": "EMP9991",
        "name": "Test Emp",
        "email": "not-an-email",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert r_bad_email.status_code == 422, f"Expected 422 for bad email, got {r_bad_email.status_code}"

    # Bad calendar date (2026-02-30)
    r_bad_date = client.post("/employees", json={
        "emp_code": "EMP9991",
        "name": "Test Emp",
        "email": "test@hrone.com",
        "department": "Engineering",
        "joined_on": "2026-02-30"
    })
    assert r_bad_date.status_code == 422, f"Expected 422 for invalid date, got {r_bad_date.status_code}"

    # Valid creation
    r_create = client.post("/employees", json={
        "emp_code": "EMP9991",
        "name": "Test Emp One",
        "email": "emp9991@hrone.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert r_create.status_code == 201, f"Failed create: {r_create.status_code} {r_create.text}"
    doc = r_create.json()
    assert "_id" not in doc, "Contract violation: _id returned"
    assert "id" not in doc, "Contract violation: id returned"
    assert isinstance(doc["created_at"], int), "created_at must be epoch ms integer"
    print("✓ POST /employees creates employee with epoch ms created_at and no _id")

    # Duplicate creation -> 409
    r_dup = client.post("/employees", json={
        "emp_code": "EMP9991",
        "name": "Duplicate",
        "email": "dup@hrone.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert r_dup.status_code == 409, f"Expected 409 on duplicate, got {r_dup.status_code}"
    print("✓ POST /employees rejects duplicate emp_code with 409 Conflict")

    # 2.3 GET /employees pagination & filtering
    # Create two more employees
    client.post("/employees", json={
        "emp_code": "EMP9992",
        "name": "Test Emp Two",
        "email": "emp9992@hrone.com",
        "department": "Product",
        "joined_on": "2026-01-01"
    })
    client.post("/employees", json={
        "emp_code": "EMP9993",
        "name": "Test Emp Three",
        "email": "emp9993@hrone.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })

    r_p1 = client.get("/employees?page=1&page_size=2")
    r_p2 = client.get("/employees?page=2&page_size=2")
    assert r_p1.status_code == 200
    assert r_p2.status_code == 200
    p1_items = r_p1.json()["items"]
    p2_items = r_p2.json()["items"]
    assert len(p1_items) == 2, f"Page 1 should have 2 items, got {len(p1_items)}"
    assert p1_items[0]["emp_code"] != p2_items[0]["emp_code"], "Page 1 and Page 2 must not duplicate items"
    print("✓ GET /employees pagination off-by-one is fixed (page 1 skip=0, page 2 skip=2)")

    # Filter by department
    r_dept = client.get("/employees?department=Product")
    dept_body = r_dept.json()
    assert dept_body["total"] == 1, f"Expected total 1 for Product, got {dept_body['total']}"
    assert all(e["department"] == "Product" for e in dept_body["items"])
    print("✓ GET /employees filtered total reflects department query")

    # 2.4 POST /attendance/punch-in
    # Unknown employee -> 404
    r_unknown = client.post("/attendance/punch-in", json={"emp_code": "EMP0000_DOES_NOT_EXIST"})
    assert r_unknown.status_code == 404, f"Expected 404 for unknown employee, got {r_unknown.status_code}"
    print("✓ POST /attendance/punch-in returns 404 for unknown employee")

    # Invalid status LEAVE -> 422
    r_bad_status = client.post("/attendance/punch-in", json={"emp_code": "EMP9991", "status": "LEAVE"})
    assert r_bad_status.status_code == 422, f"Expected 422 for LEAVE on punch-in, got {r_bad_status.status_code}"
    print("✓ POST /attendance/punch-in rejects LEAVE status with 422")

    # Valid punch-in (09:40:01 IST on 2026-07-06 = 1783311001000)
    # 2026-07-06 04:10:01 UTC = 1783311001000 ms
    dt_punch = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    ts_ms = int(dt_punch.timestamp() * 1000)

    r_punch1 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9991",
        "punched_at": ts_ms,
        "status": "PRESENT"
    })
    assert r_punch1.status_code == 201, f"Punch-in failed: {r_punch1.status_code} {r_punch1.text}"
    p_body = r_punch1.json()
    assert p_body["date"] == "2026-07-06"
    assert p_body["late_minutes"] == 10, f"Expected 10 late_minutes for 09:40:01, got {p_body['late_minutes']}"
    assert p_body["punch_in"] == ts_ms
    assert p_body["work_hours"] is None
    assert p_body["half_day"] is False
    assert "_id" not in p_body and "id" not in p_body, "Contract violation: id/_id leaked"
    print("✓ POST /attendance/punch-in records punch-in with R1/R2 calculations and clean schema")

    # Duplicate punch-in for same date -> 409
    r_punch_dup = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9991",
        "punched_at": ts_ms + 10000,
        "status": "PRESENT"
    })
    assert r_punch_dup.status_code == 409, f"Expected 409 on duplicate date punch-in, got {r_punch_dup.status_code}"
    print("✓ POST /attendance/punch-in rejects second punch-in on same date with 409")

    # 2.5 GET /attendance
    # date_from > date_to -> 422
    r_bad_range = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert r_bad_range.status_code == 422, f"Expected 422 for inverted date range, got {r_bad_range.status_code}"
    print("✓ GET /attendance returns 422 when date_from > date_to")

    r_att = client.get("/attendance?emp_code=EMP9991")
    assert r_att.status_code == 200
    att_items = r_att.json()["items"]
    assert len(att_items) == 1
    assert att_items[0]["emp_code"] == "EMP9991"
    assert "_id" not in att_items[0] and "id" not in att_items[0]
    print("✓ GET /attendance returns correct paginated records without leaking id")

# Clean up test records
db.employees.delete_many({"emp_code": {"$in": ["EMP9991", "EMP9992", "EMP9993"]}})
db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP9991", "EMP9992", "EMP9993"]}})

print()
print("=" * 60)
print("ALL PHASE 2 TESTS PASSED SUCCESSFULLY! ✓")
print("=" * 60)
