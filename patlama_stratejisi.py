"""
patlama_stratejisi.py — RÖNTGEN BULGULARINDAN ÇIKARILAN STRATEJİ TESTİ
======================================================================
2026-08-19 — patlama_rontgeni.py'nin bulgularından çıkarılan kuralı
gerçek bir strateji olarak test eder.

RÖNTGEN NE BULMUŞTU (721 patlama vs 1648 kontrol):
  - Zamanlama: patlamalar günün 2. barında (medyan) başlıyor, %34'ü
    açılışın İLK barında (kontrolde %3) — p=3.8e-106, en güçlü sinyal
  - Hacim: patlamadan ÖNCEKİ bar hacmi 1.42x (kontrol 0.76x) — p=5.6e-48
  - Sıkışma: patlayanlar 20-bar aralığı %7.85 (kontrol %5.20) — yani
    sıkışmadan DEĞİL, ZATEN OYNAK ortamdan çıkıyor — p=2.0e-53
  - VWAP: patlama anında fiyat VWAP'ın %0.69 ALTINDA — p=6.1e-17
  - Fiyat: patlayanlar medyan $5.44 (kontrol $9.16) — p=5.3e-13
  - Gap: ANLAMSIZ (p=0.15) — açılış boşluğu hiçbir şey söylemiyor

BU DOSYADAKİ EN ÖNEMLİ ŞEY — DÜRÜSTLÜK:
Kural, röntgen verisinden ÇIKARILDI. Aynı veride test etmek kendini
kandırmaktır (bugün defalarca kaçındığımız tuzak). Bu yüzden:
  1. ICERIDE (in-sample): röntgende kullanılan AYNI hisseler
  2. DISARIDA (out-of-sample): röntgende HİÇ KULLANILMAYAN farklı
     hisseler — GERÇEK sınav bu
  3. Her ikisinde de KÖR KONTROL: "sadece ilk saatte koşulsuz al"
     — çünkü zaten biliyoruz ki patlamalar ilk saatte oluyor; asıl
     soru EK FİLTRELERİN değer katıp katmadığı

Start Command:  python patlama_stratejisi.py
Bu deploy'da SADECE bu test çalışır - başka hiçbir sistem yok.
"""
import os
import time
import threading

import numpy as np
import pandas as pd
import requests
from flask import Flask

