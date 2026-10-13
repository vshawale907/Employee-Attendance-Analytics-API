"""
tests/test_punch.py
Tests for punch clock lifecycle:
- POST /attendance/punch-in
- POST /attendance/punch-out
"""
from datetime import datetime
import concurrent.futures

def dt_to_epoch_ms(dt_str: str) -> int:
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


def setup_standard_employee(test_client, emp_code="EMP1001"):
    res = test_client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Punch Tester",
        "department": "Operations",
        "email": f"{emp_code.lower()}@hrone.test",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 201


def test_punch_in_and_late_calculation(test_client, clean_db):
    setup_standard_employee(test_client, "EMP1001")

    # 1. Unknown employee -> 404
    res_404 = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP9999",
        "punched_at": dt_to_epoch_ms("2026-07-01T03:30:00Z")
    })
    assert res_404.status_code == 404

    # 2. Punch in on time (09:05 IST = 03:35 UTC) -> late_minutes = 0
    res_ontime = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP1001",
        "punched_at": dt_to_epoch_ms("2026-07-01T03:35:00Z"),
        "status": "PRESENT"
    })
    assert res_ontime.status_code == 201
    d1 = res_ontime.json()
    assert d1["date"] == "2026-07-01"
    assert d1["late_minutes"] == 0
    assert d1["status"] == "PRESENT"

    # 3. Duplicate punch in on same date -> 409
    res_dup = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP1001",
        "punched_at": dt_to_epoch_ms("2026-07-01T04:00:00Z")
    })
    assert res_dup.status_code == 409


def test_punch_in_late_grace_period(test_client, clean_db):
    setup_standard_employee(test_client, "EMP1001")

    # 09:10:00 IST (03:40:00 UTC) -> exactly 10 min late, within grace period -> 0 late minutes
    res1 = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP1001",
        "punched_at": dt_to_epoch_ms("2026-07-02T03:40:00Z")
    })
    assert res1.status_code == 201
    assert res1.json()["late_minutes"] == 0

    # Clean and test 09:10:01 IST -> grace expired -> 10 late minutes
    setup_standard_employee(test_client, "EMP1002")
    res2 = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP1002",
        "punched_at": dt_to_epoch_ms("2026-07-02T03:40:01Z")
    })
    assert res2.status_code == 201
    assert res2.json()["late_minutes"] == 10


def test_punch_out_lifecycle_and_calculations(test_client, clean_db):
    setup_standard_employee(test_client, "EMP1001")

    # Punch in at 09:00 IST (03:30 UTC)
    t_in = dt_to_epoch_ms("2026-07-03T03:30:00Z")
    test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP1001",
        "punched_at": t_in
    })

    # Punch out earlier than punch in -> 422
    res_early = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP1001",
        "punched_at": t_in - 10000
    })
    assert res_early.status_code == 422

    # Punch out > 24 hours later -> 422
    res_late = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP1001",
        "punched_at": t_in + (25 * 3600 * 1000)
    })
    assert res_late.status_code == 422

    # Normal punch out at 19:30 IST (14:00 UTC) -> 10.5h work, 90m overtime
    # Shift end: 18:00 IST (12:30 UTC), worked till 14:00 UTC -> 90 mins overtime
    t_out = dt_to_epoch_ms("2026-07-03T14:00:00Z")
    res_out = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP1001",
        "punched_at": t_out
    })
    assert res_out.status_code == 200
    d_out = res_out.json()
    assert d_out["work_hours"] == 10.5
    assert d_out["overtime_minutes"] == 90
    assert d_out["half_day"] is False

    # Second punch out on already closed record -> 409
    res_already = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP1001",
        "punched_at": t_out + 10000
    })
    assert res_already.status_code == 409


def test_overnight_shift_punch_lifecycle(test_client, clean_db):
    # Overnight shift: 22:00 to 06:00
    test_client.post("/employees", json={
        "emp_code": "EMP1003",
        "name": "Night Owl",
        "department": "Security",
        "email": "night@hrone.test",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01"
    })

    # Punch in on 2026-07-07 at 01:00 IST (2026-07-06 19:30 UTC)
    # Per R1, earlier than shift_end 06:00 belongs to shift starting July 6!
    t_in = dt_to_epoch_ms("2026-07-06T19:30:00Z")
    res_in = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP1003",
        "punched_at": t_in
    })
    assert res_in.status_code == 201
    assert res_in.json()["date"] == "2026-07-06"

    # Punch out at 07:00 IST on July 7 (01:30 UTC July 7)
    # Shift ended at 06:00 IST (00:30 UTC July 7) -> 60 min overtime
    t_out = dt_to_epoch_ms("2026-07-07T01:30:00Z")
    res_out = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP1003",
        "punched_at": t_out
    })
    assert res_out.status_code == 200
    d_out = res_out.json()
    assert d_out["work_hours"] == 6.0
    assert d_out["overtime_minutes"] == 60


def test_concurrent_punch_out_safety(test_client, clean_db):
    setup_standard_employee(test_client, "EMP1004")
    t_in = dt_to_epoch_ms("2026-07-04T03:30:00Z")
    test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP1004",
        "punched_at": t_in
    })

    t_out = dt_to_epoch_ms("2026-07-04T12:30:00Z")
    def fire():
        return test_client.post("/attendance/punch-out", json={
            "emp_code": "EMP1004",
            "punched_at": t_out
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        statuses = list(ex.map(lambda _: fire(), range(5)))

    # Exactly one request must succeed with 200, others 409
    assert statuses.count(200) == 1
    assert statuses.count(409) == 4


def test_concurrent_punch_in_race_safety(test_client, clean_db):
    setup_standard_employee(test_client, "EMP1005")
    t_in = dt_to_epoch_ms("2026-07-05T03:30:00Z")

    def fire():
        return test_client.post("/attendance/punch-in", json={
            "emp_code": "EMP1005",
            "punched_at": t_in
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        statuses = list(ex.map(lambda _: fire(), range(5)))

    # Exactly one request must succeed with 201, others 409
    assert statuses.count(201) == 1
    assert statuses.count(409) == 4

