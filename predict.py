"""
MLS Prediction System - single-file build
Combines data fetching, model, bet builder assembly, and HTML output
into one file for easy upload/deployment (e.g. via GitHub web UI).

Run with: python predict.py
Output: docs/index.html
"""

import os
import math
import json
import time
import itertools
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

# ======================================================================
# --- from py ---
# ======================================================================

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/scoreboard"
SCOREBOARD_DATE_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/scoreboard?dates={date}"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/summary?event={event_id}"
TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/teams"

REQUEST_DELAY_SECONDS = 0.3  # be polite to ESPN's servers, avoid rate limiting
MAX_RETRIES = 3


def _fetch_json(url, retries=MAX_RETRIES):
    """Fetch JSON from a URL with basic retry logic."""
    last_error = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
            last_error = e
            time.sleep(1 + attempt)
    print(f"WARNING: failed to fetch {url} after {retries} attempts: {last_error}")
    return None


def get_all_teams():
    """Returns list of {id, name, abbreviation} for all MLS teams."""
    data = _fetch_json(TEAMS_URL)
    if not data:
        return []
    teams = []
    for entry in data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", []):
        team = entry.get("team", {})
        teams.append({
            "id": team.get("id"),
            "name": team.get("displayName"),
            "abbreviation": team.get("abbreviation"),
        })
    return teams


def get_upcoming_matches():
    """Returns list of upcoming/scheduled matches from the current scoreboard."""
    data = _fetch_json(SCOREBOARD_URL)
    if not data:
        return []
    matches = []
    for event in data.get("events", []):
        status = event.get("status", {}).get("type", {}).get("state", "")
        if status != "pre":
            continue
        competitors = event.get("competitions", [{}])[0].get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})
        matches.append({
            "event_id": event.get("id"),
            "date": event.get("date"),
            "home_team_id": home.get("team", {}).get("id"),
            "home_team_name": home.get("team", {}).get("displayName"),
            "away_team_id": away.get("team", {}).get("id"),
            "away_team_name": away.get("team", {}).get("displayName"),
        })
    return matches


def get_completed_matches(days_back=90):
    """
    Pulls completed matches over the last `days_back` days.

    IMPORTANT: ESPN's scoreboard endpoint does NOT reliably return every
    event in a wide dates=START-END range in one response - in testing,
    a 90-day range silently came back with only a single matchday's worth
    of events instead of the full window, with no error or warning. This
    corrupts every downstream rating with a thin, arbitrary sample.

    IMPORTANT #2: each entry in ESPN's calendar list is not a full slate -
    an MLS "matchday" often spans Fri/Sat/Sun, but the calendar only lists
    one representative date per round, and dates=YYYYMMDD only returns
    games kicking off on that exact day. Fetching just that single day
    per calendar entry silently drops the other games in the same round.
    To catch the full round we fetch a small window (+/- 2 days) around
    each calendar date and rely on event-id dedup to avoid double counting.
    """
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=days_back)

    # First hit the plain scoreboard endpoint to read the league calendar -
    # the list of dates on which matches were actually played this season.
    base_data = _fetch_json(SCOREBOARD_URL)
    if not base_data:
        return []

    calendar_dates = []
    for league in base_data.get("leagues", []):
        for date_str in league.get("calendar", []):
            try:
                d = datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None)
            except (ValueError, TypeError):
                continue
            if start_date <= d <= end_date:
                calendar_dates.append(d)

    if not calendar_dates:
        print("WARNING: no matchdays found in league calendar for the requested window")
        return []

    # Expand each calendar date into a +/- 2 day window, then flatten to a
    # deduped, sorted set of individual days to actually query - this
    # collapses overlapping windows from adjacent calendar entries into
    # single fetches instead of re-querying the same day repeatedly.
    WINDOW_DAYS = 2
    query_days = set()
    for d in calendar_dates:
        for offset in range(-WINDOW_DAYS, WINDOW_DAYS + 1):
            day = d + timedelta(days=offset)
            if start_date <= day <= end_date:
                query_days.add(day.strftime("%Y%m%d"))
    query_days = sorted(query_days)

    matches = []
    seen_event_ids = set()
    per_day_counts = {}
    for i, date_str in enumerate(query_days):
        url = SCOREBOARD_DATE_URL.format(date=date_str)
        data = _fetch_json(url)
        if not data:
            per_day_counts[date_str] = "FETCH_FAILED"
            continue
        day_new_count = 0
        day_total_events = len(data.get("events", []))
        for event in data.get("events", []):
            event_id = event.get("id")
            if event_id in seen_event_ids:
                continue
            status = event.get("status", {}).get("type", {}).get("state", "")
            if status != "post":
                continue
            competitors = event.get("competitions", [{}])[0].get("competitors", [])
            home = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away = next((c for c in competitors if c.get("homeAway") == "away"), {})
            seen_event_ids.add(event_id)
            day_new_count += 1
            matches.append({
                "event_id": event_id,
                "date": event.get("date"),
                "home_team_id": home.get("team", {}).get("id"),
                "home_team_name": home.get("team", {}).get("displayName"),
                "away_team_id": away.get("team", {}).get("id"),
                "away_team_name": away.get("team", {}).get("displayName"),
                "home_score": home.get("score"),
                "away_score": away.get("score"),
            })
        per_day_counts[date_str] = f"{day_new_count} new / {day_total_events} total events"
        if i < len(query_days) - 1:
            time.sleep(REQUEST_DELAY_SECONDS)

    if os.environ.get("PREDICT_DEBUG"):
        print(f"[DEBUG] calendar_dates found: {len(calendar_dates)}, query_days expanded to: {len(query_days)}")
        for ds, count in per_day_counts.items():
            print(f"[DEBUG]   {ds}: {count}")

    return matches


