"""
football_data_fetcher.py
football-data.org uzerinden fikstur ve takim mac gecmisi ceker.
Dokumantasyon: https://www.football-data.org/documentation/quickstart

NOT: Bu API'nin ucretsiz planinda gercek xG verisi YOK.
xG/form hesabi golden turetilecek (football_quant_engine.py, Adim 3).
"""

import time
import requests

import football_config as fcfg

BASE_URL = "https://api.football-data.org/v4"


class DataFetchError(Exception):
    """Fikstur/takim verisi cekilirken olusan hatalari sarmalar."""
    pass


def _headers():
    if not fcfg.FOOTBALL_DATA_KEY:
        raise DataFetchError("FOOTBALL_DATA_KEY tanimli degil (env variable eksik).")
    return {"X-Auth-Token": fcfg.FOOTBALL_DATA_KEY}


def _get(endpoint, params=None, max_retries=2):
    """
    Tek bir GET istegi atar. Dakikalik kotaya (429) takilirsa
    kisa bekleme sonrasi tekrar dener.
    """
    url = f"{BASE_URL}{endpoint}"
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=15)
        except requests.RequestException as e:
            raise DataFetchError(f"Baglanti hatasi ({endpoint}): {e}")

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 429:
            if attempt < max_retries:
                time.sleep(20)
                continue
            raise DataFetchError("Rate limit asildi (429), tekrar denemeler tukendi.")

        raise DataFetchError(
            f"{endpoint} istegi basarisiz: HTTP {resp.status_code} - {resp.text[:200]}"
        )

    raise DataFetchError(f"{endpoint} istegi tum denemelerden sonra basarisiz oldu.")


def list_available_competitions():
    """
    Hesabimizin gercekte erisebildigi ligleri doner (kod + isim + plan).
    Deploy sonrasi bir kere calistirip TRACKED_COMPETITIONS'i buna gore
    dogrulamak/guncellemek icin kullanilacak.
    """
    data = _get("/competitions")
    return [
        {"code": c.get("code"), "name": c.get("name"), "plan": c.get("plan")}
        for c in data.get("competitions", [])
    ]


def get_fixtures(date_from, date_to, competitions=None):
    """
    Belirli tarih araligindaki maclari doner.
    date_from / date_to: 'YYYY-MM-DD' formatinda string.
    competitions: None ise fcfg.TRACKED_COMPETITIONS kullanilir.

    Donen liste elemanlari:
      - basariliysa fikstur bilgisi (_error: False)
      - o lig basarisiz olduysa hata bilgisi (_error: True) - boylece
        bir ligin erisim/kota hatasi digerlerini engellemez, ama cagiran
        taraf hatayi gorup loglayabilir / Telegram'a dusurebilir.
    """
    comp_list = competitions if competitions is not None else fcfg.TRACKED_COMPETITIONS
    all_matches = []

    for comp in comp_list:
        try:
            data = _get(
                f"/competitions/{comp}/matches",
                params={"dateFrom": date_from, "dateTo": date_to},
            )
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

        # 10 istek/dakika sinirina takilmamak icin ligler arasinda kucuk bekleme
        time.sleep(1.5)

    return all_matches


def get_team_recent_matches(team_id, limit=10):
    """
    Bir takimin son N biten macini doner (form/Poisson gucu hesabi icin).
    """
    data = _get(
        f"/teams/{team_id}/matches",
        params={"status": "FINISHED", "limit": limit},
    )
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
    return matches
