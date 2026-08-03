"""
football_data_fetcher_apifootball.py
API-Football (dashboard.api-football.com üzerinden alınan dogrudan key,
RapidAPI degil) uzerinden SADECE Süper Lig icin fikstur ve takim gecmisi
ceker. football-data.org'un ucretsiz planinda Süper Lig olmadigi icin
ikinci bir kaynak olarak eklendi.

ONEMLI - GUNLUK KOTA: Bu kaynagin ucretsiz plani GUNDE 100 istek.
Bu yuzden:
- Lig ID'si bir kere bulunup DATA_DIR'a cache'lenir (tekrar tekrar
  aranmaz - /leagues cagrisi kota harcar).
- Bu modul football_main.py tarafindan football-data.org kadar sik
  cagirilmamali (Gemini'nin onerdigi "ayristirilmis frekans" mimarisine
  gore, gunde birkaç kez).
"""

import os
import json
import time
from datetime import datetime, timezone

import requests

import football_config as fcfg

BASE_URL = "https://v3.football.api-sports.io"
LEAGUE_CACHE_FILENAME = "api_football_superlig_league_id.json"
LEAGUE_NAME_HINT = "Süper Lig"
COUNTRY_HINT = "Turkey"


class ApiFootballError(Exception):
    pass


def _headers():
    if not fcfg.API_FOOTBALL_KEY:
        raise ApiFootballError("API_FOOTBALL_KEY tanimli degil (env variable eksik).")
    # dashboard.api-football.com'dan alinan dogrudan key icin dogru header budur
    # (RapidAPI uzerinden alinsaydi X-RapidAPI-Key / X-RapidAPI-Host kullanilirdi).
    return {"x-apisports-key": fcfg.API_FOOTBALL_KEY}


def _get(endpoint, params=None):
    url = f"{BASE_URL}{endpoint}"
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=15)
    except requests.RequestException as e:
        raise ApiFootballError(f"Bağlantı hatası ({endpoint}): {e}")

    if resp.status_code != 200:
        raise ApiFootballError(f"{endpoint} isteği başarısız: HTTP {resp.status_code} — {resp.text[:200]}")

    data = resp.json()
    # API-Football hata mesajlarini "errors" alaninda dondurur, HTTP 200 olsa bile.
    if data.get("errors"):
        raise ApiFootballError(f"{endpoint} API hatası: {data['errors']}")

    return data


def _cache_path():
    return os.path.join(fcfg.DATA_DIR, LEAGUE_CACHE_FILENAME)


def _load_cached_league_id():
    path = _cache_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("league_id")
    except (json.JSONDecodeError, OSError):
        return None


def _save_cached_league_id(league_id):
    try:
        with open(_cache_path(), "w", encoding="utf-8") as f:
            json.dump({"league_id": league_id, "cached_at": datetime.now(timezone.utc).isoformat()}, f)
    except OSError as e:
        print(f"football_data_fetcher_apifootball: lig ID cache'lenemedi ({e})")


def get_superlig_league_id(force_refresh=False):
    """
    Süper Lig'in API-Football'daki lig ID'sini bulur. Sonucu DATA_DIR'a
    cache'ler - kotayı korumak için bir kere bulunduktan sonra tekrar
    /leagues çağrısı yapılmaz (force_refresh=True verilmedikçe).

    Doner: int (lig ID) veya None (bulunamadıysa - bu durumda çağıran
    taraf Süper Lig taramasını atlamalı, ID'yi UYDURMAMALI).
    """
    if not force_refresh:
        cached = _load_cached_league_id()
        if cached is not None:
            return cached

    data = _get("/leagues", params={"country": COUNTRY_HINT})
    for entry in data.get("response", []):
        league_name = entry.get("league", {}).get("name", "")
        if LEAGUE_NAME_HINT.lower() in league_name.lower():
            league_id = entry.get("league", {}).get("id")
            _save_cached_league_id(league_id)
            return league_id

    return None  # bulunamadı - uydurma bir ID dönmüyoruz


