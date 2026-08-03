"""
football_main.py
Tum katmanlari birlestiren orkestrasyon: fikstur cek -> takim gecmisi cek
-> Poisson modeli calistir -> oranlari cek -> esle -> value bet bul ->
Telegram'a bildir.

scan_football() stock_screener_bot.py'nin run_forever() donguesune
eklenecek fonksiyondur (bkz. entegrasyon notu asagida).
"""

from datetime import datetime, timedelta, timezone

import football_config as fcfg
import football_data_fetcher as fdf
import football_odds_fetcher as fof
import football_quant_engine as fqe
import football_value_engine as fve
import football_telegram_notifier as ftn


# football-data.org lig kodu -> The Odds API sport_key eslemesi.
# NOT: BU EŞLEME DOĞRULANMADI. İlk deploy sonrası
# football_odds_fetcher.list_soccer_sport_keys() ile gerçek kodlar
# teyit edilip gerekirse düzeltilecek.
ODDS_SPORT_KEYS = {
    "PL": "soccer_epl",
    "PD": "soccer_spain_la_liga",
    "SA": "soccer_italy_serie_a",
    "BL1": "soccer_germany_bundesliga",
    "FL1": "soccer_france_ligue_one",
    "CL": "soccer_uefa_champs_league",
}

REQUIRED_FOOTBALL_FUNCTIONS = ["scan_football"]


def self_check_football():
    """
    Açılışta futbol modülünün sağlıklı olduğunu doğrular:
    - zorunlu env variable'lar dolu mu
    - bu dosyadaki fonksiyonlar var mı (ileride bir düzenleme yanlışlıkla
      bir fonksiyonu silerse açılışta fark edilsin)
    Sorun varsa Telegram'a haber verir. Sağlıklıysa True döner.
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


def scan_football(days_ahead=3):
    """
    Tek bir tarama döngüsü: önümüzdeki days_ahead gün içindeki maçları
    tarar, value bet varsa Telegram'a bildirir.

    Döner: {"fixtures_checked", "signals_found", "signals_sent", "errors"}
    Hata durumunda çağıran taraf (run_forever) bunu loglayıp/Telegram'a
    düşürebilir — bu fonksiyon kendi içinde sessiz kalmaz, "errors"
    listesinde toplar.
    """
    errors = []
    today = datetime.now(timezone.utc).date()
    date_from = today.isoformat()
    date_to = (today + timedelta(days=days_ahead)).isoformat()

    fixtures = fdf.get_fixtures(date_from, date_to)
    scheduled_fixtures = [
        f for f in fixtures if not f.get("_error") and f.get("status") == "SCHEDULED"
    ]
    for f in fixtures:
        if f.get("_error"):
            errors.append(f"{f['competition']}: {f['message']}")

    all_signals = []

    # Oranları lig başına bir kez çekiyoruz (kota tasarrufu için).
    odds_cache = {}
    competitions_in_scope = {f["competition"] for f in scheduled_fixtures}
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

    for fixture in scheduled_fixtures:
        odds_events = odds_cache.get(fixture["competition"], [])
        if not odds_events:
            continue

        matched_event = fve.match_fixture_to_odds_event(fixture, odds_events)
        if matched_event is None:
            continue

        try:
            home_hist = fdf.get_team_recent_matches(fixture["home_team_id"])
            away_hist = fdf.get_team_recent_matches(fixture["away_team_id"])
        except fdf.DataFetchError as e:
            errors.append(
                f"team_history({fixture['home_team']} vs {fixture['away_team']}): {e}"
            )
            continue

        quant_result = fqe.analyze_match(
            home_hist, away_hist, fixture["home_team_id"], fixture["away_team_id"]
        )
        if quant_result is None:
            continue  # yeterli veri yok — tahmin üretilmedi, doğru olan bu

        signals = fve.find_value_bets(fixture, quant_result, matched_event)
        all_signals.extend(signals)

    sent = ftn.notify_value_bets(all_signals)

    return {
        "fixtures_checked": len(scheduled_fixtures),
        "signals_found": len(all_signals),
        "signals_sent": sent,
        "errors": errors,
    }
