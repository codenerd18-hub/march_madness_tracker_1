"""
March Madness Tracker
=====================
Fetches NCAA Men's Basketball stats, predicts seedings,
and generates bracket predictions. Exports everything to CSV.

Data sources:
  - henrygd/ncaa-api  → live scores, standings, rankings (no key needed)
  - barttorvik.com    → NET rankings, T-Rank, advanced metrics (no key needed)

Output files (all 360 D1 teams unless noted):
  RAW (unmodified API data):
    raw_standings.csv          - wins/losses/conference from NCAA API
    raw_rankings_ap.csv        - AP poll top 25
    raw_rankings_net.csv       - NET rankings (all ranked teams)
    raw_barttorvik.csv         - all Barttorvik T-Rank advanced metrics

  PROCESSED (merged, engineered, model-ready):
    team_stats_latest.csv      - all ~360 teams, merged metrics + seedings
    advanced_stats_latest.csv  - all ~360 teams, full advanced metrics
    bracket_predictions_latest.csv - 68 tournament teams + round win probs
    model_features_latest.csv  - fully normalized, model-ready feature matrix
"""

import csv
import math
import random
import argparse
from datetime import datetime
from pathlib import Path

import requests

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

NCAA_API_BASE = "https://ncaa-api.henrygd.me"
BBART_BASE    = "https://barttorvik.com"
HEADERS       = {"User-Agent": "MarchMadnessTracker/1.0 (educational project)"}

CURRENT_YEAR  = datetime.now().year
SEASON        = CURRENT_YEAR if datetime.now().month >= 10 else CURRENT_YEAR - 1

