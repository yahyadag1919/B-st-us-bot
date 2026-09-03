"""
abd_piyasa_oncesi.py — PİYASA ÖNCESİ HAREKET NEDEN AÇILIŞTA TERSİNE DÖNÜYOR?
==============================================================================
2026-09-02 — Kullanıcının gözlemi:
  "Tesla gibi hisseler, piyasa açılmadan önce güzel yükselmiş oluyor,
   sonra açılışta aniden düşüyor. Ya da tersi: düşmüş oluyor, açılışta
   fırlıyor. Neredeyse her gün, her hissede oluyor. NEDEN?"

ARKA PLAN: ABD'de asıl seans TR saatiyle 16:30-23:00. Ondan ÖNCE
"piyasa öncesi" (pre-market) seansı var: TR ~11:00-16:30. Orada da
işlem oluyor ama çok daha az katılımcıyla.

BU TEST TAM OLARAK "NEDEN"İ ARIYOR. Ölçülen olası sebepler:

  1. HACİM (en güçlü aday): piyasa öncesi hareket AZ hacimle olmuşsa,
     birkaç emirle fiyat oynatılmış demektir - asıl seansta gerçek
     katılımcılar gelince o fiyat reddediliyor olabilir.
  2. HAREKETİN BÜYÜKLÜĞÜ: küçük hareketler mi kalıcı, büyükler mi
     geri dönüyor?
  3. PİYASA BAĞLAMI: SPY (genel piyasa) da aynı yönde mi hareket
     etmiş, yoksa hisse tek başına mı ayrışmış?
  4. YÖN: yukarı hareketler mi daha çok dönüyor, aşağı mı?
  5. SEANS SÜRESİ: hareket erken mi oldu geç mi (son dakikada gelen
     hareket daha güvenilir olabilir - habere yakın)
  6. HİSSE BOYUTU/FİYATI

ÖLÇÜLEN SONUÇLAR (her biri ayrı):
  • Açılış boşluğu: piyasa öncesi fiyat açılışta korunuyor mu
  • Açılıştan sonraki ilk 30 dk
  • Açılıştan kapanışa
  → Böylece "tersine dönüş" tam olarak NEREDE oluyor görülecek

⚠️ VERİ NOTU: piyasa öncesi seansta likidite düşük, veri bazen eksik
olabiliyor. Veri gelmeyen günler atlanıyor, raporda kaç gün
işlendiği belirtiliyor.

Start Command:  python abd_piyasa_oncesi.py
Bu deploy'da SADECE bu analiz çalışır.
"""
import os
import time
import threading

import numpy as np
import pandas as pd
import requests
from flask import Flask
from scipy import stats as _stats

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")
PORT = int(os.environ.get("PORT", "10000"))
KOD_SURUMU = "piyasa-oncesi-v1-2026-09-02"

HAREKET_ESIK = 1.0     # piyasa oncesi +-%1 -> "hareket var" sayilir

US_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AMD", "NFLX",
    "INTC", "QCOM", "MU", "AVGO", "CRM", "ADBE", "ORCL", "CSCO", "IBM",
    "JPM", "BAC", "WFC", "GS", "MS", "C", "V", "MA", "AXP", "PYPL",
    "UNH", "JNJ", "PFE", "MRK", "ABBV", "LLY", "BMY", "GILD", "AMGN",
    "XOM", "CVX", "COP", "SLB", "OXY", "DVN", "HAL", "MRO",
    "WMT", "COST", "TGT", "HD", "LOW", "NKE", "SBUX", "MCD", "DIS",
    "BA", "CAT", "DE", "GE", "MMM", "HON", "LMT", "RTX", "UPS", "FDX",
    "T", "VZ", "CMCSA", "TMUS", "PG", "KO", "PEP", "PM", "MO",
    "PLTR", "SOFI", "COIN", "HOOD", "MSTR", "MARA", "RIOT", "SMCI",
    "LCID", "RIVN", "NIO", "F", "GM", "UBER", "ABNB", "DKNG", "SNAP",
    "PINS", "ROKU", "SHOP", "SQ", "AFRM", "UPST", "CVNA", "GME", "AMC",
    "BABA", "JD", "PDD", "SE", "MELI", "SPY", "QQQ",
]
US_TICKERS = list(dict.fromkeys(US_TICKERS))


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram kapalı] {text}", flush=True)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[Telegram hata] {e}", flush=True)


