"""
tests/test_attendance_list.py
Tests for GET /attendance listing, filtering, and pagination.
"""
from datetime import datetime

def dt_to_epoch_ms(dt_str: str) -> int:
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


def test_attendance_list_filtering_and_pagination(test_client, clean_db):
    # Setup employee
    res = test_client.post("/employees", json={
        "emp_code": "EMP4001",
        "name": "List Tester",
        "department": "HR",
        "email": "list.tester@hrone.test",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 201

    # Create 3 logs across 3 consecutive days
    days = ["2026-07-01", "2026-07-02", "2026-07-03"]
    for day in days:
        t_in = dt_to_epoch_ms(f"{day}T03:30:00Z")
        t_out = dt_to_epoch_ms(f"{day}T12:30:00Z")
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP4001", "punched_at": t_in})
        test_client.post("/attendance/punch-out", json={"emp_code": "EMP4001", "punched_at": t_out})

    # 1. Total count and pagination
    res = test_client.get("/attendance?emp_code=EMP4001&page=1&page_size=2")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 3
    assert len(data["items"]) == 2
    # Verify stable sort: date descending
    assert data["items"][0]["date"] == "2026-07-03"
    assert data["items"][1]["date"] == "2026-07-02"
    assert "_id" not in data["items"][0]

    # 2. Date range filter
    res_range = test_client.get("/attendance?emp_code=EMP4001&date_from=2026-07-01&date_to=2026-07-02")
    assert res_range.status_code == 200
    d_range = res_range.json()
    assert d_range["total"] == 2
    assert [d["date"] for d in d_range["items"]] == ["2026-07-02", "2026-07-01"]

    # 3. 422 on date_from > date_to
    res_err = test_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert res_err.status_code == 422
