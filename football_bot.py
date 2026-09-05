"""
football_bot.py
SPO-QUANT - Spor Analitik & Value Bet Sinyal Botu.

Tek dosyada birlestirilmis hali (onceden football_config.py,
football_data_fetcher.py, football_data_fetcher_apifootball.py,
football_odds_fetcher.py, football_quant_engine.py, football_value_engine.py,
football_telegram_notifier.py, football_stats_tracker.py, football_commands.py,
football_main.py olarak ayri dosyalardi - kullanicinin tek dosya yukleme
tercihi uzerine birlestirildi, 2026-08-03).

stock_screener_bot.py bu dosyayi `import football_bot as fb` ile kullanir.

Bolumler:
  1. CONFIG
  2. DATA FETCHER - football-data.org (9 buyuk Avrupa ligi)
  3. DATA FETCHER - API-Football (sadece Sueper Lig)
  4. ODDS FETCHER - The Odds API
  5. QUANT ENGINE - Poisson modeli
  6. VALUE ENGINE - EV / Kelly Kriteri
  7. TELEGRAM NOTIFIER
  8. STATS TRACKER - WON/LOST takibi
  9. TELEGRAM COMMANDS - /stats /rapor /status
  10. ORCHESTRATION - self_check, run_model_scan, run_odds_scan, run_results_update
"""

import os
import csv
import json
import math
import time
import difflib
import threading
import requests
from datetime import datetime, timedelta, timezone


# ============================================================
# 1. CONFIG
# ============================================================

FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_KEY", "")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")

FOOTBALL_TELEGRAM_TOKEN = os.environ.get("FOOTBALL_TELEGRAM_TOKEN", "")
FOOTBALL_TELEGRAM_CHAT_ID = os.environ.get("FOOTBALL_TELEGRAM_CHAT_ID", "")

DATA_DIR = os.environ.get("DATA_DIR", ".")

EV_THRESHOLD = float(os.environ.get("FOOTBALL_EV_THRESHOLD", "0.05"))
KELLY_FRACTION = float(os.environ.get("FOOTBALL_KELLY_FRACTION", "0.25"))

TRACKED_COMPETITIONS = [
    "PL", "PD", "SA", "BL1", "FL1", "CL", "DED", "PPL", "ELC",
]

# DERS (2026-08-04): FOOTBALL_NOTIFY_THROTTLE_MINUTES artik KULLANILMIYOR.
# 60 dk'lik bogucu, 240 dk'lik oran taramasinin yaninda hicbir ise yaramiyordu
# (bkz. should_notify). Yerine kalici tekrar-engeli + EV iyilesme kurali geldi.
# Sabit, eski kayitlarla uyum ve olasi geri donus icin duruyor.
FOOTBALL_NOTIFY_THROTTLE_MINUTES = int(os.environ.get("FOOTBALL_NOTIFY_THROTTLE_MINUTES", "60"))

# Bildirilmis bir bahsin TEKRAR bildirilmesi icin EV'nin en az bu kadar
# artmasi gerekir (0.04 = 4 puan, ornek: %5 -> %9). Dusuk tutmak spam'e,
# yuksek tutmak gercek iyilesmeleri kacirmaya yol acar.
FOOTBALL_EV_IMPROVE_DELTA = float(os.environ.get("FOOTBALL_EV_IMPROVE_DELTA", "0.04"))

FOOTBALL_MODEL_SCAN_INTERVAL_MINUTES = int(os.environ.get("FOOTBALL_MODEL_SCAN_INTERVAL_MINUTES", "10"))
FOOTBALL_ODDS_SCAN_INTERVAL_MINUTES = int(os.environ.get("FOOTBALL_ODDS_SCAN_INTERVAL_MINUTES", "240"))

REQUIRED_ENV_VARS = [
    "FOOTBALL_DATA_KEY", "ODDS_API_KEY", "FOOTBALL_TELEGRAM_TOKEN",
    "FOOTBALL_TELEGRAM_CHAT_ID",
]
# NOT: API_FOOTBALL_KEY kasitli olarak zorunlu degil - Sueper Lig su an
# devre disi (bkz. ENABLE_SUPERLIG), key olmasa da bot calisir.


def validate_football_config():
    return [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]


# ============================================================
# 2. DATA FETCHER - football-data.org
# ============================================================

FD_BASE_URL = "https://api.football-data.org/v4"


class DataFetchError(Exception):
    pass


# ---------------------------------------------------------------------------
# HIZ SINIRLAYICI (2026-08-04)
# ---------------------------------------------------------------------------
# SORUN: football-data.org ucretsiz plani dakikada ~10 istek veriyor. Eskiden
# sadece get_fixtures_main() lig basina 1.5 sn bekliyordu; takim gecmisi
# cagrilari (mac basina 2 takim) arka arkaya sinirsiz atiliyordu ve canlida
# "Rate limit asildi (429), tekrar denemeler tukendi" hatasi aliniyordu.
# COZUM: TUM football-data istekleri tek bir kapidan gecsin ve aralarinda
# en az FD_MIN_REQUEST_INTERVAL kadar sure olsun. Kilit kullaniyoruz cunku
# bu bot hisse botuyla ayni surecte, ayri bir thread'de calisiyor.
FD_REQUESTS_PER_MINUTE = int(os.environ.get("FD_REQUESTS_PER_MINUTE", "9"))
FD_MIN_REQUEST_INTERVAL = 60.0 / max(1, FD_REQUESTS_PER_MINUTE)

_fd_rate_lock = threading.Lock()
_fd_last_request_time = [0.0]  # liste: kilit icinde mutasyon icin

# ---------------------------------------------------------------------------
# TAMİRCİ (Auto-Healer) — 2026-08-04
# ---------------------------------------------------------------------------
# Sadece "429 alinca bekle, tekrar dene" yetmiyor: ayni tempoyla devam
# edersek ayni duvara tekrar toslariz. Tamirci, 429 gordugunde botun KENDI
# hizini kalici olarak dusuruyor; islerin yolunda gittigi her basarili
# istekte de kademeli olarak normal hiza geri donuyor.
_fd_extra_interval = [0.0]
FD_TAMIRCI_STEP = float(os.environ.get("FD_TAMIRCI_STEP", "1.5"))
FD_TAMIRCI_MAX = float(os.environ.get("FD_TAMIRCI_MAX", "20.0"))


def _fd_tamirci_slow_down() -> float:
    """429 sonrasi istekler arasi araligi artirir; yeni toplam araligi doner."""
    with _fd_rate_lock:
        _fd_extra_interval[0] = min(_fd_extra_interval[0] + FD_TAMIRCI_STEP,
                                    FD_TAMIRCI_MAX)
        return FD_MIN_REQUEST_INTERVAL + _fd_extra_interval[0]


def _fd_tamirci_recover():
    """Basarili istekte ekstra gecikmeyi yavasca geri alir."""
    with _fd_rate_lock:
        if _fd_extra_interval[0] > 0:
            _fd_extra_interval[0] = max(0.0, _fd_extra_interval[0] - FD_TAMIRCI_STEP / 5)


def _fd_wait_turn():
    """Bir onceki istekten bu yana yeterli sure gecmediyse bekler."""
    with _fd_rate_lock:
        interval = FD_MIN_REQUEST_INTERVAL + _fd_extra_interval[0]
        elapsed = time.time() - _fd_last_request_time[0]
        wait = interval - elapsed
        if wait > 0:
            time.sleep(wait)
        _fd_last_request_time[0] = time.time()


def _fd_headers():
    if not FOOTBALL_DATA_KEY:
        raise DataFetchError("FOOTBALL_DATA_KEY tanimli degil (env variable eksik).")
    return {"X-Auth-Token": FOOTBALL_DATA_KEY}