def _current_season_year():
    """
    Avrupa/Türkiye sezon konvansiyonu: sezon, başladığı yılın adıyla anılır
    (örn. Ağustos 2026'da başlayan sezon -> 2026). Temmuz ayı öncesi bir
    önceki yılın sezonu hâlâ sürüyor kabul edilir (kaba bir sezon geçiş
    varsayımı — API'nin kendi "current" bayrağı daha güvenilir olabilir,
    bu yüzden get_fixtures çağıran taraf gerekirse season parametresini
    elle de verebilir).
    """
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


def get_fixtures(date_from, date_to, season=None):
    """
    Süper Lig'in belirli tarih aralığındaki maçlarını döner.
    date_from / date_to: 'YYYY-MM-DD' formatında string.

    Döner: fikstür dict'lerinden oluşan liste (football_data_fetcher.py
    ile aynı alan isimleriyle - football_main.py ikisini birleştirebilsin).
    Lig ID bulunamazsa boş liste döner (hata fırlatmaz, ama loglar).
    """
    league_id = get_superlig_league_id()
    if league_id is None:
        print("football_data_fetcher_apifootball: Süper Lig ID'si bulunamadı, tarama atlandı.")
        return []

    season = season if season is not None else _current_season_year()
    data = _get("/fixtures", params={
        "league": league_id,
        "season": season,
        "from": date_from,
        "to": date_to,
    })

    fixtures = []
    for item in data.get("response", []):
        fixture_info = item.get("fixture", {})
        teams = item.get("teams", {})
        status_short = fixture_info.get("status", {}).get("short", "")
        # API-Football'da "NS" (Not Started) football-data.org'un "SCHEDULED"'ına denk gelir
        status = "SCHEDULED" if status_short == "NS" else status_short

        fixtures.append({
            "_error": False,
            "competition": "TR1_SUPERLIG",  # football-data.org kodlarıyla çakışmasın diye özel kod
            "fixture_id": fixture_info.get("id"),
            "utc_date": fixture_info.get("date"),
            "status": status,
            "home_team": teams.get("home", {}).get("name"),
            "away_team": teams.get("away", {}).get("name"),
            "home_team_id": teams.get("home", {}).get("id"),
            "away_team_id": teams.get("away", {}).get("id"),
        })

    return fixtures


TEAM_CACHE_FILENAME = "api_football_team_matches_cache.json"
TEAM_CACHE_TTL_HOURS = 6  # bu sureden taze bir cache varsa API'ye gidilmez - 100/gun kotasini korur


def _team_cache_path():
    return os.path.join(fcfg.DATA_DIR, TEAM_CACHE_FILENAME)


def _load_team_cache():
    path = _team_cache_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_team_cache(cache):
    try:
        with open(_team_cache_path(), "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError as e:
        print(f"football_data_fetcher_apifootball: takım cache'i kaydedilemedi ({e})")


def get_team_recent_matches(team_id, last=10):
    """
    Bir takımın son N biten maçını döner (form/Poisson gücü hesabı için) -
    football_data_fetcher.get_team_recent_matches ile aynı alan isimleri.

    GUNLUK KOTA KORUMASI: sonuç DATA_DIR'a cache'lenir, TEAM_CACHE_TTL_HOURS
    içinde tekrar istenirse API'ye gidilmez, cache'ten döner. Bu, sık
    tarama (örn. 10 dk) yapılsa bile 100/gün kotasının hızla tükenmesini
    önler.
    """
    cache = _load_team_cache()
    key = str(team_id)
    cached_entry = cache.get(key)

    if cached_entry:
        cached_at = datetime.fromisoformat(cached_entry["cached_at"])
        age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
        if age_hours < TEAM_CACHE_TTL_HOURS:
            return cached_entry["matches"]

    data = _get("/fixtures", params={"team": team_id, "last": last, "status": "FT"})

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
    _save_team_cache(cache)
    return matches