def get_match_stats(event_id):
    """
    Fetches detailed per-team stats for a completed match: goals, corners,
    shots, shots on target, possession, fouls.

    Returns dict keyed by 'home'/'away' with a flat stats dict each, or
    None if the fetch failed or stats weren't available (some older/
    lower-profile games may lack full boxscore data).
    """
    data = _fetch_json(SUMMARY_URL.format(event_id=event_id))
    if not data:
        return None

    boxscore_teams = data.get("boxscore", {}).get("teams", [])
    if not boxscore_teams or len(boxscore_teams) < 2:
        return None

    # Figure out which entry is home vs away using the header/competitors block
    header_competitors = data.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
    home_id = None
    away_id = None
    for c in header_competitors:
        if c.get("homeAway") == "home":
            home_id = c.get("team", {}).get("id")
        elif c.get("homeAway") == "away":
            away_id = c.get("team", {}).get("id")

    def extract_stats(team_block):
        stats = {}
        for stat in team_block.get("statistics", []):
            name = stat.get("name")
            value = stat.get("displayValue")
            if name and value is not None:
                try:
                    # possession/pass pct come through as decimals like '0.9' or '47.8'
                    stats[name] = float(value)
                except (ValueError, TypeError):
                    stats[name] = value
        return stats

    result = {}
    for team_block in boxscore_teams:
        team_id = team_block.get("team", {}).get("id")
        stats = extract_stats(team_block)
        stats["team_id"] = team_id
        stats["team_name"] = team_block.get("team", {}).get("displayName")
        if team_id == home_id:
            result["home"] = stats
        elif team_id == away_id:
            result["away"] = stats

    if "home" not in result or "away" not in result:
        return None

    if os.environ.get("PREDICT_DEBUG") and not getattr(get_match_stats, "_dumped", False):
        get_match_stats._dumped = True
        print(f"[DEBUG] Raw stat keys from event {event_id}:")
        print(f"  home keys: {sorted(result['home'].keys())}")
        print(f"  away keys: {sorted(result['away'].keys())}")

    return result


def build_match_dataset(days_back=90, verbose=True):
    """
    Full pipeline: get completed matches, then fetch detailed stats for each.
    Returns a list of enriched match records ready for the 

    This is the slow part (one HTTP request per match) so it prints progress
    and respects REQUEST_DELAY_SECONDS between calls.
    """
    matches = get_completed_matches(days_back=days_back)
    if verbose:
        print(f"Found {len(matches)} completed matches in the last {days_back} days")

    enriched = []
    for i, match in enumerate(matches):
        if verbose and i % 10 == 0:
            print(f"  Fetching stats {i+1}/{len(matches)}...")
        stats = get_match_stats(match["event_id"])
        if stats is None:
            continue  # skip matches with unavailable/incomplete stats
        match["stats"] = stats
        enriched.append(match)
        time.sleep(REQUEST_DELAY_SECONDS)

    if verbose:
        print(f"Successfully enriched {len(enriched)}/{len(matches)} matches with full stats")

    return enriched


# ======================================================================
# --- from py ---
# ======================================================================

from collections import defaultdict

RECENT_FORM_WEIGHT = 0.65  # weight on last-10 form vs full season-to-date
LEAGUE_AVG_GOALS_HOME = 1.4  # MLS-ish baseline, refined by actual data below
LEAGUE_AVG_GOALS_AWAY = 1.15
LEAGUE_AVG_CORNERS_HOME = 5.2
LEAGUE_AVG_CORNERS_AWAY = 4.6

DIXON_COLES_RHO = -0.13  # standard low-score correlation dampening factor
MAX_GOALS_GRID = 8  # scoreline grid computed 0-8 goals each side (covers ~99.9% of mass)
MAX_CORNERS_GRID = 16


def _safe_get(d, key, default=0.0):
    val = d.get(key, default)
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def compute_team_form(team_id, matches, is_home_only=None, last_n=10):
    """
    Aggregates a team's goals for/against, corners for/against, and shots-on-target
    for/against from their match history. Weights recent form more heavily.

    matches: list of enriched match records (from build_match_dataset)
             already filtered/sorted to this team's games, most recent first.
    is_home_only: True = only home games, False = only away games, None = all.
    """
    relevant = []
    for m in matches:
        stats = m.get("stats")
        if not stats:
            continue
        is_home = m["home_team_id"] == team_id
        is_away = m["away_team_id"] == team_id
        if not (is_home or is_away):
            continue
        if is_home_only is True and not is_home:
            continue
        if is_home_only is False and not is_away:
            continue
        relevant.append((m, is_home))

    if not relevant:
        return None

    def aggregate(subset):
        gf, ga, cf, ca, sot_f, sot_a, n = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0
        for m, is_home in subset:
            side = "home" if is_home else "away"
            other = "away" if is_home else "home"
            gf += float(m["home_score"]) if is_home else float(m["away_score"])
            ga += float(m["away_score"]) if is_home else float(m["home_score"])
            cf += _safe_get(m["stats"][side], "wonCorners")
            ca += _safe_get(m["stats"][other], "wonCorners")
            sot_f += _safe_get(m["stats"][side], "shotsOnTarget")
            sot_a += _safe_get(m["stats"][other], "shotsOnTarget")
            n += 1
        if n == 0:
            return None
        return {
            "goals_for_avg": gf / n,
            "goals_against_avg": ga / n,
            "corners_for_avg": cf / n,
            "corners_against_avg": ca / n,
            "sot_for_avg": sot_f / n,
            "sot_against_avg": sot_a / n,
            "games": n,
        }

    recent = aggregate(relevant[:last_n])
    season = aggregate(relevant)

    if recent is None:
        return None
    if season is None:
        season = recent

    blended = {}
    for key in recent:
        if key == "games":
            blended[key] = season[key]
            continue
        blended[key] = (RECENT_FORM_WEIGHT * recent[key]) + ((1 - RECENT_FORM_WEIGHT) * season[key])

    return blended