def _fd_get(endpoint, params=None, max_retries=4):
    url = f"{FD_BASE_URL}{endpoint}"
    for attempt in range(max_retries + 1):
        _fd_wait_turn()
        try:
            resp = requests.get(url, headers=_fd_headers(), params=params, timeout=15)
        except requests.RequestException as e:
            if attempt < max_retries:
                time.sleep(5 * (attempt + 1))
                continue
            raise DataFetchError(f"Baglanti hatasi ({endpoint}): {e}")

        if resp.status_code == 200:
            _fd_tamirci_recover()
            return resp.json()
        if resp.status_code == 429:
            if attempt < max_retries:
                # TAMIRCI: sadece beklemekle kalmiyoruz, tempoyu da dusuruyoruz -
                # aksi halde bir sonraki tur ayni limite yeniden takilir.
                yeni_aralik = _fd_tamirci_slow_down()
                print(f"🛠️ [TAMİRCİ] 429 alindi → istek araligi "
                      f"{yeni_aralik:.1f} sn'ye cikarildi ({endpoint})")
                # Sunucu ne kadar bekleyecegimizi soyluyorsa ONA uyuyoruz;
                # sabit 20 sn tahmin etmek yerine bu hem daha hizli hem
                # daha guvenilir.
                try:
                    wait = int(resp.headers.get("Retry-After", "0"))
                except (TypeError, ValueError):
                    wait = 0
                time.sleep(max(wait, 15 * (attempt + 1)))
                continue
            raise DataFetchError("Rate limit asildi (429), tekrar denemeler tukendi.")
        raise DataFetchError(f"{endpoint} istegi basarisiz: HTTP {resp.status_code} - {resp.text[:200]}")

    raise DataFetchError(f"{endpoint} istegi tum denemelerden sonra basarisiz oldu.")


def list_available_competitions():
    data = _fd_get("/competitions")
    return [
        {"code": c.get("code"), "name": c.get("name"), "plan": c.get("plan")}
        for c in data.get("competitions", [])
    ]


def get_fixtures_main(date_from, date_to, competitions=None):
    comp_list = competitions if competitions is not None else TRACKED_COMPETITIONS
    all_matches = []

    for comp in comp_list:
        try:
            data = _fd_get(f"/competitions/{comp}/matches", params={"dateFrom": date_from, "dateTo": date_to})
        except DataFetchError as e:
            all_matches.append({"_error": True, "competition": comp, "message": str(e)})
            continue

        for match in data.get("matches", []):
            all_matches.append({
                "_error": False,
                "competition": comp,
                "fixture_id": match.get("id"),
                "utc_date": match.get("utcDate"),
                "status": match.get("status"),
                "home_team": match.get("homeTeam", {}).get("name"),
                "away_team": match.get("awayTeam", {}).get("name"),
                "home_team_id": match.get("homeTeam", {}).get("id"),
                "away_team_id": match.get("awayTeam", {}).get("id"),
            })
        # Eskiden burada 1.5 sn'lik bir bekleme vardi; artik pacing merkezi
        # olarak _fd_wait_turn() tarafindan yapiliyor, ikisi birden gereksiz.

    return all_matches


# ---------------------------------------------------------------------------
# TAKIM GECMISI ONBELLEGI (2026-08-04)
# ---------------------------------------------------------------------------
# Asil kota tuketen sey buydu: model taramasi 10 dakikada bir calisiyor ve her
# seferinde ayni takimlarin son maclarini bastan cekiyordu. Ama bir takimin
# son 10 maci 10 dakikada degismez - en fazla gunde birkac kez degisir.
# Onbellek sayesinde ayni takim TEAM_CACHE_TTL_HOURS boyunca tek istek eder.
# Bu, istek sayisini onlarca kat dusuruyor ve 429'un kok nedenini ortadan
# kaldiriyor (hiz sinirlayici ise ikinci savunma hatti olarak kaliyor).
TEAM_CACHE_TTL_HOURS = float(os.environ.get("TEAM_CACHE_TTL_HOURS", "6"))
_team_cache = {}          # team_id -> (zaman_damgasi, mac_listesi)
_team_cache_lock = threading.Lock()


def get_team_recent_matches_main(team_id, limit=10):
    cache_key = (team_id, limit)
    now = time.time()
    with _team_cache_lock:
        cached = _team_cache.get(cache_key)
        if cached and (now - cached[0]) < TEAM_CACHE_TTL_HOURS * 3600:
            return cached[1]

    data = _fd_get(f"/teams/{team_id}/matches", params={"status": "FINISHED", "limit": limit})
    matches = []
    for match in data.get("matches", []):
        score = match.get("score", {}).get("fullTime", {})
        matches.append({
            "fixture_id": match.get("id"),
            "utc_date": match.get("utcDate"),
            "home_team_id": match.get("homeTeam", {}).get("id"),
            "away_team_id": match.get("awayTeam", {}).get("id"),
            "home_score": score.get("home"),
            "away_score": score.get("away"),
        })
    with _team_cache_lock:
        _team_cache[cache_key] = (time.time(), matches)
    return matches


def get_fixture_result_main(fixture_id):
    try:
        data = _fd_get(f"/matches/{fixture_id}")
    except DataFetchError as e:
        print(f"football_bot.get_fixture_result_main({fixture_id}): {e}")
        return None
    score = data.get("score", {}).get("fullTime", {})
    return {"status": data.get("status"), "home_score": score.get("home"), "away_score": score.get("away")}


# ============================================================
# 3. DATA FETCHER - API-Football (sadece Sueper Lig)
# ============================================================

AF_BASE_URL = "https://v3.football.api-sports.io"
AF_LEAGUE_CACHE_FILENAME = "api_football_superlig_league_id.json"
AF_TEAM_CACHE_FILENAME = "api_football_team_matches_cache.json"
AF_TEAM_CACHE_TTL_HOURS = 6
AF_LEAGUE_NAME_HINT = "Süper Lig"
AF_COUNTRY_HINT = "Turkey"


class ApiFootballError(Exception):
    pass


def _af_headers():
    if not API_FOOTBALL_KEY:
        raise ApiFootballError("API_FOOTBALL_KEY tanimli degil (env variable eksik).")
    return {"x-apisports-key": API_FOOTBALL_KEY}


def _af_get(endpoint, params=None):
    url = f"{AF_BASE_URL}{endpoint}"
    try:
        resp = requests.get(url, headers=_af_headers(), params=params, timeout=15)
    except requests.RequestException as e:
        raise ApiFootballError(f"Baglanti hatasi ({endpoint}): {e}")

    if resp.status_code != 200:
        raise ApiFootballError(f"{endpoint} istegi basarisiz: HTTP {resp.status_code} - {resp.text[:200]}")

    data = resp.json()
    if data.get("errors"):
        raise ApiFootballError(f"{endpoint} API hatasi: {data['errors']}")
    return data


def _af_league_cache_path():
    return os.path.join(DATA_DIR, AF_LEAGUE_CACHE_FILENAME)


def _af_load_cached_league_id():
    path = _af_league_cache_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("league_id")
    except (json.JSONDecodeError, OSError):
        return None


def _af_save_cached_league_id(league_id):
    try:
        with open(_af_league_cache_path(), "w", encoding="utf-8") as f:
            json.dump({"league_id": league_id, "cached_at": datetime.now(timezone.utc).isoformat()}, f)
    except OSError as e:
        print(f"football_bot: Sueper Lig ID cache'lenemedi ({e})")


def get_superlig_league_id(force_refresh=False):
    if not force_refresh:
        cached = _af_load_cached_league_id()
        if cached is not None:
            return cached

    data = _af_get("/leagues", params={"country": AF_COUNTRY_HINT})
    for entry in data.get("response", []):
        league_name = entry.get("league", {}).get("name", "")
        if AF_LEAGUE_NAME_HINT.lower() in league_name.lower():
            league_id = entry.get("league", {}).get("id")
            _af_save_cached_league_id(league_id)
            return league_id
    return None