def send_telegram_document(dosya_yolu: str, caption: str = ""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram kapalı] {dosya_yolu}", flush=True)
        return
    try:
        with open(dosya_yolu, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                          data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]},
                          files={"document": f}, timeout=60)
    except Exception as e:
        print(f"[Telegram dosya hatası] {e}", flush=True)


def _veri_cek(ticker, sert_sure=40):
    """prepost=True -> PIYASA ONCESI ve sonrasi barlari da getirir."""
    import concurrent.futures
    import yfinance as yf

    def _cek():
        return yf.Ticker(ticker).history(period="60d", interval="15m",
                                          prepost=True, timeout=25)

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(_cek).result(timeout=sert_sure)
    except concurrent.futures.TimeoutError:
        print(f"[SERT zaman aşımı] {ticker}", flush=True)
        return None
    except Exception as e:
        print(f"[Veri hatası] {ticker}: {e}", flush=True)
        return None
    finally:
        ex.shutdown(wait=False)


def _hazirla(ham):
    """Barlari New York saatine cevirip seans etiketi ekler."""
    df = ham.rename(columns={"Open": "open", "High": "high", "Low": "low",
                              "Close": "close", "Volume": "volume"})
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    idx = pd.to_datetime(df.index)
    try:
        idx = idx.tz_convert("America/New_York")
    except (TypeError, AttributeError):
        try:
            idx = idx.tz_localize("America/New_York")
        except Exception:
            return None
    df.index = idx
    df["gun"] = df.index.date
    dakika = df.index.hour * 60 + df.index.minute
    # asil seans 09:30-16:00 NY
    df["seans"] = np.where(dakika < 9 * 60 + 30, "ONCE",
                           np.where(dakika < 16 * 60, "ASIL", "SONRA"))
    return df


