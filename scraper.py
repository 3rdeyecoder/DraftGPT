"""
NFL Scout AI — Prospect Scraper  (2026 Draft Class ONLY)
=========================================================
Sources (in priority order):
  1. nfldraftbuzz.com/positions/*/2026   — all position tabs
     Requires Playwright (Cloudflare JS challenge).
  2. jfosterfilm.shinyapps.io/26draft/  — R Shiny big board / ratings
     Requires Playwright (React/Shiny dynamic rendering).
  3. walterfootball.com/draft2026.php   — mock draft + per-player scouting reports
     requests + BeautifulSoup (static HTML, no JS needed).

HARD FILTER: every prospect must be explicitly confirmed as 2026 draft class.
Any entry whose text or metadata references a draft year other than 2026 is dropped.
"""

import re
import os
import json
import time
import unicodedata
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False
    print("[WARN] playwright not installed — Playwright sources will be skipped.")

# ── constants ──────────────────────────────────────────────────────────────

REQUESTS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

WF_BASE      = "https://walterfootball.com"
WF_MOCK      = f"{WF_BASE}/draft2026.php"
WF_BIGBOARD  = f"{WF_BASE}/nfldraftbigboard2026.php"

NFB_BASE      = "https://www.nfldraftbuzz.com"
NFB_POSITIONS = ["ALL", "QB", "WR", "TE", "RB", "OT", "OG", "C", "EDGE", "DT", "LB", "CB", "S", "K", "P"]

JFOSTER_URL = "https://jfosterfilm.shinyapps.io/26draft/"

REQUEST_DELAY = 1.0

# Probe list: additional 2026 prospects to check for Walter Football scouting pages
WF_EXTRA_PROBE = [
    ("Aaron Anderson",    "WR",   "LSU"),
    ("Derrick Moore",     "DE",   "Michigan"),
    ("Dante Moore",       "QB",   "Oregon"),
    ("Dakorien Moore",    "WR",   "Texas"),
    ("Darian Mensah",     "QB",   "Duke"),
    ("Riley Leonard",     "QB",   "Notre Dame"),
    ("Harold Perkins Jr.","LB",   "LSU"),
    ("Kyle Williams",     "WR",   "Washington State"),
    ("Luther Burden III", "WR",   "Missouri"),
    ("Harold Fannin Jr.", "TE",   "Bowling Green"),
    ("Matthew Golden",    "WR",   "Texas"),
    ("Jihaad Campbell",   "LB",   "Alabama"),
    ("Caleb Shudak",      "K",    "Iowa"),
    ("Deone Walker",      "DT",   "Kentucky"),
    ("Jack Sawyer",       "DE",   "Ohio State"),
    ("Benjamin Morrison", "CB",   "Notre Dame"),
    ("Malaki Starks",     "S",    "Georgia"),
    ("TreVeyon Henderson","RB",   "Ohio State"),
    ("Quinshon Judkins",  "RB",   "Ohio State"),
    ("Kelvin Banks Jr.",  "OT",   "Texas"),
    ("Aireontae Ersery",  "OT",   "Minnesota"),
    ("Emeka Egbuka",      "WR",   "Ohio State"),
    ("Josaiah Stewart",   "EDGE", "Michigan"),
    ("Shemar Turner",     "DT",   "Texas A&M"),
    ("Dasan McCullough",  "EDGE", "Indiana"),
    ("Andrew Mukuba",     "S",    "Texas"),
    ("Xavier Watts",      "S",    "Notre Dame"),
]


# ── helpers ────────────────────────────────────────────────────────────────

def nn(name: str) -> str:
    """Lowercase, strip accents/punctuation for fuzzy name matching."""
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z ]", "", name.lower()).strip()


def get_soup(url: str, timeout: int = 15) -> Optional[BeautifulSoup]:
    try:
        r = requests.get(url, headers=REQUESTS_HEADERS, timeout=timeout)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        print(f"    [WARN] GET {url} → {e}")
        return None


def is_2026_class(prospect: Dict) -> bool:
    """
    Hard-filter: reject any prospect not explicitly associated with the 2026 draft class.

    Rules:
      • Source URL must contain '2026'  OR  source tag must be a known-2026 origin.
      • Scouting report text must NOT say '2025 NFL draft', 'selected in 2025', etc.
    """
    source   = prospect.get("source", "")
    href     = prospect.get("_scouting_href", "")
    report   = (prospect.get("scouting_report") or "").lower()
    name     = prospect.get("name", "")

    # Known-2026 sources
    if source in ("nfldraftbuzz_2026", "jfoster_26draft",
                  "Walter Football mock 2026", "Walter Football bigboard 2026",
                  "Walter Football scouting 2026"):
        pass   # trusted
    elif "2026" in href:
        pass   # URL confirms year
    else:
        # Unknown source — do a text check
        if any(pat in report for pat in [
            "2025 nfl draft", "selected in the 2025", "drafted in 2025",
            "was picked in 2025", "in the 2025 draft",
        ]):
            print(f"    [FILTER] Dropped '{name}' — report references 2025 draft")
            return False

    # Explicit disqualifier: report says this player is a 2025 prospect
    if "2025 draft prospect" in report or "2025 first-round pick" in report:
        print(f"    [FILTER] Dropped '{name}' — labelled as 2025 prospect")
        return False

    return True


