"""
ABD REDDIT TAKİBİ (abd_reddit_takip.py) — 2026-10-27
=====================================================
Reddit'te (r/wallstreetbets, r/stocks, r/options, r/investing ... ) hangi
ABD hissesinin ne kadar konuşulduğunu takip eder.

Veri kaynağı: ApeWisdom (apewisdom.io) — Reddit gruplarını tarayıp hisse
başına "son 24 saatte kaç kez bahsedildi" sayısını hazır veriyor.
Anahtar/üyelik gerekmiyor.

NOT: StockTwits'teki gibi "yükselir/düşer" düğmesi Reddit'te yok. Bu modül
şimdilik sadece KONUŞULMA MİKTARINI takip ediyor (ilgi patlaması).
Yön tahmini (kelime analizi) için Reddit'in kendi API anahtarı gerekiyor —
ileride ikinci adım olarak eklenebilir.

Ne gönderir (ABD Telegram sohbeti, TELEGRAM_TOKEN / TELEGRAM_CHAT_ID):
  1) 🔥 İlgi patlaması alarmı: bir hisse son 24 saatte bir önceki 24 saate
     göre ≥3 kat (ve en az 40 kez) konuşulursa, ya da sıralamada ilk 20'ye
     dışarıdan (50+'dan) fırlarsa. Aynı hisse için günde en fazla 1 alarm.
  2) 📋 Sabah listesi: hafta içi NY 08:50'de (piyasa açılmadan önce)
     en çok konuşulan 15 hisse.
  3) /reddit komutu: listeyi istediğin an gönderir.
     (Komut abd_backtest.py'nin Telegram döngüsünden yönlendirilir —
      aynı token'ı iki döngü dinlerse 409 hatası olur.)
"""
import os
import json
import time
import threading

import requests
import pandas as pd

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")

REDDIT_SURUM = "abd-reddit-v1-2026-10-27"
NY = "America/New_York"

API_URL = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/{sayfa}"
SAYFA_SAYISI = 2              # sayfa başı 100 hisse → ilk 200
KONTROL_ARALIGI_DK = 30       # ApeWisdom verisi zaten yavaş güncelleniyor
PATLAMA_KAT = 3.0             # son 24s / önceki 24s
PATLAMA_MIN_BAHSEDILME = 40   # az konuşulan hisselerde 2→8 gibi sahte patlamaları ele
SIRA_ZIRVE = 20               # ilk 20'ye ...
SIRA_ONCEKI_MIN = 50          # ... 50. sıradan daha aşağıdan gelirse
ALARM_SAAT_NY = (4, 20)       # gece yarısı alarm atmasın (NY 04:00-20:00)
SABAH_LISTESI_NY = (8, 50)
LISTE_UZUNLUK = 15
ENDEKS_FONLARI = {"SPY": "S&P 500", "QQQ": "Nasdaq 100"}

_oturum = requests.Session()
_oturum.headers.update({"User-Agent": "Mozilla/5.0 (yahya-ozel-bot)"})
_durum = {"tur": 0, "son_hata": "", "son_basarili": ""}


# =============================================================================
# TELEGRAM
# =============================================================================
def mesaj_gonder(metin):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Reddit devre dışı] {metin}", flush=True)
        return
    try:
        _oturum.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                     json={"chat_id": TELEGRAM_CHAT_ID, "text": metin[:4000],
                           "disable_web_page_preview": True}, timeout=20)
    except Exception as e:
        print(f"[Reddit Telegram] {e}", flush=True)


