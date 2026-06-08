"""One-shot backfill: stamp tenant_id/user_id/visibility into existing mongo analysis
docs from their Postgres task. Run once when enabling multitenancy on a populated DB."""
import sys


def backfill_doc(doc, view_task) -> dict:
    task = view_task(int(doc.get("info", {}).get("id", 0)))
    if task is None:
        return {"info.tenant_id": None, "info.user_id": None, "info.visibility": "public"}
    return {
        "info.tenant_id": getattr(task, "tenant_id", None),
        "info.user_id": getattr(task, "user_id", None),
        "info.visibility": getattr(task, "visibility", "public") or "public",
    }


def main():
    sys.path.insert(0, "/opt/CAPEv2")
    from dev_utils.mongodb import mongo_find, mongo_update_one
    from lib.cuckoo.core.database import Database, init_database
    try:
        init_database()
    except Exception:
        pass
    db = Database()
    n = 0
    for doc in mongo_find("analysis", {"info.visibility": {"$exists": False}}, {"info.id": 1}):
        mongo_update_one("analysis", {"_id": doc["_id"]}, {"$set": backfill_doc(doc, db.view_task)})
        n += 1
    print(f"backfilled {n} docs")


if __name__ == "__main__":
    main()
