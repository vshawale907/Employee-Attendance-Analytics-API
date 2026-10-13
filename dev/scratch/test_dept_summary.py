"""
dev/scratch/test_dept_summary.py
Test department summary pipeline across employees and attendance_logs.
"""
from pymongo import MongoClient

c = MongoClient("mongodb://localhost:27017")
db = c["attendance_db"]

month = "2026-07"
start_date = f"{month}-01"
end_date = f"{month}-31"

pipeline = [
    # 1. Match employees who joined on or before month end
    {
        "$match": {
            "joined_on": {"$lte": end_date}
        }
    },
    # 2. Group by department to get headcount and list of employee codes
    {
        "$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "emp_codes": {"$push": "$emp_code"}
        }
    },
    # 3. Lookup attendance logs for these employees within the month
    {
        "$lookup": {
            "from": "attendance_logs",
            "let": {"dept_emps": "$emp_codes"},
            "pipeline": [
                {
                    "$match": {
                        "$expr": {
                            "$and": [
                                {"$in": ["$emp_code", "$$dept_emps"]},
                                {"$gte": ["$date", start_date]},
                                {"$lte": ["$date", end_date]}
                            ]
                        }
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
                        "work_hours_list": {
                            "$push": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$work_hours", None]}
                                        ]
                                    },
                                    "$work_hours",
                                    "$$REMOVE"
                                ]
                            }
                        },
                        "late_count": {
                            "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                        },
                        "total_late_minutes": {"$sum": "$late_minutes"},
                        "leave_count": {
                            "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                        },
                        "on_duty_count": {
                            "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                        }
                    }
                }
            ],
            "as": "logs_summary"
        }
    },
    # 4. Project clean fields
    {
        "$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": {
                "$ifNull": [{"$first": "$logs_summary.present_days"}, 0.0]
            },
            "avg_work_hours": {
                "$let": {
                    "vars": {"hours": {"$ifNull": [{"$first": "$logs_summary.work_hours_list"}, []]}},
                    "in": {
                        "$cond": [
                            {"$gt": [{"$size": "$$hours"}, 0]},
                            {"$avg": "$$hours"},
                            None
                        ]
                    }
                }
            },
            "late_count": {
                "$ifNull": [{"$first": "$logs_summary.late_count"}, 0]
            },
            "total_late_minutes": {
                "$ifNull": [{"$first": "$logs_summary.total_late_minutes"}, 0]
            },
            "leave_count": {
                "$ifNull": [{"$first": "$logs_summary.leave_count"}, 0]
            },
            "on_duty_count": {
                "$ifNull": [{"$first": "$logs_summary.on_duty_count"}, 0]
            }
        }
    },
    {"$sort": {"department": 1}}
]

res = list(db.employees.aggregate(pipeline))
print("Department summary result:")
for row in res:
    print(row)
