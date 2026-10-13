"""
dev/scratch/test_densify.py
Test MongoDB $densify for gap-filling daily date series.
"""
from datetime import datetime, timezone
from pymongo import MongoClient

c = MongoClient("mongodb://localhost:27017")
db = c["attendance_db"]

coll = db["densify_scratch"]
coll.drop()

# Seed two dates with a gap
coll.insert_many([
    {"d": datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc), "val": 10},
    {"d": datetime(2026, 7, 5, 0, 0, 0, tzinfo=timezone.utc), "val": 50},
])

from_dt = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
to_dt = datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc) # exclusive upper bound: July 8
to_dt_excl = datetime(2026, 7, 8, 0, 0, 0, tzinfo=timezone.utc)

pipeline = [
    {
        "$densify": {
            "field": "d",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_dt_excl]
            }
        }
    },
    {"$sort": {"d": 1}}
]

docs = list(coll.aggregate(pipeline))
print(f"Densify returned {len(docs)} docs:")
for doc in docs:
    print(" ", doc["d"].strftime("%Y-%m-%d"), doc.get("val"))

coll.drop()
