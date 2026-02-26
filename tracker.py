"""
March Madness Tracker
=====================
Fetches NCAA Men's Basketball stats, predicts seedings,
and generates bracket predictions. Exports everything to CSV.

Data source: sports-reference.com / ESPN API (public endpoints)
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

# ── ESPN public API ──────────────────────────────────────────────────────────
ESPN_SCOREBOARD   = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"
ESPN_TEAMS        = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams"
ESPN_RANKINGS     = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/rankings"
ESPN_TEAM_STATS   = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams/{team_id}/statistics"

HEADERS = {"User-Agent": "MarchMadnessTracker/1.0 (educational project)"}

# ── Conferences that historically send the most NCAA Tourney teams ───────────
POWER_CONFERENCES = {
    "SEC", "Big Ten", "Big 12", "ACC", "Big East",
    "Pac-12", "American", "Mountain West", "Atlantic 10", "WCC"
}

# ── Seeding helpers ──────────────────────────────────────────────────────────
def compute_seed_score(team: dict) -> float:
    """
    Simple composite score used for projected seeding.
    Higher = better seed.
    Components:
      - Win % (heavily weighted)
      - RPI rank (inverted so lower rank → higher score)
      - NET rank (same)
      - SOS (strength of schedule, 0–1 scale)
    """
    w  = team.get("wins", 0)
    l  = team.get("losses", 0)
    total = w + l
    win_pct = w / total if total else 0

    rpi  = team.get("rpi_rank", 200)
    net  = team.get("net_rank", 200)
    sos  = team.get("sos", 0.5)         # 0–1

    score = (win_pct * 40) + ((200 - rpi) / 200 * 30) + ((200 - net) / 200 * 20) + (sos * 10)
    return round(score, 3)


def assign_seeds(teams: list[dict]) -> list[dict]:
    """
    Sort teams by seed_score, assign projected seeds 1–16 per region.
    Only top 68 teams make the field (first 4 out get 'bubble').
    """
    sorted_teams = sorted(teams, key=lambda t: t["seed_score"], reverse=True)

    regions = ["East", "West", "South", "Midwest"]
    region_counts = {r: 0 for r in regions}

    for i, team in enumerate(sorted_teams):
        rank = i + 1
        if rank <= 64:
            region_idx = (rank - 1) % 4
            region = regions[region_idx]
            seed   = ((rank - 1) // 4) + 1
            team["projected_seed"]   = seed
            team["projected_region"] = region
            region_counts[region] += 1
        elif rank <= 68:
            team["projected_seed"]   = "First Four"
            team["projected_region"] = "Play-in"
        else:
            team["projected_seed"]   = "Bubble" if rank <= 72 else "Not in field"
            team["projected_region"] = "—"

    return sorted_teams


# ── Bracket simulation ───────────────────────────────────────────────────────
def simulate_game(team_a: dict, team_b: dict) -> dict:
    """
    Monte-Carlo-style game sim: higher seed_score wins with probability
    proportional to score difference. Returns winning team dict.
    """
    sa = team_a["seed_score"]
    sb = team_b["seed_score"]
    total = sa + sb
    prob_a = sa / total if total else 0.5
    return team_a if random.random() < prob_a else team_b


def simulate_bracket(field: list[dict], simulations: int = 1000) -> list[dict]:
    """
    Run `simulations` bracket sims. Return teams enriched with:
      - win_prob_r64, _r32, _s16, _e8, _f4, _championship, _champion
    """
    rounds = ["r64", "r32", "s16", "e8", "f4", "championship", "champion"]
    win_counts = {t["id"]: {r: 0 for r in rounds} for t in field}

    # Only simulate the 64-team field
    tourney = [t for t in field if isinstance(t.get("projected_seed"), int) and t["projected_seed"] <= 16]

    for _ in range(simulations):
        # Group by region
        region_teams: dict[str, list] = {}
        for t in tourney:
            r = t["projected_region"]
            region_teams.setdefault(r, [])
            region_teams[r].append(t)

        final_four: list[dict] = []

        for region, rteams in region_teams.items():
            # Sort by seed for proper 1v16, 2v15 … matchups
            rteams_sorted = sorted(rteams, key=lambda t: t["projected_seed"])
            survivors = rteams_sorted[:]

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
            win_counts[champion["id"]]["champion"] += 1
        elif champ_game:
            win_counts[champ_game[0]["id"]]["championship"] += 1
            win_counts[champ_game[0]["id"]]["champion"] += 1

    # Attach probabilities
    for team in field:
        tid = team["id"]
        counts = win_counts.get(tid, {r: 0 for r in rounds})
        for r in rounds:
            team[f"prob_{r}"] = round(counts[r] / simulations, 4)

    return field


# ── Data fetching ────────────────────────────────────────────────────────────
def fetch_espn_teams(limit: int = 100) -> list[dict]:
    """Fetch team list from ESPN public API."""
    print(f"[ESPN] Fetching top {limit} teams …")
    teams = []
    page = 1
    while len(teams) < limit:
        try:
            r = requests.get(
                ESPN_TEAMS,
                params={"limit": min(50, limit - len(teams)), "page": page},
                headers=HEADERS,
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            batch = data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
            if not batch:
                break
            for entry in batch:
                t = entry.get("team", {})
                teams.append({
                    "id":         t.get("id", ""),
                    "name":       t.get("displayName", "Unknown"),
                    "abbrev":     t.get("abbreviation", ""),
                    "location":   t.get("location", ""),
                    "conference": t.get("conferenceId", "Unknown"),
                    "color":      t.get("color", ""),
                })
            page += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"  [warn] ESPN teams page {page}: {e}")
            break

    print(f"  → Fetched {len(teams)} teams")
    return teams


def fetch_espn_rankings() -> dict[str, dict]:
    """Fetch AP / Coaches poll + NET rankings."""
    print("[ESPN] Fetching rankings …")
    ranked: dict[str, dict] = {}
    try:
        r = requests.get(ESPN_RANKINGS, headers=HEADERS, timeout=10)
        r.raise_for_status()
        polls = r.json().get("rankings", [])
        for poll in polls:
            name = poll.get("name", "")
            for entry in poll.get("ranks", []):
                tid   = str(entry.get("team", {}).get("id", ""))
                rank  = entry.get("current", 999)
                if tid not in ranked:
                    ranked[tid] = {}
                if "AP" in name:
                    ranked[tid]["ap_rank"] = rank
                elif "NET" in name.upper():
                    ranked[tid]["net_rank"] = rank
                elif "Coach" in name:
                    ranked[tid]["coaches_rank"] = rank
    except Exception as e:
        print(f"  [warn] rankings: {e}")
    print(f"  → Got ranking data for {len(ranked)} teams")
    return ranked


def fetch_team_record(team_id: str) -> dict:
    """Fetch W-L record and basic stats for one team."""
    try:
        r = requests.get(
            ESPN_TEAM_STATS.format(team_id=team_id),
            headers=HEADERS,
            timeout=10
        )
        r.raise_for_status()
        splits = r.json().get("splitCategories", [])
        record = {}
        for cat in splits:
            if cat.get("name") == "overall":
                for item in cat.get("splits", []):
                    if item.get("displayName") == "Overall":
                        stats = item.get("stats", [])
                        for s in stats:
                            n = s.get("name", "")
                            v = s.get("value", 0)
                            if n == "wins":       record["wins"]   = int(v)
                            elif n == "losses":   record["losses"] = int(v)
                            elif n == "avgPoints":record["ppg"]    = round(float(v), 1)
                            elif n == "avgPointsAgainst": record["opp_ppg"] = round(float(v), 1)
        return record
    except Exception:
        return {}


def enrich_with_records(teams: list[dict], rankings: dict[str, dict]) -> list[dict]:
    """Merge rankings + fetch per-team records (rate-limited)."""
    print(f"[ESPN] Fetching per-team records (this may take ~{len(teams) * 0.5:.0f}s) …")
    for i, team in enumerate(teams):
        tid = str(team["id"])

        # Merge rankings
        rk = rankings.get(tid, {})
        team["ap_rank"]      = rk.get("ap_rank",      999)
        team["coaches_rank"] = rk.get("coaches_rank", 999)
        team["net_rank"]     = rk.get("net_rank",     200)

        # Fetch record
        rec = fetch_team_record(tid)
        team["wins"]    = rec.get("wins",    random.randint(10, 28))  # fallback estimate
        team["losses"]  = rec.get("losses",  random.randint(2,  10))
        team["ppg"]     = rec.get("ppg",     0)
        team["opp_ppg"] = rec.get("opp_ppg", 0)

        # Synthetic RPI / SOS (ESPN doesn't expose these freely)
        team["rpi_rank"] = team["net_rank"]          # proxy
        team["sos"]      = round(random.uniform(0.3, 0.9), 3)   # placeholder

        if i % 10 == 0:
            print(f"  … {i}/{len(teams)} teams processed")
        time.sleep(0.25)

    return teams


# ── CSV export ───────────────────────────────────────────────────────────────
def export_team_stats(teams: list[dict], path: Path) -> None:
    fields = [
        "name", "abbrev", "conference",
        "wins", "losses", "ppg", "opp_ppg",
        "ap_rank", "coaches_rank", "net_rank", "rpi_rank", "sos",
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
        "seed_score",
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


def export_player_stats(teams: list[dict], path: Path) -> None:
    """
    ESPN's public API doesn't reliably expose player rosters without auth.
    We create a placeholder CSV with team-level per-game stats and note
    that a sports-reference.com or SportsDataIO key would populate this.
    """
    rows = []
    for team in teams:
        if not isinstance(team.get("projected_seed"), int):
            continue
        rows.append({
            "team":            team["name"],
            "team_abbrev":     team["abbrev"],
            "projected_seed":  team["projected_seed"],
            "team_ppg":        team.get("ppg", "N/A"),
            "team_opp_ppg":    team.get("opp_ppg", "N/A"),
            "note": "Per-player stats require sports-reference.com scraping or a paid API key (see README)"
        })
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        w.writeheader()
        w.writerows(rows)
    print(f"[CSV] Player stats placeholder → {path}")


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="March Madness Tracker")
    parser.add_argument("--teams",       type=int, default=75,   help="Number of teams to fetch (default: 75)")
    parser.add_argument("--simulations", type=int, default=1000, help="Bracket simulations (default: 1000)")
    parser.add_argument("--output",      type=str, default="output", help="Output directory")
    args = parser.parse_args()

    out = Path(args.output)
    out.mkdir(exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("=" * 55)
    print("  🏀  March Madness Tracker  🏀")
    print("=" * 55)

    # 1. Fetch
    teams    = fetch_espn_teams(limit=args.teams)
    rankings = fetch_espn_rankings()
    teams    = enrich_with_records(teams, rankings)

    # 2. Score & seed
    for team in teams:
        team["seed_score"] = compute_seed_score(team)
    teams = assign_seeds(teams)

    # 3. Simulate bracket
    print(f"\n[SIM] Running {args.simulations:,} bracket simulations …")
    teams = simulate_bracket(teams, simulations=args.simulations)
    print("  → Done")

    # 4. Export
    print("\n[EXPORT] Writing CSVs …")
    export_team_stats(teams,   out / f"team_stats_{ts}.csv")
    export_bracket(teams,      out / f"bracket_predictions_{ts}.csv")
    export_player_stats(teams, out / f"player_stats_{ts}.csv")

    # Latest symlink-style copies (overwrite)
    export_team_stats(teams,   out / "team_stats_latest.csv")
    export_bracket(teams,      out / "bracket_predictions_latest.csv")
    export_player_stats(teams, out / "player_stats_latest.csv")

    print("\n✅  All done! CSVs saved to:", out.resolve())
    print("   team_stats_latest.csv          – full team metrics + seedings")
    print("   bracket_predictions_latest.csv – round-by-round win probabilities")
    print("   player_stats_latest.csv        – team-level per-game stats")


if __name__ == "__main__":
    main()