# =============================================================================
# VERİ
# =============================================================================
def _sayi(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def veri_cek():
    """[{ticker, ad, sira, bahsedilme, onceki_sira, onceki_bahsedilme, oy}] — hata olursa None."""
    sonuc = []
    for sayfa in range(1, SAYFA_SAYISI + 1):
        son_hata = None
        for deneme in range(3):
            try:
                r = _oturum.get(API_URL.format(sayfa=sayfa), timeout=20)
                if r.status_code == 200:
                    veri = r.json()
                    break
                son_hata = f"HTTP {r.status_code}"
            except Exception as e:
                son_hata = str(e)[:150]
            time.sleep(3 * (deneme + 1))
        else:
            _durum["son_hata"] = son_hata or "bilinmeyen"
            print(f"[Reddit] sayfa {sayfa} alınamadı: {son_hata}", flush=True)
            return sonuc or None
        for s in veri.get("results", []) or []:
            t = (s.get("ticker") or "").upper().strip()
            if not t:
                continue
            sonuc.append({
                "ticker": t,
                "ad": (s.get("name") or "").strip(),
                "sira": _sayi(s.get("rank")),
                "bahsedilme": _sayi(s.get("mentions")) or 0,
                "onceki_sira": _sayi(s.get("rank_24h_ago")),
                "onceki_bahsedilme": _sayi(s.get("mentions_24h_ago")) or 0,
                "oy": _sayi(s.get("upvotes")) or 0,
            })
        if int(veri.get("pages") or 1) <= sayfa:
            break
    return sonuc


def patlama_nedeni(h):
    """Patlama varsa açıklama metni, yoksa None."""
    b, ob = h["bahsedilme"], h["onceki_bahsedilme"]
    if b >= PATLAMA_MIN_BAHSEDILME and b >= PATLAMA_KAT * max(ob, 1):
        kat = b / max(ob, 1)
        return f"konuşulma {ob} → {b} (×{kat:.1f})"
    s, os_ = h["sira"], h["onceki_sira"]
    if s is not None and s <= SIRA_ZIRVE and (os_ is None or os_ > SIRA_ONCEKI_MIN):
        onceki = f"{os_}." if os_ else "listede yoktu"
        return f"sıralama {onceki} → {s}."
    return None


# =============================================================================
# MESAJLAR
# =============================================================================
def _degisim(h):
    ob = h["onceki_bahsedilme"]
    if ob <= 0:
        return "yeni"
    d = (h["bahsedilme"] - ob) / ob * 100
    return f"{d:+.0f}%"


def liste_metni(veri, baslik="📋 Reddit'te en çok konuşulan ABD hisseleri (son 24 saat)"):
    if not veri:
        return "❌ Reddit verisi alınamadı" + (f": {_durum['son_hata']}" if _durum["son_hata"] else ".")
    satirlar = [baslik, ""]
    for h in sorted(veri, key=lambda x: x["sira"] or 9999)[:LISTE_UZUNLUK]:
        isaret = " 🔥" if patlama_nedeni(h) else ""
        satirlar.append(f"{h['sira']}. {h['ticker']} — {h['bahsedilme']} kez "
                        f"(düne göre {_degisim(h)}){isaret}")
    # Endeks fonları her zaman ayrıca gösterilsin (ilk 15'te olmasalar bile)
    endeks = {h["ticker"]: h for h in veri if h["ticker"] in ENDEKS_FONLARI}
    satirlar += ["", "📊 Endeksler:"]
    for t, ad in ENDEKS_FONLARI.items():
        h = endeks.get(t)
        if h:
            satirlar.append(f"• {t} ({ad}) — {h['sira']}. sırada, {h['bahsedilme']} kez "
                            f"(düne göre {_degisim(h)})")
        else:
            satirlar.append(f"• {t} ({ad}) — ilk {SAYFA_SAYISI * 100}'de yok")
    satirlar +=["", "🔥 = son 24 saatte ilgi patladı",
                 "Not: Bu sadece ne kadar konuşulduğunu gösterir, yükselir/düşer demez."]
    return "\n".join(satirlar)


def alarm_metni(h, neden):
    ad = f" ({h['ad']})" if h["ad"] else ""
    return (f"🔥 REDDIT İLGİ PATLAMASI — {h['ticker']}{ad}\n\n"
            f"• {neden}\n"
            f"• Şu anki sıra: {h['sira']}. | Son 24 saat: {h['bahsedilme']} bahsedilme\n\n"
            "Not: Çok konuşulması yükseleceği anlamına gelmez — haber/grafik ile birlikte bak.\n"
            f"https://www.tradingview.com/chart/?symbol={h['ticker']}")


def liste_gonder():
    mesaj_gonder(liste_metni(veri_cek()))


# =============================================================================
# DURUM DOSYASI (Render yeniden başlayınca aynı alarm tekrar gitmesin)
# =============================================================================
def _durum_yolu():
    return os.path.join(DATA_DIR, "reddit_durum.json")


def _durum_yukle():
    try:
        with open(_durum_yolu(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"gun": "", "alarmlar": [], "liste_gunu": ""}


def _durum_kaydet(d):
    try:
        with open(_durum_yolu(), "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception as e:
        print(f"[Reddit] durum kaydedilemedi: {e}", flush=True)


# =============================================================================
# DÖNGÜ
# =============================================================================
def reddit_dongusu():
    d = _durum_yukle()
    ilk_tur = True
    while True:
        try:
            simdi = pd.Timestamp.now(tz=NY)
            gun = simdi.strftime("%Y-%m-%d")
            if d.get("gun") != gun:
                d = {"gun": gun, "alarmlar": [], "liste_gunu": d.get("liste_gunu", "")}

            veri = veri_cek()
            _durum["tur"] += 1
            if veri:
                _durum["son_basarili"] = simdi.strftime("%H:%M NY")
                _durum["son_hata"] = ""

                # İlk turda (deploy sonrası) mevcut patlamaları alarm olarak
                # atma, sadece "görüldü" say — yoksa her deploy'da mesaj yağar.
                alarm_saati = ALARM_SAAT_NY[0] <= simdi.hour < ALARM_SAAT_NY[1]
                for h in veri:
                    neden = patlama_nedeni(h)
                    if not neden or h["ticker"] in d["alarmlar"]:
                        continue
                    d["alarmlar"].append(h["ticker"])
                    if not ilk_tur and alarm_saati:
                        mesaj_gonder(alarm_metni(h, neden))
                        time.sleep(1)

                # Sabah listesi (hafta içi NY 08:50'den sonra, günde bir kez)
                if (simdi.weekday() < 5 and d.get("liste_gunu") != gun
                        and (simdi.hour, simdi.minute) >= SABAH_LISTESI_NY
                        and simdi.hour < 10):
                    mesaj_gonder(liste_metni(veri, "☀️ Açılış öncesi — Reddit'te en çok "
                                                   "konuşulan ABD hisseleri (son 24 saat)"))
                    d["liste_gunu"] = gun
                _durum_kaydet(d)
            ilk_tur = False
        except Exception as e:
            _durum["son_hata"] = str(e)[:150]
            print(f"[Reddit] döngü hatası: {e}", flush=True)

        # Sabah listesini kaçırmamak için 08:30-09:00 arası 5 dakikada bir bak
        simdi = pd.Timestamp.now(tz=NY)
        bekle = 5 if (simdi.hour == 8 and simdi.minute >= 30) else KONTROL_ARALIGI_DK
        time.sleep(bekle * 60)


def baslangic():
    veri = veri_cek()
    if veri:
        durum = f"✅ Bağlantı çalışıyor — {len(veri)} hisse okundu."
    else:
        durum = f"⚠️ Şu an veri alınamadı ({_durum['son_hata'] or 'bilinmeyen'}). Tekrar denenecek."
    mesaj_gonder(
        f"👽 Reddit Takibi AKTİF — {REDDIT_SURUM}\n\n"
        f"{durum}\n\n"
        "• 🔥 Bir hisse Reddit'te birden çok konuşulmaya başlarsa haber veririm.\n"
        "• ☀️ Hafta içi piyasa açılmadan önce en çok konuşulan 15 hisse.\n"
        "• /reddit — listeyi şimdi gönder")
