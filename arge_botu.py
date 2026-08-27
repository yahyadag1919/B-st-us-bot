"""
arge_botu.py — BIST TAVAN YAKLAŞANLARI TARAYICISI
==================================================
2026-08-19 — Kullanıcının kararıyla eski Ar-Ge botu (8000+ satır, onlarca
araştırma komutu, otomatik AI hipotez üretimi) TAMAMEN KALDIRILDI ve
yerine bu tarayıcı kondu. Eski hali arge_botu_ESKI_YEDEK.py'de ve
GitHub geçmişinde duruyor.

NE YAPIYOR — TEK İŞ:
BIST kapanışına yakın saatlerde (16:00-18:10 TR), tavana doğru hızla
ilerleyen ama HENÜZ TAVAN OLMAMIŞ hisseleri bulup bildirim gönderir.
Strateji iddiası YOK, tahmin YOK - sadece bir tarayıcı.

NEDEN BU EŞİKLER:
Bugünkü BIST araştırması şunu gösterdi: TAM TAVAN yapanlar ertesi gün
ort. +%2.42 yukarı açıyor (%81 ihtimalle), ama %8-9.5'te kapananlar
sadece +%0.39 (%48 ihtimalle - yazı tura). Yani değerli olan şey TAVAN
KİLİDİNİN KENDİSİ. Bu tarayıcı, tavan olmadan ÖNCE fark etmeni sağlıyor
ki karar verecek vaktin olsun.

DÜRÜST SINIR — VERİ GECİKMESİ:
yfinance BIST verisi ~15 dakika gecikmeli. Yani gördüğün fiyat 15 dakika
öncesinin. Kapanışa 70+ dakika varken bu genelde sorun değil ama son
15-20 dakikada körleşiyorsun. Gerçek zamanlı veri istersen Algolab
(Deniz Yatırım) hesabı gerekiyor - o zaman SADECE veri çekme kısmı
değişir, geri kalan aynı kalır.

Telegram komutları: /durum, /tara (elle tarama), /liste
"""
import os
import time
import threading
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

ARGE_KOD_SURUMU = "tavan-tarayici-v1-2026-08-19"

TELEGRAM_TOKEN = os.environ.get("ARGE_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("ARGE_TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")

# --- TARAMA AYARLARI ---
ALT_ESIK_PCT = 7.0          # bu getirinin uzerindekiler ilgi alanina girer
UST_ESIK_PCT = 9.49         # bunun ustu zaten TAVAN, gec kalmis olurduk
TEKRAR_BILDIRIM_ARTIS = 0.5 # tekrar bildirim icin en az bu kadar yukselmeli
TARAMA_ARALIGI_SANIYE = 300 # 5 dakika
PENCERE_BASLANGIC = 16 * 60      # 16:00 TR
PENCERE_BITIS = 18 * 60 + 15     # 18:15 TR

BIST_HISSELER = [
    "THYAO.IS", "GARAN.IS", "AKBNK.IS", "ISCTR.IS", "YKBNK.IS", "VAKBN.IS",
    "HALKB.IS", "SISE.IS", "EREGL.IS", "KRDMD.IS", "TUPRS.IS", "PETKM.IS",
    "ASELS.IS", "TCELL.IS", "TTKOM.IS", "BIMAS.IS", "MGROS.IS", "SOKM.IS",
    "FROTO.IS", "TOASO.IS", "ARCLK.IS", "VESTL.IS", "TAVHL.IS", "PGSUS.IS",
    "KCHOL.IS", "SAHOL.IS", "DOHOL.IS", "ALARK.IS", "ENKAI.IS", "TKFEN.IS",
    "KOZAL.IS", "KOZAA.IS", "IPEKE.IS", "ODAS.IS", "ZOREN.IS", "AKSEN.IS",
    "EKGYO.IS", "ISGYO.IS", "TRGYO.IS", "HEKTS.IS", "SASA.IS", "GUBRF.IS",
    "AEFES.IS", "ULKER.IS", "CCOLA.IS", "TATGD.IS", "BANVT.IS", "PENTA.IS",
    "SMRTG.IS", "ALFAS.IS", "ASTOR.IS", "EUPWR.IS", "CWENE.IS", "GESAN.IS",
    "KONTR.IS", "ISDMR.IS", "CIMSA.IS", "AKCNS.IS", "OYAKC.IS", "BRSAN.IS",
    "MAVI.IS", "BRYAT.IS", "AGHOL.IS", "KARSN.IS", "OTKAR.IS", "KLSER.IS",
    "EGEEN.IS", "ALTNY.IS", "REEDR.IS", "IZINV.IS", "MIATK.IS", "FORTE.IS",
]

_ARGE_AVAILABLE = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
if not _ARGE_AVAILABLE:
    print("[TARAYICI] ARGE_TELEGRAM_TOKEN/CHAT_ID tanımlı değil - "
          "tarayıcı devre dışı (ana sistemi etkilemez).", flush=True)

_bugun_bildirilen = {}   # {ticker: en_son_bildirilen_getiri}
_bugun_tarih = None
_son_tarama_ozeti = {"zaman": None, "bulunan": 0, "taranan": 0, "hata": None}
_son_update_id = None
_gunluk_kayitlar = []


def send_telegram_message(text: str):
    if not _ARGE_AVAILABLE:
        print(f"[TARAYICI-Telegram kapalı] {text}", flush=True)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[TARAYICI-Telegram hata] {e}", flush=True)


def send_telegram_document(dosya_yolu: str, caption: str = ""):
    if not _ARGE_AVAILABLE:
        print(f"[TARAYICI-Telegram kapalı] {dosya_yolu}", flush=True)
        return
    try:
        with open(dosya_yolu, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                          data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]},
                          files={"document": f}, timeout=60)
    except Exception as e:
        print(f"[TARAYICI-Telegram dosya hatası] {e}", flush=True)


