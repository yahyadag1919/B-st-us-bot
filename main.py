"""
main.py — Render Web Service başlatıcısı
=========================================
Tek işi var: `stock_screener_bot.py`'yi çalıştırmak ve Render'ın istediği
web sunucusunu ayakta tutmak.

ÖNEMLİ — NEDEN TEK BOT BAŞLATIYORUZ:
Futbol botu (`football_bot.py`) AYRI olarak başlatılmaz. `stock_screener_bot.py`
onu zaten import edip kendi döngüsüne ekliyor (model taraması, oran taraması,
sonuç güncelleme). Burada ayrıca başlatmak aynı botu İKİ KEZ çalıştırır:
çift sinyal, çift Telegram mesajı ve Odds API kotasının iki katı tüketimi.

Render Web Service bir HTTP portu dinlemek zorunda olduğu için Flask ana
thread'de çalışır, bot ise arka planda bir thread'de.
"""

import os
import time
import runpy
import threading
import traceback

import requests
from flask import Flask

BOT_FILE = os.environ.get("BOT_FILE", "stock_screener_bot.py")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

RESTART_DELAY_SECONDS = int(os.environ.get("RESTART_DELAY_SECONDS", "60"))
MAX_CONSECUTIVE_CRASHES = int(os.environ.get("MAX_CONSECUTIVE_CRASHES", "5"))
# Render ücretsiz plan 15 dk hareketsizlikte uyuduğu için bunun altında olmalı
SELF_PING_MINUTES = int(os.environ.get("SELF_PING_MINUTES", "10"))

_state = {"durum": "başlatılıyor", "detay": "", "zaman": time.time()}
_lock = threading.Lock()


def notify(text: str):
    print(text, flush=True)
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
    except Exception as e:
        print(f"Telegram bildirimi gönderilemedi: {e}", flush=True)


def set_state(durum: str, detay: str = ""):
    with _lock:
        _state.update({"durum": durum, "detay": detay, "zaman": time.time()})


def run_bot():
    """Botu çalıştırır; çökerse yeniden başlatır."""
    if not os.path.isfile(BOT_FILE):
        set_state("eksik", f"{BOT_FILE} bulunamadı")
        notify(f"⚠️ '{BOT_FILE}' repoda yok — bot başlatılamadı.")
        return

    crashes = 0
    while True:
        set_state("çalışıyor")
        try:
            runpy.run_path(BOT_FILE, run_name="__main__")
            set_state("durdu", "beklenmedik şekilde sonlandı")
            notify(f"⚠️ Bot beklenmedik şekilde sonlandı. "
                   f"{RESTART_DELAY_SECONDS} sn sonra yeniden başlatılıyor.")
        except SystemExit as e:
            # Botun kendi öz-kontrolü bilinçli olarak durdurdu (kod bütünlüğü
            # hatası). Yeniden başlatmak aynı sonucu verir, o yüzden duruyoruz.
            set_state("durduruldu", f"SystemExit({e.code})")
            notify("🛑 Bot kendini güvenli şekilde durdurdu (kod bütünlüğü hatası).\n"
                   "Kodu düzeltip yeniden deploy et.")
            return
        except Exception as e:
            crashes += 1
            set_state("çöktü", f"{type(e).__name__}: {e}")
            notify(f"🚨 Bot ÇÖKTÜ ({crashes}/{MAX_CONSECUTIVE_CRASHES}):\n"
                   f"{type(e).__name__}: {e}\n"
                   f"{RESTART_DELAY_SECONDS} sn sonra yeniden denenecek.")
            traceback.print_exc()
            if crashes >= MAX_CONSECUTIVE_CRASHES:
                set_state("bırakıldı", "çok fazla ardışık çökme")
                notify(f"🛑 Bot arka arkaya {crashes} kez çöktü — bırakılıyor.")
                return
        time.sleep(RESTART_DELAY_SECONDS)


def keep_awake():
    """Render ücretsiz Web Service'i dışarıdan 15 dk istek almazsa uyur.
    Bot kendi genel adresine düzenli istek atarak uyanık kalır — böylece
    UptimeRobot gibi ayrı bir servise gerek kalmıyor."""
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        print("RENDER_EXTERNAL_URL yok — kendi kendine ping devre dışı.", flush=True)
        return
    target = url.rstrip("/") + "/health"
    time.sleep(60)
    while True:
        try:
            requests.get(target, timeout=20)
        except Exception as e:
            print(f"Kendi kendine ping başarısız: {e}", flush=True)
        time.sleep(SELF_PING_MINUTES * 60)


app = Flask(__name__)


@app.route("/health")
def health():
    with _lock:
        s = dict(_state)
    return (f"ok\nbot: {s['durum']}"
            + (f" ({s['detay']})" if s["detay"] else "")
            + f" — {int(time.time() - s['zaman'])} sn önce"), 200


@app.route("/")
def root():
    return "Bot servisi çalışıyor. Durum için /health", 200


def main():
    threading.Thread(target=run_bot, name="bot", daemon=True).start()
    threading.Thread(target=keep_awake, name="keep-awake", daemon=True).start()
    notify("🚀 Render servisi başlatıldı.\n"
           f"Çalışan dosya: {BOT_FILE} (futbol botu bunun içinde)\n"
           f"💤 Kendi kendine ping: {SELF_PING_MINUTES} dk'da bir")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))


if __name__ == "__main__":
    main()
