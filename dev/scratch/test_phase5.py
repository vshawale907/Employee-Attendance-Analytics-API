"""
dev/scratch/test_phase5.py
Phase 5 automated test suite for GET /admin/explain/{endpoint}.
Verifies response schema, executionStats presence, IXSCAN usage, COLLSCAN absence, and 422 validation.
"""
import sys
sys.path.insert(0, ".")
from fastapi.testclient import TestClient
from app.main import app, init_indexes
import json

# Ensure indexes are initialized
init_indexes()

client = TestClient(app)

def test_attendance_list_explain():
    # Valid
    res = client.get("/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
    data = res.json()
    assert data["endpoint"] == "attendance_list"
    assert data["collection"] == "attendance_logs"
    assert "explain" in data
    assert "executionStats" in data["explain"]
    exp_str = json.dumps(data["explain"])
    assert "IXSCAN" in exp_str, "Expected IXSCAN in attendance_list"
    assert "COLLSCAN" not in exp_str, "COLLSCAN found in attendance_list!"
    print("PASS: attendance_list explain valid (IXSCAN=True, COLLSCAN=False)")

    # 422 on date_from > date_to
    res_err = client.get("/admin/explain/attendance_list?date_from=2026-07-31&date_to=2026-07-01")
    assert res_err.status_code == 422, f"Expected 422, got {res_err.status_code}"
    print("PASS: attendance_list explain date_from > date_to -> 422")


def test_employee_monthly_explain():
    # Missing params -> 422
    res_miss = client.get("/admin/explain/employee_monthly")
    assert res_miss.status_code == 422, f"Expected 422, got {res_miss.status_code}"
    print("PASS: employee_monthly explain missing params -> 422")

    # Invalid month format -> 422
    res_bad = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-13")
    assert res_bad.status_code == 422, f"Expected 422, got {res_bad.status_code}"
    print("PASS: employee_monthly explain invalid month -> 422")

    # Valid
    res = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
    data = res.json()
    assert data["endpoint"] == "employee_monthly"
    assert data["collection"] == "attendance_logs"
    exp_str = json.dumps(data["explain"])
    assert "executionStats" in exp_str, "executionStats missing from explain output"
    exp_str = json.dumps(data["explain"])
    assert "IXSCAN" in exp_str or "COUNT_SCAN" in exp_str or "ixseek" in exp_str, "Expected index scan stage"
    assert "COLLSCAN" not in exp_str, "COLLSCAN found in employee_monthly!"
    print("PASS: employee_monthly explain valid (IXSCAN/Index-backed=True, COLLSCAN=False)")


def test_department_summary_explain():
    # Missing month -> 422
    res_miss = client.get("/admin/explain/department_summary")
    assert res_miss.status_code == 422, f"Expected 422, got {res_miss.status_code}"
    print("PASS: department_summary explain missing month -> 422")

    # All departments
    res_all = client.get("/admin/explain/department_summary?month=2026-07")
    assert res_all.status_code == 200, f"Expected 200, got {res_all.status_code}: {res_all.text}"
    data_all = res_all.json()
    assert data_all["endpoint"] == "department_summary"
    assert data_all["collection"] == "employees"
    exp_str_all = json.dumps(data_all["explain"])
    assert "executionStats" in exp_str_all, "executionStats missing from explain output"
    assert "IXSCAN" in exp_str_all, "Expected IXSCAN in department_summary (all departments)"
    assert "COLLSCAN" not in exp_str_all, "COLLSCAN found in department_summary (all departments)!"
    print("PASS: department_summary (all departments) valid (IXSCAN=True, COLLSCAN=False)")

    # Specific department
    res_dept = client.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
    assert res_dept.status_code == 200, f"Expected 200, got {res_dept.status_code}"
    data_dept = res_dept.json()
    exp_str_dept = json.dumps(data_dept["explain"])
    assert "IXSCAN" in exp_str_dept, "Expected IXSCAN in department_summary (specific department)"
    assert "COLLSCAN" not in exp_str_dept, "COLLSCAN found in department_summary (specific department)!"
    print("PASS: department_summary (Engineering) valid (IXSCAN=True, COLLSCAN=False)")


def test_late_leaderboard_explain():
    # Missing month -> 422
    res_miss = client.get("/admin/explain/late_leaderboard")
    assert res_miss.status_code == 422, f"Expected 422, got {res_miss.status_code}"
    print("PASS: late_leaderboard explain missing month -> 422")

    # Valid
    res = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=10")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
    data = res.json()
    assert data["endpoint"] == "late_leaderboard"
    assert data["collection"] == "attendance_logs"
    exp_str = json.dumps(data["explain"])
    assert "executionStats" in exp_str, "executionStats missing from explain output"
    exp_str = json.dumps(data["explain"])
    assert "IXSCAN" in exp_str, "Expected IXSCAN in late_leaderboard"
    assert "COLLSCAN" not in exp_str, "COLLSCAN found in late_leaderboard!"
    print("PASS: late_leaderboard explain valid (IXSCAN=True, COLLSCAN=False)")


def test_department_trend_explain():
    # Missing params -> 422
    res_miss = client.get("/admin/explain/department_trend?department=Engineering")
    assert res_miss.status_code == 422, f"Expected 422, got {res_miss.status_code}"
    print("PASS: department_trend explain missing params -> 422")

    # to < from -> 422
    res_rev = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-10&to=2026-07-05")
    assert res_rev.status_code == 422, f"Expected 422, got {res_rev.status_code}"
    print("PASS: department_trend explain to < from -> 422")

    # > 92 days -> 422
    res_range = client.get("/admin/explain/department_trend?department=Engineering&from=2026-01-01&to=2026-05-01")
    assert res_range.status_code == 422, f"Expected 422, got {res_range.status_code}"
    print("PASS: department_trend explain > 92 days -> 422")

    # Valid
    res = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
    data = res.json()
    assert data["endpoint"] == "department_trend"
    assert data["collection"] == "attendance_logs"
    exp_str = json.dumps(data["explain"])
    assert "executionStats" in exp_str, "executionStats missing from explain output"
    exp_str = json.dumps(data["explain"])
    assert "IXSCAN" in exp_str, "Expected IXSCAN in department_trend"
    assert "COLLSCAN" not in exp_str, "COLLSCAN found in department_trend!"
    print("PASS: department_trend explain valid (IXSCAN=True, COLLSCAN=False)")


def test_invalid_endpoint():
    res = client.get("/admin/explain/invalid_endpoint")
    assert res.status_code == 422, f"Expected 422, got {res.status_code}"
    print("PASS: invalid endpoint -> 422")


if __name__ == "__main__":
    print("Running Phase 5 tests...")
    test_attendance_list_explain()
    test_employee_monthly_explain()
    test_department_summary_explain()
    test_late_leaderboard_explain()
    test_department_trend_explain()
    test_invalid_endpoint()
    print("\nALL PHASE 5 TESTS PASSED SUCCESSFULLY!")
