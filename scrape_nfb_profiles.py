"""
Scrape NFLDraftBuzz player profile pages for top-32 players per position.
Extracts: bio, strengths, weaknesses, trait scores, measurables with
percentiles, draft projection, player comparisons, season stats, honors.
Merges all data into data/prospects.json and re-ingests into ChromaDB.

Run:  python3 scrape_nfb_profiles.py
"""

import json
import re
import time
import unicodedata
from typing import Dict, List, Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

NFB_BASE   = "https://www.nfldraftbuzz.com"
NFB_POS_URL = NFB_BASE + "/positions/{pos}/{page}/2026"
POSITIONS  = ["QB", "RB", "WR", "TE", "OT", "OG", "C", "DE", "DT", "LB", "CB", "S"]
TOP_N      = 32   # profiles to scrape per position


# ── helpers ────────────────────────────────────────────────────────────────

def nn(name: str) -> str:
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z ]", "", name.lower()).strip()


def clean_text(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


# ── position page: collect top-N player hrefs ─────────────────────────────

_POS_CODES = {"QB","RB","WR","TE","OT","OG","OL","IOL","C","EDGE","DT","LB","CB","S","K","P","DE","ED","G","DL"}

def _name_from_href(href: str) -> str:
    """Extract clean player name from /Player/First-Last-POS-School slug."""
    slug = href.rstrip("/").split("/")[-1]
    parts = slug.split("-")
    name_parts = []
    for part in parts:
        if part.upper() in _POS_CODES:
            break
        name_parts.append(part)
    return " ".join(name_parts)


def collect_hrefs_for_position(page, pos: str) -> List[Dict]:
    """Collect up to TOP_N player hrefs from the NFB position ranking pages.
    Paginates through /positions/{pos}/{page}/2026 until enough entries collected."""
    entries = []
    seen_hrefs = set()
    page_num = 1

    while len(entries) < TOP_N:
        url = NFB_POS_URL.format(pos=pos, page=page_num)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            time.sleep(3)
            if "just a moment" in page.title().lower():
                print(f"  [BLOCKED] {pos} page {page_num}")
                break
        except Exception as e:
            print(f"  [ERROR] {pos} page {page_num}: {e}")
            break

        soup = BeautifulSoup(page.content(), "lxml")
        table = soup.find("table")
        if not table:
            break

        rows = table.select("tbody tr")
        if not rows:
            break

        page_entries = []
        for row in rows:
            tds = row.find_all("td")
            if len(tds) < 5:
                continue
            link = tds[1].find("a", href=True)
            if not link:
                continue
            href = link["href"]
            if href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            name_raw = _name_from_href(href)

            school = ""
            school_img = tds[4].find("img", alt=True) if len(tds) > 4 else None
            if school_img:
                school = re.sub(r"\s*(Mascot|mascot).*", "", school_img.get("alt", "")).strip()

            pos_rank = tds[0].get_text(strip=True)

            page_entries.append({
                "name":         name_raw,
                "school":       school,
                "href":         href,
                "pos":          pos,
                "nfb_pos_rank": pos_rank,
            })

        if not page_entries:
            break  # no new entries — last page

        entries.extend(page_entries)
        page_num += 1
        time.sleep(1)

    return entries[:TOP_N]


# ── player profile page parser ────────────────────────────────────────────

def parse_profile(html: str, pos_hint: str = "") -> Dict:
    soup = BeautifulSoup(html, "lxml")
    data: Dict = {}

    # ── basic info ────────────────────────────────────────────────────────
    info_details = soup.find("div", class_="player-info-details")
    if info_details:
        for item in info_details.find_all("div", class_="player-info-details__item"):
            label_el = item.find("div", class_="player-info-details__title")
            value_el = item.find("div", class_="player-info-details__value")
            if label_el and value_el:
                label = clean_text(label_el.get_text())
                value = clean_text(value_el.get_text())
                key_map = {
                    "height": "height", "weight": "weight",
                    "college": "school", "position": "position",
                    "class": "year_class", "home town": "hometown",
                    "hometown": "hometown",
                }
                field = key_map.get(label.lower())
                if field:
                    data[field] = value

    # ── basic info table (jersey, play style, forty, draft year) ─────────
    basic_table = soup.find("table", class_="basicInfoTable")
    if basic_table:
        rows = basic_table.find_all("tr")
        for row in rows:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            cells = [c for c in cells if c and c not in ("0%", "100%")]
            for i in range(0, len(cells) - 1, 2):
                label = cells[i].lower().rstrip(":")
                value = cells[i + 1]
                if "jersey" in label:
                    data["jersey"] = value
                elif "play style" in label:
                    data["play_style"] = value
                elif "forty" in label or "40" in label:
                    m = re.search(r"[\d.]+", value)
                    if m:
                        data["forty_yard"] = m.group()
                elif "height" in label:
                    data["height"] = re.sub(r"\(.*?\)", "", value).strip()
                elif "weight" in label:
                    data["weight"] = re.sub(r"\(.*?\)", "", value).strip()
                elif "hands" in label:
                    data["hand_size"] = re.sub(r"\(.*?\)", "", value).strip()
                elif "arm" in label:
                    data["arm_length"] = re.sub(r"\(.*?\)", "", value).strip()

    # ── player stats (inline strip) ───────────────────────────────────────
    stats_div = soup.find("div", class_="player-info__item--stats-inner")
    if stats_div:
        raw = clean_text(stats_div.get_text(" "))
        # Parse key-value pairs like "QB Rating 130.4 YDS 3535 ..."
        tokens = raw.split()
        stat_map: Dict[str, str] = {}
        i = 0
        while i < len(tokens):
            # Check if next token is a number
            if i + 1 < len(tokens) and re.match(r"^[\d.%]+$", tokens[i + 1]):
                stat_map[tokens[i]] = tokens[i + 1]
                i += 2
            else:
                # Two-word label
                if i + 2 < len(tokens) and re.match(r"^[\d.%]+$", tokens[i + 2]):
                    stat_map[f"{tokens[i]} {tokens[i+1]}"] = tokens[i + 2]
                    i += 3
                else:
                    i += 1
        if stat_map:
            data["stats_2025"] = stat_map

    # ── bio ───────────────────────────────────────────────────────────────
    bio_div = soup.find("div", class_="playerDescIntro")
    if bio_div:
        bio_text = clean_text(bio_div.get_text(" "))
        # Strip the "Draft Profile: Bio" prefix
        bio_text = re.sub(r"^Draft Profile:\s*Bio\s*", "", bio_text)
        data["bio"] = bio_text

    # ── strengths & weaknesses ────────────────────────────────────────────
    # Both playerDescPro and playerDescNeg may contain strengths or weaknesses
    # NFB uses "Scouting Report: Strengths" and "Scouting Report: Weaknesses" headings
    for cls in ("playerDescPro", "playerDescNeg"):
        for div in soup.find_all("div", class_=cls):
            raw = clean_text(div.get_text(" "))
            if "strengths" in raw.lower()[:40]:
                data["strengths"] = re.sub(r"^Scouting Report:\s*Strengths\s*", "", raw)
            elif "weaknesses" in raw.lower()[:40]:
                data["weaknesses"] = re.sub(r"^Scouting Report:\s*Weaknesses\s*", "", raw)
            elif "honors" in raw.lower()[:40] or "award" in raw.lower()[:60]:
                data["honors"] = re.sub(r"^Honors\s*&\s*awards?\s*", "", raw, flags=re.IGNORECASE)

    # ── trait scores — find the starRatingTable that has "Overall Rating" ──
    # (there are multiple starRatingTable elements; only one has scout ratings)
    trait_scores: Dict[str, str] = {}
    draft_proj = ""

    ratings_table = None
    for tbl in soup.find_all("table", class_="starRatingTable"):
        if tbl.find(string=re.compile(r"Overall Rating", re.IGNORECASE)):
            ratings_table = tbl
            break

    if ratings_table:
        full_text = clean_text(ratings_table.get_text(" "))
        # Extract draft projection block
        m_proj = re.search(r"DRAFT PROJECTION[:\s]*([\w\s\-–]+?)(?:Overall|$)", full_text)
        if m_proj:
            data["projected_round"] = clean_text(m_proj.group(1))
        m_ovr = re.search(r"Overall Rank[:\s#]*(\d+)", full_text, re.IGNORECASE)
        if m_ovr:
            data["nfb_overall_rank"] = m_ovr.group(1)
        m_pos = re.search(r"Position rank[:\s#]*(\d+)", full_text, re.IGNORECASE)
        if m_pos:
            data["nfb_pos_rank"] = m_pos.group(1)

        # Parse row-by-row for trait scores
        for row in ratings_table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            cells = [c for c in cells if c and c not in ("0%", "100%")]
            if len(cells) < 2:
                continue
            label = cells[0].rstrip(":").strip()
            value = cells[-1].strip()

            if not label or len(label) > 60:
                continue
            # Skip noise rows
            if any(skip in label.lower() for skip in
                   ("click the links", "average rating", "draft projection",
                    "overall rank", "position rank", "0%", "100%")):
                continue

            if "overall rating" in label.lower():
                m = re.search(r"[\d.]+", value)
                if m:
                    trait_scores["Overall Rating"] = m.group()
            elif "defense rating" in label.lower():
                trait_scores["Defense Faced Rating"] = value
            elif re.search(r"%|\d+", value):
                # Position-specific trait (e.g. "Release Speed", "Short Passing", etc.)
                trait_scores[label] = value

    if trait_scores:
        data["nfb_trait_scores"] = trait_scores

    # ── player comparisons ────────────────────────────────────────────────
    comp_table = None
    for tbl in soup.find_all("table", class_="starRatingTable"):
        rows = tbl.find_all("tr")
        for row in rows:
            if "Player Comparison" in row.get_text():
                comp_table = tbl
                break
        if comp_table:
            break

    if comp_table:
        comps = []
        for row in comp_table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            cells = [c for c in cells if c]
            if len(cells) >= 2:
                comps.append(f"{cells[0]} ({cells[1]})")
        if comps:
            data["player_comparisons"] = ", ".join(comps)

    # ── consensus rank box ────────────────────────────────────────────────
    rank_box = soup.find("div", class_=lambda c: c and "rankingBox" in c)
    if rank_box:
        rb_text = clean_text(rank_box.get_text(" "))
        m_ovr = re.search(r"Average Overall Rank\s+([\d.]+)", rb_text)
        m_pos = re.search(r"Average Position Rank\s+([\d.]+)", rb_text)
        if m_ovr:
            data["nfb_avg_ovr_rank"] = m_ovr.group(1)
        if m_pos:
            data["nfb_avg_pos_rank"] = m_pos.group(1)

    return data


# ── main scraping loop ────────────────────────────────────────────────────

def scrape_all_profiles() -> Dict[str, Dict]:
    """
    Returns a dict keyed by normalized player name → profile data dict.
    """
    all_profiles: Dict[str, Dict] = {}
    href_queue: Dict[str, Dict] = {}   # href → {name, school, pos, nfb_pos_rank}

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

        # ── Phase 1: collect hrefs from position pages ────────────────────
        print("\n── Phase 1: Collecting player hrefs from position pages ────")
        for pos in POSITIONS:
            entries = collect_hrefs_for_position(page, pos)
            print(f"  {pos}: {len(entries)} players")
            for e in entries:
                href = e["href"]
                if href not in href_queue:
                    href_queue[href] = e
            time.sleep(1)

        print(f"\n  Total unique player pages to scrape: {len(href_queue)}")

        # ── Phase 2: scrape each profile page ────────────────────────────
        print("\n── Phase 2: Scraping individual player profiles ────────────")
        for i, (href, meta) in enumerate(href_queue.items(), 1):
            url = NFB_BASE + href
            name = meta["name"]
            print(f"  [{i:>3}/{len(href_queue)}] {name} ({meta['pos']}) …", end=" ", flush=True)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                time.sleep(2.5)

                if "just a moment" in page.title().lower():
                    print("BLOCKED")
                    continue

                profile = parse_profile(page.content(), meta["pos"])
                profile["nfb_href"]     = href
                profile["nfb_pos_rank"] = meta.get("nfb_pos_rank", "")
                profile["source_pos"]   = meta["pos"]

                # Use the cleaned name from position page as primary name
                profile.setdefault("name", name)
                profile.setdefault("school", meta.get("school", ""))

                key = nn(name)
                all_profiles[key] = profile
                print(f"✓  (traits:{len(profile.get('nfb_trait_scores', {}))}, "
                      f"strengths:{'yes' if profile.get('strengths') else 'no'})")

            except Exception as e:
                print(f"ERROR: {e}")

            time.sleep(0.5)

        browser.close()

    return all_profiles


# ── merge into prospects.json ─────────────────────────────────────────────

def merge_into_prospects(profiles: Dict[str, Dict]) -> None:
    with open("data/prospects.json") as f:
        prospects = json.load(f)

    merged_count  = 0
    new_count     = 0
    seen_keys = {nn(p.get("name", "")): i for i, p in enumerate(prospects)}

    for key, profile in profiles.items():
        if key in seen_keys:
            p = prospects[seen_keys[key]]
            # Merge all profile fields
            for field, val in profile.items():
                if val and not p.get(field):
                    p[field] = val
                # Always update these richer NFB fields
                if field in ("bio", "strengths", "weaknesses", "honors",
                             "nfb_trait_scores", "player_comparisons",
                             "nfb_avg_ovr_rank", "nfb_avg_pos_rank",
                             "projected_round", "nfb_overall_rank"):
                    if val:
                        p[field] = val
            # Rebuild scouting_report to include all text
            _rebuild_scouting_report(p)
            merged_count += 1
        else:
            # New prospect — add it
            profile.setdefault("source", "nfldraftbuzz_2026")
            profile.setdefault("_draft_year", "2026")
            # Ensure position is set from the ranking page's position code
            if not profile.get("position") and profile.get("source_pos"):
                profile["position"] = profile["source_pos"]
            _rebuild_scouting_report(profile)
            prospects.append(profile)
            new_count += 1

    print(f"\n  Merged data into {merged_count} existing prospects")
    print(f"  Added {new_count} new prospects")

    # Re-rank
    has_rank = sorted([p for p in prospects if p.get("rank")], key=lambda x: x["rank"])
    no_rank  = sorted([p for p in prospects if not p.get("rank")],
                      key=lambda x: (-(float(x.get("nfb_rating") or 0)), x.get("name", "")))
    ordered  = has_rank + no_rank

    for i, p in enumerate(ordered):
        p["rank"] = i + 1

    pos_counters: Dict[str, int] = {}
    for p in ordered:
        pos = p.get("position", "")
        pos_counters[pos] = pos_counters.get(pos, 0) + 1
        p["position_rank"] = pos_counters[pos]

    with open("data/prospects.json", "w") as f:
        json.dump(ordered, f, indent=2, ensure_ascii=False)

    print(f"  Saved {len(ordered)} total prospects to data/prospects.json")


def _rebuild_scouting_report(p: Dict) -> None:
    """Combine all text fields into a rich scouting_report string."""
    parts = []

    if p.get("bio"):
        parts.append(f"Background:\n{p['bio']}")
    if p.get("nfb_summary"):
        parts.append(f"NFLDraftBuzz Summary:\n{p['nfb_summary']}")
    if p.get("strengths"):
        parts.append(f"Strengths:\n{p['strengths']}")
    if p.get("weaknesses"):
        parts.append(f"Weaknesses:\n{p['weaknesses']}")
    if p.get("honors"):
        parts.append(f"Honors & Awards:\n{p['honors']}")
    if p.get("nfb_trait_scores"):
        trait_lines = "\n".join(f"  {k}: {v}" for k, v in p["nfb_trait_scores"].items())
        parts.append(f"Trait Scores (NFLDraftBuzz):\n{trait_lines}")
    if p.get("player_comparisons"):
        parts.append(f"NFL Player Comparisons: {p['player_comparisons']}")

    # Existing WF scouting text
    if p.get("scouting_report") and "Background:" not in p.get("scouting_report", ""):
        parts.append(p["scouting_report"])

    if parts:
        p["scouting_report"] = "\n\n".join(parts)


# ── entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys as _sys
    skip_ingest = "--skip-ingest" in _sys.argv

    print("=" * 62)
    print("  NFLDraftBuzz Player Profile Scraper — 2026 Draft Class")
    print("=" * 62)

    profiles = scrape_all_profiles()
    print(f"\nScraped {len(profiles)} player profiles")

    merge_into_prospects(profiles)

    if not skip_ingest:
        print("\nRe-ingesting into ChromaDB …")
        import subprocess
        result = subprocess.run([_sys.executable, "ingest.py"], cwd=".")
        if result.returncode == 0:
            print("\n✅  Done! ChromaDB updated with full NFLDraftBuzz profiles.")
        else:
            print("\n❌  Ingest failed — check output above.")