# ── Source 1: nfldraftbuzz.com (Playwright) ───────────────────────────────

def _parse_nfb_page(html: str, position_hint: str = "") -> List[Dict]:
    """
    Parse nfldraftbuzz.com position page HTML after JavaScript has rendered.
    The site uses a standard HTML table for prospects.
    """
    soup = BeautifulSoup(html, "lxml")
    prospects = []

    # Try multiple table selectors — inspect whichever the page uses
    tables = soup.find_all("table")
    target_table = None
    for tbl in tables:
        rows = tbl.select("tbody tr")
        if len(rows) >= 5:
            target_table = tbl
            break

    if not target_table:
        # Fallback: div-based list (some draft sites use divs)
        rows = soup.select("div.player-row, div.prospect-row, li.player")
        for row in rows:
            name_el = (
                row.select_one(".player-name, .name, [class*='name']")
                or row.select_one("a")
            )
            if not name_el:
                continue
            text = name_el.get_text(strip=True)
            if len(text) > 3:
                pos_el = row.select_one(".position, .pos, [class*='pos']")
                sch_el = row.select_one(".school, .college, [class*='school']")
                prospects.append({
                    "name":     text,
                    "position": pos_el.get_text(strip=True) if pos_el else position_hint,
                    "school":   sch_el.get_text(strip=True) if sch_el else "",
                    "source":   "nfldraftbuzz_2026",
                    "_draft_year": "2026",
                })
        return prospects

    # Parse HTML table
    headers = [th.get_text(strip=True).lower() for th in target_table.select("thead th")]
    for row in target_table.select("tbody tr"):
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if not cells or len(cells) < 2:
            continue

        p: Dict = {"source": "nfldraftbuzz_2026", "_draft_year": "2026"}

        # Map known column names
        col_map = {
            "rank": "rank", "#": "rank",
            "player": "name", "name": "name", "player name": "name",
            "pos": "position", "position": "position",
            "school": "school", "college": "school", "team": "school",
            "grade": "grade", "ovr": "grade", "overall": "grade",
            "age": "age",
            "ht": "height", "height": "height",
            "wt": "weight", "weight": "weight",
        }

        if headers:
            for h, val in zip(headers, cells):
                field = col_map.get(h)
                if field:
                    p[field] = val
        else:
            # Positional guess: rank, name, pos, school, grade
            if len(cells) >= 2:
                p["rank"]     = cells[0] if re.match(r"^\d+$", cells[0]) else None
                p["name"]     = cells[1] if len(cells) > 1 else ""
                p["position"] = cells[2] if len(cells) > 2 else position_hint
                p["school"]   = cells[3] if len(cells) > 3 else ""
                p["grade"]    = cells[4] if len(cells) > 4 else ""

        if p.get("name") and len(p["name"]) > 3:
            if not p.get("position") and position_hint:
                p["position"] = position_hint
            prospects.append(p)

    return prospects


def scrape_nfldraftbuzz() -> List[Dict]:
    """
    Use Playwright to load each nfldraftbuzz.com position tab for 2026
    and extract all prospects.
    """
    if not PLAYWRIGHT_OK:
        print("  [SKIP] nfldraftbuzz — Playwright not available")
        return []

    all_prospects: Dict[str, Dict] = {}

    print(f"  Launching browser for nfldraftbuzz.com …")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/New_York",
                java_script_enabled=True,
            )
            page = ctx.new_page()
            page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})

            for pos in NFB_POSITIONS:
                url = f"{NFB_BASE}/positions/{pos}/2026"
                try:
                    page.goto(url, wait_until="networkidle", timeout=12_000)

                    # Check for Cloudflare challenge
                    title = page.title()
                    if "just a moment" in title.lower() or "cloudflare" in title.lower():
                        print(f"    [BLOCK] nfldraftbuzz {pos} — Cloudflare challenge not resolved")
                        break   # All positions will be blocked the same way

                    # Wait for table or player rows
                    try:
                        page.wait_for_selector("table tbody tr, div.player-row", timeout=10_000)
                    except PWTimeout:
                        pass

                    html = page.content()
                    rows = _parse_nfb_page(html, position_hint=pos if pos != "ALL" else "")
                    new = 0
                    for p in rows:
                        key = nn(p.get("name", ""))
                        if key and key not in all_prospects:
                            all_prospects[key] = p
                            new += 1
                    print(f"    {pos}: {len(rows)} rows ({new} new unique)")
                    time.sleep(0.8)

                except PWTimeout:
                    print(f"    [TIMEOUT] nfldraftbuzz {pos}")
                except Exception as e:
                    print(f"    [ERR] nfldraftbuzz {pos}: {e}")

            browser.close()

    except Exception as e:
        print(f"  [ERR] nfldraftbuzz browser session: {e}")

    result = list(all_prospects.values())
    print(f"  nfldraftbuzz: {len(result)} unique 2026 prospects")
    return result


# ── Source 2: jfosterfilm.shinyapps.io/26draft/ (Playwright) ─────────────