def _af_current_season_year():
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


AF_DISABLE_FILENAME = "api_football_disabled_until.json"
AF_DISABLE_HOURS_ON_PLAN_ERROR = 24  # sezon/plan kisitiyla karsilasirsa bu kadar saat tekrar denemez


def _af_disable_path():
    return os.path.join(DATA_DIR, AF_DISABLE_FILENAME)


def _af_is_temporarily_disabled():
    path = _af_disable_path()
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            until = datetime.fromisoformat(json.load(f)["until"])
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        return False
    return datetime.now(timezone.utc) < until


def _af_disable_temporarily(hours, reason):
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    try:
        with open(_af_disable_path(), "w", encoding="utf-8") as f:
            json.dump({"until": until.isoformat(), "reason": reason}, f)
    except OSError as e:
        print(f"football_bot: API-Football disable durumu kaydedilemedi ({e})")


def get_fixtures_superlig(date_from, date_to, season=None):
    if _af_is_temporarily_disabled():
        return []  # kota korumak icin - bkz. AF_DISABLE_HOURS_ON_PLAN_ERROR

    league_id = get_superlig_league_id()
    if league_id is None:
        print("football_bot: Sueper Lig ID'si bulunamadi, tarama atlandi.")
        return []

    season = season if season is not None else _af_current_season_year()
    try:
        data = _af_get("/fixtures", params={"league": league_id, "season": season, "from": date_from, "to": date_to})
    except ApiFootballError as e:
        if "plan" in str(e).lower() and "season" in str(e).lower():
            _af_disable_temporarily(
                AF_DISABLE_HOURS_ON_PLAN_ERROR,
                f"Ücretsiz plan {season} sezonuna erişemiyor: {e}",
            )
        raise

    fixtures = []
    for item in data.get("response", []):
        fixture_info = item.get("fixture", {})
        teams = item.get("teams", {})
        status_short = fixture_info.get("status", {}).get("short", "")
        status = "SCHEDULED" if status_short == "NS" else status_short
        fixtures.append({
            "_error": False,
            "competition": "TR1_SUPERLIG",
            "fixture_id": fixture_info.get("id"),
            "utc_date": fixture_info.get("date"),
            "status": status,
            "home_team": teams.get("home", {}).get("name"),
            "away_team": teams.get("away", {}).get("name"),
            "home_team_id": teams.get("home", {}).get("id"),
            "away_team_id": teams.get("away", {}).get("id"),
        })
    return fixtures


def _af_team_cache_path():
    return os.path.join(DATA_DIR, AF_TEAM_CACHE_FILENAME)


