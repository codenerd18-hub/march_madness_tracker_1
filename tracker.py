"""
March Madness Tracker
=====================
Fetches NCAA Men's Basketball stats, predicts seedings,
and generates bracket predictions. Exports everything to CSV.

Data sources:
  - ncaa.com          → standings (scraped directly, no API needed)
  - barttorvik.com    → advanced metrics via teamslicejson.php (reliable endpoint)
  - NCAA API          → rankings (with fallback to direct ncaa.com scrape)

Output files:
  RAW:
    raw_standings.csv         - wins/losses/conference
    raw_barttorvik.csv        - full T-Rank advanced metrics
    raw_rankings_ap.csv       - AP poll
    raw_rankings_net.csv      - NET rankings

  PROCESSED:
    team_stats_latest.csv           - all ~360 teams merged
    bracket_predictions_latest.csv  - 68 tourney teams + win probs
    model_features_latest.csv       - normalized, model-ready feature matrix
"""

import csv
import time
import random
import argparse
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

NCAA_STANDINGS_URL = "https://www.ncaa.com/standings/basketball-men/d1"
NCAA_AP_URL        = "https://www.ncaa.com/rankings/basketball-men/d1/associated-press"
NCAA_NET_URL       = "https://www.ncaa.com/rankings/basketball-men/d1/ncaa-mens-basketball-net-rankings"
BBART_BASE         = "https://barttorvik.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
}

CURRENT_YEAR = datetime.now().year
SEASON       = CURRENT_YEAR if datetime.now().month >= 10 else CURRENT_YEAR - 1

POWER_CONFERENCES = {
    "SEC", "Big Ten", "Big 12", "ACC", "Big East",
    "American", "Mountain West", "Atlantic 10", "WCC", "Pac-12"
}


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────
def _safe_int(val, default=0) -> int:
    try:    return int(str(val).strip().replace(",", ""))
    except: return default

def _safe_float(val, default=0.0) -> float:
    try:    return float(str(val).strip())
    except: return default

def _get(url, params=None, as_json=False):
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return r.json() if as_json else r.text
    except Exception as e:
        print(f"  [warn] GET {url}: {e}")
        return None

def _write_csv(rows: list[dict], path: Path) -> None:
    if not rows:
        print(f"  [skip] No data for {path.name}")
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"[CSV] {path.name}  ({len(rows)} rows)")

def _name_variants(name: str) -> list[str]:
    """Generate multiple lowercase variants for fuzzy name matching."""
    base     = name.lower().strip()
    no_paren = base.split("(")[0].strip()
    ALIASES  = {
        "uconn": "connecticut", "connecticut": "uconn",
        "lsu": "louisiana state", "louisiana state": "lsu",
        "ole miss": "mississippi", "mississippi": "ole miss",
        "pitt": "pittsburgh", "pittsburgh": "pitt",
        "nc state": "north carolina state",
        "north carolina state": "nc state",
        "north carolina st": "nc state",
        "miami (fl)": "miami", "miami fl": "miami",
        "saint mary's": "st. mary's", "st. mary's": "saint mary's",
        "smu": "southern methodist",
        "vcu": "virginia commonwealth",
        "unlv": "nevada las vegas",
        "utep": "texas el paso",
        "tcu": "texas christian",
        "byu": "brigham young",
        "umass": "massachusetts",
        "unc": "north carolina", "north carolina": "unc",
        "usc": "southern california",
        "ole miss": "mississippi",
        "fau": "florida atlantic",
        "uab": "alabama birmingham",
    }
    variants = {base, no_paren}
    if no_paren in ALIASES: variants.add(ALIASES[no_paren])
    if base     in ALIASES: variants.add(ALIASES[base])
    for suffix in [" university", " college", " state", " st."]:
        variants.add(no_paren.replace(suffix, "").strip())
    return [v for v in variants if v]

