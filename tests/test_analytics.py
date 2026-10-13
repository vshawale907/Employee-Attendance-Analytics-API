"""
tests/test_analytics.py
Tests for all 4 analytics aggregation endpoints:
- GET /analytics/employees/{emp_code}/monthly
- GET /analytics/departments/summary
- GET /analytics/late_leaderboard
- GET /analytics/departments/{department}/trend
"""
from datetime import datetime

def dt_to_epoch_ms(dt_str: str) -> int:
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


def seed_analytics_data(test_client):
    # 1. EMP5001: Joined Jan 2026, Engineering
    res1 = test_client.post("/employees", json={
        "emp_code": "EMP5001",
        "name": "Alice Analytics",
        "department": "Engineering",
        "email": "alice.an@hrone.test",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-01-01"
    })
    assert res1.status_code == 201

    # 2. EMP5002: Joined mid-month July 15 2026, Engineering
    res2 = test_client.post("/employees", json={
        "emp_code": "EMP5002",
        "name": "Bob Analytics",
        "department": "Engineering",
        "email": "bob.an@hrone.test",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-07-15"
    })
    assert res2.status_code == 201

    # 3. EMP5003: Joined future Aug 1 2026, Engineering
    res3 = test_client.post("/employees", json={
        "emp_code": "EMP5003",
        "name": "Charlie Future",
        "department": "Engineering",
        "email": "charlie.an@hrone.test",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-08-01"
    })
    assert res3.status_code == 201

    # 4. EMP5004: Joined Jan 2026, Engineering (Zero logs in July)
    res4 = test_client.post("/employees", json={
        "emp_code": "EMP5004",
        "name": "Dana ZeroLogs",
        "department": "Engineering",
        "email": "dana.an@hrone.test",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-01-01"
    })
    assert res4.status_code == 201

    # Log 1: EMP5001 present weekday 2026-07-01 (Wed) - Full day, 15m late
    test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP5001",
        "punched_at": dt_to_epoch_ms("2026-07-01T03:45:00Z") # 09:15 IST
    })
    test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP5001",
        "punched_at": dt_to_epoch_ms("2026-07-01T12:30:00Z") # 18:00 IST
    })

    # Log 2: EMP5001 present weekday 2026-07-02 (Thu) - Half day (4.0h)
    test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP5001",
        "punched_at": dt_to_epoch_ms("2026-07-02T03:30:00Z")
    })
    test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP5001",
        "punched_at": dt_to_epoch_ms("2026-07-02T07:30:00Z")
    })

    # Log 3: EMP5001 present WEEKEND 2026-07-05 (Sun) - 8.0h (Must NOT add to present_days!)
    test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP5001",
        "punched_at": dt_to_epoch_ms("2026-07-05T03:30:00Z")
    })
    test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP5001",
        "punched_at": dt_to_epoch_ms("2026-07-05T11:30:00Z")
    })

    # Log 4: EMP5002 present weekday 2026-07-16 (Thu) - 15m late
    test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP5002",
        "punched_at": dt_to_epoch_ms("2026-07-16T03:45:00Z") # 15m late
    })
    test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP5002",
        "punched_at": dt_to_epoch_ms("2026-07-16T12:30:00Z")
    })


def test_employee_monthly_analytics(test_client, clean_db):
    seed_analytics_data(test_client)

    # 1. EMP5001 summary:
    # Working days in July 2026 = 23 (Mon-Fri)
    # present_days = 1.0 (Wed full) + 0.5 (Thu half) = 1.5 (Sunday excluded per Trap #20)
    # attendance_pct = 1.5 / 23 * 100 = 6.52% (half-up)
    res_a = test_client.get("/analytics/employees/EMP5001/monthly?month=2026-07")
    assert res_a.status_code == 200
    d_a = res_a.json()
    assert d_a["working_days"] == 23
    assert d_a["present_days"] == 1.5
    assert d_a["attendance_pct"] == 6.52
    assert d_a["late_count"] == 1
    assert d_a["total_late_minutes"] == 15

    # 2. Mid-month joiner EMP5002 (joined July 15 2026) -> working days counted from July 15 (13 days)
    res_b = test_client.get("/analytics/employees/EMP5002/monthly?month=2026-07")
    assert res_b.status_code == 200
    d_b = res_b.json()
    assert d_b["working_days"] == 13
    assert d_b["present_days"] == 1.0

    # 3. Post-month joiner EMP5003 (joined Aug 1) -> 200 with 0 working days and null pct (A6)
    res_c = test_client.get("/analytics/employees/EMP5003/monthly?month=2026-07")
    assert res_c.status_code == 200
    d_c = res_c.json()
    assert d_c["working_days"] == 0
    assert d_c["attendance_pct"] is None


def test_department_summary_analytics(test_client, clean_db):
    seed_analytics_data(test_client)

    res = test_client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) == 1
    item = data["items"][0]
    # Headcount includes EMP5001, EMP5002, and zero-log EMP5004 (joined <= July 31), excludes EMP5003 (joined Aug 1) -> 3!
    assert item["headcount"] == 3
    assert item["present_days"] == 2.5 # EMP5001 (1.5) + EMP5002 (1.0)
    assert item["late_count"] == 2


def test_late_leaderboard_ranking_and_ties(test_client, clean_db):
    seed_analytics_data(test_client)

    # EMP5001 and EMP5002 both have exactly 15 late minutes
    res = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    data = res.json()
    items = data["items"]
    assert len(items) == 2
    # Both share rank 1 per standard competition ranking ($rank)
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 1
    # Sorted deterministically by emp_code: EMP5001 before EMP5002
    assert items[0]["emp_code"] == "EMP5001"
    assert items[1]["emp_code"] == "EMP5002"


def test_department_trend_gap_filling_and_moving_average(test_client, clean_db):
    seed_analytics_data(test_client)

    # 7 days trend: 2026-07-01 to 2026-07-07
    # Exactly 7 rows returned via $densify
    res = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
    assert res.status_code == 200
    data = res.json()
    items = data["items"]
    assert len(items) == 7
    # Verify continuity of dates
    assert [row["date"] for row in items] == [
        "2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04",
        "2026-07-05", "2026-07-06", "2026-07-07"
    ]
    # Verify moving average is present
    assert "moving_avg_7d" in items[-1]

    # Validation: range > 92 days -> 422
    res_large = test_client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01")
    assert res_large.status_code == 422

    # Validation: to < from -> 422
    res_rev = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
    assert res_rev.status_code == 422