def _af_load_team_cache():
    path = _af_team_cache_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _af_save_team_cache(cache):
    try:
        with open(_af_team_cache_path(), "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError as e:
        print(f"football_bot: takim cache'i kaydedilemedi ({e})")


def get_team_recent_matches_superlig(team_id, last=10):
    cache = _af_load_team_cache()
    key = str(team_id)
    cached_entry = cache.get(key)

    if cached_entry:
        cached_at = datetime.fromisoformat(cached_entry["cached_at"])
        age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
        if age_hours < AF_TEAM_CACHE_TTL_HOURS:
            return cached_entry["matches"]

    data = _af_get("/fixtures", params={"team": team_id, "last": last, "status": "FT"})
    matches = []
    for item in data.get("response", []):
        fixture_info = item.get("fixture", {})
        teams = item.get("teams", {})
        goals = item.get("goals", {})
        matches.append({
            "fixture_id": fixture_info.get("id"),
            "utc_date": fixture_info.get("date"),
            "home_team_id": teams.get("home", {}).get("id"),
            "away_team_id": teams.get("away", {}).get("id"),
            "home_score": goals.get("home"),
            "away_score": goals.get("away"),
        })

    cache[key] = {"matches": matches, "cached_at": datetime.now(timezone.utc).isoformat()}
    _af_save_team_cache(cache)
    return matches


def get_fixture_result_superlig(fixture_id):
    try:
        data = _af_get("/fixtures", params={"id": fixture_id})
    except ApiFootballError as e:
        print(f"football_bot.get_fixture_result_superlig({fixture_id}): {e}")
        return None

    response = data.get("response", [])
    if not response:
        return None

    item = response[0]
    status_short = item.get("fixture", {}).get("status", {}).get("short", "")
    status = "FINISHED" if status_short == "FT" else status_short
    goals = item.get("goals", {})
    return {"status": status, "home_score": goals.get("home"), "away_score": goals.get("away")}


# ============================================================
# 4. ODDS FETCHER - The Odds API
# ============================================================

ODDS_BASE_URL = "https://api.the-odds-api.com/v4"


class OddsFetchError(Exception):
    pass


def list_soccer_sport_keys():
    if not ODDS_API_KEY:
        raise OddsFetchError("ODDS_API_KEY tanimli degil (env variable eksik).")
    resp = requests.get(f"{ODDS_BASE_URL}/sports", params={"apiKey": ODDS_API_KEY}, timeout=15)
    if resp.status_code != 200:
        raise OddsFetchError(f"Sport list istegi basarisiz: HTTP {resp.status_code}")
    return [s["key"] for s in resp.json() if s.get("group") == "Soccer"]


def get_odds(sport_key, regions="eu", markets="h2h"):
    if not ODDS_API_KEY:
        raise OddsFetchError("ODDS_API_KEY tanimli degil (env variable eksik).")

    params = {"apiKey": ODDS_API_KEY, "regions": regions, "markets": markets, "oddsFormat": "decimal"}
    try:
        resp = requests.get(f"{ODDS_BASE_URL}/sports/{sport_key}/odds", params=params, timeout=15)
    except requests.RequestException as e:
        raise OddsFetchError(f"Baglanti hatasi: {e}")

    if resp.status_code != 200:
        raise OddsFetchError(f"Odds istegi basarisiz: HTTP {resp.status_code} - {resp.text[:200]}")

    quota_remaining = resp.headers.get("x-requests-remaining")
    quota_used = resp.headers.get("x-requests-used")

    target_market = markets.split(",")[0]
    matches = []
    for event in resp.json():
        bookmakers_data = []
        for bm in event.get("bookmakers", []):
            for market in bm.get("markets", []):
                if market.get("key") != target_market:
                    continue
                bookmakers_data.append({
                    "bookmaker": bm.get("title"),
                    "last_update": bm.get("last_update"),
                    "outcomes": market.get("outcomes", []),
                })
        matches.append({
            "event_id": event.get("id"),
            "commence_time": event.get("commence_time"),
            "home_team": event.get("home_team"),
            "away_team": event.get("away_team"),
            "bookmakers": bookmakers_data,
        })

    return {"matches": matches, "quota_remaining": quota_remaining, "quota_used": quota_used}


# ============================================================
# 5. QUANT ENGINE - Poisson modeli
# ============================================================

MIN_MATCHES_FOR_CONFIDENCE = 6
MAX_GOALS = 8
DEFAULT_LEAGUE_AVG_HOME_GOALS = 1.45
DEFAULT_LEAGUE_AVG_AWAY_GOALS = 1.15


def compute_team_scoring_stats(matches, team_id):
    scored, conceded, count = 0, 0, 0
    for m in matches:
        home_score = m.get("home_score")
        away_score = m.get("away_score")
        if home_score is None or away_score is None:
            continue
        if m.get("home_team_id") == team_id:
            scored += home_score
            conceded += away_score
        elif m.get("away_team_id") == team_id:
            scored += away_score
            conceded += home_score
        else:
            continue
        count += 1

    if count == 0:
        return None
    return {
        "goals_scored_avg": scored / count,
        "goals_conceded_avg": conceded / count,
        "matches_count": count,
        "low_sample": count < MIN_MATCHES_FOR_CONFIDENCE,
    }


def compute_expected_goals(home_stats, away_stats,
                            league_avg_home_goals=DEFAULT_LEAGUE_AVG_HOME_GOALS,
                            league_avg_away_goals=DEFAULT_LEAGUE_AVG_AWAY_GOALS):
    home_attack = home_stats["goals_scored_avg"] / league_avg_home_goals
    home_defense = home_stats["goals_conceded_avg"] / league_avg_away_goals
    away_attack = away_stats["goals_scored_avg"] / league_avg_away_goals
    away_defense = away_stats["goals_conceded_avg"] / league_avg_home_goals

    lambda_home = home_attack * away_defense * league_avg_home_goals
    lambda_away = away_attack * home_defense * league_avg_away_goals

    confidence = "low_sample" if (home_stats.get("low_sample") or away_stats.get("low_sample")) else "normal"
    return lambda_home, lambda_away, confidence


def _poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def build_score_matrix(lambda_home, lambda_away, max_goals=MAX_GOALS):
    home_probs = [_poisson_pmf(i, lambda_home) for i in range(max_goals + 1)]
    away_probs = [_poisson_pmf(j, lambda_away) for j in range(max_goals + 1)]
    return [[home_probs[i] * away_probs[j] for j in range(max_goals + 1)] for i in range(max_goals + 1)]


def match_outcome_probabilities(matrix):
    home_win = draw = away_win = 0.0
    n = len(matrix)
    for i in range(n):
        for j in range(n):
            p = matrix[i][j]
            if i > j:
                home_win += p
            elif i == j:
                draw += p
            else:
                away_win += p
    return {"home_win": home_win, "draw": draw, "away_win": away_win}


def over_under_probability(matrix, line=2.5):
    over = under = 0.0
    n = len(matrix)
    for i in range(n):
        for j in range(n):
            if i + j > line:
                over += matrix[i][j]
            else:
                under += matrix[i][j]
    return {"over": over, "under": under}


def analyze_match(home_matches, away_matches, home_team_id, away_team_id,
                   league_avg_home_goals=DEFAULT_LEAGUE_AVG_HOME_GOALS,
                   league_avg_away_goals=DEFAULT_LEAGUE_AVG_AWAY_GOALS, ou_line=2.5):
    home_stats = compute_team_scoring_stats(home_matches, home_team_id)
    away_stats = compute_team_scoring_stats(away_matches, away_team_id)
    if home_stats is None or away_stats is None:
        return None

    lambda_home, lambda_away, confidence = compute_expected_goals(
        home_stats, away_stats, league_avg_home_goals, league_avg_away_goals)
    matrix = build_score_matrix(lambda_home, lambda_away)

    return {
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
        "confidence": confidence,
        "outcome_probs": match_outcome_probabilities(matrix),
        "over_under": over_under_probability(matrix, line=ou_line),
        "home_matches_used": home_stats["matches_count"],
        "away_matches_used": away_stats["matches_count"],
    }


# ============================================================
# 6. VALUE ENGINE - EV / Kelly Kriteri
# ============================================================

TEAM_NAME_MATCH_THRESHOLD = 0.6
MAX_KICKOFF_DIFF_HOURS = 6
_NAME_NOISE_WORDS = ["fc", "cf", "afc", "sk", "sc", "ac", "cd", "club", "football",
                      "futbol", "the", "de", "united", "utd"]


def _normalize_team_name(name):
    cleaned = "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in name)
    words = [w for w in cleaned.split() if w not in _NAME_NOISE_WORDS]
    return " ".join(words) if words else cleaned.strip()


def _name_similarity(a, b):
    return difflib.SequenceMatcher(None, _normalize_team_name(a), _normalize_team_name(b)).ratio()


def _parse_iso(dt_str):
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def match_fixture_to_odds_event(fixture, odds_events):
    fixture_time = _parse_iso(fixture.get("utc_date"))
    best_match = None
    best_score = 0.0

    for event in odds_events:
        event_time = _parse_iso(event.get("commence_time"))
        if fixture_time and event_time:
            hours_diff = abs((fixture_time - event_time).total_seconds()) / 3600
            if hours_diff > MAX_KICKOFF_DIFF_HOURS:
                continue

        home_sim = _name_similarity(fixture.get("home_team", ""), event.get("home_team", ""))
        away_sim = _name_similarity(fixture.get("away_team", ""), event.get("away_team", ""))
        combined = (home_sim + away_sim) / 2
        if combined > best_score:
            best_score = combined
            best_match = event

    if best_match is not None and best_score >= TEAM_NAME_MATCH_THRESHOLD:
        return best_match
    return None


def compute_ev(probability, decimal_odds):
    return (probability * decimal_odds) - 1


def compute_kelly_fraction(probability, decimal_odds, fractional_multiplier=None):
    b = decimal_odds - 1
    if b <= 0:
        return 0.0
    raw_kelly = (probability * (b + 1) - 1) / b
    if raw_kelly <= 0:
        return 0.0
    multiplier = fractional_multiplier if fractional_multiplier is not None else KELLY_FRACTION
    return raw_kelly * multiplier


def _best_price_for_outcome(bookmakers, outcome_name):
    best_price = None
    best_bookmaker = None
    for bm in bookmakers:
        for outcome in bm.get("outcomes", []):
            if outcome.get("name") == outcome_name:
                price = outcome.get("price")
                if price and (best_price is None or price > best_price):
                    best_price = price
                    best_bookmaker = bm.get("bookmaker")
    return best_price, best_bookmaker


def find_value_bets(fixture, quant_result, odds_event, ev_threshold=None):
    threshold = ev_threshold if ev_threshold is not None else EV_THRESHOLD
    probs = quant_result["outcome_probs"]
    bookmakers = odds_event.get("bookmakers", [])

    outcome_map = [
        ("home_win", odds_event.get("home_team")),
        ("away_win", odds_event.get("away_team")),
        ("draw", "Draw"),
    ]

    signals = []
    for prob_key, outcome_name in outcome_map:
        if not outcome_name:
            continue
        price, bookmaker = _best_price_for_outcome(bookmakers, outcome_name)
        if price is None:
            continue

        probability = probs[prob_key]
        ev = compute_ev(probability, price)
        if ev < threshold:
            continue

        kelly = compute_kelly_fraction(probability, price)
        signals.append({
            "fixture_id": fixture.get("fixture_id"),
            "home_team": fixture.get("home_team"),
            "away_team": fixture.get("away_team"),
            "competition": fixture.get("competition"),
            "commence_time": fixture.get("utc_date"),
            "outcome": prob_key,
            "outcome_label": outcome_name,
            "model_probability": probability,
            "odds": price,
            "bookmaker": bookmaker,
            "ev": ev,
            "kelly_fraction": kelly,
            "confidence": quant_result.get("confidence"),
            "low_sample_warning": quant_result.get("confidence") == "low_sample",
        })

    return signals


# ============================================================
# 7. TELEGRAM NOTIFIER
# ============================================================

NOTIF_STATE_FILENAME = "football_notified.json"


def _notif_state_path():
    return os.path.join(DATA_DIR, NOTIF_STATE_FILENAME)


def _notif_load_state():
    path = _notif_state_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _notif_save_state(state):
    try:
        with open(_notif_state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError as e:
        print(f"football_bot: bildirim state'i kaydedilemedi ({e})")


def _signal_key(signal):
    return f"{signal['fixture_id']}_{signal['outcome']}"


def should_notify(signal):
    """Bir sinyalin bildirilip bildirilmeyecegine karar verir.

    DERS (2026-08-04): eskiden bu 60 DAKIKALIK bir bogucuydu, ama oran
    taramasi 4 SAATTE bir calisiyor - yani bogucu hicbir zaman devreye
    girmiyordu. Sonuc: ayni bahis, mac oynanana kadar her taramada yeniden
    bildiriliyordu. 3 gunluk pencere x gunde 6 tarama = TEK bir bahis icin
    18 mesaj; 5-10 aktif sinyalle ayda 1000+ mesaj. Kripto botundaki
    bildirim seli sorununun aynisi.
    Yeni kural: her (mac + secenek) icin BIR KEZ bildir. Sadece EV belirgin
    sekilde iyilesirse tekrar haber ver - cunku o zaman gercekten yeni bir
    bilgi vardir (oran daha da lehine dondu)."""
    state = _notif_load_state()
    rec = state.get(_signal_key(signal))
    if rec is None:
        return True
    # Eski format (duz ISO string) ile geriye donuk uyumluluk: o kayitlar
    # zaten bildirilmis demektir, tekrar bildirme.
    if not isinstance(rec, dict):
        return False
    try:
        last_ev = float(rec.get("last_ev", -99))
    except (TypeError, ValueError):
        return False
    current_ev = signal.get("ev")
    if current_ev is None:
        return False
    return (current_ev - last_ev) >= FOOTBALL_EV_IMPROVE_DELTA


def mark_notified(signal):
    state = _notif_load_state()
    state[_signal_key(signal)] = {
        "last_notified": datetime.now(timezone.utc).isoformat(),
        "last_ev": signal.get("ev"),
    }
    _notif_save_state(state)


def _previous_ev(signal):
    """Daha once bildirilmisse onceki EV'yi doner (mesajda 'iyilesti' notu
    gosterebilmek icin)."""
    rec = _notif_load_state().get(_signal_key(signal))
    if isinstance(rec, dict):
        try:
            return float(rec.get("last_ev"))
        except (TypeError, ValueError):
            return None
    return None


def format_value_bet_message(signal):
    outcome_labels = {
        "home_win": "Ev Sahibi Kazanır (1)",
        "draw": "Beraberlik (X)",
        "away_win": "Deplasman Kazanır (2)",
    }
    warning = "\n⚠️ Az örneklem — düşük güven, dikkatli değerlendir" if signal.get("low_sample_warning") else ""
    return (
        f"⚽ [VALUE BET] {signal['home_team']} - {signal['away_team']} ({signal['competition']})\n"
        f"Seçim: {outcome_labels.get(signal['outcome'], signal['outcome_label'])}\n"
        f"Model olasılığı: %{signal['model_probability']*100:.1f} | "
        f"Oran: {signal['odds']} ({signal['bookmaker']})\n"
        f"EV: %{signal['ev']*100:.1f} | Önerilen Kelly bahis: bakiyenin %{signal['kelly_fraction']*100:.2f}'i"
        + warning
    )


def send_football_message(text):
    if not FOOTBALL_TELEGRAM_TOKEN or not FOOTBALL_TELEGRAM_CHAT_ID:
        print("football_bot: token/chat_id eksik, mesaj gönderilemedi")
        return
    url = f"https://api.telegram.org/bot{FOOTBALL_TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": FOOTBALL_TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, data=payload, timeout=15)
    except Exception as e:
        print(f"football_bot: Telegram gönderim hatası ({e})")


def notify_value_bets(signals):
    sent_signals = []
    for signal in signals:
        if should_notify(signal):
            prev = _previous_ev(signal)
            msg = format_value_bet_message(signal)
            if prev is not None:
                # Tekrar bildirimi ancak EV belirgin iyilestiginde olur -
                # kullanici bunun yeni bir sinyal degil, ayni bahsin
                # iyilesmis hali oldugunu gormeli.
                msg += (f"\n\n🔁 Bu bahis daha önce bildirilmişti "
                        f"(EV %{prev * 100:.1f} → %{signal['ev'] * 100:.1f}). "
                        f"Oran lehine döndüğü için tekrar hatırlatılıyor.")
            send_football_message(msg)
            mark_notified(signal)
            sent_signals.append(signal)
    return sent_signals


# ============================================================
# 8. STATS TRACKER - WON/LOST takibi
# ============================================================

SIGNALS_CSV_FILENAME = "football_signals.csv"
SIGNALS_FIELDNAMES = [
    "signal_id", "fixture_id", "logged_at", "competition", "home_team", "away_team",
    "outcome", "outcome_label", "model_probability", "odds", "bookmaker",
    "ev", "kelly_fraction", "commence_time", "status",
    "settled_at", "home_score", "away_score",
]
RESULT_CHECK_BUFFER_HOURS = 3


def _stats_csv_path():
    return os.path.join(DATA_DIR, SIGNALS_CSV_FILENAME)


def _stats_ensure_csv_exists():
    path = _stats_csv_path()
    if not os.path.exists(path):
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=SIGNALS_FIELDNAMES).writeheader()
        except OSError as e:
            print(f"football_bot: sinyal CSV'si olusturulamadi ({e})")