def _find_bbart(name: str, bbart: dict) -> dict:
    for v in _name_variants(name):
        if v in bbart: return bbart[v]
    # partial match fallback
    key = name.lower().strip()
    for k, val in bbart.items():
        if k in key or key in k: return val
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# RAW fetchers
# ─────────────────────────────────────────────────────────────────────────────
def fetch_raw_standings(out: Path) -> list[dict]:
    """Scrape D1 standings directly from ncaa.com."""
    print("[NCAA.com] Fetching ALL D1 standings …")
    html = _get(NCAA_STANDINGS_URL)
    if not html:
        return []

    soup  = BeautifulSoup(html, "html.parser")
    rows  = []
    tid   = 1

    # ncaa.com renders standings as a table with conference sections
    for table in soup.select("table"):
        # Try to get conference name from nearest heading
        conf = "Unknown"
        prev = table.find_previous(["h3", "h4", "h2", "caption"])
        if prev:
            conf = prev.get_text(strip=True)

        headers_row = table.select("thead th")
        headers     = [h.get_text(strip=True) for h in headers_row]

        for tr in table.select("tbody tr"):
            cells = [td.get_text(strip=True) for td in tr.select("td")]
            if not cells or len(cells) < 3:
                continue
            row_dict = dict(zip(headers, cells)) if headers else {}

            # Flexible column extraction
            school   = (row_dict.get("School") or row_dict.get("Team") or
                        cells[0] if cells else "Unknown")
            conf_w   = _safe_int(row_dict.get("Conference W", row_dict.get("W", 0)))
            conf_l   = _safe_int(row_dict.get("Conference L", row_dict.get("L", 0)))
            ov_w     = _safe_int(row_dict.get("Overall W",    "0"))
            ov_l     = _safe_int(row_dict.get("Overall L",    "0"))

            if not school or school in ("School", "Team"):
                continue

            rows.append({
                "id":          str(tid),
                "name":        school,
                "conference":  conf,
                "conf_wins":   conf_w,
                "conf_losses": conf_l,
                "overall_wins":   ov_w,
                "overall_losses": ov_l,
                "overall_pct": round(ov_w / (ov_w + ov_l), 4) if (ov_w + ov_l) else 0,
                "streak":      row_dict.get("Overall STREAK", row_dict.get("STREAK", "")),
            })
            tid += 1

    # Fallback: if scrape failed (JS-rendered), try JSON endpoint via NCAA API proxy
    if not rows:
        print("  [warn] HTML scrape returned no data — trying NCAA API proxy …")
        rows = _fetch_standings_api_fallback(tid)

    _write_csv(rows, out / "raw_standings.csv")
    print(f"  → {len(rows)} teams")
    return rows


def _fetch_standings_api_fallback(start_id: int = 1) -> list[dict]:
    """Fallback: try the NCAA API public instance."""
    try:
        r = requests.get(
            "https://ncaa-api.henrygd.me/standings/basketball-men/d1",
            headers=HEADERS, timeout=20
        )
        r.raise_for_status()
        data  = r.json()
        rows  = []
        tid   = start_id
        for conf_block in data.get("data", []):
            conf = conf_block.get("conference", "Unknown")
            for entry in conf_block.get("standings", []):
                school = entry.get("School", "Unknown")
                ov_w   = _safe_int(entry.get("Overall W", 0))
                ov_l   = _safe_int(entry.get("Overall L", 0))
                rows.append({
                    "id":           str(tid),
                    "name":         school,
                    "conference":   conf,
                    "conf_wins":    _safe_int(entry.get("Conference W", 0)),
                    "conf_losses":  _safe_int(entry.get("Conference L", 0)),
                    "overall_wins":  ov_w,
                    "overall_losses":ov_l,
                    "overall_pct":  round(ov_w / (ov_w + ov_l), 4) if (ov_w + ov_l) else 0,
                    "streak":       entry.get("Overall STREAK", ""),
                })
                tid += 1
        return rows
    except Exception as e:
        print(f"  [warn] API fallback also failed: {e}")
        return []