def calistir():
    # once SPY'i cek - piyasa baglami icin
    spy_harita = {}
    spy_ham = _veri_cek("SPY")
    if spy_ham is not None and not spy_ham.empty:
        s = _hazirla(spy_ham)
        if s is not None:
            gunler = sorted(set(s["gun"]))
            for gi in range(1, len(gunler)):
                dun_asil = s[(s["gun"] == gunler[gi - 1]) & (s["seans"] == "ASIL")]
                bugun_once = s[(s["gun"] == gunler[gi]) & (s["seans"] == "ONCE")]
                if dun_asil.empty or bugun_once.empty:
                    continue
                dk = float(dun_asil.iloc[-1]["close"])
                if dk > 0:
                    spy_harita[gunler[gi]] = (float(bugun_once.iloc[-1]["close"]) - dk) / dk * 100
    print(f"[SPY] {len(spy_harita)} gun piyasa baglami hazir.", flush=True)

    kayitlar = []
    islenen, atlanan = 0, 0
    for n_i, ticker in enumerate(US_TICKERS, 1):
        if ticker == "SPY":
            continue
        print(f"[Piyasa Öncesi {n_i}/{len(US_TICKERS)}] {ticker}...", flush=True)
        ham = _veri_cek(ticker)
        if ham is None or ham.empty or len(ham) < 100:
            atlanan += 1
            time.sleep(0.35)
            continue
        try:
            df = _hazirla(ham)
            if df is None:
                atlanan += 1
                time.sleep(0.35)
                continue
            gunler = sorted(set(df["gun"]))
            # gecmis piyasa oncesi hacim ortalamasi icin
            once_hacimler = []
            for gi in range(1, len(gunler)):
                dun_asil = df[(df["gun"] == gunler[gi - 1]) & (df["seans"] == "ASIL")]
                bugun_once = df[(df["gun"] == gunler[gi]) & (df["seans"] == "ONCE")]
                bugun_asil = df[(df["gun"] == gunler[gi]) & (df["seans"] == "ASIL")]
                if dun_asil.empty or bugun_once.empty or len(bugun_asil) < 3:
                    continue
                dun_kapanis = float(dun_asil.iloc[-1]["close"])
                if dun_kapanis <= 0:
                    continue

                once_son = float(bugun_once.iloc[-1]["close"])
                once_getiri = (once_son - dun_kapanis) / dun_kapanis * 100
                once_hacim = float(bugun_once["volume"].sum())
                once_hacimler.append(once_hacim)
                ort_once_hacim = float(np.mean(once_hacimler[-20:])) if len(once_hacimler) >= 3 else None
                once_hacim_orani = (once_hacim / ort_once_hacim) if ort_once_hacim and ort_once_hacim > 0 else None
                dun_asil_hacim = float(dun_asil["volume"].sum())
                once_asil_orani = (once_hacim / dun_asil_hacim * 100) if dun_asil_hacim > 0 else None
                o_yuksek, o_dusuk = float(bugun_once["high"].max()), float(bugun_once["low"].min())
                once_aralik = ((o_yuksek - o_dusuk) / dun_kapanis * 100) if dun_kapanis > 0 else None
                # hareket erken mi gec mi olustu
                if len(bugun_once) >= 2:
                    ilk_yari = bugun_once.iloc[:len(bugun_once) // 2]
                    ilk_yari_getiri = (float(ilk_yari.iloc[-1]["close"]) - dun_kapanis) / dun_kapanis * 100
                    gec_pay = (once_getiri - ilk_yari_getiri)
                else:
                    gec_pay = None

                acilis = float(bugun_asil.iloc[0]["open"])
                if acilis <= 0:
                    continue
                ilk30_kapanis = float(bugun_asil.iloc[min(1, len(bugun_asil) - 1)]["close"])
                asil_kapanis = float(bugun_asil.iloc[-1]["close"])

                kayitlar.append({
                    "ticker": ticker, "tarih": str(gunler[gi]),
                    # --- PIYASA ONCESI DURUM ---
                    "once_getiri_pct": round(once_getiri, 3),
                    "once_hacim_orani": round(once_hacim_orani, 2) if once_hacim_orani else None,
                    "once_hacim_asilin_yuzdesi": round(once_asil_orani, 2) if once_asil_orani else None,
                    "once_aralik_pct": round(once_aralik, 3) if once_aralik else None,
                    "gec_gelen_hareket": round(gec_pay, 3) if gec_pay is not None else None,
                    "spy_once_pct": round(spy_harita[gunler[gi]], 3) if gunler[gi] in spy_harita else None,
                    "fiyat": round(dun_kapanis, 2),
                    # --- SONUC: hareket korundu mu tersine mi dondu ---
                    "acilis_boslugu_pct": round((acilis - dun_kapanis) / dun_kapanis * 100, 3),
                    "once_korundu_pct": round(((acilis - dun_kapanis) / dun_kapanis * 100) - once_getiri, 3),
                    "acilis_ilk30_pct": round((ilk30_kapanis - acilis) / acilis * 100, 3),
                    "acilis_kapanis_pct": round((asil_kapanis - acilis) / acilis * 100, 3),
                })
            islenen += 1
        except Exception as e:
            print(f"[Piyasa Öncesi] {ticker} hata: {e}", flush=True)
            atlanan += 1
        time.sleep(0.35)

    if not kayitlar:
        return None, "Hiç kayıt üretilemedi (piyasa öncesi verisi alınamamış olabilir)."
    tum = pd.DataFrame(kayitlar)
    dosya = os.path.join(DATA_DIR, "abd_piyasa_oncesi.csv")
    tum.to_csv(dosya, index=False, encoding="utf-8-sig")

    hareketli = tum[tum.once_getiri_pct.abs() >= HAREKET_ESIK].copy()
    yukari = hareketli[hareketli.once_getiri_pct > 0]
    asagi = hareketli[hareketli.once_getiri_pct < 0]

    sonuc = {"islenen": islenen, "atlanan": atlanan, "toplam": len(tum),
             "hareketli": len(hareketli), "yukari": len(yukari), "asagi": len(asagi),
             "genel": {}, "sebepler": [], "hacim_dilimleri": []}

    for ad, veri in [("Piyasa öncesi YUKARI (≥+%1)", yukari),
                     ("Piyasa öncesi AŞAĞI (≤-%1)", asagi)]:
        if len(veri) < 30:
            continue
        sonuc["genel"][ad] = {
            "n": len(veri),
            "once_ort": round(float(veri.once_getiri_pct.mean()), 3),
            "acilista_korunan": round(float(veri.acilis_boslugu_pct.mean()), 3),
            "acilis_ilk30": round(float(veri.acilis_ilk30_pct.mean()), 3),
            "acilis_kapanis": round(float(veri.acilis_kapanis_pct.mean()), 3),
            "ters_donme_orani": round(float(
                ((veri.acilis_kapanis_pct > 0) if ad.startswith("Piyasa öncesi AŞAĞI")
                 else (veri.acilis_kapanis_pct < 0)).mean() * 100), 1),
        }

    # NEDEN? - hangi ozellik "tersine donme"yi aciklıyor
    if len(yukari) >= 50:
        for kol in ["once_hacim_orani", "once_hacim_asilin_yuzdesi", "once_getiri_pct",
                    "once_aralik_pct", "gec_gelen_hareket", "spy_once_pct", "fiyat"]:
            a = yukari[[kol, "acilis_kapanis_pct"]].dropna()
            if len(a) < 40:
                continue
            try:
                r, p = _stats.spearmanr(a[kol], a["acilis_kapanis_pct"])
            except Exception:
                continue
            sonuc["sebepler"].append({"ozellik": kol, "r": round(float(r), 4), "p": float(p)})
        sonuc["sebepler"].sort(key=lambda x: x["p"])

        # hacim dilimlerine gore
        a = yukari[["once_hacim_asilin_yuzdesi", "acilis_boslugu_pct",
                    "acilis_kapanis_pct", "once_getiri_pct"]].dropna()
        if len(a) >= 60:
            a = a.copy()
            a["dilim"] = pd.qcut(a.once_hacim_asilin_yuzdesi, 4,
                                  labels=["1) en düşük hacim", "2)", "3)", "4) en yüksek hacim"])
            for d, g in a.groupby("dilim", observed=True):
                sonuc["hacim_dilimleri"].append({
                    "dilim": str(d), "n": len(g),
                    "once_ort": round(float(g.once_getiri_pct.mean()), 3),
                    "acilis_boslugu": round(float(g.acilis_boslugu_pct.mean()), 3),
                    "acilis_kapanis": round(float(g.acilis_kapanis_pct.mean()), 3),
                })
    return dosya, sonuc


def _rapor(o):
    s = [f"🌅 PİYASA ÖNCESİ HAREKET NEDEN TERSİNE DÖNÜYOR? — {KOD_SURUMU}",
         f"İşlenen: {o['islenen']} hisse | Gün-kaydı: {o['toplam']}",
         f"Piyasa öncesi ≥%{HAREKET_ESIK} hareket eden: {o['hareketli']} "
         f"(yukarı {o['yukari']}, aşağı {o['asagi']})\n"]
    for ad, g in o["genel"].items():
        s.append(f"═══ {ad} (n={g['n']}) ═══")
        s.append(f"   Piyasa öncesi hareket:      %{g['once_ort']:+.3f}")
        s.append(f"   Açılışta korunan (boşluk):  %{g['acilista_korunan']:+.3f}")
        s.append(f"   Açılış→ilk 30 dk:           %{g['acilis_ilk30']:+.3f}")
        s.append(f"   Açılış→kapanış:             %{g['acilis_kapanis']:+.3f}")
        s.append(f"   TERSİNE DÖNME oranı:        %{g['ters_donme_orani']}\n")
    if o["sebepler"]:
        s.append("NEDEN? (yukarı hareket edenlerde, açılış sonrası getiriyle ilişki):")
        s.append(f"{'özellik':<28}{'korelasyon':>12}{'p':>10}")
        for x in o["sebepler"]:
            yildiz = " ★" if x["p"] < 0.05 else ""
            s.append(f"{x['ozellik']:<28}{x['r']:>+12.4f}{x['p']:>10.3f}{yildiz}")
    if o["hacim_dilimleri"]:
        s.append("\nPİYASA ÖNCESİ HACME GÖRE (yukarı hareket edenler):")
        s.append(f"{'dilim':<22}{'n':>6}{'öncesi':>9}{'boşluk':>9}{'açılış→kapanış':>16}")
        for d in o["hacim_dilimleri"]:
            s.append(f"{d['dilim']:<22}{d['n']:>6}{d['once_ort']:>+8.2f}%"
                     f"{d['acilis_boslugu']:>+8.2f}%{d['acilis_kapanis']:>+15.2f}%")
    s.append("\n⚠️ NASIL OKUNMALI:\n"
             "  'Tersine dönme oranı' %50'nin belirgin üstündeyse → gözlemin "
             "doğru, örüntü gerçek.\n"
             "  'NEDEN' tablosunda ★ olan özellikler, dönüşü açıklıyor demektir.\n"
             "  Hacim dilimlerinde: DÜŞÜK hacimli piyasa öncesi hareketler "
             "daha çok geri dönüyorsa, klasik 'az katılımlı fiyat oyunu' "
             "açıklaması doğrulanmış olur - ve bu KULLANILABİLİR bir filtredir.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (piyasa oncesi)", 200


def _ping():
    harici = (os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
              or os.environ.get("HARICI_URL", "").rstrip("/"))
    time.sleep(30)
    while True:
        try:
            if harici:
                requests.get(f"{harici}/health", timeout=20)
            else:
                requests.get(f"http://127.0.0.1:{PORT}/health", timeout=10)
        except Exception:
            pass
        time.sleep(600)


def _calis():
    time.sleep(5)
    send_telegram_message(
        f"🌅 PİYASA ÖNCESİ ANALİZİ başlıyor — {KOD_SURUMU}\n\n"
        f"Senin gözlemin: 'hisse piyasa açılmadan önce güzel yükselmiş "
        f"oluyor, açılışta düşüyor - ya da tersi. NEDEN?'\n\n"
        f"{len(US_TICKERS)} hissenin 60 günlük PİYASA ÖNCESİ verisi "
        f"(prepost) çekiliyor.\n\n"
        f"Aranan sebepler: piyasa öncesi HACİM (en güçlü aday - az "
        f"hacimle oynatılan fiyat asıl seansta reddediliyor olabilir), "
        f"hareketin büyüklüğü, SPY bağlamı, hareketin erken/geç oluşu, "
        f"fiyat seviyesi.\n\n"
        f"⚠️ Bu deploy'da SADECE bu analiz çalışıyor.\nBitince CSV + özet göndereceğim."
    )
    try:
        dosya, sonuc = calistir()
        if dosya is None:
            send_telegram_message(f"🌅 Analiz başarısız: {sonuc}")
            return
        send_telegram_document(dosya, caption=_rapor(sonuc))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"🌅 Analiz hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] abd_piyasa_oncesi.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