def blend_goals_with_shot_quality(goals_avg, sot_avg, league_avg_sot=4.5, blend_weight=0.2):
    """
    Smooths a team's raw goals-for average using their shots-on-target rate as
    a stability signal. If a team's SOT rate suggests they should be scoring
    more/less than their raw goal average shows, nudge the estimate slightly
    toward that - without ever surfacing SOT as a market itself.

    blend_weight kept modest (0.2) since this is a secondary smoothing signal,
    not a replacement for actual scoring data.
    """
    if sot_avg is None or league_avg_sot == 0:
        return goals_avg
    # crude conversion-rate-implied goals: assumes league-average SOT-to-goal ratio
    league_avg_goals = (LEAGUE_AVG_GOALS_HOME + LEAGUE_AVG_GOALS_AWAY) / 2
    implied_goals_from_sot = (sot_avg / league_avg_sot) * league_avg_goals
    return (1 - blend_weight) * goals_avg + blend_weight * implied_goals_from_sot


MIN_GAMES_FOR_RATING = 5  # below this, sample is too thin to trust - skip the team entirely


def compute_team_ratings(team_id, all_matches):
    """
    Builds a full rating profile for a team: home attack/defense strength,
    away attack/defense strength, and corners for/against rates.
    Strength expressed as a multiplier vs league average (1.0 = average).

    Returns None if the team has fewer than MIN_GAMES_FOR_RATING total games -
    thin samples produce false-confident predictions (e.g. a team that's 1-0-1
    in 2 games looks either unbeatable or hopeless by pure averages, when it's
    really just noise). Better to skip the match entirely than ship a bad number.
    """
    home_form = compute_team_form(team_id, all_matches, is_home_only=True)
    away_form = compute_team_form(team_id, all_matches, is_home_only=False)
    overall_form = compute_team_form(team_id, all_matches, is_home_only=None)

    if overall_form is None:
        return None  # not enough data for this team yet

    if overall_form["games"] < MIN_GAMES_FOR_RATING:
        return None  # sample too thin to trust

    # fall back to overall form if home/away specific splits are too thin
    home_form = home_form or overall_form
    away_form = away_form or overall_form

    home_goals_for = blend_goals_with_shot_quality(
        home_form["goals_for_avg"], home_form["sot_for_avg"]
    )
    away_goals_for = blend_goals_with_shot_quality(
        away_form["goals_for_avg"], away_form["sot_for_avg"]
    )

    return {
        "team_id": team_id,
        "home_attack_strength": home_goals_for / LEAGUE_AVG_GOALS_HOME,
        "home_defense_strength": home_form["goals_against_avg"] / LEAGUE_AVG_GOALS_AWAY,
        "away_attack_strength": away_goals_for / LEAGUE_AVG_GOALS_AWAY,
        "away_defense_strength": away_form["goals_against_avg"] / LEAGUE_AVG_GOALS_HOME,
        "home_corners_for": home_form["corners_for_avg"],
        "home_corners_against": home_form["corners_against_avg"],
        "away_corners_for": away_form["corners_for_avg"],
        "away_corners_against": away_form["corners_against_avg"],
        "games_played": overall_form["games"],
    }


def expected_goals(home_ratings, away_ratings):
    """Returns (home_xg, away_xg) for a matchup using strength ratings."""
    home_xg = (
        home_ratings["home_attack_strength"]
        * away_ratings["away_defense_strength"]
        * LEAGUE_AVG_GOALS_HOME
    )
    away_xg = (
        away_ratings["away_attack_strength"]
        * home_ratings["home_defense_strength"]
        * LEAGUE_AVG_GOALS_AWAY
    )
    return max(home_xg, 0.1), max(away_xg, 0.1)


def expected_corners(home_ratings, away_ratings):
    """Returns (home_corners_xg, away_corners_xg) for a matchup."""
    home_cx = (
        (home_ratings["home_corners_for"] / LEAGUE_AVG_CORNERS_HOME)
        * (away_ratings["away_corners_against"] / LEAGUE_AVG_CORNERS_AWAY)
        * LEAGUE_AVG_CORNERS_HOME
    )
    away_cx = (
        (away_ratings["away_corners_for"] / LEAGUE_AVG_CORNERS_AWAY)
        * (home_ratings["home_corners_against"] / LEAGUE_AVG_CORNERS_HOME)
        * LEAGUE_AVG_CORNERS_AWAY
    )
    return max(home_cx, 0.5), max(away_cx, 0.5)


def _poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def _dixon_coles_adjustment(home_goals, away_goals, home_xg, away_xg, rho):
    """Low-score correlation correction. Only affects 0-0, 1-0, 0-1, 1-1 cells."""
    if home_goals == 0 and away_goals == 0:
        return 1 - (home_xg * away_xg * rho)
    elif home_goals == 0 and away_goals == 1:
        return 1 + (home_xg * rho)
    elif home_goals == 1 and away_goals == 0:
        return 1 + (away_xg * rho)
    elif home_goals == 1 and away_goals == 1:
        return 1 - rho
    return 1.0


