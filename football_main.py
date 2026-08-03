"""
football_main.py
Ayristirilmis frekans mimarisi (Gemini'nin onayladigi tasarim):

1) run_model_scan() - SIK calisir (varsayilan 10 dk). Fikstur + takim
   gecmisi + Poisson modelini calistirir, sonuclari DATA_DIR'a cache'ler.
   football-data.org bol kotali (10 istek/dk) oldugu icin sik calismasi
   sorun degil. Sueper Lig icin API-Football kullanilir - o da kendi
   6 saatlik takim-cache'ine sahip (bkz. football_data_fetcher_apifootball.py)
   oldugu icin 10 dk'da bir cagrilsa da gunluk 100 istek kotasini asmaz.

2) run_odds_scan() - SEYREK calisir (varsayilan 240 dk = 4 saat). En son
   model cache'ini okur, SADECE bu adimda The Odds API'den oran ceker,
   esler, EV/Kelly hesaplar, Telegram'a bildirir. Boylece Odds API'nin
   aylik 500 istek kotasi korunur.

Bu iki fonksiyon stock_screener_bot.py'nin run_forever() dongusune
BAGIMSIZ iki zaman araligiyla eklenir.
"""

import os
import json
from datetime import datetime, timedelta, timezone

import football_config as fcfg
import football_data_fetcher as fdf
import football_data_fetcher_apifootball as fdf_apif
import football_odds_fetcher as fof
import football_quant_engine as fqe
import football_value_engine as fve
import football_telegram_notifier as ftn


# football-data.org / api-football lig kodu -> The Odds API sport_key eslemesi.
# NOT: BU EŞLEME DOĞRULANMADI. İlk deploy sonrası
# football_odds_fetcher.list_soccer_sport_keys() ile gerçek kodlar
# teyit edilip gerekirse düzeltilecek. "TR1_SUPERLIG" -> "soccer_turkey_super_league"
# tahmini bir kod, The Odds API'nin sports listesinde "Soccer: Turkey Super League"
# olarak goruldu ama tam key string'i dogrulanmadi.
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

REQUIRED_FOOTBALL_FUNCTIONS = ["run_model_scan", "run_odds_scan"]

MODEL_CACHE_FILENAME = "football_model_cache.json"


def self_check_football():
    """
    Açılışta futbol modülünün sağlıklı olduğunu doğrular. Sorun varsa
    Telegram'a haber verir. Sağlıklıysa True döner.
    """
    missing_env = fcfg.validate_football_config()
    if missing_env:
        ftn.send_football_message(
            "🚨 [FUTBOL BOT BAŞLATMA HATASI] Eksik env variable(lar): "
            + ", ".join(missing_env)
        )
        return False

    for func_name in REQUIRED_FOOTBALL_FUNCTIONS:
        if func_name not in globals():
            ftn.send_football_message(
                f"🚨 [FUTBOL BOT BAŞLATMA HATASI] Beklenen fonksiyon eksik: {func_name}"
            )
            return False

    return True


def _model_cache_path():
    return os.path.join(fcfg.DATA_DIR, MODEL_CACHE_FILENAME)


def _save_model_cache(entries):
    try:
        with open(_model_cache_path(), "w", encoding="utf-8") as f:
            json.dump({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "entries": entries,
            }, f)
    except OSError as e:
        print(f"football_main: model cache kaydedilemedi ({e})")


def _load_model_cache():
    path = _model_cache_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _fetch_all_fixtures(date_from, date_to):
    """
    football-data.org (9 lig) + API-Football (sadece Süper Lig) fikstürlerini
    birleştirip tek bir liste olarak döner. Hatalar "_error" ile işaretlenir,
    sessizce yutulmaz.
    """
    fixtures = fdf.get_fixtures(date_from, date_to)

    try:
        superlig_fixtures = fdf_apif.get_fixtures(date_from, date_to)
        fixtures.extend(superlig_fixtures)
    except fdf_apif.ApiFootballError as e:
        fixtures.append({"_error": True, "competition": "TR1_SUPERLIG", "message": str(e)})

    return fixtures


