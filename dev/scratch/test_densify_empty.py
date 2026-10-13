"""
dev/scratch/test_densify_empty.py
Test $densify when initial input is empty.
"""
from datetime import datetime, timezone, timedelta
from pymongo import MongoClient

c = MongoClient("mongodb://localhost:27017")
db = c["attendance_db"]

from_dt = datetime(2026, 7, 1, tzinfo=timezone.utc)
to_dt_excl = datetime(2026, 7, 5, tzinfo=timezone.utc)

res = list(db.attendance_logs.aggregate([
    {"$match": {"emp_code": "NON_EXISTENT_CODE"}},
    {
        "$densify": {
            "field": "date_dt",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_dt_excl]
            }
        }
    }
]))
print("Densify on empty match returned:", len(res), "documents")