def build_goals_grid(home_xg, away_xg, max_goals=MAX_GOALS_GRID, rho=DIXON_COLES_RHO):
    """
    Returns a 2D grid (dict keyed by (home_goals, away_goals)) of probabilities
    for every scoreline, Dixon-Coles adjusted. All goal-based markets are
    derived from this single grid to keep them internally consistent.
    """
    grid = {}
    total = 0.0
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            base_prob = _poisson_pmf(h, home_xg) * _poisson_pmf(a, away_xg)
            adjustment = _dixon_coles_adjustment(h, a, home_xg, away_xg, rho)
            prob = max(base_prob * adjustment, 0.0)
            grid[(h, a)] = prob
            total += prob

    # renormalize so probabilities sum to 1 (Dixon-Coles adjustment can shift the total slightly)
    if total > 0:
        for key in grid:
            grid[key] /= total

    return grid


def build_corners_grid(home_cx, away_cx, max_corners=MAX_CORNERS_GRID):
    """Simple independent Poisson grid for corners (no Dixon-Coles - that
    correlation adjustment is specific to low-scoring goal correlation, not
    applicable here)."""
    grid = {}
    total = 0.0
    for h in range(max_corners + 1):
        for a in range(max_corners + 1):
            prob = _poisson_pmf(h, home_cx) * _poisson_pmf(a, away_cx)
            grid[(h, a)] = prob
            total += prob
    if total > 0:
        for key in grid:
            grid[key] /= total
    return grid


# ---- Market derivation from grids ----

def market_double_chance(goals_grid):
    """Returns probabilities for Home-or-Draw, Away-or-Draw, Home-or-Away."""
    home_win = draw = away_win = 0.0
    for (h, a), p in goals_grid.items():
        if h > a:
            home_win += p
        elif h == a:
            draw += p
        else:
            away_win += p
    return {
        "home_or_draw": home_win + draw,
        "away_or_draw": away_win + draw,
        "home_or_away": home_win + away_win,
        "home_win": home_win,
        "draw": draw,
        "away_win": away_win,
    }


def market_total_over_under(grid, lines, side="total"):
    """
    Generic O/U market builder for a 2D grid (works for both goals and corners).
    side: 'total' = sum of both, 'home' = home side only, 'away' = away side only.
    lines: list of line values, e.g. [1.5, 2.5, 3.5]
    Returns dict: {line: {'over': prob, 'under': prob}}
    """
    results = {line: {"over": 0.0, "under": 0.0} for line in lines}
    for (h, a), p in grid.items():
        if side == "total":
            val = h + a
        elif side == "home":
            val = h
        else:
            val = a
        for line in lines:
            if val > line:
                results[line]["over"] += p
            else:
                results[line]["under"] += p
    return results


def market_btts(goals_grid):
    """Both Teams To Score - Yes/No probability."""
    yes = sum(p for (h, a), p in goals_grid.items() if h > 0 and a > 0)
    return {"yes": yes, "no": 1 - yes}


def pick_standard_lines(expected_value, market_type="goals"):
    """
    Given an expected value (xG or expected corners), returns 2-3 sensible
    standard betting lines centered around it. Always returns X.5 values
    (never whole numbers) since that's how sportsbooks post lines - avoids
    any ambiguity about pushes.
    """
    # find the nearest X.5 value to the expected value
    base = math.floor(expected_value) + 0.5
    if expected_value - math.floor(expected_value) > 0.75:
        base += 1  # closer to the next .5 up

    if market_type == "goals":
        candidates = [base - 1, base, base + 1]
    else:  # corners - wider spread since values are bigger
        candidates = [base - 2, base, base + 2]

    # filter out nonsensical negative/zero lines, dedupe, keep 2-3, always .5
    lines = sorted(set(c for c in candidates if c > 0))
    return lines[:3]


# ======================================================================
# --- from py ---
# ======================================================================

