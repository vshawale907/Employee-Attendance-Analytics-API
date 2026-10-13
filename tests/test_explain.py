"""
tests/test_explain.py
Tests for GET /admin/explain/{endpoint}.
Verifies plan executionStats, presence of IXSCAN, absence of COLLSCAN, and parameter validation.
"""
import json

def seed_explain_data(test_client):
    res = test_client.post("/employees", json={
        "emp_code": "EMP6001",
        "name": "Explain Tester",
        "department": "Engineering",
        "email": "exp@hrone.test",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 201

    res_punch = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP6001",
        "punched_at": 1782879000000 # 2026-07-01
    })
    assert res_punch.status_code == 201


def test_explain_all_endpoints(test_client, clean_db):
    seed_explain_data(test_client)

    # 1. attendance_list
    res1 = test_client.get("/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31")
    assert res1.status_code == 200
    d1 = res1.json()
    assert d1["endpoint"] == "attendance_list"
    assert d1["collection"] == "attendance_logs"
    s1 = json.dumps(d1["explain"])
    assert "IXSCAN" in s1
    assert "COLLSCAN" not in s1

    # 2. employee_monthly
    res2 = test_client.get("/admin/explain/employee_monthly?emp_code=EMP6001&month=2026-07")
    assert res2.status_code == 200
    d2 = res2.json()
    assert d2["endpoint"] == "employee_monthly"
    assert d2["collection"] == "attendance_logs"
    s2 = json.dumps(d2["explain"])
    assert "IXSCAN" in s2 or "COUNT_SCAN" in s2 or "ixseek" in s2
    assert "COLLSCAN" not in s2

    # 3. department_summary
    res3 = test_client.get("/admin/explain/department_summary?month=2026-07")
    assert res3.status_code == 200
    d3 = res3.json()
    assert d3["endpoint"] == "department_summary"
    assert d3["collection"] == "employees"
    s3 = json.dumps(d3["explain"])
    assert "IXSCAN" in s3
    assert "COLLSCAN" not in s3

    # 4. late_leaderboard
    res4 = test_client.get("/admin/explain/late_leaderboard?month=2026-07&limit=10")
    assert res4.status_code == 200
    d4 = res4.json()
    assert d4["endpoint"] == "late_leaderboard"
    assert d4["collection"] == "attendance_logs"
    s4 = json.dumps(d4["explain"])
    assert "IXSCAN" in s4
    assert "COLLSCAN" not in s4

    # 5. department_trend
    res5 = test_client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07")
    assert res5.status_code == 200
    d5 = res5.json()
    assert d5["endpoint"] == "department_trend"
    assert d5["collection"] == "attendance_logs"
    s5 = json.dumps(d5["explain"])
    assert "IXSCAN" in s5
    assert "COLLSCAN" not in s5


def test_explain_validation_errors(test_client, clean_db):
    # Missing required query params -> 422
    assert test_client.get("/admin/explain/employee_monthly").status_code == 422
    assert test_client.get("/admin/explain/department_summary").status_code == 422
    assert test_client.get("/admin/explain/late_leaderboard").status_code == 422
    assert test_client.get("/admin/explain/department_trend").status_code == 422

    # Invalid endpoint name -> 422
    assert test_client.get("/admin/explain/nonexistent_endpoint").status_code == 422
