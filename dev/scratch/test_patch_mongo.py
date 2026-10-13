from pymongo import MongoClient

c = MongoClient("mongodb://localhost:27017")
coll = c["attendance_db"]["scratch_test"]
coll.drop()
doc_id = coll.insert_one({"k": 1, "history": []}).inserted_id
res = coll.find_one_and_update(
    {"_id": doc_id, "history": []},
    {"$set": {"k": 2}, "$push": {"history": {"x": 1}}},
    return_document=True
)
print("Updated successfully:", res)

res2 = coll.find_one_and_update(
    {"_id": doc_id, "history": []},
    {"$set": {"k": 3}, "$push": {"history": {"x": 2}}},
    return_document=True
)
print("Second update with stale guard:", res2)
coll.drop()