def generate_all_legs(goals_grid, corners_grid, home_xg, away_xg, home_cx, away_cx):
    """
    Builds the full list of individual leg candidates for a match, each
    tagged with its source grid ('goals' or 'corners') so the combiner
    knows which legs can be jointly evaluated vs multiplied.

    Returns list of leg dicts:
        {label, probability, grid_source, grid_filter_fn}
    grid_filter_fn takes a (home_val, away_val) tuple and returns True if
    that cell counts toward this leg - used for joint re-querying when
    stacking same-grid legs together.
    """
    legs = []

    # --- Double Chance (from goals grid) ---
    dc = market_double_chance(goals_grid)
    legs.append({
        "label": "Home or Draw (Double Chance)",
        "probability": dc["home_or_draw"],
        "grid_source": "goals",
        "grid_filter_fn": lambda h, a: h >= a,
    })
    legs.append({
        "label": "Away or Draw (Double Chance)",
        "probability": dc["away_or_draw"],
        "grid_source": "goals",
        "grid_filter_fn": lambda h, a: a >= h,
    })
    legs.append({
        "label": "Home or Away (No Draw)",
        "probability": dc["home_or_away"],
        "grid_source": "goals",
        "grid_filter_fn": lambda h, a: h != a,
    })

    # --- BTTS (from goals grid) ---
    btts = market_btts(goals_grid)
    legs.append({
        "label": "Both Teams to Score - Yes",
        "probability": btts["yes"],
        "grid_source": "goals",
        "grid_filter_fn": lambda h, a: h > 0 and a > 0,
    })
    legs.append({
        "label": "Both Teams to Score - No",
        "probability": btts["no"],
        "grid_source": "goals",
        "grid_filter_fn": lambda h, a: not (h > 0 and a > 0),
    })

    # --- Total Goals O/U (match) ---
    goal_lines = pick_standard_lines(home_xg + away_xg, market_type="goals")
    total_goals = market_total_over_under(goals_grid, goal_lines, side="total")
    for line, probs in total_goals.items():
        legs.append({
            "label": f"Total Goals Over {line}",
            "probability": probs["over"],
            "grid_source": "goals",
            "grid_filter_fn": (lambda h, a, ln=line: (h + a) > ln),
        })
        legs.append({
            "label": f"Total Goals Under {line}",
            "probability": probs["under"],
            "grid_source": "goals",
            "grid_filter_fn": (lambda h, a, ln=line: (h + a) <= ln),
        })

    # --- Team Goals O/U (home & away separately) ---
    home_goal_lines = pick_standard_lines(home_xg, market_type="goals")
    home_goals = market_total_over_under(goals_grid, home_goal_lines, side="home")
    for line, probs in home_goals.items():
        legs.append({
            "label": f"Home Team Goals Over {line}",
            "probability": probs["over"],
            "grid_source": "goals",
            "grid_filter_fn": (lambda h, a, ln=line: h > ln),
        })
        legs.append({
            "label": f"Home Team Goals Under {line}",
            "probability": probs["under"],
            "grid_source": "goals",
            "grid_filter_fn": (lambda h, a, ln=line: h <= ln),
        })

    away_goal_lines = pick_standard_lines(away_xg, market_type="goals")
    away_goals = market_total_over_under(goals_grid, away_goal_lines, side="away")
    for line, probs in away_goals.items():
        legs.append({
            "label": f"Away Team Goals Over {line}",
            "probability": probs["over"],
            "grid_source": "goals",
            "grid_filter_fn": (lambda h, a, ln=line: a > ln),
        })
        legs.append({
            "label": f"Away Team Goals Under {line}",
            "probability": probs["under"],
            "grid_source": "goals",
            "grid_filter_fn": (lambda h, a, ln=line: a <= ln),
        })

    # --- Total Corners O/U (match) ---
    corner_lines = pick_standard_lines(home_cx + away_cx, market_type="corners")
    total_corners = market_total_over_under(corners_grid, corner_lines, side="total")
    for line, probs in total_corners.items():
        legs.append({
            "label": f"Total Corners Over {line}",
            "probability": probs["over"],
            "grid_source": "corners",
            "grid_filter_fn": (lambda h, a, ln=line: (h + a) > ln),
        })
        legs.append({
            "label": f"Total Corners Under {line}",
            "probability": probs["under"],
            "grid_source": "corners",
            "grid_filter_fn": (lambda h, a, ln=line: (h + a) <= ln),
        })

    # --- Team Corners O/U (home & away separately) ---
    home_corner_lines = pick_standard_lines(home_cx, market_type="corners")
    home_corners = market_total_over_under(corners_grid, home_corner_lines, side="home")
    for line, probs in home_corners.items():
        legs.append({
            "label": f"Home Team Corners Over {line}",
            "probability": probs["over"],
            "grid_source": "corners",
            "grid_filter_fn": (lambda h, a, ln=line: h > ln),
        })
        legs.append({
            "label": f"Home Team Corners Under {line}",
            "probability": probs["under"],
            "grid_source": "corners",
            "grid_filter_fn": (lambda h, a, ln=line: h <= ln),
        })

    away_corner_lines = pick_standard_lines(away_cx, market_type="corners")
    away_corners = market_total_over_under(corners_grid, away_corner_lines, side="away")
    for line, probs in away_corners.items():
        legs.append({
            "label": f"Away Team Corners Over {line}",
            "probability": probs["over"],
            "grid_source": "corners",
            "grid_filter_fn": (lambda h, a, ln=line: a > ln),
        })
        legs.append({
            "label": f"Away Team Corners Under {line}",
            "probability": probs["under"],
            "grid_source": "corners",
            "grid_filter_fn": (lambda h, a, ln=line: a <= ln),
        })

    return legs


def _joint_probability(leg_group, grid):
    """Given a list of legs all sourced from the same grid, computes the TRUE
    joint probability (all conditions simultaneously true) by summing grid
    cells that satisfy every leg's filter function - not by multiplying
    individual probabilities, which would be wrong for correlated same-grid legs."""
    total = 0.0
    for (h, a), p in grid.items():
        if all(leg["grid_filter_fn"](h, a) for leg in leg_group):
            total += p
    return total


def _is_contradictory_pair(leg_a, leg_b):
    """Quick heuristic: two legs on the same market type (e.g. both 'Total Goals')
    should not both appear in one builder, even if not literally contradictory,
    since stacking 'Over 2.5' and 'Under 4.5' on the same market is redundant/confusing
    for a real bet builder slip. We detect this by comparing label prefixes."""
    def market_key(label):
        # strip the Over/Under + number, keep the market description
        for marker in [" Over ", " Under "]:
            if marker in label:
                return label.split(marker)[0]
        return label
    return market_key(leg_a["label"]) == market_key(leg_b["label"])


def combined_probability(leg_combo, goals_grid, corners_grid):
    """
    Computes the true combined probability of a set of legs, correctly
    handling same-grid joint probability vs cross-grid independence.
    """
    goals_legs = [l for l in leg_combo if l["grid_source"] == "goals"]
    corners_legs = [l for l in leg_combo if l["grid_source"] == "corners"]

    prob = 1.0
    if goals_legs:
        prob *= _joint_probability(goals_legs, goals_grid)
    if corners_legs:
        prob *= _joint_probability(corners_legs, corners_grid)

    return prob