def _tr_dakika():
    """Turkiye saatiyle gunun kacinci dakikasi (UTC+3)."""
    u = datetime.now(timezone.utc)
    return ((u.hour + 3) % 24) * 60 + u.minute


def pencere_icinde_mi():
    u = datetime.now(timezone.utc)
    if u.weekday() >= 5:
        return False
    return PENCERE_BASLANGIC <= _tr_dakika() <= PENCERE_BITIS


def _toplu_veri_cek(sert_sure=90):
    """TUM hisseleri TEK istekte ceker - yfinance'in coklu-ticker
    ozelligiyle. Hisse basina ayri istek atmak gun boyu 'Too Many
    Requests' hatalarina yol acmisti; bu yontem o riski kokten cozuyor."""
    import concurrent.futures
    import yfinance as yf

    def _cek():
        return yf.download(tickers=" ".join(BIST_HISSELER), period="5d",
                           interval="15m", group_by="ticker", progress=False,
                           threads=False, auto_adjust=False)

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(_cek).result(timeout=sert_sure)
    except concurrent.futures.TimeoutError:
        print("[TARAYICI] Toplu veri çekimi SERT zaman aşımına uğradı.", flush=True)
        return None
    except Exception as e:
        print(f"[TARAYICI] Veri hatası: {e}", flush=True)
        return None
    finally:
        ex.shutdown(wait=False)


def _hisse_durumu(veri, ticker):
    """Bir hissenin bugunku getirisini ve hacim oranini hesaplar.
    Doner: dict ya da None."""
    try:
        df = veri[ticker] if ticker in veri.columns.get_level_values(0) else None
        if df is None or df.empty:
            return None
        df = df.dropna(subset=["Close"])
        if len(df) < 10:
            return None
        idx = pd.to_datetime(df.index)
        try:
            idx = idx.tz_localize(None)
        except Exception:
            idx = idx.tz_convert(None)
        df = df.copy()
        df["gun"] = idx.date

        gunler = sorted(set(df["gun"]))
        if len(gunler) < 2:
            return None
        bugun, dun = gunler[-1], gunler[-2]
        bugun_barlar = df[df["gun"] == bugun]
        dun_barlar = df[df["gun"] == dun]
        if bugun_barlar.empty or dun_barlar.empty:
            return None

        onceki_kapanis = float(dun_barlar["Close"].iloc[-1])
        son_fiyat = float(bugun_barlar["Close"].iloc[-1])
        if onceki_kapanis <= 0:
            return None
        getiri = (son_fiyat - onceki_kapanis) / onceki_kapanis * 100

        bugun_hacim = float(bugun_barlar["Volume"].sum())
        gecmis_hacimler = [float(df[df["gun"] == g]["Volume"].sum()) for g in gunler[:-1]]
        gecmis_hacimler = [h for h in gecmis_hacimler if h > 0]
        hacim_orani = (bugun_hacim / np.mean(gecmis_hacimler)) if gecmis_hacimler else None

        # son 1 saatte (4 bar) ne kadar hizlandi
        son4 = bugun_barlar["Close"].tail(5)
        hiz = ((son4.iloc[-1] - son4.iloc[0]) / son4.iloc[0] * 100) if len(son4) >= 2 and son4.iloc[0] > 0 else None

        return {"ticker": ticker.replace(".IS", ""), "fiyat": round(son_fiyat, 2),
                "getiri_pct": round(getiri, 2),
                "tavana_kalan_pct": round(10.0 - getiri, 2),
                "hacim_orani": round(hacim_orani, 2) if hacim_orani else None,
                "son1saat_pct": round(hiz, 2) if hiz is not None else None,
                "son_bar_saati": bugun_barlar.index[-1].strftime("%H:%M")}
    except Exception:
        return None