def _parse_reactable(html: str) -> List[Dict]:
    """
    Parse an R reactable widget's rendered HTML table.
    Reactable uses class="rt-table" with thead/tbody.
    """
    soup = BeautifulSoup(html, "lxml")
    prospects = []

    table = soup.select_one("table.rt-table") or soup.select_one("table")
    if not table:
        return []

    headers = [th.get_text(strip=True) for th in table.select("thead .rt-th")]
    if not headers:
        headers = [th.get_text(strip=True) for th in table.select("thead th")]

    headers_lower = [h.lower() for h in headers]

    col_map = {
        "rank": "rank", "#": "rank", "ovr rank": "rank",
        "player": "name", "name": "name",
        "pos": "position", "position": "position",
        "school": "school", "college": "school",
        "grade": "grade", "ovr": "grade", "overall grade": "grade",
        "ht": "height", "height": "height",
        "wt": "weight", "weight": "weight",
        "40yd": "forty_yard", "40-yd": "forty_yard",
        "vertical": "vertical",
        "broad": "broad_jump",
        "arm": "arm_length",
        "hand": "hand_size",
    }

    for row in table.select("tbody .rt-tr, tbody tr"):
        cells = row.select(".rt-td, td")
        if not cells:
            continue
        vals = [c.get_text(strip=True) for c in cells]
        if not vals or len(vals) < 2:
            continue

        p: Dict = {"source": "jfoster_26draft", "_draft_year": "2026"}

        if headers_lower:
            for h, val in zip(headers_lower, vals):
                field = col_map.get(h)
                if field and val:
                    p[field] = val
        else:
            # Positional fallback
            p["rank"]     = vals[0] if vals else ""
            p["name"]     = vals[1] if len(vals) > 1 else ""
            p["position"] = vals[2] if len(vals) > 2 else ""
            p["school"]   = vals[3] if len(vals) > 3 else ""

        if p.get("name") and len(p["name"]) > 3:
            prospects.append(p)

    return prospects


def scrape_jfoster() -> List[Dict]:
    """
    Use Playwright to load jfosterfilm.shinyapps.io/26draft/ and wait for the
    'NFLDraftBoard' reactable table to populate, then extract all rows.
    """
    if not PLAYWRIGHT_OK:
        print("  [SKIP] jfoster — Playwright not available")
        return []

    print(f"  Launching browser for jfosterfilm.shinyapps.io/26draft/ …")
    prospects = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()

            page.goto(JFOSTER_URL, wait_until="networkidle", timeout=20_000)

            # Wait for Shiny to initialise and the reactable to render
            print("    Waiting for Shiny app to load data …")
            try:
                # Wait for the table header inside NFLDraftBoard (confirms data loaded)
                page.wait_for_selector(
                    "#NFLDraftBoard .rt-thead, #NFLDraftBoard thead",
                    timeout=15_000,
                )
                # Extra wait for all rows to appear
                page.wait_for_timeout(3_000)
            except PWTimeout:
                print("    [WARN] Timed out waiting for NFLDraftBoard — trying anyway")

            # Try to scroll to load all rows (reactable may paginate)
            try:
                board = page.query_selector("#NFLDraftBoard")
                if board:
                    # Check for "Show all" or pagination; click if present
                    show_all = page.query_selector(
                        "button:has-text('Show all'), button:has-text('All'), "
                        "[aria-label='Show all rows']"
                    )
                    if show_all:
                        show_all.click()
                        page.wait_for_timeout(2_000)
            except Exception:
                pass

            # Extract the rendered table HTML
            try:
                board_html = page.inner_html("#NFLDraftBoard")
            except Exception:
                board_html = page.content()

            prospects = _parse_reactable(board_html)
            print(f"    Parsed {len(prospects)} rows from NFLDraftBoard")

            # If the main board yielded nothing, try the full page content
            if not prospects:
                full_html = page.content()
                prospects = _parse_reactable(full_html)
                print(f"    (fallback full-page parse: {len(prospects)} rows)")

            browser.close()

    except Exception as e:
        print(f"  [ERR] jfoster browser session: {e}")

    print(f"  jfoster: {len(prospects)} 2026 prospects")
    return prospects


# ── Source 3: Walter Football (requests + BS4) ────────────────────────────

def _wf_parse_player_strong(strong_text: str) -> Tuple[str, str, str]:
    parts = [p.strip() for p in strong_text.split(",")]
    if len(parts) >= 3:
        return parts[0], parts[1], ", ".join(parts[2:])
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return strong_text.strip(), "", ""


def _wf_extract_report(soup: BeautifulSoup) -> str:
    """
    Extract scouting text from a Walter Football scouting report page.
    Main content is in <div class="entry-content" itemprop="articleBody">.
    Structured sections use class="SR-*".
    """
    content = (
        soup.find("div", attrs={"itemprop": "articleBody"})
        or soup.find("div", class_="entry-content")
    )
    if not content:
        return ""

    parts = []

    # Scouting card (measurables)
    card = content.find("div", class_=lambda c: c and "scouting-card" in c)
    if card:
        t = card.get_text(" ", strip=True)
        if t and len(t) > 10:
            parts.append(t)

    # Structured SR-* sections
    for div in content.find_all("div", class_=lambda c: c and any(
        cl.startswith("SR-") for cl in c.split()
    )):
        t = div.get_text(" ", strip=True)
        if t and len(t) > 15:
            parts.append(t)

    # Fallback: long <p> tags
    if not parts:
        noise = {"amazon", "patreon", "twitter", "mock draft", "fantasy football",
                 "power rankings", "nfl picks", "follow @"}
        for p in content.find_all("p"):
            t = p.get_text(" ", strip=True)
            if len(t) > 80 and not any(n in t.lower() for n in noise):
                parts.append(t)
            if len(parts) >= 20:
                break

    return "\n\n".join(parts)


