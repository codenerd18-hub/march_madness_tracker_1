"""
March Madness Tracker
=====================
Fetches NCAA Men's Basketball stats, predicts seedings,
and generates bracket predictions. Exports everything to CSV.

Data sources:
  - henrygd/ncaa-api  → live scores, standings, rankings (no key needed)
  - barttorvik.com    → NET rankings, T-Rank, advanced metrics (no key needed)
"""

import csv
import json
import time
import random
import argparse
from datetime import datetime
from pathlib import Path

import requests

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

# ── API base URLs ─────────────────────────────────────────────────────────────
NCAA_API_BASE  = "https://ncaa-api.henrygd.me"
BBART_BASE     = "https://barttorvik.com"

HEADERS = {"User-Agent": "MarchMadnessTracker/1.0 (educational project)"}

# Current season year (update each year)
CURRENT_YEAR = datetime.now().year
SEASON       = CURRENT_YEAR if datetime.now().month >= 10 else CURRENT_YEAR - 1

# ── Conferences that historically send the most NCAA Tourney teams ─────────────
POWER_CONFERENCES = {
    "SEC", "Big Ten", "Big 12", "ACC", "Big East",
    "Pac-12", "American", "Mountain West", "Atlantic 10", "WCC"
}


# ── Seeding helpers ───────────────────────────────────────────────────────────
def compute_seed_score(team: dict) -> float:
    """
    Composite score used for projected seeding. Higher = better seed.
    Components:
      - Win %           (40 pts) — heavily weighted
      - NET rank        (25 pts) — official NCAA metric
      - T-Rank/Barthag  (20 pts) — Barttorvik strength metric
      - SOS             (15 pts) — strength of schedule
    """
    w     = team.get("wins",    0)
    l     = team.get("losses",  0)
    total = w + l
    win_pct = w / total if total else 0

    net   = team.get("net_rank",  200)
    trank = team.get("trank",     200)   # Barttorvik T-Rank
    sos   = team.get("sos",       0.5)   # 0–1 scale

    score = (
        (win_pct * 40) +
        ((200 - net)   / 200 * 25) +
        ((200 - trank) / 200 * 20) +
        (sos * 15)
    )
    return round(score, 3)