POWER_CONFERENCES = {
    "SEC", "Big Ten", "Big 12", "ACC", "Big East",
    "American", "Mountain West", "Atlantic 10", "WCC", "Pac-12"
}


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────
def ncaa_get(path: str, params: dict = None):
    try:
        r = requests.get(f"{NCAA_API_BASE}{path}", params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [warn] NCAA API {path}: {e}")
        return None


def bbart_get(path: str, params: dict = None):
    try:
        r = requests.get(f"{BBART_BASE}{path}", params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [warn] Barttorvik {path}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# RAW data fetchers  (return unmodified API data + save raw CSVs)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_raw_standings(out: Path) -> list[dict]:
    """Fetch ALL D1 standings — no limit. Save raw CSV."""
    print("[NCAA API] Fetching ALL D1 standings …")
    data = ncaa_get("/standings/basketball-men/d1")
    if not data:
        return []

    raw_rows = []
    team_id  = 1

    for conf_block in data.get("data", []):
        conf = conf_block.get("conference", "Unknown")
        for entry in conf_block.get("standings", []):
            row = {
                "id":                str(team_id),
                "name":              entry.get("School", "Unknown"),
                "conference":        conf,
                "conf_wins":         entry.get("Conference W", 0),
                "conf_losses":       entry.get("Conference L", 0),
                "conf_pct":          entry.get("Conference PCT", 0),
                "overall_wins":      entry.get("Overall W", 0),
                "overall_losses":    entry.get("Overall L", 0),
                "overall_pct":       entry.get("Overall PCT", 0),
                "streak":            entry.get("Overall STREAK", ""),
            }
            raw_rows.append(row)
            team_id += 1

    # Save raw
    _write_csv(raw_rows, out / "raw_standings.csv")
    print(f"  → {len(raw_rows)} teams")
    return raw_rows


def fetch_raw_rankings(out: Path) -> tuple[dict, dict]:
    """Fetch AP + NET rankings. Save raw CSVs. Return name→rank dicts."""
    print("[NCAA API] Fetching AP + NET rankings …")

    ap_rows, net_rows = [], []
    ap_map,  net_map  = {}, {}

    # AP
    ap_data = ncaa_get("/rankings/basketball-men/d1/associated-press")
    if ap_data:
        for entry in ap_data.get("data", []):
            school = entry.get("SCHOOL", "").split("(")[0].strip()
            rank   = _safe_int(entry.get("RANK"), 999)
            points = entry.get("POINTS", "")
            prev   = entry.get("PREVIOUS", "")
            record = entry.get("RECORD", "")
            if school:
                ap_rows.append({"rank": rank, "school": school, "points": points,
                                "previous_rank": prev, "record": record})
                # Store under multiple name variants for better matching
                for variant in _name_variants(school):
                    ap_map[variant] = rank
    _write_csv(ap_rows, out / "raw_rankings_ap.csv")

    # NET
    net_data = ncaa_get("/rankings/basketball-men/d1/ncaa-mens-basketball-net-rankings")
    if net_data:
        for entry in net_data.get("data", []):
            school = entry.get("SCHOOL", entry.get("Team", "")).strip()
            rank   = _safe_int(entry.get("RANK", entry.get("NET")), 999)
            if school:
                net_rows.append({"net_rank": rank, "school": school})
                for variant in _name_variants(school):
                    net_map[variant] = rank
    _write_csv(net_rows, out / "raw_rankings_net.csv")

    print(f"  → AP: {len(ap_rows)} teams | NET: {len(net_rows)} teams")
    return ap_map, net_map


def _name_variants(name: str) -> list[str]:
    """
    Generate multiple lowercase variants of a school name
    so fuzzy matching works across NCAA API / Barttorvik / standings
    name differences (e.g. 'UConn' vs 'Connecticut', 'Saint Mary's (CA)' vs "Saint Mary's").
    """
    base = name.lower().strip()
    # Strip parenthetical suffixes like "(CA)", "(OH)"
    no_paren = base.split("(")[0].strip()
    # Common abbreviation expansions
    ALIASES = {
        "uconn":            "connecticut",
        "connecticut":      "uconn",
        "lsu":              "louisiana state",
        "louisiana state":  "lsu",
        "ucl a":            "ucla",
        "usc":              "southern california",
        "southern cal":     "southern california",
        "ole miss":         "mississippi",
        "mississippi":      "ole miss",
        "pitt":             "pittsburgh",
        "pittsburgh":       "pitt",
        "miami (fl)":       "miami",
        "miami fl":         "miami",
        "nc state":         "north carolina state",
        "north carolina st":"nc state",
        "saint mary's":     "st. mary's",
        "st. mary's":       "saint mary's",
        "smu":              "southern methodist",
        "vcu":              "virginia commonwealth",
        "unlv":             "nevada las vegas",
        "utep":             "texas el paso",
        "utsa":             "texas san antonio",
        "tcu":              "texas christian",
        "byu":              "brigham young",
        "wku":              "western kentucky",
        "fiu":              "florida international",
        "fau":              "florida atlantic",
        "uab":              "alabama birmingham",
        "umass":            "massachusetts",
        "unc":              "north carolina",
        "north carolina":   "unc",
    }
    variants = {base, no_paren}
    # Add alias if known
    if no_paren in ALIASES:
        variants.add(ALIASES[no_paren])
    if base in ALIASES:
        variants.add(ALIASES[base])
    # Strip common suffixes
    for suffix in [" university", " college", " state", " st."]:
        variants.add(no_paren.replace(suffix, "").strip())
    return list(variants)


def fetch_raw_barttorvik(year: int, out: Path) -> dict:
    """Fetch all Barttorvik T-Rank data. Save raw CSV. Return name→stats dict."""
    print(f"[Barttorvik] Fetching T-Rank advanced stats ({year}) …")
    data = bbart_get("/trank.php", params={"year": year, "json": 1})

    raw_rows = []
    bbart    = {}

    if not data:
        print("  [warn] No Barttorvik data — advanced metrics will use defaults")
        return bbart

    rows = data if isinstance(data, list) else data.get("teams", [])
    for i, row in enumerate(rows):
        name = (row.get("team") or row.get("Team") or "").strip()
        if not name:
            continue

        # Capture every field Barttorvik returns as-is for the raw file
        raw_row = {"trank_position": i + 1, "team": name}
        raw_row.update({k: v for k, v in row.items() if k not in ("team", "Team")})
        raw_rows.append(raw_row)

        # Structured subset for enrichment
        bbart[name.lower()] = {
            "trank":        i + 1,
            "barthag":      _safe_float(row.get("barthag",  row.get("Barthag")),   0.5),
            "adj_oe":       _safe_float(row.get("adjoe",    row.get("AdjOE")),     100.0),
            "adj_de":       _safe_float(row.get("adjde",    row.get("AdjDE")),     100.0),
            "adj_tempo":    _safe_float(row.get("adjtempo", row.get("AdjTempo")),   67.0),
            "sos":          _safe_float(row.get("sos",      row.get("SOS")),         0.5),
            "elite_sos":    _safe_float(row.get("elite_sos"),                        0.0),
            "conf":         str(row.get("conf", row.get("Conf", ""))),
            "seed":         _safe_int(row.get("seed"), 0),           # actual seed if known
            "wins":         _safe_int(row.get("wins",  row.get("W")), 0),
            "losses":       _safe_int(row.get("losses",row.get("L")), 0),
            "ppg":          _safe_float(row.get("obs_ef",  row.get("ORtg")),  0.0),
            "opp_ppg":      _safe_float(row.get("dbs_ef",  row.get("DRtg")),  0.0),
            # Shooting / four factors
            "efg_pct":      _safe_float(row.get("efg_o",   row.get("EFG%")),   0.0),
            "efg_d":        _safe_float(row.get("efg_d"),                       0.0),
            "tov_pct":      _safe_float(row.get("tov_o",   row.get("TO%")),     0.0),
            "tov_d":        _safe_float(row.get("tov_d"),                       0.0),
            "orb_pct":      _safe_float(row.get("orb",     row.get("OR%")),     0.0),
            "drb_pct":      _safe_float(row.get("drb",     row.get("DR%")),     0.0),
            "ft_rate":      _safe_float(row.get("ftr",     row.get("FTRate")),  0.0),
            "ft_rate_d":    _safe_float(row.get("ftrd"),                        0.0),
            "two_pt_pct":   _safe_float(row.get("two_o",   row.get("2P%")),     0.0),
            "three_pt_pct": _safe_float(row.get("three_o", row.get("3P%")),     0.0),
            "three_pt_d":   _safe_float(row.get("three_d"),                     0.0),
            "blk_pct":      _safe_float(row.get("blk"),                         0.0),
            "stl_pct":      _safe_float(row.get("stl"),                         0.0),
            "avg_hgt":      _safe_float(row.get("avg_hgt"),                     0.0),
            "experience":   _safe_float(row.get("exp",     row.get("Exp")),     0.0),
            "ap_rank":      _safe_int(row.get("ap",      row.get("APRank")),   999),
        }

    _write_csv(raw_rows, out / "raw_barttorvik.csv")
    print(f"  → {len(bbart)} teams")
    return bbart


# ─────────────────────────────────────────────────────────────────────────────
# Enrichment — merge all sources into one team list
# ─────────────────────────────────────────────────────────────────────────────
def enrich_teams(standings: list[dict], ap_map: dict, net_map: dict,
                 bbart: dict) -> list[dict]:
    print("[ENRICH] Merging all sources …")
    teams = []

    for row in standings:
        name = row["name"]

        bb = _find_bbart(name, bbart)

        wins   = _safe_int(row["overall_wins"],   bb.get("wins",   0))
        losses = _safe_int(row["overall_losses"],  bb.get("losses", 0))
        total  = wins + losses

        # Use all name variants for ranking lookups to handle UConn/Connecticut etc.
        ap_rank  = 999
        net_rank = 200
        for variant in _name_variants(name):
            if ap_rank  == 999 and variant in ap_map:  ap_rank  = ap_map[variant]
            if net_rank == 200 and variant in net_map: net_rank = net_map[variant]
        # Fallback: Barttorvik tracks ap_rank directly
        if ap_rank == 999 and bb.get("ap_rank", 999) != 999:
            ap_rank = bb["ap_rank"]

        team = {
            # Identity
            "id":           row["id"],
            "name":         name,
            "abbrev":       name[:6].upper().replace(" ", ""),
            "conference":   row["conference"],
            "power_conf":   1 if row["conference"] in POWER_CONFERENCES else 0,
            # Record
            "wins":         wins,
            "losses":       losses,
            "win_pct":      round(wins / total, 4) if total else 0,
            "conf_wins":    _safe_int(row["conf_wins"],   0),
            "conf_losses":  _safe_int(row["conf_losses"], 0),
            "streak":       row.get("streak", ""),
            # Rankings
            "ap_rank":      ap_rank,
            "net_rank":     net_rank,
            # Barttorvik advanced
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
            # Four factors (offense)
            "efg_pct":      bb.get("efg_pct",        0.0),
            "tov_pct":      bb.get("tov_pct",        0.0),
            "orb_pct":      bb.get("orb_pct",        0.0),
            "ft_rate":      bb.get("ft_rate",        0.0),
            # Four factors (defense)
            "efg_d":        bb.get("efg_d",          0.0),
            "tov_d":        bb.get("tov_d",          0.0),
            "drb_pct":      bb.get("drb_pct",        0.0),
            "ft_rate_d":    bb.get("ft_rate_d",      0.0),
            # Shooting splits
            "two_pt_pct":   bb.get("two_pt_pct",     0.0),
            "three_pt_pct": bb.get("three_pt_pct",   0.0),
            "three_pt_d":   bb.get("three_pt_d",     0.0),
            "blk_pct":      bb.get("blk_pct",        0.0),
            "stl_pct":      bb.get("stl_pct",        0.0),
            # Roster
            "avg_hgt":      bb.get("avg_hgt",        0.0),
            "experience":   bb.get("experience",     0.0),
            # Computed later
            "seed_score":   0,
            "projected_seed":   "Not in field",
            "projected_region": "—",
        }
        teams.append(team)

    print(f"  → {len(teams)} teams enriched")
    return teams


# ─────────────────────────────────────────────────────────────────────────────
# Seeding
# ─────────────────────────────────────────────────────────────────────────────
def compute_seed_score(team: dict) -> float:
    win_pct = team.get("win_pct", 0)
    net     = team.get("net_rank",  200)
    trank   = team.get("trank",     200)
    sos     = team.get("sos",       0.5)
    margin  = team.get("adj_margin", 0)

    score = (
        (win_pct             * 35) +
        ((200 - net)  / 200  * 25) +
        ((200 - trank)/ 200  * 20) +
        (sos                 * 10) +
        (min(max(margin, -30), 30) / 30 * 10)   # cap margin contribution
    )
    return round(score, 4)


def assign_seeds(teams: list[dict]) -> list[dict]:
    sorted_teams = sorted(teams, key=lambda t: t["seed_score"], reverse=True)
    regions      = ["East", "West", "South", "Midwest"]

    for i, team in enumerate(sorted_teams):
        rank = i + 1
        if rank <= 64:
            team["projected_seed"]   = ((rank - 1) // 4) + 1
            team["projected_region"] = regions[(rank - 1) % 4]
        elif rank <= 68:
            team["projected_seed"]   = "First Four"
            team["projected_region"] = "Play-in"
        elif rank <= 72:
            team["projected_seed"]   = "Bubble"
            team["projected_region"] = "—"
        else:
            team["projected_seed"]   = "Not in field"
            team["projected_region"] = "—"

    return sorted_teams


# ─────────────────────────────────────────────────────────────────────────────
# Bracket simulation
# ─────────────────────────────────────────────────────────────────────────────
def simulate_game(a: dict, b: dict) -> dict:
    sa = a.get("barthag", a["seed_score"] / 100)
    sb = b.get("barthag", b["seed_score"] / 100)
    t  = sa + sb
    return a if random.random() < (sa / t if t else 0.5) else b


def simulate_bracket(field: list[dict], simulations: int = 1000) -> list[dict]:
    rounds     = ["r64", "r32", "s16", "e8", "f4", "championship", "champion"]
    win_counts = {t["id"]: {r: 0 for r in rounds} for t in field}
    tourney    = [t for t in field
                  if isinstance(t.get("projected_seed"), int) and t["projected_seed"] <= 16]

    for _ in range(simulations):
        by_region: dict[str, list] = {}
        for t in tourney:
            by_region.setdefault(t["projected_region"], []).append(t)

        final_four: list[dict] = []
        for region, rteams in by_region.items():
            survivors = sorted(rteams, key=lambda t: t["projected_seed"])
            for rnd in ["r64", "r32", "s16", "e8"]:
                nxt = []
                for j in range(0, len(survivors), 2):
                    w = simulate_game(survivors[j], survivors[j+1]) if j+1 < len(survivors) else survivors[j]
                    win_counts[w["id"]][rnd] += 1
                    nxt.append(w)
                survivors = nxt
            if survivors:
                final_four.append(survivors[0])

        champ_game: list[dict] = []
        for j in range(0, len(final_four), 2):
            w = simulate_game(final_four[j], final_four[j+1]) if j+1 < len(final_four) else final_four[j]
            win_counts[w["id"]]["f4"] += 1
            champ_game.append(w)

        if len(champ_game) >= 2:
            champ = simulate_game(champ_game[0], champ_game[1])
            win_counts[champ["id"]]["championship"] += 1
            win_counts[champ["id"]]["champion"]     += 1
        elif champ_game:
            win_counts[champ_game[0]["id"]]["championship"] += 1
            win_counts[champ_game[0]["id"]]["champion"]     += 1

    for team in field:
        counts = win_counts.get(team["id"], {r: 0 for r in rounds})
        for r in rounds:
            team[f"prob_{r}"] = round(counts[r] / simulations, 4)

    return field


# ─────────────────────────────────────────────────────────────────────────────
# Feature engineering for model training
# ─────────────────────────────────────────────────────────────────────────────
def build_model_features(teams: list[dict]) -> list[dict]:
    """
    Produces a normalized, model-ready feature matrix.
    - All numeric features scaled 0-1 using observed min/max
    - Categorical features one-hot encoded (conference, region)
    - Target columns included: projected_seed (numeric only), prob_champion
    """
    numeric_cols = [
        "win_pct", "net_rank", "trank", "barthag", "adj_oe", "adj_de",
        "adj_margin", "adj_tempo", "sos", "elite_sos",
        "efg_pct", "efg_d", "tov_pct", "tov_d", "orb_pct", "drb_pct",
        "ft_rate", "ft_rate_d", "two_pt_pct", "three_pt_pct", "three_pt_d",
        "blk_pct", "stl_pct", "avg_hgt", "experience",
        "conf_wins", "conf_losses", "wins", "losses",
        "prob_r64", "prob_r32", "prob_s16", "prob_e8",
        "prob_f4", "prob_championship", "prob_champion",
    ]

    # Compute min/max for normalization
    mins = {c: min(t.get(c, 0) for t in teams) for c in numeric_cols}
    maxs = {c: max(t.get(c, 0) for t in teams) for c in numeric_cols}

    def norm(val, col):
        lo, hi = mins[col], maxs[col]
        return round((val - lo) / (hi - lo), 6) if hi > lo else 0.0

    all_confs   = sorted(set(t["conference"] for t in teams))
    all_regions = ["East", "West", "South", "Midwest", "Play-in", "—"]

    rows = []
    for t in teams:
        row = {
            "name":       t["name"],
            "conference": t["conference"],
            "power_conf": t["power_conf"],
        }

        # Normalized numeric features
        for c in numeric_cols:
            row[f"norm_{c}"] = norm(t.get(c, 0), c)

        # Raw target labels
        row["target_seed"]         = t["projected_seed"] if isinstance(t["projected_seed"], int) else -1
        row["target_in_tourney"]   = 1 if isinstance(t["projected_seed"], int) else 0
        row["target_prob_champion"]= t.get("prob_champion", 0)
        row["target_region"]       = t.get("projected_region", "—")

        # One-hot: conference
        for conf in all_confs:
            row[f"conf_{conf.replace(' ', '_').replace('-', '_')}"] = 1 if t["conference"] == conf else 0

        # One-hot: projected region
        for reg in all_regions:
            row[f"region_{reg.replace(' ', '_').replace('-', '_')}"] = 1 if t.get("projected_region") == reg else 0

        rows.append(row)

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# CSV writers
# ─────────────────────────────────────────────────────────────────────────────
def _write_csv(rows: list[dict], path: Path) -> None:
    if not rows:
        print(f"  [skip] No data for {path.name}")
        return
    fields = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"[CSV] {path.name}  ({len(rows)} rows)")


def export_all(teams: list[dict], model_rows: list[dict], out: Path, ts: str) -> None:
    team_fields = [
        "name", "abbrev", "conference", "power_conf",
        "wins", "losses", "win_pct", "conf_wins", "conf_losses", "streak",
        "ap_rank", "net_rank", "trank", "barthag",
        "adj_oe", "adj_de", "adj_margin", "adj_tempo",
        "sos", "elite_sos", "ppg", "opp_ppg",
        "efg_pct", "efg_d", "tov_pct", "tov_d",
        "orb_pct", "drb_pct", "ft_rate", "ft_rate_d",
        "two_pt_pct", "three_pt_pct", "three_pt_d",
        "blk_pct", "stl_pct", "avg_hgt", "experience",
        "seed_score", "projected_seed", "projected_region",
    ]
    bracket_fields = [
        "name", "abbrev", "conference", "projected_seed", "projected_region",
        "seed_score", "barthag", "adj_margin", "net_rank", "trank",
        "prob_r64", "prob_r32", "prob_s16", "prob_e8",
        "prob_f4", "prob_championship", "prob_champion",
    ]

    tourney = sorted(
        [t for t in teams if isinstance(t.get("projected_seed"), int)],
        key=lambda t: (t["projected_region"], t["projected_seed"])
    )

    # Timestamped
    _write_csv(teams,       out / f"team_stats_{ts}.csv")
    _write_csv(tourney,     out / f"bracket_predictions_{ts}.csv")
    _write_csv(model_rows,  out / f"model_features_{ts}.csv")

    # Latest (overwrite)
    _write_csv(teams,       out / "team_stats_latest.csv")
    _write_csv(tourney,     out / "bracket_predictions_latest.csv")
    _write_csv(model_rows,  out / "model_features_latest.csv")


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────
def _safe_int(val, default=0) -> int:
    try:    return int(val)
    except: return default

def _safe_float(val, default=0.0) -> float:
    try:    return float(val)
    except: return default

def _find_bbart(name: str, bbart: dict) -> dict:
    key = name.lower().strip()
    if key in bbart:
        return bbart[key]
    for suffix in [" university", " college", " state", " st.", " a&m"]:
        short = key.replace(suffix, "").strip()
        if short in bbart:
            return bbart[short]
    for k, v in bbart.items():
        if k in key or key in k:
            return v
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="March Madness Tracker")
    parser.add_argument("--simulations", type=int, default=1000)
    parser.add_argument("--output",      type=str, default="output")
    parser.add_argument("--year",        type=int, default=SEASON)
    args = parser.parse_args()

    out = Path(args.output)
    out.mkdir(exist_ok=True)
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("=" * 60)
    print("  🏀  March Madness Tracker  🏀")
    print(f"  Sources: NCAA API + Barttorvik  |  Season {args.year}")
    print("=" * 60)

    # ── 1. Fetch raw data (saves raw CSVs automatically) ──────────────────
    standings         = fetch_raw_standings(out)
    ap_map, net_map   = fetch_raw_rankings(out)
    bbart             = fetch_raw_barttorvik(args.year, out)

    # ── 2. Merge & enrich ─────────────────────────────────────────────────
    teams = enrich_teams(standings, ap_map, net_map, bbart)

    # ── 3. Seed scoring ───────────────────────────────────────────────────
    for t in teams:
        t["seed_score"] = compute_seed_score(t)
    teams = assign_seeds(teams)

    # ── 4. Bracket simulation ─────────────────────────────────────────────
    print(f"\n[SIM] Running {args.simulations:,} bracket simulations …")
    teams = simulate_bracket(teams, simulations=args.simulations)
    print("  → Done")

    # ── 5. Feature engineering ────────────────────────────────────────────
    print("\n[FEATURES] Building model feature matrix …")
    model_rows = build_model_features(teams)
    print(f"  → {len(model_rows)} rows × {len(model_rows[0]) if model_rows else 0} features")

    # ── 6. Export everything ──────────────────────────────────────────────
    print("\n[EXPORT] Writing CSVs …")
    export_all(teams, model_rows, out, ts)

    print(f"""
✅  All done!  Output → {out.resolve()}

  RAW (unprocessed API data):
    raw_standings.csv          – wins/losses/conf for all ~360 D1 teams
    raw_rankings_ap.csv        – AP Top 25
    raw_rankings_net.csv       – NCAA NET rankings
    raw_barttorvik.csv         – full T-Rank dump (all available fields)

  PROCESSED:
    team_stats_latest.csv      – all teams, merged metrics + seedings
    bracket_predictions_latest.csv – 64 tourney teams + round win probs
    model_features_latest.csv  – normalized feature matrix for model training
                                  (0-1 scaled numerics + one-hot categoricals)
""")


if __name__ == "__main__":
    main()