def taramayi_calistir(elle=False):
    """Bir tarama turu. elle=True ise pencere kontrolu atlanir."""
    global _bugun_bildirilen, _bugun_tarih, _son_tarama_ozeti, _gunluk_kayitlar

    bugun = datetime.now(timezone.utc).date()
    if _bugun_tarih != bugun:
        _bugun_tarih = bugun
        _bugun_bildirilen = {}
        _gunluk_kayitlar = []

    veri = _toplu_veri_cek()
    if veri is None or veri.empty:
        _son_tarama_ozeti = {"zaman": datetime.now().strftime("%H:%M:%S"),
                              "bulunan": 0, "taranan": 0, "hata": "veri alınamadı"}
        return []

    bulunanlar, taranan = [], 0
    for ticker in BIST_HISSELER:
        d = _hisse_durumu(veri, ticker)
        if d is None:
            continue
        taranan += 1
        if ALT_ESIK_PCT <= d["getiri_pct"] <= UST_ESIK_PCT:
            bulunanlar.append(d)
        elif d["getiri_pct"] > UST_ESIK_PCT:
            d["tavan_oldu"] = True
            bulunanlar.append(d)

    _son_tarama_ozeti = {"zaman": datetime.now().strftime("%H:%M:%S"),
                          "bulunan": len(bulunanlar), "taranan": taranan, "hata": None}

    yeni_bildirimler = []
    for d in sorted(bulunanlar, key=lambda x: -x["getiri_pct"]):
        onceki = _bugun_bildirilen.get(d["ticker"])
        if onceki is not None and d["getiri_pct"] < onceki + TEKRAR_BILDIRIM_ARTIS:
            continue  # zaten bildirdik, kayda deger yukselis yok
        _bugun_bildirilen[d["ticker"]] = d["getiri_pct"]
        yeni_bildirimler.append(d)
        _gunluk_kayitlar.append({**d, "bildirim_saati": datetime.now().strftime("%H:%M")})

    if yeni_bildirimler:
        satirlar = [f"🔺 TAVANA YAKLAŞANLAR ({len(yeni_bildirimler)} hisse) "
                    f"— veri saati ~{yeni_bildirimler[0].get('son_bar_saati','?')}"]
        for d in yeni_bildirimler:
            if d.get("tavan_oldu"):
                satirlar.append(f"\n🔒 {d['ticker']}: %{d['getiri_pct']} — TAVAN OLDU "
                                f"(fiyat {d['fiyat']})")
            else:
                satirlar.append(f"\n📈 {d['ticker']}: %{d['getiri_pct']} "
                                f"(tavana %{d['tavana_kalan_pct']} kaldı)")
            satirlar.append(f"   Fiyat: {d['fiyat']}" +
                            (f" | Hacim: {d['hacim_orani']}x ort." if d.get("hacim_orani") else "") +
                            (f" | Son 1sa: %{d['son1saat_pct']}" if d.get("son1saat_pct") is not None else ""))
        satirlar.append("\n⏰ Veri ~15 dk gecikmeli olabilir - karar verirken hesaba kat.")
        send_telegram_message("\n".join(satirlar))
    elif elle:
        send_telegram_message(f"🔍 Tarama bitti: {taranan} hisse tarandı, "
                               f"{ALT_ESIK_PCT}-{UST_ESIK_PCT}% aralığında yeni hisse yok.")
    return yeni_bildirimler


def maybe_run_scan():
    """Ana bot bunu dongude cagirir - kendi kendini zamanlar."""
    if not _ARGE_AVAILABLE:
        return
    if not pencere_icinde_mi():
        return
    global _son_calisma
    simdi = time.time()
    if simdi - globals().get("_son_calisma", 0) < TARAMA_ARALIGI_SANIYE:
        return
    globals()["_son_calisma"] = simdi
    try:
        taramayi_calistir()
    except Exception as e:
        print(f"[TARAYICI] Tarama hatası: {e}", flush=True)


