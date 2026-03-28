"""
Patch script: re-scrape EDGE and DT position pages from NFLDraftBuzz
with the corrected _POS_CODES (now includes "DL") so that players whose
href slugs use "DL" (e.g. /Player/Rueben-BainJr-DL-Miami) get clean names
and merge correctly into data/prospects.json.

Run:  python3 patch_edge_dt.py
"""

import importlib.util, sys, os

# Load scrape_nfb_profiles as a module
spec = importlib.util.spec_from_file_location(
    "snp", os.path.join(os.path.dirname(__file__), "scrape_nfb_profiles.py")
)
snp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snp)

import json, time, re, unicodedata
from playwright.sync_api import sync_playwright

TARGET_POSITIONS = ["EDGE", "DT"]
TOP_N = 12

print("=" * 62)
print("  EDGE / DT Profile Patch — NFLDraftBuzz 2026")
print("=" * 62)

all_profiles = {}
href_queue = {}

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--window-size=1280,900",
        ],
    )
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="America/New_York",
    )
    ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); "
        "window.chrome = { runtime: {} };"
    )
    page = ctx.new_page()

    print("\n── Phase 1: Collecting player hrefs ────────────────────────")
    for pos in TARGET_POSITIONS:
        entries = snp.collect_hrefs_for_position(page, pos)
        print(f"  {pos}: {len(entries)} players")
        for e in entries:
            if e["href"] not in href_queue:
                href_queue[e["href"]] = e
        time.sleep(1)

    print(f"\n  Total unique player pages: {len(href_queue)}")

    print("\n── Phase 2: Scraping individual profiles ───────────────────")
    for i, (href, meta) in enumerate(href_queue.items(), 1):
        url = snp.NFB_BASE + href
        name = meta["name"]
        print(f"  [{i:>3}/{len(href_queue)}] {name} ({meta['pos']}) …", end=" ", flush=True)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            time.sleep(2.5)

            if "just a moment" in page.title().lower():
                print("BLOCKED")
                continue

            profile = snp.parse_profile(page.content(), meta["pos"])
            profile["nfb_href"]     = href
            profile["nfb_pos_rank"] = meta.get("nfb_pos_rank", "")
            profile["source_pos"]   = meta["pos"]
            profile.setdefault("name", name)
            profile.setdefault("school", meta.get("school", ""))

            key = snp.nn(name)
            all_profiles[key] = profile
            print(f"✓  (traits:{len(profile.get('nfb_trait_scores', {}))}, "
                  f"strengths:{'yes' if profile.get('strengths') else 'no'})")

        except Exception as e:
            print(f"ERROR: {e}")

        time.sleep(0.5)

    browser.close()

print(f"\nScraped {len(all_profiles)} EDGE/DT profiles")
snp.merge_into_prospects(all_profiles)

print("\nRe-ingesting into ChromaDB …")
import subprocess
result = subprocess.run([sys.executable, "ingest.py"], cwd=".")
if result.returncode == 0:
    print("\n✅  Done! ChromaDB updated with corrected EDGE/DT profiles.")
else:
    print("\n❌  Ingest failed — check output above.")
