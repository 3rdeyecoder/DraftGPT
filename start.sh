#!/bin/bash
set -e

# Check if ChromaDB collection exists and has prospects.
# If missing or empty, run ingest.py to build it from data/prospects.json.
python3 - <<'EOF'
import sys
import chromadb

try:
    client = chromadb.PersistentClient(path="./chroma_db")
    col = client.get_collection("nfl_prospects_2026")
    count = col.count()
    if count > 0:
        print(f"ChromaDB ready: {count} prospects loaded.")
        sys.exit(0)
    else:
        print("Collection exists but is empty — rebuilding.")
        sys.exit(1)
except Exception as e:
    print(f"Collection not found ({e}) — running ingest.")
    sys.exit(1)
EOF

if [ $? -ne 0 ]; then
    echo "Running ingest.py..."
    python3 ingest.py
fi

exec python3 -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
