#!/bin/bash
# ──────────────────────────────────────────────────────────────
#  run_all.sh  —  Start MongoDB + Scrape all pages + Insert data
#  Usage:
#    ./run_all.sh                        # scrape all pages to MongoDB
#    ./run_all.sh --pages 50             # limit pages
#    ./run_all.sh --mongo-host 1.2.3.4  # remote MongoDB server
# ──────────────────────────────────────────────────────────────

set -e

# ─── CONFIG (edit as needed) ───────────────────────────────────
PAGES=1000
DELAY=1.5
MONGO_HOST="localhost"
MONGO_PORT=27018
MONGO_DB="wabetainfo"
MONGO_COL="articles"
OUTPUT="wabetainfo_data.json"
VENV="./venv"
# ──────────────────────────────────────────────────────────────

# Parse CLI overrides
while [[ "$#" -gt 0 ]]; do
  case $1 in
    --pages)        PAGES="$2";       shift ;;
    --mongo-host)   MONGO_HOST="$2";  shift ;;
    --mongo-port)   MONGO_PORT="$2";  shift ;;
    --delay)        DELAY="$2";       shift ;;
    --output)       OUTPUT="$2";      shift ;;
    *) echo "Unknown param: $1"; exit 1 ;;
  esac
  shift
done

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  WABetaInfo Full Pipeline"
echo "  Pages      : $PAGES"
echo "  Delay      : ${DELAY}s"
echo "  MongoDB    : $MONGO_HOST:$MONGO_PORT/$MONGO_DB.$MONGO_COL"
echo "  Output     : $OUTPUT"
echo "════════════════════════════════════════════════════════════"
echo ""

# ─── STEP 1: Activate venv ─────────────────────────────────────
echo "[1/4] Activating virtual environment..."
if [ -f "$VENV/bin/activate" ]; then
  source "$VENV/bin/activate"
  echo "      ✓ venv activated"
else
  echo "      ! venv not found — using system Python"
fi

# ─── STEP 2: Start MongoDB via Docker (only if localhost) ───────
if [ "$MONGO_HOST" = "localhost" ] || [ "$MONGO_HOST" = "127.0.0.1" ]; then
  echo ""
  echo "[2/4] Starting MongoDB Docker on port $MONGO_PORT..."

  if docker ps --format '{{.Names}}' | grep -q "wabetainfo-mongo"; then
    echo "      ✓ Container already running"
  else
    docker compose -f docker-compose.mongo.yml up -d
    echo "      Waiting for MongoDB to be ready..."
    sleep 5
  fi

  # Verify MongoDB is up
  MAX_TRIES=10
  COUNT=0
  until docker exec wabetainfo-mongo mongosh --eval "db.adminCommand('ping')" --quiet &>/dev/null; do
    COUNT=$((COUNT+1))
    if [ $COUNT -ge $MAX_TRIES ]; then
      echo "      ✗ MongoDB didn't start in time. Aborting."
      exit 1
    fi
    echo "      Waiting... ($COUNT/$MAX_TRIES)"
    sleep 2
  done
  echo "      ✓ MongoDB is ready"
else
  echo ""
  echo "[2/4] Using remote MongoDB at $MONGO_HOST:$MONGO_PORT (skipping Docker)"
fi

# ─── STEP 3: Insert existing JSON if present ───────────────────
echo ""
# Skipping JSON upsert: directly insert into MongoDB via scraper
# echo "\n[*] Checking for existing data file..."
# if [ -f "$OUTPUT" ]; then
#   EXISTING=$(python3 -c "import json; d=json.load(open('$OUTPUT')); print(len(d.get('articles', [])))" 2>/dev/null || echo "0")
#   if [ "$EXISTING" -gt "0" ]; then
#     echo "      Found $EXISTING articles in $OUTPUT — upserting into MongoDB..."
#     python insert_mongo.py \
#       --input "$OUTPUT" \
#       --host "$MONGO_HOST" \
#       --port "$MONGO_PORT" \
#       --db "$MONGO_DB" \
#       --col "$MONGO_COL" \
#       --upsert
#   else
#     echo "      No existing articles found, skipping."
#   fi
# else
#   echo "      No existing file, starting fresh."
# fi

# ─── STEP 4: Crawl + auto-insert into MongoDB ──────────────────
echo ""
echo "[4/4] Starting scraper (crawl $PAGES pages + real-time MongoDB insert)..."
echo "      Press Ctrl+C to stop anytime — data already saved will stay in DB."
echo ""

python scrape-wabeta.py \
  --pages "$PAGES" \
  --delay "$DELAY" \
  --output "$OUTPUT" \
  --mongo \
  --mongo-host "$MONGO_HOST" \
  --mongo-port "$MONGO_PORT" \
  --mongo-db "$MONGO_DB" \
  --mongo-col "$MONGO_COL"

# ─── DONE ──────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
echo "  ✓  Pipeline complete!"
echo "  JSON   : $OUTPUT"
echo "  MongoDB: $MONGO_HOST:$MONGO_PORT/$MONGO_DB.$MONGO_COL"
if [ "$MONGO_HOST" = "localhost" ] || [ "$MONGO_HOST" = "127.0.0.1" ]; then
  echo "  UI     : http://localhost:27019  (Mongo Express)"
fi
echo "════════════════════════════════════════════════════════════"
echo ""
