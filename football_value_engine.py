"""
football_value_engine.py
Poisson modelinin urettigi olasiliklari (football_quant_engine) piyasa
oranlariyla (football_odds_fetcher) karsilastirir; EV (Expected Value)
ve Kelly Kriteri'ne gore "value bet" sinyalleri uretir.

ONEMLI - DURUST NOT: football-data.org ve The Odds API takim isimlerini
FARKLI formatlarda veriyor (orn. "Manchester United FC" vs
"Manchester United"). Bunlari otomatik eslestirmek icin bulanik
(fuzzy) string eslestirme kullaniyoruz - %100 guvenilir degil, yanlis
eslestirme riski var. Bu yuzden dusuk benzerlikte eslesenler
ATLANIYOR (sessizce yanlis maca oran baglamaktansa hic sinyal
uretmemek daha guvenli - gercek parayla ilgili).
"""

import difflib
from datetime import datetime, timezone

import football_config as fcfg

TEAM_NAME_MATCH_THRESHOLD = 0.6   # bunun altinda eslestirme reddedilir
MAX_KICKOFF_DIFF_HOURS = 6        # ayni mac icin kabul edilebilir saat farki

# Takim isimlerinden atilacak yaygin ekler - eslestirme kalitesini artirir
_NAME_NOISE_WORDS = [
    "fc", "cf", "afc", "sk", "sc", "ac", "cd", "club", "football",
    "futbol", "the", "de", "united", "utd",
]


def _normalize_team_name(name):
    """
    Takim ismini karsilastirmaya uygun sade bir forma indirger:
    kucuk harf, noktalama temizligi, yaygin ek kelimeleri cikarma.
    NOT: "united"/"utd" gibi kelimeleri de temizliyoruz cunku bazi
    kaynaklar bunlari isme dahil ederken bazilari etmiyor - bu riskli
    (Manchester United vs Newcastle United ayrimini bozabilir), o yuzden
    sonucta hala tam kelime benzerligine (SequenceMatcher) guveniyoruz,
    bu sadece on temizlik.
    """
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
    """
    Bir football-data.org fikstürünü, The Odds API'den gelen event
    listesiyle eslestirmeye calisir (ev+deplasman isim benzerligi VE
    yakin kickoff saati).

    fixture: football_data_fetcher.get_fixtures() elemani
    odds_events: football_odds_fetcher.get_odds()["matches"]

    Doner: en iyi eslesen event dict'i, ya da None (guvenilir eslesme yoksa).
    """
    fixture_time = _parse_iso(fixture.get("utc_date"))
    best_match = None
    best_score = 0.0

    for event in odds_events:
        event_time = _parse_iso(event.get("commence_time"))
        if fixture_time and event_time:
            hours_diff = abs((fixture_time - event_time).total_seconds()) / 3600
            if hours_diff > MAX_KICKOFF_DIFF_HOURS:
                continue  # farkli gun/mac olma ihtimali yuksek, dene bile

        home_sim = _name_similarity(fixture.get("home_team", ""), event.get("home_team", ""))
        away_sim = _name_similarity(fixture.get("away_team", ""), event.get("away_team", ""))
        combined = (home_sim + away_sim) / 2

        if combined > best_score:
            best_score = combined
            best_match = event

    if best_match is not None and best_score >= TEAM_NAME_MATCH_THRESHOLD:
        return best_match
    return None  # guvenilir eslesme yok - bu mac icin sinyal uretilmeyecek


def compute_ev(probability, decimal_odds):
    """
    EV = (p * oran) - 1
    """
    return (probability * decimal_odds) - 1


def compute_kelly_fraction(probability, decimal_odds, fractional_multiplier=None):
    """
    Kelly Kriteri: f* = (p*(b+1) - 1) / b,  b = decimal_odds - 1
    Tam Kelly agresif/riskli oldugu icin fractional_multiplier ile
    kucultuyoruz (varsayilan: football_config.KELLY_FRACTION, orn. 0.25
    = "Ceyrek Kelly").

    Negatif veya sifir sonuc -> 0.0 (bahis onerilmiyor demek).
    """
    b = decimal_odds - 1
    if b <= 0:
        return 0.0

    raw_kelly = (probability * (b + 1) - 1) / b
    if raw_kelly <= 0:
        return 0.0

    multiplier = fractional_multiplier if fractional_multiplier is not None else fcfg.KELLY_FRACTION
    return raw_kelly * multiplier


def _best_price_for_outcome(bookmakers, outcome_name):
    """
    Birden fazla bahis burosu arasindan bir sonuc (orn. takim adi, 'Draw')
    icin en yuksek orani secer - value bet acisindan en avantajli olan budur.
    """
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
    """
    Bir mac icin 1X2 piyasasinda value bet var mi kontrol eder.

    fixture: football_data_fetcher fikstur dict'i
    quant_result: football_quant_engine.analyze_match() ciktisi
    odds_event: match_fixture_to_odds_event() ile eslesmis event

    Doner: value bet sinyallerinin listesi (bos liste = sinyal yok).
    Her sinyal: outcome, probability, odds, bookmaker, ev, kelly_fraction,
    confidence, low_sample_warning.
    """
    threshold = ev_threshold if ev_threshold is not None else fcfg.EV_THRESHOLD
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
            continue  # bu piyasada oran bulunamadi

        probability = probs[prob_key]
        ev = compute_ev(probability, price)

        if ev < threshold:
            continue  # yeterli edge yok

        kelly = compute_kelly_fraction(probability, price)

        signals.append({
            "fixture_id": fixture.get("fixture_id"),
            "home_team": fixture.get("home_team"),
            "away_team": fixture.get("away_team"),
            "competition": fixture.get("competition"),
            "outcome": prob_key,           # "home_win" / "draw" / "away_win"
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