def fetch_raw_rankings(out: Path) -> tuple[dict, dict]:
    """Scrape AP + NET rankings from ncaa.com. Returns name→rank maps."""
    print("[NCAA.com] Fetching AP + NET rankings …")
    ap_rows, net_rows = [], []
    ap_map,  net_map  = {}, {}

    def scrape_rankings_table(url, rank_col="RANK", school_col="SCHOOL"):
        html = _get(url)
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        results = []
        table   = soup.select_one("table")
        if not table:
            return results
        headers = [th.get_text(strip=True) for th in table.select("thead th")]
        for tr in table.select("tbody tr"):
            cells = [td.get_text(strip=True) for td in tr.select("td")]
            if not cells:
                continue
            row = dict(zip(headers, cells))
            results.append(row)
        return results

    # AP poll
    for entry in scrape_rankings_table(NCAA_AP_URL):
        school = entry.get("SCHOOL", entry.get("School", "")).split("(")[0].strip()
        rank   = _safe_int(entry.get("RANK", entry.get("Rank")), 999)
        if not school: continue
        ap_rows.append({
            "rank": rank, "school": school,
            "points":        entry.get("POINTS", ""),
            "previous_rank": entry.get("PREVIOUS", ""),
            "record":        entry.get("RECORD", ""),
        })
        for v in _name_variants(school):
            ap_map[v] = rank

    # NET rankings
    for entry in scrape_rankings_table(NCAA_NET_URL, rank_col="NET"):
        school = entry.get("SCHOOL", entry.get("Team", "")).strip()
        rank   = _safe_int(entry.get("NET", entry.get("RANK")), 999)
        if not school: continue
        net_rows.append({"net_rank": rank, "school": school})
        for v in _name_variants(school):
            net_map[v] = rank

    _write_csv(ap_rows,  out / "raw_rankings_ap.csv")
    _write_csv(net_rows, out / "raw_rankings_net.csv")
    print(f"  → AP: {len(ap_rows)} | NET: {len(net_rows)}")
    return ap_map, net_map


def fetch_raw_barttorvik(year: int, out: Path) -> dict:
    """
    Fetch Barttorvik data via teamslicejson.php — more reliable than trank.php.
    Falls back to trank.php if needed.
    """
    print(f"[Barttorvik] Fetching advanced stats ({year}) …")

    data = None

    # Primary: teamslicejson.php (used by Barttorvik's own site internally)
    for type_param in ["R", "A"]:   # R=regular season, A=all games
        resp = _get(
            f"{BBART_BASE}/teamslicejson.php",
            params={"year": year, "conyes": 1, "type": type_param},
            as_json=False
        )
        if resp and resp.strip().startswith("["):
            try:
                import json
                data = json.loads(resp)
                print(f"  → teamslicejson.php (type={type_param}) OK")
                break
            except Exception:
                pass

    # Fallback: trank.php CSV mode
    if not data:
        resp = _get(
            f"{BBART_BASE}/trank.php",
            params={"year": year, "csv": 1},
            as_json=False
        )
        if resp and "," in resp:
            try:
                import io
                reader = csv.DictReader(io.StringIO(resp))
                data   = list(reader)
                print("  → trank.php CSV fallback OK")
            except Exception as e:
                print(f"  [warn] CSV fallback failed: {e}")

    if not data:
        print("  [warn] All Barttorvik endpoints failed — advanced metrics unavailable")
        return {}

    raw_rows = []
    bbart    = {}

    rows = data if isinstance(data, list) else data.get("teams", [])
    for i, row in enumerate(rows):
        # teamslicejson returns arrays; trank returns dicts — handle both
        if isinstance(row, list):
            # teamslicejson columns (known order):
            # team, conf, g, rec, barthag, adj_oe, adj_de, adj_t, wab, ...
            if len(row) < 6: continue
            name = str(row[0]).strip()
            entry = {
                "team": name, "conf": str(row[1]),
                "barthag": row[4], "adjoe": row[5], "adjde": row[6],
                "adjtempo": row[7] if len(row) > 7 else 67,
            }
        else:
            name  = (str(row.get("team") or row.get("Team") or "")).strip()
            entry = row

        if not name: continue

        raw_row = {"trank_position": i + 1, "team": name}
        raw_row.update({k: v for k, v in entry.items() if k not in ("team", "Team")})
        raw_rows.append(raw_row)

        bbart[name.lower()] = {
            "trank":        i + 1,
            "barthag":      _safe_float(entry.get("barthag",  entry.get("Barthag")),   0.5),
            "adj_oe":       _safe_float(entry.get("adjoe",    entry.get("AdjOE")),    100.0),
            "adj_de":       _safe_float(entry.get("adjde",    entry.get("AdjDE")),    100.0),
            "adj_tempo":    _safe_float(entry.get("adjtempo", entry.get("AdjTempo")),  67.0),
            "sos":          _safe_float(entry.get("sos",      entry.get("SOS")),        0.5),
            "elite_sos":    _safe_float(entry.get("elite_sos"),                         0.0),
            "wins":         _safe_int(entry.get("wins",   entry.get("W")),  0),
            "losses":       _safe_int(entry.get("losses", entry.get("L")),  0),
            "ppg":          _safe_float(entry.get("obs_ef",  entry.get("ORtg")),  0.0),
            "opp_ppg":      _safe_float(entry.get("dbs_ef",  entry.get("DRtg")),  0.0),
            "efg_pct":      _safe_float(entry.get("efg_o",   entry.get("EFG%")),  0.0),
            "efg_d":        _safe_float(entry.get("efg_d"),                        0.0),
            "tov_pct":      _safe_float(entry.get("tov_o",   entry.get("TO%")),   0.0),
            "tov_d":        _safe_float(entry.get("tov_d"),                        0.0),
            "orb_pct":      _safe_float(entry.get("orb",     entry.get("OR%")),   0.0),
            "drb_pct":      _safe_float(entry.get("drb",     entry.get("DR%")),   0.0),
            "ft_rate":      _safe_float(entry.get("ftr",     entry.get("FTRate")),0.0),
            "ft_rate_d":    _safe_float(entry.get("ftrd"),                         0.0),
            "two_pt_pct":   _safe_float(entry.get("two_o",   entry.get("2P%")),   0.0),
            "three_pt_pct": _safe_float(entry.get("three_o", entry.get("3P%")),   0.0),
            "three_pt_d":   _safe_float(entry.get("three_d"),                      0.0),
            "blk_pct":      _safe_float(entry.get("blk"),                          0.0),
            "stl_pct":      _safe_float(entry.get("stl"),                          0.0),
            "avg_hgt":      _safe_float(entry.get("avg_hgt"),                      0.0),
            "experience":   _safe_float(entry.get("exp",     entry.get("Exp")),   0.0),
            "ap_rank":      _safe_int(entry.get("ap",        entry.get("APRank")),999),
        }

    _write_csv(raw_rows, out / "raw_barttorvik.csv")
    print(f"  → {len(bbart)} teams from Barttorvik")
    return bbart


