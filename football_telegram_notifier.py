"""
football_telegram_notifier.py
Value bet sinyallerini ayri Telegram botu (mac_analiz_yahya_bot) uzerinden
gonderir. Bildirim seli onlemi: ayni fikstur+sonuc icin
FOOTBALL_NOTIFY_THROTTLE_MINUTES icinde tekrar bildirim atmaz - kripto
botunda yasanan "100+ art arda bildirim" hatasinin ayni cozumu.
"""

import os
import json
import requests
from datetime import datetime, timezone

import football_config as fcfg

_STATE_FILENAME = "football_notified.json"


def _state_path():
    return os.path.join(fcfg.DATA_DIR, _STATE_FILENAME)


def _load_state():
    path = _state_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state):
    try:
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError as e:
        print(f"football_telegram_notifier: state kaydedilemedi ({e})")


def _signal_key(signal):
    return f"{signal['fixture_id']}_{signal['outcome']}"


def should_notify(signal):
    """
    Ayni sinyal (fixture_id + outcome) son FOOTBALL_NOTIFY_THROTTLE_MINUTES
    icinde bildirildiyse False doner.
    """
    state = _load_state()
    last = state.get(_signal_key(signal))
    if last is None:
        return True
    last_dt = datetime.fromisoformat(last)
    minutes_passed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
    return minutes_passed >= fcfg.FOOTBALL_NOTIFY_THROTTLE_MINUTES


def mark_notified(signal):
    state = _load_state()
    state[_signal_key(signal)] = datetime.now(timezone.utc).isoformat()
    _save_state(state)


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
    if not fcfg.FOOTBALL_TELEGRAM_TOKEN or not fcfg.FOOTBALL_TELEGRAM_CHAT_ID:
        print("football_telegram_notifier: token/chat_id eksik, mesaj gönderilemedi")
        return
    url = f"https://api.telegram.org/bot{fcfg.FOOTBALL_TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": fcfg.FOOTBALL_TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, data=payload, timeout=15)
    except Exception as e:
        print(f"football_telegram_notifier: gönderim hatası ({e})")


def notify_value_bets(signals):
    """
    Sinyal listesini dolaşır, throttle'a takılmayanları Telegram'a gönderir
    ve gönderilenleri işaretler. Kaç tanesinin gönderildiğini döner.
    """
    sent_count = 0
    for signal in signals:
        if should_notify(signal):
            send_football_message(format_value_bet_message(signal))
            mark_notified(signal)
            sent_count += 1
    return sent_count