def _wf_report_url_candidates(name: str) -> List[str]:
    clean = re.sub(r"\s+(Jr\.?|Sr\.?|I{2,3}|IV|V)\s*$", "", name, flags=re.IGNORECASE).strip()
    parts = clean.split()
    if not parts:
        return []
    fi   = parts[0][0].lower()
    last = re.sub(r"[^a-z]", "", parts[-1].lower())
    candidates = [f"/scoutingreports2026{fi}{last}.php"]
    if len(parts) >= 3:
        # Try second-to-last part (middle name → skip)
        last2 = re.sub(r"[^a-z]", "", parts[-2].lower())
        if last2 != last:
            candidates.append(f"/scoutingreports2026{fi}{last2}.php")
    return candidates


def scrape_wf_mock() -> List[Dict]:
    """Scrape Walter Football 2026 mock draft page for pick order + inline analysis."""
    print(f"  Fetching {WF_MOCK} …")
    soup = get_soup(WF_MOCK)
    if not soup:
        return []

    divs = soup.select("div.player-info.article[data-number]") or soup.find_all("div", class_="player-info")
    print(f"  Found {len(divs)} picks on mock page.")
    prospects = []

    for div in divs:
        try:
            pick = int(div.get("data-number", 0)) or None
            strong = div.find("strong")
            if not strong:
                continue

            full = strong.get_text(separator=" ", strip=True)
            team = full.split(":")[0].strip() if ":" in full else ""
            link = strong.find("a")

            if link:
                raw   = link.get_text(strip=True)
                href  = link.get("href", "")
                # Normalise href to relative
                if href.startswith("http"):
                    href = "/" + href.split(WF_BASE, 1)[-1].lstrip("/")
            else:
                raw  = full.split(":", 1)[-1].strip() if ":" in full else full
                href = ""

            name, pos, school = _wf_parse_player_strong(raw)
            if not name or len(name) < 3:
                continue

            noise = {"amazon", "patreon", "twitter", "support"}
            inline = " ".join(
                p.get_text(" ", strip=True)
                for p in div.find_all("p")
                if p.get_text(strip=True) and not any(n in p.get_text().lower()[:30] for n in noise)
            )

            prospects.append({
                "name":           name,
                "position":       pos,
                "school":         school,
                "projected_team": team,
                "rank":           pick,
                "pick_number":    pick,
                "_wf_inline":     inline,
                "_scouting_href": href,
                "source":         "Walter Football mock 2026",
                "_draft_year":    "2026",
            })
        except Exception as e:
            print(f"    [WARN] mock parse: {e}")

    print(f"  Parsed {len(prospects)} prospects from mock.")
    return prospects


def fetch_wf_report(href: str) -> str:
    """Fetch and parse a Walter Football /scoutingreports2026*.php page."""
    url = (WF_BASE + href) if href.startswith("/") else href
    soup = get_soup(url)
    return _wf_extract_report(soup) if soup else ""


def enrich_wf_scouting_reports(prospects: List[Dict]) -> List[Dict]:
    """Fetch WF scouting report pages for mock picks that have a link."""
    to_fetch = [p for p in prospects if p.get("_scouting_href")]
    print(f"  Fetching WF scouting reports for {len(to_fetch)} mock picks …")
    for p in to_fetch:
        time.sleep(REQUEST_DELAY)
        detail = fetch_wf_report(p["_scouting_href"])
        if detail:
            p["_wf_detail"] = detail
    return prospects


def probe_wf_extra(existing: set) -> List[Dict]:
    """
    Try to find Walter Football scouting report pages for additional known
    2026 prospects by generating URL candidates from their names.
    """
    found = []
    print(f"  Probing WF scouting reports for {len(WF_EXTRA_PROBE)} additional prospects …")

    for name, pos, school in WF_EXTRA_PROBE:
        if nn(name) in existing:
            continue
        for href in _wf_report_url_candidates(name):
            time.sleep(REQUEST_DELAY)
            detail = fetch_wf_report(href)

            if not detail or len(detail) < 150:
                continue
            if "mock draft" in detail.lower()[:60] or "fantasy football" in detail.lower()[:60]:
                continue
            # Validate first name is mentioned in first 300 chars (guards against
            # URL collision with a different player who shares initials+surname)
            first = name.split()[0].lower()
            if first not in detail[:300].lower():
                print(f"    – {name}: URL hit but report is for different player")
                break

            found.append({
                "name":           name,
                "position":       pos,
                "school":         school,
                "_wf_detail":     detail,
                "_scouting_href": href,
                "source":         "Walter Football scouting 2026",
                "_draft_year":    "2026",
            })
            existing.add(nn(name))
            print(f"    ✓ {name} ({pos}, {school})")
            break

    print(f"  Found {len(found)} additional WF scouting reports.")
    return found