def build_best_bet_builder(legs, goals_grid, corners_grid, min_legs=2, max_legs=5,
                            candidates_per_size=200):
    """
    Searches for the best-combined-probability bet builder for this match,
    trying sizes from min_legs to max_legs and returning the single best one
    found (by combined probability), along with a couple of alternates.

    Since the full leg list can be 20-30+ candidates, we don't brute-force
    every combination at every size (combinatorially expensive) - instead we
    greedily build up from the highest-individual-probability legs first,
    which tends to find strong combos without exhaustive search, then
    supplement with some randomized/diverse combos for alternates.
    """
    # sort legs by individual probability, descending - greedy seed
    sorted_legs = sorted(legs, key=lambda l: l["probability"], reverse=True)

    results = []

    for size in range(min_legs, max_legs + 1):
        # Build a non-contradictory candidate combo greedily
        combo = []
        for leg in sorted_legs:
            if len(combo) >= size:
                break
            if any(_is_contradictory_pair(leg, existing) for existing in combo):
                continue
            combo.append(leg)

        if len(combo) < size:
            continue  # not enough non-contradictory legs available at this size

        combined_prob = combined_probability(combo, goals_grid, corners_grid)
        results.append({
            "legs": combo,
            "num_legs": size,
            "combined_probability": combined_prob,
        })

    if not results:
        return None, []

    # best = highest combined probability (this naturally tends to favor
    # smaller builders, which fits the "prefer safer lines" instruction -
    # but we still return all sizes so the user can see the tradeoff)
    best = max(results, key=lambda r: r["combined_probability"])
    alternates = [r for r in results if r is not best]

    return best, alternates


# ======================================================================
# --- from py ---
# ======================================================================

def _fmt_pct(p):
    return f"{p * 100:.1f}%"


def _leg_card_html(leg):
    return f"""
        <div class="leg">
            <span class="leg-label">{leg['label']}</span>
            <span class="leg-prob">{_fmt_pct(leg['probability'])}</span>
        </div>"""


def _bet_builder_card_html(match, best_builder, alternates):
    home = match["home_team_name"]
    away = match["away_team_name"]
    date_str = match.get("display_date", "")

    if best_builder is None:
        return f"""
    <div class="card">
        <div class="card-header">
            <span class="matchup">{home} vs {away}</span>
            <span class="match-date">{date_str}</span>
        </div>
        <p class="no-data">Not enough data yet to generate a builder for this match.</p>
    </div>"""

    legs_html = "".join(_leg_card_html(leg) for leg in best_builder["legs"])
    combined_pct = _fmt_pct(best_builder["combined_probability"])

    alternates_html = ""
    if alternates:
        alt_rows = ""
        for alt in sorted(alternates, key=lambda a: a["num_legs"]):
            alt_rows += f"""
                <div class="alt-row">
                    <span>{alt['num_legs']}-leg builder</span>
                    <span>{_fmt_pct(alt['combined_probability'])}</span>
                </div>"""
        alternates_html = f"""
        <details class="alternates">
            <summary>See other builder sizes ({len(alternates)})</summary>
            {alt_rows}
        </details>"""

    return f"""
    <div class="card">
        <div class="card-header">
            <span class="matchup">{home} vs {away}</span>
            <span class="match-date">{date_str}</span>
        </div>
        <div class="combined-prob">
            <span class="combined-label">{best_builder['num_legs']}-Leg Bet Builder</span>
            <span class="combined-value">{combined_pct} combined</span>
        </div>
        <div class="legs">{legs_html}
        </div>
        {alternates_html}
    </div>"""


def _top_picks_html(all_match_legs, top_n=15):
    """Flattens legs across all matches, sorts by probability, shows the safest picks."""
    flat = []
    for match, legs in all_match_legs:
        for leg in legs:
            flat.append((match, leg))

    flat.sort(key=lambda x: x[1]["probability"], reverse=True)
    top = flat[:top_n]

    rows = ""
    for match, leg in top:
        home = match["home_team_name"]
        away = match["away_team_name"]
        rows += f"""
        <div class="pick-row">
            <div class="pick-match">{home} vs {away}</div>
            <div class="pick-leg">{leg['label']}</div>
            <div class="pick-prob">{_fmt_pct(leg['probability'])}</div>
        </div>"""

    return f"""
    <div class="card">
        <div class="card-header"><span class="matchup">Top {top_n} Safest Picks Across All Matches</span></div>
        <div class="picks-list">{rows}
        </div>
    </div>"""


def _all_matches_html(match, all_legs):
    home = match["home_team_name"]
    away = match["away_team_name"]
    date_str = match.get("display_date", "")

    # group legs by category for readability
    categories = {
        "Result": [],
        "Both Teams to Score": [],
        "Total Goals": [],
        "Team Goals": [],
        "Total Corners": [],
        "Team Corners": [],
    }
    for leg in all_legs:
        label = leg["label"]
        if "Double Chance" in label or "No Draw" in label:
            categories["Result"].append(leg)
        elif "Both Teams" in label:
            categories["Both Teams to Score"].append(leg)
        elif "Total Goals" in label:
            categories["Total Goals"].append(leg)
        elif "Team Goals" in label:
            categories["Team Goals"].append(leg)
        elif "Total Corners" in label:
            categories["Total Corners"].append(leg)
        elif "Team Corners" in label:
            categories["Team Corners"].append(leg)

    sections_html = ""
    for cat_name, cat_legs in categories.items():
        if not cat_legs:
            continue
        legs_html = "".join(_leg_card_html(leg) for leg in cat_legs)
        sections_html += f"""
        <div class="market-section">
            <h4>{cat_name}</h4>
            <div class="legs">{legs_html}
            </div>
        </div>"""

    return f"""
    <div class="card">
        <div class="card-header">
            <span class="matchup">{home} vs {away}</span>
            <span class="match-date">{date_str}</span>
        </div>
        {sections_html}
    </div>"""