def _stats_read_all_rows():
    _stats_ensure_csv_exists()
    try:
        with open(_stats_csv_path(), "r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError as e:
        print(f"football_bot: sinyal CSV'si okunamadi ({e})")
        return []


def _stats_write_all_rows(rows):
    try:
        with open(_stats_csv_path(), "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=SIGNALS_FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)
    except OSError as e:
        print(f"football_bot: sinyal CSV'si yazilamadi ({e})")


def log_signal(signal):
    rows = _stats_read_all_rows()
    sid = _signal_key(signal)
    if any(r["signal_id"] == sid for r in rows):
        return

    rows.append({
        "signal_id": sid,
        "fixture_id": signal.get("fixture_id", ""),
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "competition": signal.get("competition", ""),
        "home_team": signal.get("home_team", ""),
        "away_team": signal.get("away_team", ""),
        "outcome": signal.get("outcome", ""),
        "outcome_label": signal.get("outcome_label", ""),
        "model_probability": signal.get("model_probability", ""),
        "odds": signal.get("odds", ""),
        "bookmaker": signal.get("bookmaker", ""),
        "ev": signal.get("ev", ""),
        "kelly_fraction": signal.get("kelly_fraction", ""),
        "commence_time": signal.get("commence_time", ""),
        "status": "PENDING",
        "settled_at": "",
        "home_score": "",
        "away_score": "",
    })
    _stats_write_all_rows(rows)


def _actual_outcome(home_score, away_score):
    if home_score > away_score:
        return "home_win"
    if home_score < away_score:
        return "away_win"
    return "draw"


def _stats_fetch_result(row):
    fixture_id = row["fixture_id"]
    if row["competition"] == "TR1_SUPERLIG":
        return get_fixture_result_superlig(fixture_id)
    return get_fixture_result_main(fixture_id)


def update_results():
    rows = _stats_read_all_rows()
    checked = 0
    updated = 0

    for row in rows:
        if row["status"] != "PENDING":
            continue
        commence = row.get("commence_time", "")
        if not commence:
            continue
        try:
            commence_dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        except ValueError:
            continue
        if datetime.now(timezone.utc) < commence_dt + timedelta(hours=RESULT_CHECK_BUFFER_HOURS):
            continue

        checked += 1
        result = _stats_fetch_result(row)
        if result is None or result.get("status") != "FINISHED":
            continue

        home_score = result.get("home_score")
        away_score = result.get("away_score")
        if home_score is None or away_score is None:
            continue

        actual = _actual_outcome(home_score, away_score)
        row["status"] = "WON" if actual == row["outcome"] else "LOST"
        row["settled_at"] = datetime.now(timezone.utc).isoformat()
        row["home_score"] = home_score
        row["away_score"] = away_score
        updated += 1

    _stats_write_all_rows(rows)
    still_pending = sum(1 for r in rows if r["status"] == "PENDING")
    return {"checked": checked, "updated": updated, "still_pending": still_pending}


def compute_stats():
    rows = _stats_read_all_rows()
    total = len(rows)
    won = sum(1 for r in rows if r["status"] == "WON")
    lost = sum(1 for r in rows if r["status"] == "LOST")
    pending = sum(1 for r in rows if r["status"] == "PENDING")

    settled = won + lost
    win_rate_pct = (won / settled * 100) if settled > 0 else 0.0

    profit = 0.0
    for r in rows:
        if r["status"] == "WON":
            profit += float(r["odds"]) - 1
        elif r["status"] == "LOST":
            profit -= 1
    roi_pct = (profit / settled * 100) if settled > 0 else 0.0

    return {"total": total, "won": won, "lost": lost, "pending": pending,
            "win_rate_pct": win_rate_pct, "roi_pct": roi_pct}


def get_recent_history(n=10):
    rows = _stats_read_all_rows()
    return sorted(rows, key=lambda r: r.get("logged_at", ""), reverse=True)[:n]


# ============================================================
# 9. TELEGRAM COMMANDS - /stats /rapor /status
# ============================================================

CMD_OFFSET_FILENAME = "football_telegram_offset.json"


def _cmd_offset_path():
    return os.path.join(DATA_DIR, CMD_OFFSET_FILENAME)


def _cmd_load_offset():
    path = _cmd_offset_path()
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("offset", 0)
    except (json.JSONDecodeError, OSError):
        return 0


def _cmd_save_offset(offset):
    try:
        with open(_cmd_offset_path(), "w", encoding="utf-8") as f:
            json.dump({"offset": offset}, f)
    except OSError as e:
        print(f"football_bot: komut offset'i kaydedilemedi ({e})")


def _cmd_get_updates(offset):
    if not FOOTBALL_TELEGRAM_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{FOOTBALL_TELEGRAM_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 0}, timeout=15)
    except requests.RequestException as e:
        print(f"football_bot: getUpdates hatası ({e})")
        return []
    if resp.status_code != 200:
        print(f"football_bot: getUpdates HTTP {resp.status_code}")
        return []
    return resp.json().get("result", [])


def _format_stats():
    s = compute_stats()
    if s["total"] == 0:
        return "📊 [STATS] Henüz kaydedilmiş sinyal yok."
    return (
        f"📊 [STATS]\n"
        f"Toplam sinyal: {s['total']} (Bekleyen: {s['pending']})\n"
        f"Sonuçlanan: {s['won'] + s['lost']} — Kazanan: {s['won']} | Kaybeden: {s['lost']}\n"
        f"Win Rate: %{s['win_rate_pct']:.1f}\n"
        f"ROI: %{s['roi_pct']:.1f} (1 birim sabit stake varsayımıyla — gerçek "
        f"getiri kendi stake büyüklüğüne göre değişir)"
    )


def _format_history(n=10):
    rows = get_recent_history(n)
    if not rows:
        return "📋 [RAPOR] Henüz kaydedilmiş sinyal yok."
    lines = [f"📋 [RAPOR] Son {len(rows)} sinyal:"]
    status_emoji = {"WON": "✅", "LOST": "❌", "PENDING": "⏳"}
    for r in rows:
        emoji = status_emoji.get(r["status"], "❔")
        lines.append(f"{emoji} {r['home_team']} - {r['away_team']} | {r['outcome_label']} @ {r['odds']} | {r['status']}")
    return "\n".join(lines)


def _format_status():
    cache = _load_model_cache()
    if cache is None:
        model_line = "Model taraması: henüz hiç çalışmadı"
    else:
        generated_at = cache.get("generated_at", "bilinmiyor")
        entry_count = len(cache.get("entries", []))
        model_line = f"Son model taraması: {generated_at} ({entry_count} maç analiz edildi)"

    s = compute_stats()
    satirlar = [f"💗 [DURUM] Bot çalışıyor.", model_line,
                f"Bekleyen sinyal: {s['pending']} | Toplam sinyal: {s['total']}"]
    if _SON_TARAMA:
        satirlar.append(f"\n🔍 Son tarama teşhisi:")
        satirlar.append(f"   API'den gelen maç: {_SON_TARAMA['ham_mac']}")
        satirlar.append(f"   Oynanacak (SCHEDULED/TIMED): {_SON_TARAMA['oynanacak']}")
        satirlar.append(f"   Modellenen: {_SON_TARAMA['modellenen']}")
        if _SON_TARAMA["durumlar"]:
            d = ", ".join(f"{k}={v}" for k, v in _SON_TARAMA["durumlar"].items())
            satirlar.append(f"   Maç durumları: {d}")
        if _SON_TARAMA["hatalar"]:
            satirlar.append(f"   ⚠️ Hatalar:")
            for h in _SON_TARAMA["hatalar"]:
                satirlar.append(f"      • {h[:120]}")
    else:
        satirlar.append("\n(Model taraması henüz bu oturumda çalışmadı - "
                        "ilk tarama 10 dk içinde olur)")
    return "\n".join(satirlar)


def poll_and_respond():
    offset = _cmd_load_offset()
    updates = _cmd_get_updates(offset)
    if not updates:
        return 0

    responded = 0
    max_update_id = offset - 1

    for update in updates:
        update_id = update.get("update_id", 0)
        max_update_id = max(max_update_id, update_id)

        message = update.get("message", {})
        text = (message.get("text") or "").strip().lower()
        chat_id = str(message.get("chat", {}).get("id", ""))

        if chat_id != str(FOOTBALL_TELEGRAM_CHAT_ID):
            continue

        if text.startswith("/stats"):
            send_football_message(_format_stats())
            responded += 1
        elif text.startswith("/rapor") or text.startswith("/history"):
            send_football_message(_format_history())
            responded += 1
        elif text.startswith("/status"):
            send_football_message(_format_status())
            responded += 1

    _cmd_save_offset(max_update_id + 1)
    return responded


# ============================================================
# 10. ORCHESTRATION
# ============================================================

ODDS_SPORT_KEYS = {
    "PL": "soccer_epl",
    "PD": "soccer_spain_la_liga",
    "SA": "soccer_italy_serie_a",
    "BL1": "soccer_germany_bundesliga",
    "FL1": "soccer_france_ligue_one",
    "CL": "soccer_uefa_champs_league",
    "DED": "soccer_netherlands_eredivisie",
    "PPL": "soccer_portugal_primeira_liga",
    "ELC": "soccer_efl_champ",
    "TR1_SUPERLIG": "soccer_turkey_super_league",
}

REQUIRED_FOOTBALL_FUNCTIONS = ["run_model_scan", "run_odds_scan", "run_results_update", "poll_and_respond"]
MODEL_CACHE_FILENAME = "football_model_cache.json"


def self_check_football():
    missing_env = validate_football_config()
    if missing_env:
        send_football_message("🚨 [FUTBOL BOT BAŞLATMA HATASI] Eksik env variable(lar): " + ", ".join(missing_env))
        return False
    for func_name in REQUIRED_FOOTBALL_FUNCTIONS:
        if func_name not in globals():
            send_football_message(f"🚨 [FUTBOL BOT BAŞLATMA HATASI] Beklenen fonksiyon eksik: {func_name}")
            return False
    return True


def _model_cache_path():
    return os.path.join(DATA_DIR, MODEL_CACHE_FILENAME)


def _save_model_cache(entries):
    try:
        with open(_model_cache_path(), "w", encoding="utf-8") as f:
            json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "entries": entries}, f)
    except OSError as e:
        print(f"football_bot: model cache kaydedilemedi ({e})")