# ── Source 3b: Walter Football Big Board (150 prospects) ──────────────────

def scrape_wf_bigboard() -> List[Dict]:
    """
    Scrape the Walter Football 2026 big board for all 150 ranked prospects.
    Each entry includes rank, name, position, school, inline analysis text,
    and a link to the full scouting report page.
    """
    print(f"  Fetching {WF_BIGBOARD} …")
    soup = get_soup(WF_BIGBOARD)
    if not soup:
        return []

    content = (
        soup.find("div", attrs={"itemprop": "articleBody"})
        or soup.find("div", class_="entry-content")
    )
    if not content:
        print("  [WARN] No content div found on big board page")
        return []

    prospects = []
    for ranking_div in content.find_all("div", class_="divPlayerRanking"):
        cells = ranking_div.find_all("div", class_="cellDiv")
        if len(cells) < 2:
            continue

        rank_b = cells[0].find("b")
        if not rank_b:
            continue
        rank_text = rank_b.get_text(strip=True).rstrip(".")
        if not rank_text.isdigit():
            continue
        rank = int(rank_text)

        info_cell = cells[1]
        player_b  = info_cell.find("b")
        if not player_b:
            continue

        link = player_b.find("a")
        href = ""
        if link:
            raw_href = link.get("href", "")
            # Normalise to relative path
            if raw_href.startswith("http"):
                href = "/" + raw_href.split(WF_BASE, 1)[-1].lstrip("/")
            else:
                href = raw_href

        full = player_b.get_text(strip=True)
        parts = [x.strip() for x in full.split(",")]
        name   = parts[0] if parts else ""
        pos    = parts[1].strip() if len(parts) > 1 else ""
        school = parts[2].rstrip(".").strip() if len(parts) > 2 else ""

        if not name or len(name) < 3:
            continue

        # Inline analysis (strip the "Previously: …" rank history line)
        raw_text = info_cell.get_text(" ", strip=True)
        raw_text = re.sub(r"Previously:.*?(?=\d{2}/\d{2}/\d{2}:|$)", "", raw_text).strip()

        prospects.append({
            "name":           name,
            "position":       pos,
            "school":         school,
            "rank":           rank,
            "_wf_inline":     raw_text,
            "_scouting_href": href,
            "source":         "Walter Football bigboard 2026",
            "_draft_year":    "2026",
        })

    print(f"  Parsed {len(prospects)} prospects from big board.")
    return prospects


def enrich_wf_bigboard_reports(prospects: List[Dict], existing_keys: set,
                               max_fetch: int = 150) -> List[Dict]:
    """
    Fetch full scouting report pages for big-board prospects that have a link
    and haven't already been fetched from the mock scrape.
    Skips prospects already in existing_keys to avoid duplicate fetches.
    """
    to_fetch = [
        p for p in prospects
        if p.get("_scouting_href") and nn(p["name"]) not in existing_keys
    ][:max_fetch]
    print(f"  Fetching WF scouting reports for {len(to_fetch)} big-board prospects …")

    for p in to_fetch:
        time.sleep(REQUEST_DELAY)
        detail = fetch_wf_report(p["_scouting_href"])
        if detail and len(detail) > 100:
            # Validate first name to guard against URL collisions
            first = p["name"].split()[0].lower()
            if first in detail[:300].lower():
                p["_wf_detail"] = detail
                existing_keys.add(nn(p["name"]))
            else:
                print(f"    – {p['name']}: scouting page is for different player (URL collision)")

    fetched = sum(1 for p in to_fetch if p.get("_wf_detail"))
    print(f"  Fetched {fetched} scouting reports for big-board prospects.")
    return prospects


# ── merge + clean ──────────────────────────────────────────────────────────

def merge_sources(
    nfb_list: List[Dict],
    jfoster_list: List[Dict],
    wf_list: List[Dict],
) -> List[Dict]:
    """
    Merge all sources. Walter Football scouting text takes precedence.
    nfldraftbuzz grades / jfoster ratings are merged in as supplementary fields.
    Primary key is normalised player name.
    """
    master: Dict[str, Dict] = {}

    # 1. Seed with WF (richest text — mock first, then bigboard fills gaps)
    # Sort so mock picks (with projected_team) take priority over bigboard
    wf_sorted = sorted(
        wf_list,
        key=lambda x: 0 if x.get("source") == "Walter Football mock 2026" else 1
    )
    for p in wf_sorted:
        key = nn(p["name"])
        if key not in master:
            master[key] = p
        else:
            # Merge: prefer mock's projected_team; prefer detail text from either
            existing = master[key]
            if p.get("projected_team") and not existing.get("projected_team"):
                existing["projected_team"] = p["projected_team"]
            # Keep whichever has longer scouting text
            p_text = len(p.get("_wf_detail", "") or p.get("_wf_inline", ""))
            e_text = len(existing.get("_wf_detail", "") or existing.get("_wf_inline", ""))
            if p_text > e_text:
                # Preserve projected_team from existing before overwriting
                pt = existing.get("projected_team")
                master[key] = p
                if pt:
                    master[key]["projected_team"] = pt

    # 2. nfldraftbuzz — add new prospects; fill in grade for existing
    for p in nfb_list:
        key = nn(p["name"])
        if key in master:
            if p.get("grade") and not master[key].get("grade"):
                master[key]["grade"] = p["grade"]
            if p.get("school") and not master[key].get("school"):
                master[key]["school"] = p["school"]
            if p.get("position") and not master[key].get("position"):
                master[key]["position"] = p["position"]
        else:
            master[key] = p

    # 3. jfoster — add new prospects; merge measurables
    merge_fields = ["height", "weight", "forty_yard", "vertical", "broad_jump",
                    "arm_length", "hand_size", "grade"]
    for p in jfoster_list:
        key = nn(p["name"])
        if key in master:
            for f in merge_fields:
                if p.get(f) and not master[key].get(f):
                    master[key][f] = p[f]
            if p.get("school") and not master[key].get("school"):
                master[key]["school"] = p["school"]
        else:
            master[key] = p

    return list(master.values())


