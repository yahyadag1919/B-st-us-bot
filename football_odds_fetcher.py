"""
football_odds_fetcher.py
The Odds API uzerinden mac oranlarini (1X2 / Alt-Ust) ceker.
Dokumantasyon: https://the-odds-api.com/liveapi/guides/v4/
"""

import requests

import football_config as fcfg

BASE_URL = "https://api.the-odds-api.com/v4"


class OddsFetchError(Exception):
    pass


def list_soccer_sport_keys():
    """
    The Odds API'nin destekledigi futbol lig kodlarini listeler
    (orn. 'soccer_epl', 'soccer_germany_bundesliga').
    Bunu bir kez calistirip lig kodlarini football_config.py'ye sabitlemek
    en verimlisi; her seferinde cekmek gereksiz kota harcar.
    """
    if not fcfg.ODDS_API_KEY:
        raise OddsFetchError("ODDS_API_KEY tanimli degil (env variable eksik).")

    resp = requests.get(
        f"{BASE_URL}/sports", params={"apiKey": fcfg.ODDS_API_KEY}, timeout=15
    )
    if resp.status_code != 200:
        raise OddsFetchError(f"Sport list istegi basarisiz: HTTP {resp.status_code}")

    return [s["key"] for s in resp.json() if s.get("group") == "Soccer"]


def get_odds(sport_key, regions="eu", markets="h2h"):
    """
    Belirtilen lig icin guncel oranlari doner.
    sport_key: spesifik lig kodu, orn. 'soccer_epl' (genel 'soccer' anahtari
               cogu planda calismaz - spesifik kod kullanmak gerekiyor).
    regions: 'eu', 'uk', 'us', 'au' (bookmaker bolgesi)
    markets: 'h2h' (1X2) veya 'totals' (alt/ust)
    """
    if not fcfg.ODDS_API_KEY:
        raise OddsFetchError("ODDS_API_KEY tanimli degil (env variable eksik).")

    url = f"{BASE_URL}/sports/{sport_key}/odds"
    params = {
        "apiKey": fcfg.ODDS_API_KEY,
        "regions": regions,
        "markets": markets,
        "oddsFormat": "decimal",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
    except requests.RequestException as e:
        raise OddsFetchError(f"Baglanti hatasi: {e}")

    if resp.status_code != 200:
        raise OddsFetchError(
            f"Odds istegi basarisiz: HTTP {resp.status_code} - {resp.text[:200]}"
        )

    # Kalan/kullanilan kota bilgisi header'da geliyor - /stats komutunda
    # kullaniciya "kac istegin kaldi" gostermek icin kullanilacak.
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

    return {
        "matches": matches,
        "quota_remaining": quota_remaining,
        "quota_used": quota_used,
    }
