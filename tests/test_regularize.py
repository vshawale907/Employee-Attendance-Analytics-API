"""
tests/test_regularize.py
Tests for PATCH /attendance/{emp_code}/{date} regularization and audit history.
"""
import concurrent.futures
from datetime import datetime

def dt_to_epoch_ms(dt_str: str) -> int:
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


def setup_attendance_record(test_client, emp_code="EMP3001"):
    res = test_client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Reg Tester",
        "department": "Engineering",
        "email": f"{emp_code.lower()}@hrone.test",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 201

    t_in = dt_to_epoch_ms("2026-07-06T03:30:00Z")
    t_out = dt_to_epoch_ms("2026-07-06T12:30:00Z")
    test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": t_in})
    test_client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": t_out})


def test_regularize_to_absent_clears_punches_and_records_history(test_client, clean_db):
    setup_attendance_record(test_client, "EMP3001")

    patch_payload = {
        "status": "ABSENT",
        "reason": "Family emergency - requested leave was not applied",
        "regularized_by": "HR_ADMIN"
    }
    res = test_client.patch("/attendance/EMP3001/2026-07-06", json=patch_payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ABSENT"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["late_minutes"] == 0
    assert len(data["history"]) == 1

    entry = data["history"][0]
    assert entry["reason"] == patch_payload["reason"]
    assert entry["by"] == "HR_ADMIN"
    assert "status" in entry["changes"]
    assert entry["changes"]["status"]["from"] == "PRESENT"
    assert entry["changes"]["status"]["to"] == "ABSENT"


def test_regularize_validation_rules(test_client, clean_db):
    setup_attendance_record(test_client, "EMP3001")

    # 1. 404 on nonexistent record with valid emp_code format
    res_404 = test_client.patch("/attendance/EMP9999/2026-07-06", json={
        "status": "WFH",
        "reason": "Valid reason length",
        "regularized_by": "HR"
    })
    assert res_404.status_code == 404

    # 2. 422 on invalid date path parameter
    res_bad_date = test_client.patch("/attendance/EMP3001/2026-02-30", json={
        "status": "WFH",
        "reason": "Valid reason length",
        "regularized_by": "HR"
    })
    assert res_bad_date.status_code == 422

    # 3. 422 when request changes nothing (A11)
    res_no_change = test_client.patch("/attendance/EMP3001/2026-07-06", json={
        "status": "PRESENT",
        "reason": "No change applied",
        "regularized_by": "HR"
    })
    assert res_no_change.status_code == 422

    # 4. 422 when supplying punch times with ABSENT status (A7)
    res_absent_punch = test_client.patch("/attendance/EMP3001/2026-07-06", json={
        "status": "ABSENT",
        "punch_in": dt_to_epoch_ms("2026-07-06T03:30:00Z"),
        "reason": "Contradictory punch",
        "regularized_by": "HR"
    })
    assert res_absent_punch.status_code == 422


def test_regularize_updates_half_day_and_work_hours(test_client, clean_db):
    setup_attendance_record(test_client, "EMP3002")

    # Regularize to 4.0 work hours (09:00 to 13:00 IST = 03:30 to 07:30 UTC)
    # 4.0 hours < 4.5 threshold -> half_day = True (R5)
    t_out = dt_to_epoch_ms("2026-07-06T07:30:00Z")
    res = test_client.patch("/attendance/EMP3002/2026-07-06", json={
        "punch_out": t_out,
        "reason": "Left early for doctor appointment",
        "regularized_by": "MANAGER_01"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] == 4.0
    assert data["half_day"] is True
    assert len(data["history"]) == 1


def test_concurrent_patch_lost_update_safety(test_client, clean_db):
    setup_attendance_record(test_client, "EMP3003")

    def fire(i):
        return test_client.patch("/attendance/EMP3003/2026-07-06", json={
            "status": "WFH" if i % 2 == 0 else "ON_DUTY",
            "reason": f"Concurrent edit attempt {i}",
            "regularized_by": f"ADMIN_{i}"
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        statuses = list(ex.map(fire, range(5)))

    # Concurrency safe: no unhandled exceptions (no 500s)
    assert all(code in (200, 409, 422) for code in statuses)
    assert 500 not in statuses

