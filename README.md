# Noob Helper

A personal League of Legends coach that runs on your PC and opens in your browser.

- **Champ select:** follows picks and bans live and suggests champions in three sub-tabs:
  - **My champs:** from your pool, based on your win rate, the matchup against your likely lane opponent (high elo data and your own history) and team balance (AD/AP, frontline).
  - **Counters (high elo):** champions with the best win rate against your lane opponent and the rest of the enemy team.
  - **Mains:** the top 5 mains of the champion you picked and their build.
- **After lock in:** the most common build for that champion **in that matchup**, from Challenger/GM/Master players:
  - rune page shown as a tree, like in the client;
  - summoner spells, skills (first 3 levels and max order), starting items and the order of the first 3 items with the average minute;
  - situational tips (anti-heal, MR/armor, penetration…).
- **Jungle pathing:** the numbered full clear on the minimap for the most common starting camp (blue or red side), the real routes junglers took in the first 7 minutes, and where the first gank happens.
- **Mains:** for each champion, the 5 Challenger/GM players from EUW and KR with the most mastery points (or LP). Shows region, rank, win rate, KDA, rune, items and their combined build. You can pick each main to see their build, or "Compare" all 5 side by side. Opening a champion updates the mains' recent games in the background (at most twice a day per champion).
- **Match History:** all your games since the start of the season, split by queue (Ranked Solo/Duo, Flex, Normals, ARAM, Other).
  - Each queue has a summary: win rate, KDA, CS/min, damage, gold, KP, average score and most played champions.
  - Each game shows KDA, score (0–10, with MVP/ACE), CS, damage, gold, vision, spells, runes and items. Click a game to see all 10 players.
- **Post-game:** analyses each game automatically when it ends.
  - Feedback on farming, laning, deaths, vision, control wards, fights, items and comparisons with your averages and high elo. Checkboxes pick which categories count towards the score.
  - Rift minimap with all 10 players minute by minute (slider and ▶), kills/deaths and objectives where they happened, your path and the enemy jungler's path.
  - Clickable event list, full build path grouped by recall, skill grid and gold chart (team and lane opponent).
- **Daily automatic update** (while the app is open, can be turned off in "My data"):
  - collects high elo games from EUW and KR, with timelines;
  - re-checks masteries and the top 5 mains: updates LP and removes players who dropped out of Challenger/GM;
  - syncs your games.

Everything is calculated locally from the Riot API. No paid services.

## Running it

1. Install Python 3.11+.
2. Get a key at <https://developer.riotgames.com>. A *Development Key* lasts 24h; to avoid renewing it every day, request a **Personal API Key** under "Register Product".
3. Double-click `run.bat`. The first time it creates the Python environment and opens `.env` so you can fill in:
   - `RIOT_API_KEY=RGAPI-...`
   - `RIOT_ID=YourName#TAG`
4. Run `run.bat` again. The browser opens at <http://127.0.0.1:8765>.
5. In the **My data** tab:
   - click **Sync season** to fetch all your games;
   - click **Collect** under "High elo data", with EUW and KR ticked. It takes a while because of the key's request limit; run it again from time to time, especially after a patch.
6. In the **Mains** tab, click **Collect masteries** (~20 min, regions in parallel).

After that, just open the LoL client and join a queue: the app connects by itself.

To test without queueing, use the **simulator** at the bottom of the Champ Select tab.

## Structure

| File | What it does |
|---|---|
| `coach/lcu.py` | Connects to the LoL client (local API + websocket) and receives champ select and game phase events |
| `coach/champselect.py` | Turns the champ select session into a simple state |
| `coach/riot_api.py` | Riot API client with rate limiting (20/s, 100/2min, per region) |
| `coach/ddragon.py` | Static data (champions, runes, items, spells) from Data Dragon |
| `coach/data.py` | Collects high elo games (with timelines: item order, skills, jungle routes) into the SQLite database (`data/coach.db`) |
| `coach/analysis.py` | Pick, counter and build rules |
| `coach/mains.py` | Top mains per champion (mastery) and their builds |
| `coach/history.py` | Season match history and the per-player score |
| `coach/postgame.py` | Post-game analysis (metrics + timeline + feedback) |
| `coach/main.py` | FastAPI server, websocket for the UI and daily tasks |
| `static/` | Web UI |

## Notes and limits

- The app **only reads** data. It never picks, bans or locks in for you, and it doesn't show anything the game hides (such as enemy names). That's what Riot's third-party app policy requires.
- In ranked you don't see enemy roles: the app guesses them from the meta role distribution and each champion's class.
- Item order, skills and jungle routes come from each game's timeline, which costs 1 extra request per game. "Backfill old games" fetches timelines for games collected earlier.
- Riot gives each player's position once per minute. Jungle routes therefore show the start and direction of the clear, not every single camp.
- Post-game details come from the Riot API timeline. The replay file (.rofl) has no public format, so it isn't read.
- Each player's score is calculated by the app (it isn't op.gg's). It combines KDA, kill participation, damage %, gold %, vision per minute, damage taken and damage to objectives, each compared with the best value in that game. The best player in the game gets 10.
- The season start date is `SEASON_START` in `.env` (default 2026-01-08).
- `NOOB_NO_AUTO=1` turns off the daily tasks (useful for testing).
- No scraping of op.gg / onetricks.gg: they have no public API and their terms of use don't allow it. The equivalent data is calculated from the Riot API.