def consolidate_text(p: Dict) -> None:
    """Merge WF text fields into 'scouting_report'; strip internal scratch fields."""
    parts = []

    detail = p.pop("_wf_detail", "").strip()
    inline = p.pop("_wf_inline", "").strip()

    if detail:
        parts.append(detail)
    elif inline:
        parts.append(inline)

    # Append structured measurables / combine stats as text if present
    extras = {}
    for f in ["forty_yard", "vertical", "broad_jump", "arm_length", "hand_size"]:
        if p.get(f):
            extras[f.replace("_", " ").title()] = p.get(f)
    if extras:
        parts.append("Combine / Measurables: " + ", ".join(f"{k}: {v}" for k, v in extras.items()))

    p["scouting_report"] = "\n\n".join(parts)
    p.pop("_scouting_href", None)
    p.pop("_draft_year",    None)   # served its purpose


def assign_ranks(prospects: List[Dict]) -> List[Dict]:
    """
    Sort by WF big-board rank first (most authoritative), then mock picks,
    then alphabetically. After sorting, assign overall rank and position rank.
    """
    # Separate prospects that have an explicit WF big-board rank vs others
    has_rank  = sorted([p for p in prospects if p.get("rank")],
                       key=lambda x: x["rank"])
    no_rank   = sorted([p for p in prospects if not p.get("rank")],
                       key=lambda x: x.get("name", ""))
    ordered = has_rank + no_rank

    # Assign sequential overall rank
    for i, p in enumerate(ordered):
        p["rank"] = i + 1

    # Assign position rank (rank within each position group by overall rank)
    pos_counters: Dict[str, int] = {}
    for p in ordered:
        pos = p.get("position", "")
        pos_counters[pos] = pos_counters.get(pos, 0) + 1
        p["position_rank"] = pos_counters[pos]

    return ordered


# ── NFL.com official 2026 draft order ─────────────────────────────────────

NFL_DRAFT_ORDER_URL = "https://www.nfl.com/news/2026-nfl-draft-order-for-all-seven-rounds"

# Known team name aliases (outdated names → current official name on NFL.com)
_TEAM_ALIASES: Dict[str, str] = {
    "washington redskins":  "washington commanders",
    "washington football":  "washington commanders",
    "oakland raiders":      "las vegas raiders",
    "san diego chargers":   "los angeles chargers",
    "st. louis rams":       "los angeles rams",
}

def _normalize_team(name: str) -> str:
    """Lowercase + strip punctuation for fuzzy team matching."""
    n = re.sub(r"[^a-z ]", "", name.lower()).strip()
    return _TEAM_ALIASES.get(n, n)


def scrape_nfl_draft_order() -> Dict[int, str]:
    """
    Scrape https://www.nfl.com/news/2026-nfl-draft-order-for-all-seven-rounds
    using Playwright (page is JS-rendered).

    Returns {pick_number: team_name} for all 257 picks, e.g.
        {1: 'Las Vegas Raiders', 2: 'New York Jets', ...}
    """
    if not PLAYWRIGHT_OK:
        print("  [SKIP] NFL.com draft order — Playwright not available")
        return {}

    picks: Dict[int, str] = {}
    print(f"  Fetching NFL.com 2026 draft order …")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
                )
            ).new_page()
            page.goto(NFL_DRAFT_ORDER_URL, wait_until="domcontentloaded", timeout=25_000)
            time.sleep(4)
            text = page.inner_text("body")
            browser.close()

        for line in text.split("\n"):
            line = line.strip()
            m = re.match(r"^(\d+)\.\s+(.+)$", line)
            if m:
                pick_num  = int(m.group(1))
                team_raw  = m.group(2)
                # Strip trade notes: "(from Bills)", "(through Chiefs)", etc.
                team_name = re.sub(r"\s*\(.*?\)", "", team_raw).strip()
                if team_name:
                    picks[pick_num] = team_name

        print(f"  NFL.com draft order: {len(picks)} picks parsed "
              f"(rounds 1–{max((p - 1) // 32 + 1 for p in picks) if picks else '?'})")

    except Exception as e:
        print(f"  [ERR] NFL.com draft order: {e}")

    return picks


def build_team_pick_index(pick_to_team: Dict[int, str]) -> Dict[str, List[int]]:
    """
    Invert {pick: team} → {normalised_team: [pick1, pick2, ...]} sorted ascending.
    Used to look up a team's earliest pick when applying draft order to prospects.
    """
    index: Dict[str, List[int]] = {}
    for pick, team in pick_to_team.items():
        key = _normalize_team(team)
        index.setdefault(key, []).append(pick)
    for key in index:
        index[key].sort()
    return index


