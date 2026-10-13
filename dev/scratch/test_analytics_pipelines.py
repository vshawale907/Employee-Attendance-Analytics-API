"""
dev/scratch/test_analytics_pipelines.py
Test MongoDB aggregation pipelines for the four analytics endpoints.
"""
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pymongo import MongoClient

c = MongoClient("mongodb://localhost:27017")
db = c["attendance_db"]

print("Connected to MongoDB:", c.server_info()["version"])
print("Employees count:", db.employees.count_documents({}))
print("Attendance logs count:", db.attendance_logs.count_documents({}))

# ---------------------------------------------------------------------------
# Test 1: Employee Monthly
# ---------------------------------------------------------------------------
print("\n--- Test 1: Employee Monthly Pipeline ---")
emp_code = "EMP0001"
month = "2026-07"
start_date = f"{month}-01"
end_date = f"{month}-31"

pipeline_monthly = [
    {
        "$match": {
            "emp_code": emp_code,
            "date": {"$gte": start_date, "$lte": end_date}
        }
    },
    {
        "$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                # Mon-Fri check: $isoDayOfWeek between 1 and 5
                                {
                                    "$in": [
                                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                        [1, 2, 3, 4, 5]
                                    ]
                                }
                            ]
                        },
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            },
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"}
        }
    }
]

res = list(db.attendance_logs.aggregate(pipeline_monthly))
print("Monthly result for EMP0001:", res)

# ---------------------------------------------------------------------------
# Test 2: Leaderboard Late Pipeline
# ---------------------------------------------------------------------------
print("\n--- Test 2: Leaderboard Late Pipeline ---")
pipeline_leaderboard = [
    {
        "$match": {
            "date": {"$gte": start_date, "$lte": end_date},
            "late_minutes": {"$gt": 0}
        }
    },
    {
        "$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }
    },
    {
        "$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp"
        }
    },
    {"$unwind": "$emp"},
    {
        "$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1
        }
    },
    {
        "$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            }
        }
    },
    {"$match": {"rank": {"$lte": 10}}},
    {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
]

res_lb = list(db.attendance_logs.aggregate(pipeline_leaderboard))
print("Leaderboard result:", res_lb)

print("\nScratch tests finished successfully!")