# ─────────────────────────────────────────────────────────────────────────────
# Enrichment
# ─────────────────────────────────────────────────────────────────────────────
def enrich_teams(standings, ap_map, net_map, bbart) -> list[dict]:
    print("[ENRICH] Merging all sources …")
    teams = []

    for row in standings:
        name = row["name"]
        bb   = _find_bbart(name, bbart)

        wins   = _safe_int(row["overall_wins"],   bb.get("wins",   0))
        losses = _safe_int(row["overall_losses"],  bb.get("losses", 0))
        total  = wins + losses

        # Rank lookups with variant matching
        ap_rank  = 999
        net_rank = 200
        for v in _name_variants(name):
            if ap_rank  == 999 and v in ap_map:  ap_rank  = ap_map[v]
            if net_rank == 200 and v in net_map: net_rank = net_map[v]
        # Barttorvik ap_rank fallback
        if ap_rank == 999 and bb.get("ap_rank", 999) != 999:
            ap_rank = bb["ap_rank"]

        teams.append({
            "id":           row["id"],
            "name":         name,
            "abbrev":       name[:6].upper().replace(" ", ""),
            "conference":   row["conference"],
            "power_conf":   1 if row["conference"] in POWER_CONFERENCES else 0,
            "wins":         wins,
            "losses":       losses,
            "win_pct":      round(wins / total, 4) if total else 0,
            "conf_wins":    _safe_int(row.get("conf_wins",   0)),
            "conf_losses":  _safe_int(row.get("conf_losses", 0)),
            "streak":       row.get("streak", ""),
            "ap_rank":      ap_rank,
            "net_rank":     net_rank,
            "trank":        bb.get("trank",        200),
            "barthag":      bb.get("barthag",       0.5),
            "adj_oe":       bb.get("adj_oe",       100.0),
            "adj_de":       bb.get("adj_de",       100.0),
            "adj_margin":   round(bb.get("adj_oe", 100.0) - bb.get("adj_de", 100.0), 2),
            "adj_tempo":    bb.get("adj_tempo",     67.0),
            "sos":          bb.get("sos",            0.5),
            "elite_sos":    bb.get("elite_sos",      0.0),
            "ppg":          bb.get("ppg",            0.0),
            "opp_ppg":      bb.get("opp_ppg",        0.0),
            "efg_pct":      bb.get("efg_pct",        0.0),
            "efg_d":        bb.get("efg_d",          0.0),
            "tov_pct":      bb.get("tov_pct",        0.0),
            "tov_d":        bb.get("tov_d",          0.0),
            "orb_pct":      bb.get("orb_pct",        0.0),
            "drb_pct":      bb.get("drb_pct",        0.0),
            "ft_rate":      bb.get("ft_rate",        0.0),
            "ft_rate_d":    bb.get("ft_rate_d",      0.0),
            "two_pt_pct":   bb.get("two_pt_pct",     0.0),
            "three_pt_pct": bb.get("three_pt_pct",   0.0),
            "three_pt_d":   bb.get("three_pt_d",     0.0),
            "blk_pct":      bb.get("blk_pct",        0.0),
            "stl_pct":      bb.get("stl_pct",        0.0),
            "avg_hgt":      bb.get("avg_hgt",        0.0),
            "experience":   bb.get("experience",     0.0),
            "seed_score":   0,
            "projected_seed":   "Not in field",
            "projected_region": "—",
        })

    print(f"  → {len(teams)} teams enriched")
    return teams