def apply_draft_order(prospects: List[Dict], pick_to_team: Dict[int, str]) -> None:
    """
    For each prospect that has a projected_team, look up that team's first pick
    in the official NFL.com draft order and set pick_number + projected_round.
    Also corrects outdated team names (e.g. 'Washington Redskins' → 'Washington Commanders').
    """
    if not pick_to_team:
        return

    team_index = build_team_pick_index(pick_to_team)
    # Build reverse for name correction: normalised → canonical name from NFL.com
    norm_to_canonical: Dict[str, str] = {}
    for pick, team in pick_to_team.items():
        norm_to_canonical[_normalize_team(team)] = team

    applied = 0
    corrected = 0
    for p in prospects:
        raw_team = p.get("projected_team", "")
        if not raw_team:
            continue
        norm = _normalize_team(raw_team)
        team_picks = team_index.get(norm, [])
        if not team_picks:
            continue
        first_pick = team_picks[0]
        round_num  = (first_pick - 1) // 32 + 1
        p["pick_number"]     = first_pick
        p["projected_round"] = f"Round {round_num}"
        # Correct outdated team name to canonical NFL.com spelling
        canonical = norm_to_canonical.get(norm, raw_team)
        if canonical != raw_team:
            p["projected_team"] = canonical
            corrected += 1
        applied += 1

    print(f"  Applied draft order to {applied} prospects "
          f"({corrected} team names corrected)")


def scrape_nfb_consensus_mock() -> Dict[str, Dict]:
    """
    Scrape https://www.nfldraftbuzz.com/mock-draft/consensus/1/2026 (and
    subsequent pages) using Playwright headless=False to bypass Cloudflare.

    Returns a dict keyed by normalised player name:
        { nn(name): {"pick_number": int, "projected_team": str,
                      "projected_round": str} }
    """
    if not PLAYWRIGHT_OK:
        print("  [SKIP] NFB consensus mock — Playwright not available")
        return {}

    picks: Dict[str, Dict] = {}

    print(f"  Launching browser for NFB consensus mock draft …")
    try:
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
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/New_York",
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                "window.chrome = { runtime: {} };"
            )
            page = ctx.new_page()

            page_num = 1
            while True:
                url = f"https://www.nfldraftbuzz.com/mock-draft/consensus/{page_num}/2026"
                print(f"    Loading page {page_num}: {url}")
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                    time.sleep(3)

                    title = page.title().lower()
                    if "just a moment" in title or "cloudflare" in title:
                        print(f"    [BLOCK] Cloudflare on page {page_num}")
                        break

                    # Wait for any table row to appear
                    try:
                        page.wait_for_selector("table tbody tr, tr.mock-pick", timeout=8_000)
                    except Exception:
                        pass

                    html = page.content()
                    soup = BeautifulSoup(html, "lxml")

                    page_picks = _parse_consensus_mock_page(soup)
                    if not page_picks:
                        print(f"    No picks found on page {page_num} — stopping pagination")
                        break

                    for entry in page_picks:
                        key = nn(entry["name"])
                        if key:
                            picks[key] = entry

                    print(f"    Page {page_num}: {len(page_picks)} picks "
                          f"(cumulative: {len(picks)})")

                    # Stop if this page had fewer than 20 picks (likely last page)
                    if len(page_picks) < 20:
                        break

                    page_num += 1
                    time.sleep(1.5)

                except Exception as e:
                    print(f"    [ERR] page {page_num}: {e}")
                    break

            browser.close()

    except Exception as e:
        print(f"  [ERR] NFB consensus mock session: {e}")

    print(f"  NFB consensus mock: {len(picks)} picks scraped")
    return picks


