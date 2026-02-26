# 🏀 March Madness Tracker

Automatically fetches NCAA Men's Basketball stats, projects seedings, simulates the bracket, and exports everything to CSV — updated daily via GitHub Actions.

## Output CSVs

| File | Contents |
|------|----------|
| `output/team_stats_latest.csv` | Win/loss, PPG, rankings (AP, Coaches, NET), projected seed & region |
| `output/bracket_predictions_latest.csv` | Round-by-round win probabilities for all 64 teams |
| `output/player_stats_latest.csv` | Team-level per-game stats (see note below) |

Timestamped snapshots (e.g. `team_stats_20250315_070012.csv`) are also committed each run.

## Seeding Logic

Teams are scored using a composite metric:

```
seed_score = (win% × 40) + (NET rank × 30) + (RPI rank × 20) + (SOS × 10)
```

The top 64 teams are placed into 4 regions (East/West/South/Midwest) with seeds 1–16. Teams 65–68 go to the First Four play-in.

## Bracket Simulation

The bracket is simulated 2,000 times (configurable). Each game is won probabilistically based on each team's `seed_score`. Output columns:

- `prob_r64` – probability of winning Round of 64
- `prob_r32` – Round of 32
- `prob_s16` – Sweet Sixteen
- `prob_e8` – Elite Eight
- `prob_f4` – Final Four
- `prob_championship` – Championship game
- `prob_champion` – National Champion

## Player Stats

ESPN's public API doesn't expose per-player stats without authentication.
Options to get real player data:

- **Sports Reference** (`sports-reference.com/cbb`) – free scraping with rate limiting
- **SportsDataIO** – paid API, ~$30/month
- **ESPN API key** – request at developer.espn.com

Modify `fetch_player_stats()` in `tracker.py` to plug in your source.

## Usage

### Local

```bash
git clone https://github.com/YOUR_USERNAME/march-madness-tracker
cd march-madness-tracker
pip install -r requirements.txt
python tracker.py
```

Options:
```
--teams        Number of teams to fetch (default: 75)
--simulations  Bracket simulations to run (default: 1000)
--output       Output directory (default: output/)
```

### GitHub Actions (automatic)

1. Push this repo to GitHub
2. The workflow in `.github/workflows/tracker.yml` runs **daily at 7 AM UTC**
3. Updated CSVs are automatically committed back to the repo
4. You can also trigger it manually via **Actions → Run workflow**

## Data Source

All live data comes from **ESPN's public (unauthenticated) API**. No API key required.

## License

MIT
