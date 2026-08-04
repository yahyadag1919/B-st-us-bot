"""
main.py — ÇOK BOTLU BAŞLATICI (Render ücretsiz Web Service için)
================================================================
İki botu tek serviste, ayrı thread'lerde çalıştırır:
  1. BIST/ABD sinyal botu  (stock_screener_bot.py)
  2. Futbol analiz botu    (FOOTBALL_BOT_FILE env değişkeni ile belirlenir)

Kripto botu bu sistemden TAMAMEN çıkarıldı (2026-08-04 kararı).

NEDEN THREAD, SUBPROCESS DEĞİL:
Render ücretsiz katmanı ~0.5 GB RAM veriyor. Ayrı süreçler (subprocess)
her bot için ayrı bir Python yorumlayıcısı + ayrı pandas/numpy kopyası
yükler ve bu sınırı büyük ihtimalle aşar. Thread'lerde pandas/numpy bir
kez yüklenir, botlar aynı belleği paylaşır.
Bunun bedeli: botlar birbirini bellek/CPU olarak etkileyebilir. Bu yüzden
her bot kendi gözetmen döngüsünde çalışır ve biri çökerse diğeri devam eder.

ÖNEMLİ - UYKU SORUNU:
Render'ın ücretsiz Web Service'i, DIŞARIDAN 15 dakika istek almazsa uyur.
Aşağıdaki /health endpoint'i tek başına bunu ENGELLEMEZ; dışarıdan düzenli
ping atan bir servis (UptimeRobot, cron-job.org vb.) kurmak gerekir.
Endpoint bu ping'in hedefidir, çözümün kendisi değil.

ÖNEMLİ - KALICI DEPOLAMA:
Render ücretsiz katmanında kalıcı disk YOK. DATA_DIR ayarlansa bile
dosyalar her deploy/yeniden başlatmada silinir. BIST botu takibe aldığı
her sinyali ayrıca Telegram'a logladığı için kayıt tamamen kaybolmaz,
ama takip zinciri sıfırlanır. Bunu bilerek kabul ediyoruz.
"""

import os
import sys
import time
import runpy
import threading
import traceback

import requests
from flask import Flask

# ------------------------------------------------------------
# Ayarlar
# ------------------------------------------------------------
STOCK_BOT_FILE = os.environ.get("STOCK_BOT_FILE", "stock_screener_bot.py")
FOOTBALL_BOT_FILE = os.environ.get("FOOTBALL_BOT_FILE", "football_bot.py")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Çöken bir bot bu kadar bekleyip yeniden başlar
RESTART_DELAY_SECONDS = int(os.environ.get("RESTART_DELAY_SECONDS", "60"))
# Arka arkaya bu kadar çökerse o bot bırakılır (sonsuz çökme döngüsü olmasın)
MAX_CONSECUTIVE_CRASHES = int(os.environ.get("MAX_CONSECUTIVE_CRASHES", "5"))
# Kendi kendine ping aralığı (Render 15 dk'da uyuttuğu için altında olmalı)
SELF_PING_MINUTES = int(os.environ.get("SELF_PING_MINUTES", "10"))

# Sağlık endpoint'inin gösterdiği durum
_status = {}
_status_lock = threading.Lock()


def notify(text: str):
    """main.py'nin kendi bildirimleri. Botların kendi Telegram fonksiyonları
    ayrı çalışır; bu sadece başlatıcı seviyesindeki olaylar içindir."""
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


def set_status(name: str, state: str, detail: str = ""):
    with _status_lock:
        _status[name] = {"state": state, "detail": detail, "updated": time.time()}