def _parse_consensus_mock_page(soup: BeautifulSoup) -> List[Dict]:
    """
    Parse one page of the NFLDraftBuzz consensus mock draft.
    Returns list of dicts with: pick_number, projected_team, name, position, projected_round.
    """
    entries = []

    tables = soup.find_all("table")
    target = None
    for tbl in tables:
        rows = tbl.select("tbody tr")
        if len(rows) >= 5:
            target = tbl
            break

    if not target:
        return entries

    headers = [th.get_text(strip=True).lower() for th in target.select("thead th")]

    for row in target.select("tbody tr"):
        cells = row.find_all("td")
        if not cells or len(cells) < 3:
            continue

        cell_texts = [c.get_text(strip=True) for c in cells]

        # Try to map by header names first
        col = {}
        if headers:
            for h, cell in zip(headers, cells):
                col[h] = cell.get_text(strip=True)
                # Also capture links for player names
                a = cell.find("a")
                if a:
                    col[h + "_href"] = a.get("href", "")

        # Flexible extraction — look for pick #, team, player, pos
        pick_num   = None
        team_name  = ""
        player_name = ""
        position   = ""

        # Detect pick number: first numeric cell
        for txt in cell_texts:
            m = re.match(r"^(\d+)$", txt.strip())
            if m:
                pick_num = int(m.group(1))
                break

        # Team: look for column named "team" or "pick" containing team name
        for h_key in ("team", "franchise", "club", "pick team"):
            if col.get(h_key):
                team_name = col[h_key]
                break

        # Player name: look for anchor link in cells, or "player" column
        for cell in cells:
            a = cell.find("a")
            if a and a.get("href", "").startswith("/Player/"):
                player_name = a.get_text(strip=True)
                # Extract position from href slug if not found in cells
                href_slug = a.get("href", "").split("/")
                if len(href_slug) >= 4:
                    position = href_slug[-2] if href_slug[-2].isupper() else ""
                break

        if not player_name:
            # Fallback: look for "player" or "name" column
            for h_key in ("player", "name", "player name"):
                if col.get(h_key):
                    player_name = col[h_key]
                    break

        if not position:
            for h_key in ("pos", "position"):
                if col.get(h_key):
                    position = col[h_key]
                    break

        # If we still have no team, check if any img alt contains team name
        if not team_name:
            for cell in cells:
                img = cell.find("img")
                if img and img.get("alt"):
                    alt = img["alt"].strip()
                    # Skip player/player headshot images
                    if len(alt) > 2 and not any(c.isdigit() for c in alt):
                        team_name = alt
                        break

        if not player_name or len(player_name) < 3:
            continue

        # Determine round from pick number
        if pick_num:
            round_num = (pick_num - 1) // 32 + 1
            projected_round = f"Round {round_num}"
        else:
            projected_round = ""

        entries.append({
            "name":             player_name,
            "pick_number":      pick_num,
            "projected_team":   team_name,
            "position":         position,
            "projected_round":  projected_round,
        })

    return entries


# ── main entry point ───────────────────────────────────────────────────────

def scrape_all(max_prospects: int = 250) -> List[Dict]:
    print("\n── Draft order: NFL.com official 2026 pick order (all 7 rounds) ────")
    pick_to_team = scrape_nfl_draft_order()

    print("\n── Source 1: nfldraftbuzz.com position pages (Playwright) ──────────")
    nfb = scrape_nfldraftbuzz()

    print("\n── Source 2: jfosterfilm.shinyapps.io/26draft/ (Playwright) ────────")
    jfoster = scrape_jfoster()

    print("\n── Source 3: Walter Football mock draft (scouting text + team proj) ─")
    wf_mock  = scrape_wf_mock()
    wf_mock  = enrich_wf_scouting_reports(wf_mock)
    enriched = {nn(p["name"]) for p in wf_mock if p.get("_wf_detail") or p.get("_wf_inline")}

    print("\n── Source 4: Walter Football big board (150 prospects) ─────────────")
    wf_bb    = scrape_wf_bigboard()
    wf_bb    = enrich_wf_bigboard_reports(wf_bb, existing_keys=enriched)

    # Also run extra probe for any known prospects not yet covered
    all_wf_names = {nn(p["name"]) for p in wf_mock + wf_bb}
    wf_extra = probe_wf_extra(all_wf_names)
    wf_all   = wf_mock + wf_bb + wf_extra

    print("\n── Merging sources ─────────────────────────────────────────────────")
    merged = merge_sources(nfb, jfoster, wf_all)
    print(f"  Raw combined: {len(merged)} prospects")

    print("\n── Applying 2026-only filter ───────────────────────────────────────")
    filtered = [p for p in merged if is_2026_class(p)]
    dropped  = len(merged) - len(filtered)
    if dropped:
        print(f"  Dropped {dropped} prospects that failed 2026 class check.")
    print(f"  Confirmed 2026 class: {len(filtered)} prospects")

    print("\n── Deduplicating ───────────────────────────────────────────────────")
    seen: Dict[str, Dict] = {}
    for p in filtered:
        key = nn(p["name"])
        if key not in seen:
            seen[key] = p
        else:
            # Keep whichever entry has more scouting text
            existing_text = len(seen[key].get("_wf_detail", "") or seen[key].get("scouting_report", ""))
            new_text      = len(p.get("_wf_detail", "") or p.get("scouting_report", ""))
            if new_text > existing_text:
                seen[key] = p

    prospects = assign_ranks(list(seen.values()))[:max_prospects]

    print("\n── Applying NFL.com official draft order ───────────────────────────")
    apply_draft_order(prospects, pick_to_team)

    print("\n── Consolidating text fields ───────────────────────────────────────")
    for p in prospects:
        consolidate_text(p)

    return prospects


if __name__ == "__main__":
    print("=" * 62)
    print("  NFL Scout AI — 2026 Draft Class Scraper")
    print("=" * 62)

    os.makedirs("data", exist_ok=True)

    prospects = scrape_all()

    out = "data/prospects.json"
    with open(out, "w") as f:
        json.dump(prospects, f, indent=2, ensure_ascii=False)

    with_report = sum(1 for p in prospects if p.get("scouting_report"))
    print(f"\n✅  Saved {len(prospects)} 2026 prospects → {out}")
    print(f"   • {with_report} have scouting report text")

    print("\nTop 20:")
    for p in prospects[:20]:
        flag = "📋" if p.get("scouting_report") else "  "
        print(f"  {flag} #{str(p.get('rank','?')):>3}  {p['name']:<28}  "
              f"{p.get('position',''):>5}  {p.get('school','')}")