TELEGRAM_TOKEN = os.environ.get("ARGE_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("ARGE_TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")
PORT = int(os.environ.get("PORT", "10000"))
KOD_SURUMU = "strateji-v1-2026-08-19"

# --- RÖNTGENDEN ÇIKAN KURAL PARAMETRELERİ ---
MAX_BAR_NO = 4          # gunun ilk 4 bari (ilk ~1 saat)
MIN_HACIM_ORANI = 1.2   # onceki bar hacmi >= ortalamanin 1.2 kati
MIN_SIKISMA_PCT = 6.0   # 20-bar araligi >= %6 (ZATEN OYNAK olsun)
MAX_FIYAT = 20.0        # dusuk fiyatli hisseler
HEDEF_PCT = 5.0         # ayni gun %5 hedef
PENCERE_BAR = 8         # 2 saat icinde

# ICERIDE: rontgende kullanilan hisseler
ICERIDE_HISSELER = [
    "GME", "AMC", "MARA", "RIOT", "MSTR", "PLTR", "SOFI", "LCID", "RIVN",
    "NIO", "XPEV", "LI", "OCGN", "INO", "VXRT", "BNGO", "SPCE", "NKLA",
    "CLOV", "BB", "IONQ", "RGTI", "SMCI", "UPST", "AFRM", "CVNA", "DKNG",
    "HOOD", "COIN", "ROKU", "SNAP", "PLUG", "FCEL", "CHPT", "QS", "BBAI",
]
# DISARIDA: rontgende HIC KULLANILMAYAN, benzer karakterde hisseler
DISARIDA_HISSELER = [
    "FUBO", "GNUS", "NAKD", "ZOM", "CTRM", "SHIP", "TOPS", "XELA", "BORR",
    "TELL", "AMPE", "AEMD", "ENZC", "TRXA", "SNDL", "NBEV", "IDEX", "JAGX",
    "KOSS", "EXPR", "NAOV", "AYTU", "CEI", "INDO", "HUSA", "IMPP", "RDBX",
    "GREE", "SIRI", "F", "NOK", "BBBY", "APE", "MMAT", "TTOO", "OPTT",
]


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


def _veri_cek(ticker, period, interval, sert_sure=30):
    import concurrent.futures
    import yfinance as yf

    def _cek():
        return yf.Ticker(ticker).history(period=period, interval=interval, timeout=20)

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


def _gun_ici_sonuc(df, idx, gun):
    """AYNI GÜN çıkış: hedef tutarsa +HEDEF_PCT, tutmazsa gün sonu
    GERÇEK kapanış getirisi. Döner: (sonuc_etiketi, gercek_getiri_pct)."""
    giris = df.iloc[idx]["close"]
    if giris <= 0:
        return None
    son_idx = idx
    for off in range(1, PENCERE_BAR + 1):
        j = idx + off
        if j >= len(df) or df.iloc[j]["gun"] != gun:
            break
        son_idx = j
        if (df.iloc[j]["high"] - giris) / giris * 100 >= HEDEF_PCT:
            return "HEDEF", HEDEF_PCT
    if son_idx == idx:
        return None
    return "PENCERE_SONU", (df.iloc[son_idx]["close"] - giris) / giris * 100


def _tara(hisseler, etiket):
    strateji, kor = [], []
    for n_i, ticker in enumerate(hisseler, 1):
        print(f"[{etiket} {n_i}/{len(hisseler)}] {ticker}...", flush=True)
        ham = _veri_cek(ticker, "60d", "15m")
        if ham is None or ham.empty or len(ham) < 60:
            time.sleep(0.4)
            continue
        try:
            df = ham.reset_index()
            df.columns = [str(c) for c in df.columns]
            df = df.rename(columns={df.columns[0]: "ts", "Open": "open", "High": "high",
                                     "Low": "low", "Close": "close", "Volume": "volume"})
            df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
            df["gun"] = df["ts"].dt.date

            gun_ilk = {}
            for gun, grup in df.groupby("gun", sort=True):
                gun_ilk[gun] = grup.index[0]

            for idx in range(25, len(df) - PENCERE_BAR):
                gun = df.iloc[idx]["gun"]
                bar_no = idx - gun_ilk[gun]
                if bar_no > MAX_BAR_NO:
                    continue  # ILK SAAT disi - hem strateji hem kor icin

                sonuc = _gun_ici_sonuc(df, idx, gun)
                if sonuc is None:
                    continue
                etiket_s, getiri = sonuc

                # KOR: ilk saatteki HER bar (kosulsuz LONG)
                kor.append({"ticker": ticker, "getiri_pct": getiri, "sonuc": etiket_s})

                # STRATEJI FILTRELERI (rontgenden)
                bar = df.iloc[idx]
                gecmis = df.iloc[max(0, idx - 20):idx]
                if len(gecmis) < 15:
                    continue
                ort_hacim = gecmis["volume"].mean()
                if ort_hacim <= 0 or pd.isna(ort_hacim):
                    continue
                onceki_hacim_orani = df.iloc[idx - 1]["volume"] / ort_hacim
                sikisma = (gecmis["high"].max() - gecmis["low"].min()) / bar["close"] * 100
                gun_barlari = df.iloc[gun_ilk[gun]:idx + 1]
                tipik = (gun_barlari["high"] + gun_barlari["low"] + gun_barlari["close"]) / 3
                vwap = (tipik * gun_barlari["volume"]).sum() / max(gun_barlari["volume"].sum(), 1e-9)
                gun_acilis = gun_barlari.iloc[0]["open"]

                if (onceki_hacim_orani >= MIN_HACIM_ORANI
                        and sikisma >= MIN_SIKISMA_PCT
                        and bar["close"] <= MAX_FIYAT
                        and bar["close"] < vwap
                        and bar["close"] < gun_acilis):
                    strateji.append({"ticker": ticker, "tarih": str(gun),
                                      "saat": bar["ts"].strftime("%H:%M"),
                                      "getiri_pct": getiri, "sonuc": etiket_s})
        except Exception as e:
            print(f"[{etiket}] {ticker} hata: {e}", flush=True)
        time.sleep(0.4)
    return strateji, kor


def _ozetle(kayitlar, isim):
    if not kayitlar:
        return {"isim": isim, "n": 0}
    g = np.array([k["getiri_pct"] for k in kayitlar])
    return {
        "isim": isim, "n": len(g),
        "ort_getiri_pct": round(float(g.mean()), 3),
        "medyan_getiri_pct": round(float(np.median(g)), 3),
        "kazanma_orani_pct": round(float((g > 0).mean() * 100), 2),
        "hedef_tutma_orani_pct": round(float(sum(1 for k in kayitlar if k["sonuc"] == "HEDEF") / len(kayitlar) * 100), 2),
        "en_iyi_pct": round(float(g.max()), 2),
        "en_kotu_pct": round(float(g.min()), 2),
    }


def calistir():
    tum_satirlar, ozetler = [], []
    for hisseler, etiket in [(ICERIDE_HISSELER, "İÇERİDE"), (DISARIDA_HISSELER, "DIŞARIDA")]:
        strateji, kor = _tara(hisseler, etiket)
        for k in strateji:
            k["grup"] = etiket; k["tip"] = "STRATEJİ"; tum_satirlar.append(k)
        ozetler.append(_ozetle(strateji, f"{etiket} — STRATEJİ (röntgen filtreleri)"))
        ozetler.append(_ozetle(kor, f"{etiket} — KÖR (sadece ilk saatte koşulsuz al)"))

    if not tum_satirlar:
        return None, "Hiç sinyal üretilemedi."
    dosya = os.path.join(DATA_DIR, "patlama_stratejisi.csv")
    pd.DataFrame(tum_satirlar).to_csv(dosya, index=False, encoding="utf-8-sig")
    return dosya, ozetler


def _rapor(ozetler):
    s = [f"🎯 PATLAMA STRATEJİSİ TESTİ — {KOD_SURUMU}",
         f"Kural: ilk {MAX_BAR_NO} bar + önceki bar hacmi >= {MIN_HACIM_ORANI}x "
         f"+ 20-bar aralığı >= %{MIN_SIKISMA_PCT} + fiyat <= ${MAX_FIYAT} "
         f"+ VWAP altı + açılış altı",
         f"Çıkış: aynı gün %{HEDEF_PCT} hedef, tutmazsa {PENCERE_BAR}. barda gerçek fiyat\n"]
    for o in ozetler:
        if o.get("n", 0) == 0:
            s.append(f"{o['isim']}: sinyal yok"); continue
        s.append(f"{o['isim']}:\n  n={o['n']}, ort=%{o['ort_getiri_pct']}, "
                 f"medyan=%{o['medyan_getiri_pct']}, kazanma=%{o['kazanma_orani_pct']}, "
                 f"hedef tutma=%{o['hedef_tutma_orani_pct']}, "
                 f"en iyi=%{o['en_iyi_pct']}, en kötü=%{o['en_kotu_pct']}")
    s.append("\n⚠️ ASIL SINAV: 'DIŞARIDA' satırları. Kural İÇERİDE'ki veriden "
             "çıkarıldı, orada iyi görünmesi normal. DIŞARIDA da körü geçiyorsa "
             "gerçek bir bulgu, geçmiyorsa sadece veriye uydurmuşuz demektir.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (patlama stratejisi)", 200


def _ping():
    time.sleep(30)
    while True:
        try:
            requests.get(f"http://127.0.0.1:{PORT}/health", timeout=10)
        except Exception:
            pass
        time.sleep(600)


def _calis():
    time.sleep(5)
    send_telegram_message(
        f"🎯 PATLAMA STRATEJİSİ TESTİ başlıyor — {KOD_SURUMU}\n\n"
        f"Röntgenden çıkan kural test ediliyor.\n"
        f"İÇERİDE: {len(ICERIDE_HISSELER)} hisse (röntgende kullanılanlar)\n"
        f"DIŞARIDA: {len(DISARIDA_HISSELER)} hisse (HİÇ kullanılmayanlar - ASIL SINAV)\n"
        f"Her ikisinde de KÖR kontrol: 'sadece ilk saatte koşulsuz al'\n\n"
        f"⚠️ Bu deploy'da SADECE bu test çalışıyor.\nBitince CSV + özet göndereceğim."
    )
    try:
        dosya, sonuc = calistir()
        if dosya is None:
            send_telegram_message(f"🎯 Test başarısız: {sonuc}")
            return
        send_telegram_document(dosya, caption=_rapor(sonuc))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"🎯 Test hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] patlama_stratejisi.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