def build_html(matches_with_builders):
    """
    matches_with_builders: list of dicts, each:
        {
            "match": match_record,
            "all_legs": [leg, ...],
            "best_builder": {...} or None,
            "alternates": [...]
        }
    """
    now_utc = datetime.now(timezone.utc)
    now_gmt6 = now_utc.astimezone(timezone(timedelta(hours=6)))
    generated_at = (
        f"{now_utc.strftime('%Y-%m-%d %H:%M UTC')} "
        f"({now_gmt6.strftime('%Y-%m-%d %H:%M')} GMT+6)"
    )

    bet_builder_cards = "".join(
        _bet_builder_card_html(m["match"], m["best_builder"], m["alternates"])
        for m in matches_with_builders
    )

    top_picks_section = _top_picks_html(
        [(m["match"], m["all_legs"]) for m in matches_with_builders]
    )

    all_matches_cards = "".join(
        _all_matches_html(m["match"], m["all_legs"])
        for m in matches_with_builders
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MLS Predictions</title>
<style>
    :root {{
        --bg: #0f1115;
        --card-bg: #1a1d24;
        --card-border: #2a2e38;
        --text-primary: #f2f3f5;
        --text-secondary: #b8bcc4;
        --accent: #4ade80;
        --accent-dim: #2f7a52;
        --tab-inactive: #6b7280;
        --danger: #f87171;
    }}
    * {{ box-sizing: border-box; }}
    body {{
        background: var(--bg);
        color: var(--text-primary);
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        margin: 0;
        padding: 0;
    }}
    header {{
        padding: 24px 16px 12px;
        text-align: center;
        border-bottom: 1px solid var(--card-border);
    }}
    header h1 {{
        margin: 0 0 4px;
        font-size: 1.5rem;
        color: var(--text-primary);
    }}
    header p {{
        margin: 0;
        color: var(--text-secondary);
        font-size: 0.85rem;
    }}
    .disclaimer {{
        text-align: center;
        color: var(--text-secondary);
        font-size: 0.75rem;
        padding: 8px 16px;
        opacity: 0.8;
    }}
    .tabs {{
        display: flex;
        justify-content: center;
        gap: 4px;
        padding: 12px 16px;
        border-bottom: 1px solid var(--card-border);
    }}
    .tab-button {{
        background: transparent;
        border: none;
        color: var(--tab-inactive);
        padding: 10px 18px;
        font-size: 0.95rem;
        font-weight: 600;
        cursor: pointer;
        border-radius: 8px;
        transition: all 0.15s ease;
    }}
    .tab-button.active {{
        color: var(--bg);
        background: var(--accent);
    }}
    .tab-content {{
        display: none;
        padding: 16px;
        max-width: 700px;
        margin: 0 auto;
    }}
    .tab-content.active {{
        display: block;
    }}
    .card {{
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: 12px;
        padding: 16px;
        margin-bottom: 14px;
    }}
    .card-header {{
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        flex-wrap: wrap;
        gap: 4px;
        margin-bottom: 10px;
        padding-bottom: 10px;
        border-bottom: 1px solid var(--card-border);
    }}
    .matchup {{
        font-weight: 700;
        font-size: 1.05rem;
        color: var(--text-primary);
    }}
    .match-date {{
        font-size: 0.8rem;
        color: var(--text-secondary);
    }}
    .combined-prob {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 12px;
    }}
    .combined-label {{
        font-size: 0.9rem;
        color: var(--text-secondary);
    }}
    .combined-value {{
        font-size: 1.2rem;
        font-weight: 800;
        color: var(--accent);
    }}
    .legs {{
        display: flex;
        flex-direction: column;
        gap: 6px;
    }}
    .leg {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        background: rgba(255,255,255,0.03);
        border-radius: 8px;
        padding: 10px 12px;
    }}
    .leg-label {{
        font-size: 0.9rem;
        color: var(--text-primary);
    }}
    .leg-prob {{
        font-size: 0.9rem;
        font-weight: 700;
        color: var(--accent);
        white-space: nowrap;
        margin-left: 12px;
    }}
    .no-data {{
        color: var(--text-secondary);
        font-size: 0.9rem;
        margin: 0;
    }}
    .alternates {{
        margin-top: 12px;
    }}
    .alternates summary {{
        color: var(--text-secondary);
        font-size: 0.85rem;
        cursor: pointer;
        padding: 6px 0;
    }}
    .alt-row {{
        display: flex;
        justify-content: space-between;
        font-size: 0.85rem;
        color: var(--text-secondary);
        padding: 6px 0;
        border-top: 1px solid var(--card-border);
    }}
    .market-section {{
        margin-bottom: 14px;
    }}
    .market-section h4 {{
        margin: 0 0 8px;
        font-size: 0.85rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: var(--text-secondary);
    }}
    .picks-list {{
        display: flex;
        flex-direction: column;
        gap: 8px;
    }}
    .pick-row {{
        display: grid;
        grid-template-columns: 1fr auto;
        grid-template-rows: auto auto;
        background: rgba(255,255,255,0.03);
        border-radius: 8px;
        padding: 10px 12px;
        row-gap: 2px;
    }}
    .pick-match {{
        font-size: 0.8rem;
        color: var(--text-secondary);
        grid-column: 1 / 2;
    }}
    .pick-leg {{
        font-size: 0.95rem;
        font-weight: 600;
        color: var(--text-primary);
        grid-column: 1 / 2;
    }}
    .pick-prob {{
        font-size: 1.1rem;
        font-weight: 800;
        color: var(--accent);
        grid-column: 2 / 3;
        grid-row: 1 / 3;
        align-self: center;
    }}
    footer {{
        text-align: center;
        color: var(--text-secondary);
        font-size: 0.75rem;
        padding: 20px 16px 30px;
    }}