def _load_model_cache():
    path = _model_cache_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


ENABLE_SUPERLIG = os.environ.get("ENABLE_SUPERLIG", "false").lower() == "true"
# Simdilik kapali: API-Football'in ucretsiz plani sadece 2022-2024 sezonlarina
# erisim veriyor, guncel sezona (2026) erisemiyor - bkz. Gemini Rapor 3.
# Bir cozum bulununca ENABLE_SUPERLIG=true env variable'i ile acilabilir,
# kod tarafinda baska bir degisiklik gerekmez.


def _fetch_all_fixtures(date_from, date_to):
    fixtures = get_fixtures_main(date_from, date_to)
    if not ENABLE_SUPERLIG:
        return fixtures
    try:
        fixtures.extend(get_fixtures_superlig(date_from, date_to))
    except ApiFootballError as e:
        fixtures.append({"_error": True, "competition": "TR1_SUPERLIG", "message": str(e)})
    return fixtures


def _get_team_history(fixture, team_role):
    team_id = fixture[f"{team_role}_team_id"]
    if fixture["competition"] == "TR1_SUPERLIG":
        return get_team_recent_matches_superlig(team_id)
    return get_team_recent_matches_main(team_id)


_SON_TARAMA = None


def run_model_scan(days_ahead=3):
    errors = []
    today = datetime.now(timezone.utc).date()
    date_from = today.isoformat()
    date_to = (today + timedelta(days=days_ahead)).isoformat()

    fixtures = _fetch_all_fixtures(date_from, date_to)
    # 2026-09-05 KRİTİK DÜZELTME: önceden SADECE status == "SCHEDULED"
    # olan maçlar alınıyordu. Ama football-data.org'da maçın kesin
    # başlama saati belli olunca durum "TIMED"a dönüyor - yani YAKIN
    # tarihli maçların neredeyse hepsi TIMED oluyor ve bu filtre onları
    # ELİYORDU. Belirti: kullanıcı 5 Eylül'de "bugün Premier Lig maçı
    # var ama bot 0 maç analiz etti" dedi. Sebep buydu.
    OYNANACAK = {"SCHEDULED", "TIMED"}
    scheduled_fixtures = [f for f in fixtures
                          if not f.get("_error") and f.get("status") in OYNANACAK]
    for f in fixtures:
        if f.get("_error"):
            errors.append(f"{f['competition']}: {f['message']}")

    model_entries = []
    for fixture in scheduled_fixtures:
        try:
            home_hist = _get_team_history(fixture, "home")
            away_hist = _get_team_history(fixture, "away")
        except (DataFetchError, ApiFootballError) as e:
            errors.append(f"team_history({fixture['home_team']} vs {fixture['away_team']}): {e}")
            continue

        quant_result = analyze_match(home_hist, away_hist, fixture["home_team_id"], fixture["away_team_id"])
        if quant_result is None:
            continue
        model_entries.append({"fixture": fixture, "quant_result": quant_result})

    _save_model_cache(model_entries)
    # 2026-09-05: son tarama teşhisini sakla - /status'ta gösterilecek.
    # Kullanıcı "0 maç analiz edildi" görüp neden olduğunu anlayamadı;
    # ham maç sayısı ve hatalar görünmediği için körlemesine kalmıştık.
    global _SON_TARAMA
    _SON_TARAMA = {
        "ham_mac": len([f for f in fixtures if not f.get("_error")]),
        "oynanacak": len(scheduled_fixtures),
        "modellenen": len(model_entries),
        "durumlar": {},
        "hatalar": errors[:5],
    }
    for f in fixtures:
        if not f.get("_error"):
            d = f.get("status", "?")
            _SON_TARAMA["durumlar"][d] = _SON_TARAMA["durumlar"].get(d, 0) + 1
    return {"fixtures_checked": len(scheduled_fixtures), "model_computed": len(model_entries), "errors": errors}


