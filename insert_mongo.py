#!/usr/bin/env python3
"""
Insert wabetainfo_data.json into MongoDB.
Usage:
    python insert_mongo.py                          # insert default file
    python insert_mongo.py --input full_articles.json
    python insert_mongo.py --host localhost --port 27018
    python insert_mongo.py --upsert                 # update existing docs
"""

import json
import argparse
import sys
from datetime import datetime, timezone

try:
    # pyrefly: ignore [missing-import]
    from pymongo import MongoClient, UpdateOne, ASCENDING
    # pyrefly: ignore [missing-import]
    from pymongo.errors import BulkWriteError
except ImportError:
    print("[!] pymongo not installed. Run: pip install pymongo")
    sys.exit(1)


# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 27018
DEFAULT_DB   = "wabetainfo"
DEFAULT_COL  = "articles"


# ─────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────

def connect(host, port, db_name, col_name):
    client = MongoClient(host=host, port=port, serverSelectionTimeoutMS=5000)
    # Test connection
    client.admin.command("ping")
    db  = client[db_name]
    col = db[col_name]
    return client, col


def ensure_indexes(col):
    """Create useful indexes once."""
    col.create_index("id",         unique=True,  name="idx_id")
    col.create_index("slug",       unique=False, name="idx_slug")
    col.create_index("date",                     name="idx_date")
    col.create_index("categories",               name="idx_categories")
    col.create_index([("title", ASCENDING)],     name="idx_title")
    print("[✓] Indexes ensured")


def prepare_doc(article: dict) -> dict:
    """Map article dict to a MongoDB document."""
    doc = dict(article)
    # Use the deterministic UUID as _id so re-inserts are idempotent
    doc["_id"]        = doc.get("id", doc.get("slug", ""))
    doc["_inserted_at"] = datetime.now(timezone.utc).isoformat()
    return doc


# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Insert wabetainfo JSON → MongoDB")
    parser.add_argument("--input",  default="wabetainfo_data.json", help="Input JSON file")
    parser.add_argument("--host",   default=DEFAULT_HOST)
    parser.add_argument("--port",   type=int, default=DEFAULT_PORT)
    parser.add_argument("--db",     default=DEFAULT_DB,  help="Database name")
    parser.add_argument("--col",    default=DEFAULT_COL, help="Collection name")
    parser.add_argument("--upsert", action="store_true", help="Upsert (update) existing docs")
    args = parser.parse_args()

    # ── Load JSON ──
    print(f"[*] Loading {args.input}...")
    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    articles = data.get("articles", data) if isinstance(data, dict) else data
    meta     = data.get("scrape_metadata", {}) if isinstance(data, dict) else {}
    print(f"[*] {len(articles)} articles loaded")
    if meta:
        print(f"    Source   : {meta.get('source', '?')}")
        print(f"    Scraped  : {meta.get('scraped_at', '?')[:19]}")
        print(f"    Pages    : {meta.get('pages_crawled', '?')}")

    # ── Connect ──
    print(f"\n[*] Connecting to MongoDB {args.host}:{args.port}...")
    try:
        client, col = connect(args.host, args.port, args.db, args.col)
        print(f"[✓] Connected → db={args.db}  collection={args.col}")
    except Exception as e:
        print(f"[!] Connection failed: {e}")
        print("    → Make sure MongoDB is running on port", args.port)
        sys.exit(1)

    ensure_indexes(col)

    # ── Insert / Upsert ──
    docs = [prepare_doc(a) for a in articles]

    if args.upsert:
        ops = [
            UpdateOne({"_id": d["_id"]}, {"$set": d}, upsert=True)
            for d in docs
        ]
        print(f"\n[*] Upserting {len(ops)} documents...")
        result = col.bulk_write(ops, ordered=False)
        print(f"[✓] Upserted  : {result.upserted_count}")
        print(f"[✓] Modified  : {result.modified_count}")
        print(f"[✓] Matched   : {result.matched_count}")
    else:
        print(f"\n[*] Inserting {len(docs)} documents (skipping duplicates)...")
        inserted = 0
        skipped  = 0
        for doc in docs:
            try:
                col.insert_one(doc)
                inserted += 1
            except Exception:
                skipped += 1
        print(f"[✓] Inserted  : {inserted}")
        print(f"[~] Skipped   : {skipped}  (already exist)")

    total = col.count_documents({})
    print(f"\n[✓] Total docs in collection '{args.col}': {total}")
    print(f"    → Mongo Express (if running): http://{args.host}:27019")

    client.close()


if __name__ == "__main__":
    main()
