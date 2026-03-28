#!/usr/bin/env python3
"""
NFL Scout AI — Setup
Scrapes 2026 draft prospects, embeds them, and rebuilds ChromaDB.

Steps:
  1. Walter Football big board + scouting reports (scraper.py)
  2. NFLDraftBuzz top-12 per position — profiles, traits, strengths/weaknesses
     (scrape_nfb_profiles.py)
  3. Embed all prospects and rebuild ChromaDB (ingest.py)

Run:  python3 setup.py
"""

import json
import os
import sys
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))


def run(label: str, *cmd) -> bool:
    print(f"\n{'─' * 62}")
    print(f"  {label}")
    print(f"{'─' * 62}")
    result = subprocess.run(list(cmd), cwd=BASE)
    return result.returncode == 0


def main():
    print("=" * 62)
    print("   NFL Scout AI — Full Setup (2026 Draft Class)")
    print("=" * 62)

    env_path = os.path.join(BASE, ".env")
    if not os.path.exists(env_path):
        print("\n  No .env file found.")
        print("  Create one before starting the server:")
        print("    echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env\n")

    os.makedirs(os.path.join(BASE, "data"),   exist_ok=True)
    os.makedirs(os.path.join(BASE, "static"), exist_ok=True)

    # ── Step 1: Walter Football big board + scouting reports ─────────────────
    ok = run(
        "Step 1 / 3 — Walter Football big board + scouting reports",
        sys.executable, "scraper.py",
    )
    if not ok:
        print("\n  Scraper failed. Check output above.")
        sys.exit(1)

    data_file = os.path.join(BASE, "data", "prospects.json")
    if not os.path.exists(data_file):
        print("\n  data/prospects.json was not created.")
        sys.exit(1)

    with open(data_file) as f:
        count = len(json.load(f))
    print(f"\n  {count} prospects saved after Step 1")

    # ── Step 2: NFLDraftBuzz top-12 per position (profiles + traits) ──────────
    # Positions scraped: QB RB WR TE OT IOL DE DT LB CB S
    # Uses Playwright headless=False to bypass Cloudflare.
    # Pass --skip-ingest so we control ingest ourselves in Step 3.
    ok = run(
        "Step 2 / 3 — NFLDraftBuzz: top-12 per position "
        "(bio, traits, strengths, weaknesses, comparisons)",
        sys.executable, "scrape_nfb_profiles.py", "--skip-ingest",
    )
    if not ok:
        print("\n  NFLDraftBuzz scrape failed. Check output above.")
        print("  Continuing to ingest whatever data was collected …")

    with open(data_file) as f:
        count = len(json.load(f))
    print(f"\n  {count} total prospects after merging NFLDraftBuzz profiles")

    # ── Step 3: Embed and rebuild ChromaDB ────────────────────────────────────
    ok = run(
        "Step 3 / 3 — Embedding all prospects into ChromaDB",
        sys.executable, "ingest.py",
    )
    if not ok:
        print("\n  Ingestion failed. Check output above.")
        sys.exit(1)

    print("\n" + "=" * 62)
    print("   Setup complete!")
    print("=" * 62)
    print()
    print("Start the server:")
    print("    uvicorn main:app --reload")
    print()
    print("Open in browser:")
    print("    http://localhost:8000")
    print()


if __name__ == "__main__":
    main()