</style>
</head>
<body>

<header>
    <h1>MLS Predictions</h1>
    <p>Generated {generated_at}</p>
</header>
<p class="disclaimer">For entertainment purposes. Bet responsibly — probabilities are model estimates, not guarantees.</p>

<div class="tabs">
    <button class="tab-button active" onclick="showTab('builders')">Bet Builders</button>
    <button class="tab-button" onclick="showTab('picks')">Top Picks</button>
    <button class="tab-button" onclick="showTab('all')">All Matches</button>
</div>

<div id="builders" class="tab-content active">
    {bet_builder_cards if bet_builder_cards.strip() else '<p class="no-data" style="text-align:center;">No upcoming matches found.</p>'}
</div>

<div id="picks" class="tab-content">
    {top_picks_section}
</div>

<div id="all" class="tab-content">
    {all_matches_cards if all_matches_cards.strip() else '<p class="no-data" style="text-align:center;">No upcoming matches found.</p>'}
</div>

<footer>
    Model: Poisson + Dixon-Coles goal grid, independent Poisson corners grid.<br>
    Not affiliated with MLS, ESPN, or any sportsbook.
</footer>

<script>
function showTab(tabId) {{
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tab-button').forEach(el => el.classList.remove('active'));
    document.getElementById(tabId).classList.add('active');
    event.target.classList.add('active');
}}
</script>

</body>
</html>"""


# ======================================================================
# --- from predict.py ---
# ======================================================================

OUTPUT_DIR = "docs"
OUTPUT_FILE = "index.html"
HISTORY_DAYS = 90


def build_team_ratings_lookup(historical_matches):
    """Computes ratings for every team that appears in the historical dataset."""
    team_ids = set()
    for m in historical_matches:
        team_ids.add(m["home_team_id"])
        team_ids.add(m["away_team_id"])

    ratings = {}
    for team_id in team_ids:
        r = compute_team_ratings(team_id, historical_matches)
        if r is not None:
            ratings[team_id] = r
    return ratings


def process_match(match, ratings_lookup):
    """
    Given an upcoming match and the ratings lookup, computes the full grid,
    legs, and best bet builder. Returns a dict for html_output, or None if
    either team lacks enough historical data yet.
    """
    home_id = match["home_team_id"]
    away_id = match["away_team_id"]

    home_ratings = ratings_lookup.get(home_id)
    away_ratings = ratings_lookup.get(away_id)

    if home_ratings is None or away_ratings is None:
        print(f"  SKIP: {match['home_team_name']} vs {match['away_team_name']} "
              f"- insufficient historical data for one or both teams")
        return None

    home_xg, away_xg = expected_goals(home_ratings, away_ratings)
    home_cx, away_cx = expected_corners(home_ratings, away_ratings)

    if os.environ.get("PREDICT_DEBUG"):
        print(f"\n[DEBUG] {match['home_team_name']} vs {match['away_team_name']}")
        print(f"  home_ratings: {home_ratings}")
        print(f"  away_ratings: {away_ratings}")
        print(f"  home_xg={home_xg:.3f} away_xg={away_xg:.3f} (combined={home_xg+away_xg:.3f})")
        print(f"  home_cx={home_cx:.3f} away_cx={away_cx:.3f}")

    goals_grid = build_goals_grid(home_xg, away_xg)
    corners_grid = build_corners_grid(home_cx, away_cx)

    all_legs = generate_all_legs(
        goals_grid, corners_grid, home_xg, away_xg, home_cx, away_cx
    )

    best, alternates = build_best_bet_builder(all_legs, goals_grid, corners_grid)

    # add a display-friendly date to the match record
    try:
        dt = datetime.fromisoformat(match["date"].replace("Z", "+00:00"))
        match["display_date"] = dt.strftime("%a %b %d, %I:%M %p UTC")
    except (ValueError, KeyError, AttributeError):
        match["display_date"] = match.get("date", "")

    return {
        "match": match,
        "all_legs": all_legs,
        "best_builder": best,
        "alternates": alternates,
    }


def main():
    print("=== MLS Prediction System ===\n")

    print(f"Step 1: Fetching last {HISTORY_DAYS} days of completed matches (with stats)...")
    historical_matches = build_match_dataset(days_back=HISTORY_DAYS)

    if not historical_matches:
        print("ERROR: No historical match data available. Cannot build ratings. Exiting.")
        return

    print(f"\nStep 2: Building team strength ratings from {len(historical_matches)} matches...")
    ratings_lookup = build_team_ratings_lookup(historical_matches)
    print(f"  Ratings computed for {len(ratings_lookup)} teams")

    print("\nStep 3: Fetching upcoming matches...")
    upcoming_matches = get_upcoming_matches()
    print(f"  Found {len(upcoming_matches)} upcoming matches")

    if not upcoming_matches:
        print("  No upcoming matches found. The site will render with an empty state.")

    print("\nStep 4: Building predictions for each upcoming match...")
    processed = []
    for match in upcoming_matches:
        result = process_match(match, ratings_lookup)
        if result is not None:
            processed.append(result)
            print(f"  OK: {match['home_team_name']} vs {match['away_team_name']}")

    print(f"\n{len(processed)}/{len(upcoming_matches)} matches successfully processed")

    print("\nStep 5: Rendering HTML...")
    html = build_html(processed)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
    with open(output_path, "w") as f:
        f.write(html)

    print(f"\nDone. Output written to: {output_path}")


if __name__ == "__main__":
    main()