def run_odds_scan():
    errors = []
    cache = _load_model_cache()
    if cache is None:
        return {"model_entries_used": 0, "signals_found": 0, "signals_sent": 0,
                "errors": ["model cache henüz oluşmadı — run_model_scan hiç çalışmamış olabilir"]}

    model_entries = cache.get("entries", [])
    all_signals = []
    all_candidates = []

    odds_cache = {}
    competitions_in_scope = {e["fixture"]["competition"] for e in model_entries}
    for comp in competitions_in_scope:
        sport_key = ODDS_SPORT_KEYS.get(comp)
        if not sport_key:
            continue
        try:
            odds_data = get_odds(sport_key)
            odds_cache[comp] = odds_data["matches"]
        except OddsFetchError as e:
            errors.append(f"odds({comp}): {e}")
            odds_cache[comp] = []

    for entry in model_entries:
        fixture = entry["fixture"]
        quant_result = entry["quant_result"]
        odds_events = odds_cache.get(fixture["competition"], [])
        if not odds_events:
            continue
        matched_event = match_fixture_to_odds_event(fixture, odds_events)
        if matched_event is None:
            continue
        # Esigi ASMAYAN adaylari da topluyoruz (ekstra API cagrisi YOK, ayni
        # veriden hesaplaniyor). Amac: gunluk ozette "hic maç yoktu" ile
        # "maç vardı ama avantaj yetmedi" ayrimini yapabilmek. Sinyal olarak
        # sadece esigi gecenler kullanilir - davranis degismiyor.
        candidates = find_value_bets(fixture, quant_result, matched_event, ev_threshold=-99)
        all_candidates.extend(candidates)
        all_signals.extend([c for c in candidates if c.get("ev", -99) >= EV_THRESHOLD])

    sent_signals = notify_value_bets(all_signals)
    for signal in sent_signals:
        log_signal(signal)

    best_ev = max((c.get("ev", -99) for c in all_candidates), default=None)
    _save_daily_state({
        "odds_scan_at": datetime.now(timezone.utc).isoformat(),
        "candidates": len(all_candidates),
        "best_ev": best_ev,
        "signals_found": len(all_signals),
        "odds_errors": len(errors),
    })

    return {
        "model_entries_used": len(model_entries),
        "signals_found": len(all_signals),
        "signals_sent": len(sent_signals),
        "best_ev": best_ev,
        "errors": errors,
    }


# ------------------------------------------------------------
# GÜNLÜK ÖZET (2026-08-04)
# ------------------------------------------------------------
# NEDEN VAR: hisse botuna gunluk "yasam sinyali" eklemistik cunku "bot
# calisiyor ama sinyal yok" ile "bot olmus" ayirt edilemiyordu. Futbol
# tarafinda bu eksikti ve kullanici tam olarak o belirsizligi yasadi
# (gunlerce hic mesaj gelmedi, sebebi anlasilmadi).
# Ozet sadece "calisiyorum" demiyor; KAC MAC tarandigini ve en yuksek EV'nin
# esige ne kadar yaklastigini da soyluyor - boylece "hic maç yoktu" ile
# "maç vardı ama avantaj yetmedi" ayrimi yapilabiliyor.
FOOTBALL_SUMMARY_HOUR = int(os.environ.get("FOOTBALL_SUMMARY_HOUR", "20"))
DAILY_STATE_FILENAME = "football_daily_state.json"


def _daily_state_path():
    return os.path.join(DATA_DIR, DAILY_STATE_FILENAME)