def send_startup_message():
    if not _ARGE_AVAILABLE:
        return
    send_telegram_message(
        f"🔺 BIST TAVAN TARAYICISI başlatıldı — {ARGE_KOD_SURUMU}\n\n"
        f"Eski Ar-Ge botu (araştırma komutları, otomatik AI hipotez üretimi) "
        f"TAMAMEN KALDIRILDI. Bu bot artık TEK İŞ yapıyor:\n\n"
        f"📊 {len(BIST_HISSELER)} BIST hissesi taranıyor\n"
        f"🎯 Aranan: günlük getirisi %{ALT_ESIK_PCT} ile %{UST_ESIK_PCT} arası "
        f"(tavana yaklaşan ama HENÜZ tavan olmamış)\n"
        f"🔒 Tavan olanlar da ayrıca işaretlenip bildiriliyor\n"
        f"⏰ Çalışma penceresi: 16:00-18:15 TR, {TARAMA_ARALIGI_SANIYE//60} dakikada bir\n"
        f"🔁 Aynı hisse için tekrar bildirim: sadece %{TEKRAR_BILDIRIM_ARTIS} "
        f"daha yükselirse (bildirim kirliliği olmasın)\n\n"
        f"⚠️ ÖNEMLİ: Veri ~15 dakika gecikmeli. Kapanışa 70+ dk varken sorun "
        f"değil ama son 15-20 dakikada körleşiyorsun. Gerçek zamanlı veri "
        f"için Algolab hesabı gerekir - o zaman sadece veri çekme kısmı "
        f"değişir.\n\n"
        f"Komutlar: /tara (elle tarama) | /durum | /liste\n\n"
        f"ℹ️ Bu bot strateji ÖNERMİYOR, tahmin YAPMIYOR - sadece tarayıp "
        f"haber veriyor. Karar tamamen sana ait."
    )


def poll_arge_commands():
    """Ana botun komut dongusu bunu cagirir."""
    global _son_update_id
    if not _ARGE_AVAILABLE:
        return
    try:
        params = {"timeout": 5}
        if _son_update_id:
            params["offset"] = _son_update_id + 1
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                         params=params, timeout=15)
        data = r.json()
    except Exception as e:
        print(f"[TARAYICI] Komut alma hatası: {e}", flush=True)
        return

    for u in data.get("result", []):
        _son_update_id = u["update_id"]
        msg = u.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if chat_id != str(TELEGRAM_CHAT_ID) or not text.startswith("/"):
            continue

        if text.startswith("/tara"):
            send_telegram_message("🔍 Elle tarama başlatıldı...")
            threading.Thread(target=lambda: taramayi_calistir(elle=True), daemon=True).start()
        elif text.startswith("/durum"):
            o = _son_tarama_ozeti
            send_telegram_message(
                f"📊 Tarayıcı Durumu — {ARGE_KOD_SURUMU}\n"
                f"Pencere içinde mi: {'✅ evet' if pencere_icinde_mi() else '❌ hayır (16:00-18:15 TR dışı)'}\n"
                f"Son tarama: {o['zaman'] or 'henüz yok'}\n"
                f"Taranan hisse: {o['taranan']} | Bulunan: {o['bulunan']}\n"
                f"Hata: {o['hata'] or 'yok'}\n"
                f"Bugün bildirilen: {len(_bugun_bildirilen)} hisse"
            )
        elif text.startswith("/liste"):
            if not _gunluk_kayitlar:
                send_telegram_message("Bugün henüz bildirim yok.")
                continue
            yol = os.path.join(DATA_DIR, "tavan_tarayici_bugun.csv")
            pd.DataFrame(_gunluk_kayitlar).to_csv(yol, index=False, encoding="utf-8-sig")
            send_telegram_document(yol, caption=f"Bugünkü {len(_gunluk_kayitlar)} bildirim")


if __name__ == "__main__":
    from flask import Flask
    _PORT = int(os.environ.get("PORT", "10000"))
    _app = Flask(__name__)

    @_app.route("/health")
    def _h():
        return "OK (tavan tarayici)", 200

    def _dongu():
        send_startup_message()
        while True:
            try:
                maybe_run_scan()
            except Exception as e:
                print(f"[TARAYICI] Döngü hatası: {e}", flush=True)
            try:
                poll_arge_commands()
            except Exception as e:
                print(f"[TARAYICI] Komut hatası: {e}", flush=True)
            time.sleep(5)

    def _ping():
        time.sleep(30)
        while True:
            try:
                requests.get(f"http://127.0.0.1:{_PORT}/health", timeout=10)
            except Exception:
                pass
            time.sleep(600)

    print(f"[BAŞLANGIÇ] arge_botu.py BAĞIMSIZ modda — {ARGE_KOD_SURUMU}", flush=True)
    threading.Thread(target=_dongu, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    _app.run(host="0.0.0.0", port=_PORT)