# ------------------------------------------------------------
# Bot gözetmeni
# ------------------------------------------------------------
def run_bot(name: str, filename: str):
    """Bir botu kendi thread'inde çalıştırır, çökerse yeniden başlatır."""
    if not os.path.isfile(filename):
        set_status(name, "eksik", f"{filename} bulunamadı")
        notify(f"⚠️ {name}: '{filename}' dosyası repoda yok — bu bot başlatılmadı.\n"
               f"Dosyayı yükleyip yeniden deploy et (ya da env değişkeniyle doğru adı ver).")
        return

    crashes = 0
    while True:
        set_status(name, "çalışıyor")
        try:
            # run_name="__main__" → botun kendi `if __name__ == "__main__"`
            # bloğu çalışır, yani dosyayı hiç değiştirmeden kullanabiliyoruz.
            runpy.run_path(filename, run_name="__main__")
            # Buraya düşmesi = bot kendi kendine bitti (normalde sonsuz döngü)
            set_status(name, "durdu", "beklenmedik şekilde sonlandı")
            notify(f"⚠️ {name} beklenmedik şekilde sonlandı. "
                   f"{RESTART_DELAY_SECONDS} sn sonra yeniden başlatılıyor.")
        except SystemExit as e:
            # Botun kendi öz-kontrolü (_self_check) bilinçli olarak durdurdu.
            # Bu bir kod bütünlüğü hatasıdır; yeniden başlatmak aynı sonucu
            # verir, o yüzden bu botu bırakıyoruz.
            set_status(name, "durduruldu", f"SystemExit({e.code})")
            notify(f"🛑 {name} kendini güvenli şekilde durdurdu (kod bütünlüğü hatası).\n"
                   f"Yeniden başlatılmayacak — kodu düzeltip yeniden deploy et.")
            return
        except Exception as e:
            crashes += 1
            set_status(name, "çöktü", f"{type(e).__name__}: {e}")
            notify(f"🚨 {name} ÇÖKTÜ ({crashes}/{MAX_CONSECUTIVE_CRASHES}):\n"
                   f"{type(e).__name__}: {e}\n"
                   f"{RESTART_DELAY_SECONDS} sn sonra yeniden denenecek.")
            traceback.print_exc()
            if crashes >= MAX_CONSECUTIVE_CRASHES:
                set_status(name, "bırakıldı", "çok fazla ardışık çökme")
                notify(f"🛑 {name} arka arkaya {crashes} kez çöktü — bırakılıyor.\n"
                       f"Diğer bot çalışmaya devam ediyor.")
                return
        time.sleep(RESTART_DELAY_SECONDS)


# ------------------------------------------------------------
# Sağlık endpoint'i (dış ping'in hedefi)
# ------------------------------------------------------------
app = Flask(__name__)


@app.route("/health")
def health():
    with _status_lock:
        snapshot = dict(_status)
    lines = ["ok"]
    now = time.time()
    for name, s in snapshot.items():
        age = int(now - s["updated"])
        lines.append(f"{name}: {s['state']}"
                     + (f" ({s['detail']})" if s["detail"] else "")
                     + f" — {age} sn önce")
    return "\n".join(lines), 200


@app.route("/")
def root():
    return "Bot servisi çalışıyor. Durum için /health", 200


def keep_awake():
    """Render'ın ücretsiz Web Service'i 15 dk boyunca DIŞARIDAN istek almazsa
    uyur. Bunu önlemek için normalde UptimeRobot gibi bir dış servis kurmak
    gerekir; onun yerine bot kendi genel adresine düzenli istek atıyor.
    İstek dışarıdan (Render'ın yönlendiricisi üzerinden) geldiği için
    'etkinlik' sayılır ve servis uyanık kalır.
    Not: Servis bir kez uyursa süreç durduğu için bu thread de durur — yani
    bu bir 'uyumayı önleme' mekanizmasıdır, 'uyandırma' değil. Uyanıkken
    çalıştığı sürece uyumaya hiç sıra gelmez."""
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        print("RENDER_EXTERNAL_URL yok — kendi kendine ping devre dışı "
              "(yerel çalıştırmada normaldir).", flush=True)
        return
    target = url.rstrip("/") + "/health"
    time.sleep(60)  # servis tam ayağa kalksın
    while True:
        try:
            requests.get(target, timeout=20)
        except Exception as e:
            print(f"Kendi kendine ping başarısız: {e}", flush=True)
        time.sleep(SELF_PING_MINUTES * 60)


def main():
    bots = [
        ("BIST/ABD botu", STOCK_BOT_FILE),
        ("Futbol botu", FOOTBALL_BOT_FILE),
    ]
    started = []
    for name, filename in bots:
        t = threading.Thread(target=run_bot, args=(name, filename),
                             name=name, daemon=True)
        t.start()
        started.append(name)
        # Botların açılış işlemleri (ticker doğrulama, API çağrıları) aynı anda
        # başlayıp birbirini hız limitine sokmasın diye araya biraz boşluk.
        time.sleep(3)

    threading.Thread(target=keep_awake, name="keep-awake", daemon=True).start()

    notify("🚀 Render servisi başlatıldı.\n"
           f"Çalıştırılan botlar: {', '.join(started)}\n"
           "Sağlık kontrolü: /health\n"
           f"💤 Uykuya karşı kendi kendine ping: {SELF_PING_MINUTES} dk'da bir.")

    # Flask ana thread'de çalışır ve Render'ın beklediği PORT'u dinler.
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
