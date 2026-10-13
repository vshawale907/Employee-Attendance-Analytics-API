"""
tests/conftest.py
Shared test fixtures and database setup for pytest.
Runs against isolated attendance_test database per Safety Rule #7.
"""
import os
import pytest
from fastapi.testclient import TestClient

# Configure test database before importing main app
os.environ["MONGO_DB"] = "attendance_test"

import app.main as main_module
from app.main import app, client, init_indexes

# Ensure main_module.db points to attendance_test
test_db = client["attendance_test"]
main_module.db = test_db

@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """Initializes test database indexes and clears test collections."""
    test_db.employees.delete_many({})
    test_db.attendance_logs.delete_many({})
    init_indexes()
    yield
    # Cleanup after all tests finish
    test_db.employees.delete_many({})
    test_db.attendance_logs.delete_many({})


@pytest.fixture(scope="function")
def clean_db():
    """Cleans test collections before each test for test isolation."""
    test_db.employees.delete_many({})
    test_db.attendance_logs.delete_many({})
    yield
    test_db.employees.delete_many({})
    test_db.attendance_logs.delete_many({})


@pytest.fixture(scope="session")
def test_client():
    """Provides a TestClient instance for API requests."""
    return TestClient(app)