# ─────────────────────────────────────────────────────────────────────────────
# Seeding
# ─────────────────────────────────────────────────────────────────────────────
def compute_seed_score(team: dict) -> float:
    win_pct = team.get("win_pct",    0)
    net     = team.get("net_rank",  200)
    trank   = team.get("trank",     200)
    sos     = team.get("sos",        0.5)
    margin  = team.get("adj_margin", 0)
    return round(
        (win_pct             * 35) +
        ((200 - net)  / 200  * 25) +
        ((200 - trank)/ 200  * 20) +
        (sos                 * 10) +
        (min(max(margin, -30), 30) / 30 * 10),
        4
    )


def assign_seeds(teams: list[dict]) -> list[dict]:
    regions = ["East", "West", "South", "Midwest"]
    for i, team in enumerate(sorted(teams, key=lambda t: t["seed_score"], reverse=True)):
        r = i + 1
        if r <= 64:
            team["projected_seed"]   = ((r - 1) // 4) + 1
            team["projected_region"] = regions[(r - 1) % 4]
        elif r <= 68:
            team["projected_seed"]   = "First Four"
            team["projected_region"] = "Play-in"
        elif r <= 72:
            team["projected_seed"]   = "Bubble"
            team["projected_region"] = "—"
        else:
            team["projected_seed"]   = "Not in field"
            team["projected_region"] = "—"
    return teams


# ─────────────────────────────────────────────────────────────────────────────
# Bracket simulation
# ─────────────────────────────────────────────────────────────────────────────
def simulate_game(a, b):
    sa, sb = a.get("barthag", a["seed_score"]/100), b.get("barthag", b["seed_score"]/100)
    t = sa + sb
    return a if random.random() < (sa/t if t else 0.5) else b


def simulate_bracket(field, simulations=1000):
    rounds     = ["r64","r32","s16","e8","f4","championship","champion"]
    win_counts = {t["id"]: {r: 0 for r in rounds} for t in field}
    tourney    = [t for t in field if isinstance(t.get("projected_seed"), int) and t["projected_seed"] <= 16]

    for _ in range(simulations):
        by_region = {}
        for t in tourney:
            by_region.setdefault(t["projected_region"], []).append(t)

        ff = []
        for region, rteams in by_region.items():
            survivors = sorted(rteams, key=lambda t: t["projected_seed"])
            for rnd in ["r64","r32","s16","e8"]:
                nxt = []
                for j in range(0, len(survivors), 2):
                    w = simulate_game(survivors[j], survivors[j+1]) if j+1 < len(survivors) else survivors[j]
                    win_counts[w["id"]][rnd] += 1
                    nxt.append(w)
                survivors = nxt
            if survivors: ff.append(survivors[0])

        cg = []
        for j in range(0, len(ff), 2):
            w = simulate_game(ff[j], ff[j+1]) if j+1 < len(ff) else ff[j]
            win_counts[w["id"]]["f4"] += 1
            cg.append(w)

        if len(cg) >= 2:
            ch = simulate_game(cg[0], cg[1])
            win_counts[ch["id"]]["championship"] += 1
            win_counts[ch["id"]]["champion"]     += 1
        elif cg:
            win_counts[cg[0]["id"]]["championship"] += 1
            win_counts[cg[0]["id"]]["champion"]     += 1

    for t in field:
        counts = win_counts.get(t["id"], {r: 0 for r in rounds})
        for r in rounds:
            t[f"prob_{r}"] = round(counts[r] / simulations, 4)
    return field


# ─────────────────────────────────────────────────────────────────────────────
# Feature engineering
# ─────────────────────────────────────────────────────────────────────────────
def build_model_features(teams: list[dict]) -> list[dict]:
    if not teams:
        return []

    numeric_cols = [
        "win_pct","net_rank","trank","barthag","adj_oe","adj_de",
        "adj_margin","adj_tempo","sos","elite_sos",
        "efg_pct","efg_d","tov_pct","tov_d","orb_pct","drb_pct",
        "ft_rate","ft_rate_d","two_pt_pct","three_pt_pct","three_pt_d",
        "blk_pct","stl_pct","avg_hgt","experience",
        "conf_wins","conf_losses","wins","losses",
        "prob_r64","prob_r32","prob_s16","prob_e8",
        "prob_f4","prob_championship","prob_champion",
    ]

    mins = {c: min(t.get(c, 0) for t in teams) for c in numeric_cols}
    maxs = {c: max(t.get(c, 0) for t in teams) for c in numeric_cols}

    def norm(val, col):
        lo, hi = mins[col], maxs[col]
        return round((val - lo) / (hi - lo), 6) if hi > lo else 0.0

    all_confs   = sorted(set(t["conference"] for t in teams))
    all_regions = ["East", "West", "South", "Midwest", "Play-in", "—"]

    rows = []
    for t in teams:
        row = {"name": t["name"], "conference": t["conference"], "power_conf": t["power_conf"]}
        for c in numeric_cols:
            row[f"norm_{c}"] = norm(t.get(c, 0), c)
        row["target_seed"]          = t["projected_seed"] if isinstance(t["projected_seed"], int) else -1
        row["target_in_tourney"]    = 1 if isinstance(t["projected_seed"], int) else 0
        row["target_prob_champion"] = t.get("prob_champion", 0)
        row["target_region"]        = t.get("projected_region", "—")
        for conf in all_confs:
            row[f"conf_{conf.replace(' ','_').replace('-','_')}"] = 1 if t["conference"] == conf else 0
        for reg in all_regions:
            row[f"region_{reg.replace(' ','_').replace('-','_')}"] = 1 if t.get("projected_region") == reg else 0
        rows.append(row)

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────────────────────
def export_all(teams, model_rows, out, ts):
    tourney = sorted(
        [t for t in teams if isinstance(t.get("projected_seed"), int)],
        key=lambda t: (t["projected_region"], t["projected_seed"])
    )
    _write_csv(teams,      out / f"team_stats_{ts}.csv")
    _write_csv(tourney,    out / f"bracket_predictions_{ts}.csv")
    _write_csv(model_rows, out / f"model_features_{ts}.csv")
    _write_csv(teams,      out / "team_stats_latest.csv")
    _write_csv(tourney,    out / "bracket_predictions_latest.csv")
    _write_csv(model_rows, out / "model_features_latest.csv")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulations", type=int, default=1000)
    parser.add_argument("--output",      type=str, default="output")
    parser.add_argument("--year",        type=int, default=SEASON)
    args = parser.parse_args()

    out = Path(args.output)
    out.mkdir(exist_ok=True)
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("=" * 60)
    print("  🏀  March Madness Tracker  🏀")
    print(f"  Sources: ncaa.com + Barttorvik  |  Season {args.year}")
    print("=" * 60)

    standings       = fetch_raw_standings(out)
    ap_map, net_map = fetch_raw_rankings(out)
    bbart           = fetch_raw_barttorvik(args.year, out)

    if not standings:
        print("\n❌ No standings data — cannot continue. Check network/sources.")
        return

    teams = enrich_teams(standings, ap_map, net_map, bbart)
    for t in teams:
        t["seed_score"] = compute_seed_score(t)
    teams = assign_seeds(teams)

    print(f"\n[SIM] Running {args.simulations:,} bracket simulations …")
    teams = simulate_bracket(teams, simulations=args.simulations)
    print("  → Done")

    print("\n[FEATURES] Building model feature matrix …")
    model_rows = build_model_features(teams)
    print(f"  → {len(model_rows)} rows × {len(model_rows[0]) if model_rows else 0} features")

    print("\n[EXPORT] Writing CSVs …")
    export_all(teams, model_rows, out, ts)

    print(f"""
✅  Done!  →  {out.resolve()}

  RAW:        raw_standings.csv / raw_barttorvik.csv
              raw_rankings_ap.csv / raw_rankings_net.csv
  PROCESSED:  team_stats_latest.csv  ({len(teams)} teams)
              bracket_predictions_latest.csv  ({sum(1 for t in teams if isinstance(t.get('projected_seed'), int))} tourney teams)
              model_features_latest.csv  ({len(model_rows)} rows)
""")


if __name__ == "__main__":
    main()