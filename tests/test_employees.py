"""
tests/test_employees.py
Tests for employee management endpoints:
- POST /employees
- GET /employees
"""
def test_create_employee_success(test_client, clean_db):
    payload = {
        "emp_code": "EMP0101",
        "name": "Alice Green",
        "department": "Engineering",
        "email": "alice.green@hrone.test",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-01-01"
    }
    response = test_client.post("/employees", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["emp_code"] == "EMP0101"
    assert data["name"] == "Alice Green"
    assert "_id" not in data
    assert isinstance(data["created_at"], int)


def test_create_duplicate_employee_conflict(test_client, clean_db):
    payload = {
        "emp_code": "EMP0102",
        "name": "Bob Blue",
        "department": "Sales",
        "email": "bob.blue@hrone.test",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-01-01"
    }
    res1 = test_client.post("/employees", json=payload)
    assert res1.status_code == 201

    res2 = test_client.post("/employees", json=payload)
    assert res2.status_code == 409
    assert "already exists" in res2.json()["detail"]


def test_create_employee_validation_errors(test_client, clean_db):
    # Invalid emp_code regex
    bad_code = {
        "emp_code": "INVALID_CODE",
        "name": "Charlie",
        "department": "Sales",
        "email": "charlie@hrone.test",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-01-01"
    }
    res = test_client.post("/employees", json=bad_code)
    assert res.status_code == 422

    # Invalid email
    bad_email = {
        "emp_code": "EMP0103",
        "name": "Charlie",
        "department": "Sales",
        "email": "invalid-email",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-01-01"
    }
    res = test_client.post("/employees", json=bad_email)
    assert res.status_code == 422

    # Invalid shift time format
    bad_shift = {
        "emp_code": "EMP0104",
        "name": "Dave",
        "department": "Sales",
        "email": "dave@hrone.test",
        "shift_start": "9:00",
        "shift_end": "18:00",
        "joined_on": "2026-01-01"
    }
    res = test_client.post("/employees", json=bad_shift)
    assert res.status_code == 422

    # Invalid joined_on date
    bad_date = {
        "emp_code": "EMP0105",
        "name": "Eve",
        "department": "Sales",
        "email": "eve@hrone.test",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-02-31"
    }
    res = test_client.post("/employees", json=bad_date)
    assert res.status_code == 422


def test_list_employees_pagination_and_filtering(test_client, clean_db):
    # Seed 3 Engineering employees (EMP1001-1003) and 2 Sales employees (EMP2001-2002)
    for i in range(1, 4):
        test_client.post("/employees", json={
            "emp_code": f"EMP100{i}",
            "name": f"Engineer {i}",
            "department": "Engineering",
            "email": f"eng{i}@hrone.test",
            "shift_start": "09:00",
            "shift_end": "18:00",
            "joined_on": "2026-01-01"
        })
    for i in range(1, 3):
        test_client.post("/employees", json={
            "emp_code": f"EMP200{i}",
            "name": f"Sales {i}",
            "department": "Sales",
            "email": f"sal{i}@hrone.test",
            "shift_start": "09:00",
            "shift_end": "18:00",
            "joined_on": "2026-01-01"
        })

    # Test full list
    res_all = test_client.get("/employees?page=1&page_size=10")
    assert res_all.status_code == 200
    data_all = res_all.json()
    assert data_all["total"] == 5
    assert len(data_all["items"]) == 5

    # Test department filter
    res_eng = test_client.get("/employees?department=Engineering")
    assert res_eng.status_code == 200
    data_eng = res_eng.json()
    assert data_eng["total"] == 3
    assert all(e["department"] == "Engineering" for e in data_eng["items"])

    # Test pagination (page 1, page_size 2)
    res_p1 = test_client.get("/employees?page=1&page_size=2")
    assert res_p1.status_code == 200
    data_p1 = res_p1.json()
    assert len(data_p1["items"]) == 2
    assert data_p1["items"][0]["emp_code"] == "EMP1001"
    assert data_p1["items"][1]["emp_code"] == "EMP1002"

    # Test pagination page 2 (verifies 0-indexed skip offset)
    res_p2 = test_client.get("/employees?page=2&page_size=2")
    assert res_p2.status_code == 200
    data_p2 = res_p2.json()
    assert len(data_p2["items"]) == 2
    assert data_p2["items"][0]["emp_code"] == "EMP1003"
    assert data_p2["items"][1]["emp_code"] == "EMP2001"