def _get_team_history(fixture, team_role):
    """
    team_role: 'home' veya 'away'. Fikstürün hangi kaynaktan geldiğine göre
    (competition == 'TR1_SUPERLIG' ise API-Football, değilse football-data.org)
    doğru fetcher'ı seçer.
    """
    team_id = fixture[f"{team_role}_team_id"]
    if fixture["competition"] == "TR1_SUPERLIG":
        return fdf_apif.get_team_recent_matches(team_id)
    return fdf.get_team_recent_matches(team_id)


def run_model_scan(days_ahead=3):
    """
    SIK çalışan tur: fikstür + takım geçmişi + Poisson modeli. Oran
    ÇEKMEZ, Telegram'a bildirim ATMAZ — sadece sonucu cache'e yazar.
    run_odds_scan() bu cache'i okuyup value bet hesaplar.

    Döner: {"fixtures_checked", "model_computed", "errors"}
    """
    errors = []
    today = datetime.now(timezone.utc).date()
    date_from = today.isoformat()
    date_to = (today + timedelta(days=days_ahead)).isoformat()

    fixtures = _fetch_all_fixtures(date_from, date_to)
    scheduled_fixtures = [
        f for f in fixtures if not f.get("_error") and f.get("status") == "SCHEDULED"
    ]
    for f in fixtures:
        if f.get("_error"):
            errors.append(f"{f['competition']}: {f['message']}")

    model_entries = []
    for fixture in scheduled_fixtures:
        try:
            home_hist = _get_team_history(fixture, "home")
            away_hist = _get_team_history(fixture, "away")
        except (fdf.DataFetchError, fdf_apif.ApiFootballError) as e:
            errors.append(
                f"team_history({fixture['home_team']} vs {fixture['away_team']}): {e}"
            )
            continue

        quant_result = fqe.analyze_match(
            home_hist, away_hist, fixture["home_team_id"], fixture["away_team_id"]
        )
        if quant_result is None:
            continue  # yeterli veri yok — tahmin üretilmedi

        model_entries.append({"fixture": fixture, "quant_result": quant_result})

    _save_model_cache(model_entries)

    return {
        "fixtures_checked": len(scheduled_fixtures),
        "model_computed": len(model_entries),
        "errors": errors,
    }


def run_odds_scan():
    """
    SEYREK çalışan tur: run_model_scan()'ın cache'lediği model sonuçlarını
    okur, The Odds API'den oran çeker, eşler, EV/Kelly hesaplar, value bet
    varsa Telegram'a bildirir.

    Döner: {"model_entries_used", "signals_found", "signals_sent", "errors"}
    """
    errors = []
    cache = _load_model_cache()
    if cache is None:
        return {"model_entries_used": 0, "signals_found": 0, "signals_sent": 0,
                "errors": ["model cache henüz oluşmadı — run_model_scan hiç çalışmamış olabilir"]}

    model_entries = cache.get("entries", [])
    all_signals = []

    # Oranları lig başına bir kez çekiyoruz (kota tasarrufu için).
    odds_cache = {}
    competitions_in_scope = {e["fixture"]["competition"] for e in model_entries}
    for comp in competitions_in_scope:
        sport_key = ODDS_SPORT_KEYS.get(comp)
        if not sport_key:
            continue
        try:
            odds_data = fof.get_odds(sport_key)
            odds_cache[comp] = odds_data["matches"]
        except fof.OddsFetchError as e:
            errors.append(f"odds({comp}): {e}")
            odds_cache[comp] = []

    for entry in model_entries:
        fixture = entry["fixture"]
        quant_result = entry["quant_result"]
        odds_events = odds_cache.get(fixture["competition"], [])
        if not odds_events:
            continue

        matched_event = fve.match_fixture_to_odds_event(fixture, odds_events)
        if matched_event is None:
            continue

        signals = fve.find_value_bets(fixture, quant_result, matched_event)
        all_signals.extend(signals)

    sent = ftn.notify_value_bets(all_signals)

    return {
        "model_entries_used": len(model_entries),
        "signals_found": len(all_signals),
        "signals_sent": sent,
        "errors": errors,
    }