def _load_daily_state():
    try:
        with open(_daily_state_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_daily_state(update: dict):
    state = _load_daily_state()
    state.update(update)
    try:
        with open(_daily_state_path(), "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"football_daily_state yazilamadi: {e}")


def maybe_send_daily_summary():
    """Gunde bir kez, FOOTBALL_SUMMARY_HOUR'dan sonra ozet gonderir."""
    now = datetime.now()
    today = now.date().isoformat()
    state = _load_daily_state()
    if state.get("summary_sent_date") == today:
        return False
    if now.hour < FOOTBALL_SUMMARY_HOUR:
        return False

    cache = _load_model_cache()
    entries = cache.get("entries", []) if cache else []
    fixtures_n = len(entries)
    best_ev = state.get("best_ev")
    candidates = state.get("candidates", 0)
    s = compute_stats()

    lines = ["⚽ [GÜNLÜK ÖZET] Maç analiz botu çalışıyor."]
    if cache:
        lines.append(f"Son model taraması: {cache.get('generated_at', '?')}")
    lines.append(f"Analiz edilen maç: {fixtures_n}")

    if fixtures_n == 0:
        lines.append("Takip edilen 9 ligde önümüzdeki 3 gün içinde programlanmış maç yok.")
        lines.append("Avrupa ligleri genelde Ağustos ortasında başlar — sezon açılınca "
                     "maçlar otomatik gelmeye başlayacak.")
    elif candidates == 0:
        lines.append("Maçlar analiz edildi ama oran eşleşmesi yapılamadı "
                     "(bahis şirketleri henüz oran yayınlamamış olabilir).")
    else:
        lines.append(f"Oran karşılaştırılan seçenek: {candidates}")
        if best_ev is not None:
            lines.append(f"En yüksek EV: %{best_ev * 100:.1f} (eşik: %{EV_THRESHOLD * 100:.0f})")
            if best_ev < EV_THRESHOLD:
                lines.append("Yani maçlar tarandı, ama hiçbirinde eşiği aşan "
                             "matematiksel avantaj yoktu — sinyal üretmemek DOĞRU davranış.")

    lines.append(f"\nBekleyen sinyal: {s['pending']} | Toplam sinyal: {s['total']}")
    send_football_message("\n".join(lines))
    _save_daily_state({"summary_sent_date": today})
    return True


def run_results_update():
    return update_results()


# ============================================================
# 11. ÇALIŞTIRICI — 2026-09-04 EKLENDİ
# ============================================================
# Bu dosyada model, değer motoru, sonuç takibi, komutlar - hepsi
# VARDI ama onları periyodik çağıracak ANA DÖNGÜ yoktu. Eskiden
# stock_screener_bot.py `import football_bot as fb` deyip çağırıyordu;
# o dosya kaldırılınca bot çalışamaz hale geldi (fonksiyonlar duruyor
# ama kimse tetiklemiyor).
# Aşağısı o eksik parçayı tamamlıyor: kendi başına çalışabilen,
# Render'da ayakta kalan bir servis.

import traceback
from flask import Flask

_fb_app = Flask(__name__)
_FB_PORT = int(os.environ.get("PORT", "10000"))
FB_SURUM = "football-bot-v3-TIMED-durumu-duzeltmesi-2026-09-05"


@_fb_app.route("/health")
def _fb_health():
    return "OK (football bot)", 200


@_fb_app.route("/")
def _fb_ana():
    try:
        s = compute_stats()
        gecmis = get_recent_history(15)
        h = [f"<h2>SPO-QUANT Futbol Botu</h2><p>{FB_SURUM}</p>",
             f"<p>Toplam sinyal: {s['total']} | Bekleyen: {s['pending']} | "
             f"Kazanan: {s.get('won', 0)} | Kaybeden: {s.get('lost', 0)}</p>",
             "<h3>Son sinyaller</h3><pre>"]
        for r in gecmis:
            h.append(str(r))
        h.append("</pre>")
        return "\n".join(h)
    except Exception as e:
        return f"<pre>{e}</pre>"


def _fb_dis_ping():
    """Render ücretsiz katmanı hareketsizlikte uyutuyor. Loopback ping
    İŞE YARAMIYOR (Render dış trafiğe bakıyor) - bu yüzden dış adrese."""
    harici = (os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
              or os.environ.get("HARICI_URL", "").rstrip("/"))
    if not harici:
        print("[Futbol ping] UYARI: RENDER_EXTERNAL_URL/HARICI_URL yok - "
              "servis uyuyabilir!", flush=True)
    time.sleep(30)
    while True:
        try:
            if harici:
                requests.get(f"{harici}/health", timeout=20)
        except Exception:
            pass
        time.sleep(600)


def _fb_komut_dongusu():
    """Telegram komutlarını dinler (/stats /rapor /status).
    Ayrı thread - tarama donsa bile komutlar çalışmaya devam eder."""
    while True:
        try:
            poll_and_respond()
        except Exception as e:
            print(f"[Futbol komut] Hata: {e}", flush=True)
        time.sleep(5)


def _fb_ana_dongu():
    """Model taraması, oran taraması, sonuç güncelleme ve günlük özet.
    Her biri AYRI try/except - biri hata verse diğerleri etkilenmez."""
    son_model = son_oran = son_sonuc = 0.0
    model_ara = FOOTBALL_MODEL_SCAN_INTERVAL_MINUTES * 60
    oran_ara = FOOTBALL_ODDS_SCAN_INTERVAL_MINUTES * 60
    sonuc_ara = 3600      # saatte bir sonuc kontrolu

    while True:
        simdi = time.time()
        if simdi - son_model >= model_ara:
            son_model = simdi
            try:
                n = run_model_scan()
                print(f"[Futbol] Model taraması bitti: {n}", flush=True)
            except Exception as e:
                print(f"[Futbol] Model tarama hatası: {e}", flush=True)
                traceback.print_exc()
        if simdi - son_oran >= oran_ara:
            son_oran = simdi
            try:
                n = run_odds_scan()
                print(f"[Futbol] Oran taraması bitti: {n}", flush=True)
            except Exception as e:
                print(f"[Futbol] Oran tarama hatası: {e}", flush=True)
                traceback.print_exc()
        if simdi - son_sonuc >= sonuc_ara:
            son_sonuc = simdi
            try:
                run_results_update()
            except Exception as e:
                print(f"[Futbol] Sonuç güncelleme hatası: {e}", flush=True)
        try:
            maybe_send_daily_summary()
        except Exception as e:
            print(f"[Futbol] Günlük özet hatası: {e}", flush=True)
        time.sleep(60)


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] football_bot.py — {FB_SURUM}", flush=True)
    eksikler = validate_football_config()
    if eksikler:
        print(f"[UYARI] Eksik ayar: {eksikler}", flush=True)
    try:
        rapor = self_check_football()
        print(f"[Öz kontrol] {rapor}", flush=True)
    except Exception as e:
        rapor = f"öz kontrol hatası: {e}"
        print(f"[Öz kontrol] {e}", flush=True)

    send_football_message(
        f"⚽ SPO-QUANT FUTBOL BOTU başlatıldı — {FB_SURUM}\n\n"
        f"Bu bot, maçları Poisson modeliyle analiz edip bahis oranlarıyla "
        f"karşılaştırıyor ve matematiksel avantajı (değer) olan maçları "
        f"bildiriyor.\n\n"
        f"📊 Takip edilen ligler: {', '.join(TRACKED_COMPETITIONS)} + Süper Lig\n"
        f"🎯 Değer eşiği: EV ≥ %{EV_THRESHOLD*100:.0f} | "
        f"Kelly çarpanı: {KELLY_FRACTION}\n"
        f"⏱️ Model taraması: {FOOTBALL_MODEL_SCAN_INTERVAL_MINUTES} dk | "
        f"Oran taraması: {FOOTBALL_ODDS_SCAN_INTERVAL_MINUTES} dk\n"
        f"🔁 Kendi kendine ping: 10 dk (Render uyumasın diye)\n\n"
        f"Komutlar: /stats  /rapor  /status\n\n"
        f"{('⚠️ Eksik ayar: ' + str(eksikler)) if eksikler else '✅ Ayarlar tam'}\n"
        f"Öz kontrol: {rapor}"
    )

    threading.Thread(target=_fb_ana_dongu, daemon=True).start()
    print("[BAŞLANGIÇ] Ana tarama döngüsü başlatıldı.", flush=True)
    threading.Thread(target=_fb_komut_dongusu, daemon=True).start()
    print("[BAŞLANGIÇ] Komut dinleme thread'i başlatıldı.", flush=True)
    threading.Thread(target=_fb_dis_ping, daemon=True).start()
    print("[BAŞLANGIÇ] Dış ping thread'i başlatıldı.", flush=True)
    _fb_app.run(host="0.0.0.0", port=_FB_PORT)