def assign_seeds(teams: list[dict]) -> list[dict]:
    """
    Sort teams by seed_score, assign projected seeds 1–16 per region.
    Top 68 teams make the field; next 4 are 'bubble'.
    """
    sorted_teams = sorted(teams, key=lambda t: t["seed_score"], reverse=True)
    regions      = ["East", "West", "South", "Midwest"]

    for i, team in enumerate(sorted_teams):
        rank = i + 1
        if rank <= 64:
            region_idx              = (rank - 1) % 4
            region                  = regions[region_idx]
            seed                    = ((rank - 1) // 4) + 1
            team["projected_seed"]   = seed
            team["projected_region"] = region
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


# ── Bracket simulation ────────────────────────────────────────────────────────
def simulate_game(team_a: dict, team_b: dict) -> dict:
    """
    Monte-Carlo game sim weighted by seed_score AND barthag (if available).
    Returns the winning team dict.
    """
    # Use barthag (win prob vs average D1) if available, else seed_score
    sa = team_a.get("barthag", team_a["seed_score"] / 100)
    sb = team_b.get("barthag", team_b["seed_score"] / 100)
    total    = sa + sb
    prob_a   = sa / total if total else 0.5
    return team_a if random.random() < prob_a else team_b


def simulate_bracket(field: list[dict], simulations: int = 1000) -> list[dict]:
    """
    Run `simulations` bracket sims. Enriches each team with:
      prob_r64, prob_r32, prob_s16, prob_e8, prob_f4,
      prob_championship, prob_champion
    """
    rounds     = ["r64", "r32", "s16", "e8", "f4", "championship", "champion"]
    win_counts = {t["id"]: {r: 0 for r in rounds} for t in field}

    tourney = [
        t for t in field
        if isinstance(t.get("projected_seed"), int) and t["projected_seed"] <= 16
    ]

    for _ in range(simulations):
        region_teams: dict[str, list] = {}
        for t in tourney:
            r = t["projected_region"]
            region_teams.setdefault(r, [])
            region_teams[r].append(t)

        final_four: list[dict] = []

        for region, rteams in region_teams.items():
            rteams_sorted = sorted(rteams, key=lambda t: t["projected_seed"])
            survivors     = rteams_sorted[:]

            for round_name in ["r64", "r32", "s16", "e8"]:
                next_round = []
                for j in range(0, len(survivors), 2):
                    if j + 1 < len(survivors):
                        winner = simulate_game(survivors[j], survivors[j + 1])
                    else:
                        winner = survivors[j]
                    win_counts[winner["id"]][round_name] += 1
                    next_round.append(winner)
                survivors = next_round

            if survivors:
                final_four.append(survivors[0])

        # Final Four
        champ_game: list[dict] = []
        for j in range(0, len(final_four), 2):
            if j + 1 < len(final_four):
                winner = simulate_game(final_four[j], final_four[j + 1])
            else:
                winner = final_four[j]
            win_counts[winner["id"]]["f4"] += 1
            champ_game.append(winner)

        # Championship
        if len(champ_game) >= 2:
            champion = simulate_game(champ_game[0], champ_game[1])
            win_counts[champion["id"]]["championship"] += 1
            win_counts[champion["id"]]["champion"]     += 1
        elif champ_game:
            win_counts[champ_game[0]["id"]]["championship"] += 1
            win_counts[champ_game[0]["id"]]["champion"]     += 1

    # Attach probabilities to each team
    for team in field:
        tid    = team["id"]
        counts = win_counts.get(tid, {r: 0 for r in rounds})
        for r in rounds:
            team[f"prob_{r}"] = round(counts[r] / simulations, 4)

    return field


# ── NCAA API helpers ──────────────────────────────────────────────────────────
def ncaa_get(path: str, params: dict = None) -> dict | list | None:
    """GET from the henrygd NCAA API with error handling."""
    url = f"{NCAA_API_BASE}{path}"
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [warn] NCAA API {path}: {e}")
        return None


def bbart_get(path: str, params: dict = None) -> dict | list | None:
    """GET from barttorvik.com with error handling."""
    url = f"{BBART_BASE}{path}"
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [warn] Barttorvik {path}: {e}")
        return None


# ── Data fetching ─────────────────────────────────────────────────────────────
def fetch_standings(limit: int = 100) -> list[dict]:
    """
    Fetch D1 men's basketball standings from the NCAA API.
    Returns a flat list of team dicts with wins/losses/conference.
    """
    print("[NCAA API] Fetching D1 standings …")
    data = ncaa_get("/standings/basketball-men/d1")
    if not data:
        return []

    teams   = []
    team_id = 1  # synthetic ID since standings don't carry ESPN-style IDs

    for conf_block in data.get("data", []):
        conf_name = conf_block.get("conference", "Unknown")
        for entry in conf_block.get("standings", []):
            school = entry.get("School", "Unknown")
            wins   = int(entry.get("Overall W", 0))
            losses = int(entry.get("Overall L", 0))
            teams.append({
                "id":         str(team_id),
                "name":       school,
                "abbrev":     school[:6].upper().replace(" ", ""),
                "conference": conf_name,
                "wins":       wins,
                "losses":     losses,
                # placeholders — enriched later
                "ppg":        0,
                "opp_ppg":    0,
                "ap_rank":    999,
                "coaches_rank": 999,
                "net_rank":   200,
                "trank":      200,
                "barthag":    0.5,
                "sos":        0.5,
                "rpi_rank":   200,
            })
            team_id += 1

            if len(teams) >= limit:
                break
        if len(teams) >= limit:
            break

    print(f"  → {len(teams)} teams from standings")
    return teams


def fetch_ncaa_rankings() -> dict[str, int]:
    """
    Fetch AP and NET rankings from the NCAA API.
    Returns { team_name_lower: net_rank } mapping.
    """
    print("[NCAA API] Fetching rankings (AP + NET) …")
    net_map: dict[str, int] = {}
    ap_map:  dict[str, int] = {}

    # AP poll
    ap_data = ncaa_get("/rankings/basketball-men/d1/associated-press")
    if ap_data:
        for entry in ap_data.get("data", []):
            school = entry.get("SCHOOL", "").split("(")[0].strip().lower()
            rank   = int(entry.get("RANK", 999))
            ap_map[school] = rank

    # NET rankings (ncaa.com publishes these)
    net_data = ncaa_get("/rankings/basketball-men/d1/ncaa-mens-basketball-net-rankings")
    if net_data:
        for entry in net_data.get("data", []):
            school = entry.get("SCHOOL", entry.get("Team", "")).lower()
            rank   = int(entry.get("RANK", entry.get("NET", 200)))
            net_map[school] = rank

    print(f"  → AP rankings: {len(ap_map)} teams | NET rankings: {len(net_map)} teams")
    return {"ap": ap_map, "net": net_map}


def fetch_barttorvik_stats(year: int = SEASON) -> dict[str, dict]:
    """
    Pull T-Rank / advanced stats from barttorvik.com (free, no key).
    Returns { team_name_lower: { trank, barthag, adj_oe, adj_de, sos, ... } }
    """
    print(f"[Barttorvik] Fetching T-Rank / advanced stats for {year} …")
    # Barttorvik exposes a JSON endpoint used by their own site
    data = bbart_get("/trank.php", params={"year": year, "json": 1})

    result: dict[str, dict] = {}
    if not data:
        print("  [warn] Barttorvik returned no data — metrics will use fallback values")
        return result

    # Response is a list of team rows
    rows = data if isinstance(data, list) else data.get("teams", [])
    for i, row in enumerate(rows):
        # Field names vary slightly; handle both styles
        name    = (row.get("team") or row.get("Team") or "").lower().strip()
        if not name:
            continue
        result[name] = {
            "trank":   i + 1,                                              # position in list = T-Rank
            "barthag": float(row.get("barthag",   row.get("Barthag",   0.5))),
            "adj_oe":  float(row.get("adjoe",     row.get("AdjOE",    100))),
            "adj_de":  float(row.get("adjde",     row.get("AdjDE",    100))),
            "sos":     float(row.get("sos",        row.get("SOS",      0.5))),
            "ppg":     float(row.get("obs_ef",     row.get("ORtg",      0))),  # off rating proxy
            "opp_ppg": float(row.get("dbs_ef",     row.get("DRtg",      0))),
        }

    print(f"  → Got Barttorvik data for {len(result)} teams")
    return result


def enrich_teams(
    teams:     list[dict],
    rankings:  dict[str, dict],
    bbart:     dict[str, dict],
) -> list[dict]:
    """
    Merge NCAA API rankings + Barttorvik advanced stats into each team.
    Uses fuzzy name matching (lowercase, strip suffixes).
    """
    print("[ENRICH] Merging rankings + advanced stats …")

    ap_map  = rankings.get("ap",  {})
    net_map = rankings.get("net", {})

    def find_bbart(name: str) -> dict:
        """Try to match team name to a Barttorvik key."""
        key = name.lower().strip()
        if key in bbart:
            return bbart[key]
        # Try without common suffixes
        for suffix in [" university", " college", " state", " st."]:
            short = key.replace(suffix, "").strip()
            if short in bbart:
                return bbart[short]
        # Partial match
        for k, v in bbart.items():
            if k in key or key in k:
                return v
        return {}

    for team in teams:
        name = team["name"]
        key  = name.lower().strip()

        # Rankings
        team["ap_rank"]      = ap_map.get(key,  999)
        team["coaches_rank"] = 999   # not separately published by NCAA API
        team["net_rank"]     = net_map.get(key, 200)
        team["rpi_rank"]     = team["net_rank"]   # NET is the modern RPI replacement

        # Barttorvik advanced stats
        bb = find_bbart(name)
        if bb:
            team["trank"]   = bb.get("trank",   200)
            team["barthag"] = bb.get("barthag",  0.5)
            team["adj_oe"]  = bb.get("adj_oe",  100)
            team["adj_de"]  = bb.get("adj_de",  100)
            team["sos"]     = bb.get("sos",      0.5)
            if bb.get("ppg"):
                team["ppg"]     = round(bb["ppg"],     1)
                team["opp_ppg"] = round(bb["opp_ppg"], 1)
        else:
            # Fallback so seed_score still works
            team["trank"]   = 200
            team["barthag"] = 0.5
            team["adj_oe"]  = 100
            team["adj_de"]  = 100

    print("  → Enrichment complete")
    return teams


# ── CSV export ────────────────────────────────────────────────────────────────
def export_team_stats(teams: list[dict], path: Path) -> None:
    fields = [
        "name", "abbrev", "conference",
        "wins", "losses", "ppg", "opp_ppg",
        "ap_rank", "net_rank", "trank", "barthag",
        "adj_oe", "adj_de", "sos",
        "seed_score", "projected_seed", "projected_region",
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(teams)
    print(f"[CSV] Team stats → {path}")


def export_bracket(teams: list[dict], path: Path) -> None:
    fields = [
        "name", "abbrev", "projected_seed", "projected_region",
        "seed_score", "barthag",
        "prob_r64", "prob_r32", "prob_s16", "prob_e8",
        "prob_f4", "prob_championship", "prob_champion",
    ]
    tourney = [t for t in teams if isinstance(t.get("projected_seed"), int)]
    tourney_sorted = sorted(
        tourney,
        key=lambda t: (t.get("projected_region", ""), t.get("projected_seed", 99))
    )
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(tourney_sorted)
    print(f"[CSV] Bracket predictions → {path}")


def export_advanced_stats(teams: list[dict], path: Path) -> None:
    """Export Barttorvik-sourced advanced metrics for tournament teams."""
    fields = [
        "name", "abbrev", "conference",
        "projected_seed", "projected_region",
        "wins", "losses",
        "net_rank", "trank", "barthag",
        "adj_oe", "adj_de", "sos",
        "ppg", "opp_ppg",
    ]
    tourney = [t for t in teams if isinstance(t.get("projected_seed"), int)]
    tourney_sorted = sorted(
        tourney,
        key=lambda t: (t.get("projected_region", ""), t.get("projected_seed", 99))
    )
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(tourney_sorted)
    print(f"[CSV] Advanced stats → {path}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="March Madness Tracker")
    parser.add_argument("--teams",       type=int, default=75,   help="Number of teams to fetch (default: 75)")
    parser.add_argument("--simulations", type=int, default=1000, help="Bracket simulations (default: 1000)")
    parser.add_argument("--output",      type=str, default="output", help="Output directory")
    parser.add_argument("--year",        type=int, default=SEASON,   help=f"Season year (default: {SEASON})")
    args = parser.parse_args()

    out = Path(args.output)
    out.mkdir(exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("=" * 55)
    print("  🏀  March Madness Tracker  🏀")
    print(f"  Sources: NCAA API + Barttorvik | Season {args.year}")
    print("=" * 55)

    # 1. Fetch base data
    teams     = fetch_standings(limit=args.teams)
    rankings  = fetch_ncaa_rankings()
    bbart     = fetch_barttorvik_stats(year=args.year)

    # 2. Merge everything
    teams = enrich_teams(teams, rankings, bbart)

    # 3. Score & seed
    for team in teams:
        team["seed_score"] = compute_seed_score(team)
    teams = assign_seeds(teams)

    # 4. Simulate bracket
    print(f"\n[SIM] Running {args.simulations:,} bracket simulations …")
    teams = simulate_bracket(teams, simulations=args.simulations)
    print("  → Done")

    # 5. Export timestamped + latest copies
    print("\n[EXPORT] Writing CSVs …")
    export_team_stats(teams,     out / f"team_stats_{ts}.csv")
    export_bracket(teams,        out / f"bracket_predictions_{ts}.csv")
    export_advanced_stats(teams, out / f"advanced_stats_{ts}.csv")

    export_team_stats(teams,     out / "team_stats_latest.csv")
    export_bracket(teams,        out / "bracket_predictions_latest.csv")
    export_advanced_stats(teams, out / "advanced_stats_latest.csv")

    print("\n✅  All done! CSVs saved to:", out.resolve())
    print("   team_stats_latest.csv          – full team metrics + seedings")
    print("   bracket_predictions_latest.csv – round-by-round win probabilities")
    print("   advanced_stats_latest.csv      – NET rank, T-Rank, Barthag, adj. ratings")


if __name__ == "__main__":
    main()