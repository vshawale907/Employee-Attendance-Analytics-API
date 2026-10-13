"""
dev/scratch/test_trend_pipeline.py
Test department trend aggregation pipeline with $densify, $setWindowFields, and $avg.
"""
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal, ROUND_HALF_UP
from pymongo import MongoClient

c = MongoClient("mongodb://localhost:27017")
db = c["attendance_db"]

department = "Engineering"
from_str = "2026-07-01"
to_str = "2026-07-10"

from_dt = datetime.fromisoformat(from_str).replace(tzinfo=timezone.utc)
to_dt = datetime.fromisoformat(to_str).replace(tzinfo=timezone.utc)
to_dt_excl = to_dt + timedelta(days=1)

# Check department exists
dept_exists = db.employees.count_documents({"department": department}) > 0
print("Dept exists:", dept_exists)

# Get all employees of department with their joined_on
dept_employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
emp_codes = [e["emp_code"] for e in dept_employees]
print(f"Dept {department} employees:", dept_employees)

# Build pipeline on attendance_logs
# First: group attendance_logs by date
pipeline = [
    # 1. Match logs for department employees in the date range
    {
        "$match": {
            "emp_code": {"$in": emp_codes},
            "date": {"$gte": from_str, "$lte": to_str}
        }
    },
    # 2. Group by date to get daily present_count and late_count
    {
        "$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            }
        }
    },
    # 3. Project date as datetime for $densify
    {
        "$project": {
            "_id": 0,
            "date_str": "$_id",
            "date_dt": {"$dateFromString": {"dateString": "$_id"}},
            "present_count": 1,
            "late_count": 1
        }
    },
    # 4. Densify gap days across [from_dt, to_dt_excl)
    {
        "$densify": {
            "field": "date_dt",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_dt_excl]
            }
        }
    },
    # 5. Populate defaults for densified empty days
    {
        "$project": {
            "date_dt": 1,
            "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
            "present_count": {"$ifNull": ["$present_count", 0.0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            # Add department employees list into document to calculate daily headcount
            "dept_emps": {"$literal": dept_employees}
        }
    },
    # 6. Calculate is_working_day and headcount
    {
        "$project": {
            "date": "$date_str",
            "date_dt": 1,
            "is_working_day": {
                "$in": [{"$isoDayOfWeek": "$date_dt"}, [1, 2, 3, 4, 5]]
            },
            "headcount": {
                "$size": {
                    "$filter": {
                        "input": "$dept_emps",
                        "as": "emp",
                        "cond": {"$lte": ["$$emp.joined_on", "$date_str"]}
                    }
                }
            },
            "present_count": 1,
            "late_count": 1
        }
    },
    # 7. Sort by date_dt ascending
    {"$sort": {"date_dt": 1}}
]

days = list(db.attendance_logs.aggregate(pipeline))
print(f"Pipeline produced {len(days)} days (expected 10):")
for d in days:
    print(d["date"], "is_work:", d["is_working_day"], "headcount:", d["headcount"], "present:", d["present_count"], "late:", d["late_count"])
