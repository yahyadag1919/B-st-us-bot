"""
kibrit_avi.py — PATLAMALARIN ARKASINDA 8-K / BİLANÇO VAR MI?
=============================================================
2026-08-19 — "Kibrit haberde/bilançoda" tespitinden sonra kullanıcının
sorusu: "geçmişte patlayan hisselerin patlamadan önceki haber/bilanço
açıklamasına ulaşabilir miyiz?"

CEVAP: kısmen. Bu dosya ulaşılabilir olanı ölçüyor:
  1. SEC 8-K bildirimleri: şirketin "önemli olay" duyurusu (bilanço
     sonucu, büyük sözleşme, satın alma, yönetici değişikliği...).
     Tarihi kesin, ücretsiz, yıllar geriye gidiyor. Bildirimin "items"
     kodu haberin TÜRÜNÜ de söylüyor.
  2. Bilanço tarihleri + sürpriz oranı (yfinance).
Gerçek haber BAŞLIKLARI ücretsiz/güvenilir şekilde geçmişe dönük
alınamıyor - o yüzden kapsam KISMİ olacak (promosyonel basın bülteni,
analist notu, sosyal medya dalgası gibi 8-K gerektirmeyen sebepler bu
analizde GÖRÜNMEYECEK). Sonucu buna göre yorumla.

EN ÖNEMLİ TASARIM KARARI — KONTROL GRUBU:
"Patlamaların %X'inde 8-K vardı" tek başına HİÇBİR ŞEY söylemez; 8-K'lar
zaten sıksa bu çakışma tesadüf olur. Bu yüzden patlama OLMAYAN günlerde
de aynı oranı ölçüyoruz (taban oran). Asıl soru:
    patlama günlerinde 8-K oranı, sıradan günlerdekinden YÜKSEK Mİ?

Start Command:  python kibrit_avi.py
Bu deploy'da SADECE bu analiz çalışır.
"""
import os
import time
import threading
from datetime import timedelta

import numpy as np
import pandas as pd
import requests
from flask import Flask
from scipy import stats as _stats

TELEGRAM_TOKEN = os.environ.get("ARGE_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("ARGE_TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")
PORT = int(os.environ.get("PORT", "10000"))
KOD_SURUMU = "kibrit-avi-v1-2026-08-19"

PATLADI_ESIK = 5.0
PENCERE_BAR = 8
MAX_BAR_NO = 4

EDGAR_HEADERS = {"User-Agent": "arastirma-botu yahyadag1919@gmail.com"}

HISSELER = [
    "GME", "AMC", "MARA", "RIOT", "MSTR", "PLTR", "SOFI", "LCID", "RIVN",
    "NIO", "XPEV", "LI", "OCGN", "INO", "VXRT", "BNGO", "SPCE", "NKLA",
    "CLOV", "BB", "IONQ", "RGTI", "SMCI", "UPST", "AFRM", "CVNA", "DKNG",
    "HOOD", "COIN", "ROKU", "SNAP", "PLUG", "FCEL", "CHPT", "QS", "BBAI",
    "SOUN", "CRSP", "NTLA", "BEAM", "RXRX", "ACHR", "JOBY", "DNA", "GEVO",
    "MULN", "TLRY", "CGC", "OPEN", "RUN", "BLNK", "EVGO", "LAZR", "MVIS",
    "GSAT", "EOSE", "FUBO", "SNDL", "KOSS", "EXPR",
]

# 8-K item kodlari - haberin TURUNU soyluyor (en sik/onemliler)
ITEM_ACIKLAMA = {
    "2.02": "Bilanço/finansal sonuç açıklaması",
    "1.01": "Önemli sözleşme imzalandı",
    "8.01": "Diğer önemli olay",
    "7.01": "Yatırımcı sunumu/duyuru (Reg FD)",
    "5.02": "Yönetici/kurul değişikliği",
    "3.02": "Hisse satışı (sulandırma)",
    "1.02": "Önemli sözleşme feshi",
    "2.01": "Satın alma/varlık satışı tamamlandı",
    "5.07": "Genel kurul sonuçları",
    "9.01": "Ek mali tablolar/ekler",
}


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


def _sert(fonk, sure=30, etiket=""):
    """Herhangi bir cagriyi SERT zaman asimiyla sarar - bugun defalarca
    yasadigimiz donma sorununa karsi."""
    import concurrent.futures
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(fonk).result(timeout=sure)
    except concurrent.futures.TimeoutError:
        print(f"[SERT zaman aşımı] {etiket}", flush=True)
        return None
    except Exception as e:
        print(f"[Hata] {etiket}: {e}", flush=True)
        return None
    finally:
        ex.shutdown(wait=False)


def _cik_haritasi():
    def _f():
        r = requests.get("https://www.sec.gov/files/company_tickers.json",
                         headers=EDGAR_HEADERS, timeout=20)
        return r.json()
    d = _sert(_f, 40, "CIK haritası")
    if not d:
        return {}
    return {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in d.values()}


def _8k_bildirimleri(cik, ticker):
    """Bir hissenin 8-K bildirimlerini {tarih: [item kodlari]} olarak döner.
    TEK istek - bugun donmalara yol acan Form4 detay dongusu gibi degil."""
    def _f():
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                         headers=EDGAR_HEADERS, timeout=20)
        return r.json()
    d = _sert(_f, 40, f"8-K {ticker}")
    if not d:
        return {}
    son = d.get("filings", {}).get("recent", {})
    formlar = son.get("form", [])
    tarihler = son.get("filingDate", [])
    itemler = son.get("items", [""] * len(formlar))
    harita = {}
    for i, form in enumerate(formlar):
        if form != "8-K":
            continue
        try:
            t = pd.to_datetime(tarihler[i]).date()
        except Exception:
            continue
        kodlar = [x.strip() for x in str(itemler[i] if i < len(itemler) else "").split(",") if x.strip()]
        harita.setdefault(t, []).extend(kodlar)
    return harita


def _bilanco_tarihleri(ticker):
    """yfinance'ten gecmis bilanco tarihleri + surpriz orani."""
    def _f():
        import yfinance as yf
        df = yf.Ticker(ticker).earnings_dates
        if df is None or df.empty:
            return {}
        out = {}
        for ts, satir in df.iterrows():
            try:
                t = pd.to_datetime(ts).date()
            except Exception:
                continue
            sur = satir.get("Surprise(%)", np.nan)
            out[t] = float(sur) if pd.notna(sur) else None
        return out
    r = _sert(_f, 30, f"bilanço {ticker}")
    return r or {}


def _veri_cek(ticker):
    def _f():
        import yfinance as yf
        return yf.Ticker(ticker).history(period="60d", interval="15m", timeout=20)
    return _sert(_f, 30, f"fiyat {ticker}")


def calistir():
    cik_harita = _cik_haritasi()
    if not cik_harita:
        return None, "SEC CIK haritası alınamadı."
    print(f"[CIK] {len(cik_harita)} şirket yüklendi.", flush=True)

    kayitlar = []
    for n_i, ticker in enumerate(HISSELER, 1):
        print(f"[Kibrit {n_i}/{len(HISSELER)}] {ticker}...", flush=True)
        ham = _veri_cek(ticker)
        if ham is None or ham.empty or len(ham) < 60:
            time.sleep(0.5)
            continue
        cik = cik_harita.get(ticker.upper())
        sekiz_k = _8k_bildirimleri(cik, ticker) if cik else {}
        time.sleep(0.3)
        bilanco = _bilanco_tarihleri(ticker)
        time.sleep(0.3)

        try:
            df = ham.reset_index()
            df.columns = [str(c) for c in df.columns]
            df = df.rename(columns={df.columns[0]: "ts", "Open": "open", "High": "high",
                                     "Low": "low", "Close": "close", "Volume": "volume"})
            df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
            df["gun"] = df["ts"].dt.date

            gun_ilk = {g: grup.index[0] for g, grup in df.groupby("gun", sort=True)}
            patlama_gunleri = set()
            for idx in range(25, len(df) - PENCERE_BAR):
                gun = df.iloc[idx]["gun"]
                if (idx - gun_ilk[gun]) > MAX_BAR_NO:
                    continue
                giris = df.iloc[idx]["close"]
                if giris <= 0:
                    continue
                ileri = df.iloc[idx + 1: idx + 1 + PENCERE_BAR]
                ileri = ileri[ileri["gun"] == gun]
                if ileri.empty:
                    continue
                if (ileri["high"].max() - giris) / giris * 100 >= PATLADI_ESIK:
                    patlama_gunleri.add(gun)

            for gun in sorted(gun_ilk.keys()):
                onceki = gun - timedelta(days=1)
                onceki2 = gun - timedelta(days=2)
                kodlar = []
                for t in (gun, onceki, onceki2):
                    kodlar.extend(sekiz_k.get(t, []))
                bilanco_var = any(t in bilanco for t in (gun, onceki, onceki2))
                surpriz = next((bilanco[t] for t in (gun, onceki, onceki2)
                                if t in bilanco and bilanco[t] is not None), None)
                kayitlar.append({
                    "ticker": ticker, "tarih": str(gun),
                    "patladi": int(gun in patlama_gunleri),
                    "8k_var": int(len(kodlar) > 0),
                    "8k_itemler": ",".join(sorted(set(kodlar))),
                    "bilanco_var": int(bilanco_var),
                    "bilanco_surpriz_pct": surpriz,
                    "haber_var": int(len(kodlar) > 0 or bilanco_var),
                })
        except Exception as e:
            print(f"[Kibrit] {ticker} hata: {e}", flush=True)
        time.sleep(0.4)

    if not kayitlar:
        return None, "Hiç kayıt üretilemedi."
    tum = pd.DataFrame(kayitlar)
    dosya = os.path.join(DATA_DIR, "kibrit_avi.csv")
    tum.to_csv(dosya, index=False, encoding="utf-8-sig")

    p = tum[tum.patladi == 1]
    k = tum[tum.patladi == 0]
    ozet = {"patlama_gun": len(p), "normal_gun": len(k)}
    for kol in ["8k_var", "bilanco_var", "haber_var"]:
        po = float(p[kol].mean() * 100) if len(p) else 0.0
        ko = float(k[kol].mean() * 100) if len(k) else 0.0
        try:
            tablo = [[int(p[kol].sum()), int(len(p) - p[kol].sum())],
                     [int(k[kol].sum()), int(len(k) - k[kol].sum())]]
            _, pv = _stats.fisher_exact(tablo)
        except Exception:
            pv = None
        ozet[kol] = {"patlama_pct": round(po, 1), "normal_pct": round(ko, 1),
                     "kat": round(po / ko, 2) if ko > 0 else None, "p": pv}

    # patlama gunlerinde en sik 8-K item kodlari
    sayac = {}
    for s in p["8k_itemler"].dropna():
        for kod in str(s).split(","):
            kod = kod.strip()
            if kod:
                sayac[kod] = sayac.get(kod, 0) + 1
    ozet["en_sik_itemler"] = sorted(sayac.items(), key=lambda x: -x[1])[:8]
    return dosya, ozet


def _rapor(o):
    s = [f"🔥 KİBRİT AVI — {KOD_SURUMU}",
         f"Patlama günü: {o['patlama_gun']} | Normal gün: {o['normal_gun']}\n",
         "PATLAMA GÜNÜ vs NORMAL GÜN (haber/bildirim oranı):"]
    for kol, ad in [("8k_var", "SEC 8-K bildirimi"),
                    ("bilanco_var", "Bilanço açıklaması"),
                    ("haber_var", "Herhangi biri")]:
        d = o[kol]
        pv = f"p={d['p']:.2e}" if d.get("p") is not None else "p=?"
        s.append(f"  {ad}: patlama %{d['patlama_pct']} | normal %{d['normal_pct']} "
                 f"→ {d['kat']}x, {pv}")
    if o.get("en_sik_itemler"):
        s.append("\nPatlama günlerinde en sık 8-K türleri:")
        for kod, adet in o["en_sik_itemler"]:
            s.append(f"  {kod} ({ITEM_ACIKLAMA.get(kod, 'bilinmeyen')}): {adet} kez")
    s.append("\n⚠️ YORUM REHBERİ:\n"
             "  'kat' 1'e yakınsa → 8-K/bilanço patlamayı açıklamıyor, kibrit "
             "başka yerde (promosyon, analist notu, sosyal medya).\n"
             "  'kat' belirgin yüksekse (2x+) → kibriti bulduk demektir ve "
             "bilanço TAKVİMİ önceden belli olduğu için kullanılabilir.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (kibrit avi)", 200


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
        f"🔥 KİBRİT AVI başlıyor — {KOD_SURUMU}\n\n"
        f"{len(HISSELER)} hissenin son 60 günü taranıyor. Her gün için:\n"
        f"  • O gün (ya da 1-2 gün öncesinde) SEC 8-K bildirimi var mı?\n"
        f"  • Bilanço açıklaması var mı (+sürpriz oranı)?\n"
        f"  • O gün gün-içi patlama (>=%{PATLADI_ESIK}) oldu mu?\n\n"
        f"Sonra karşılaştırılıyor: patlama günlerinde haber oranı, normal "
        f"günlerdekinden YÜKSEK Mİ? (kontrol grubu olmadan çakışma bir şey "
        f"kanıtlamaz)\n\n"
        f"⚠️ Bu deploy'da SADECE bu analiz çalışıyor.\nBitince CSV + özet göndereceğim."
    )
    try:
        dosya, sonuc = calistir()
        if dosya is None:
            send_telegram_message(f"🔥 Kibrit avı başarısız: {sonuc}")
            return
        send_telegram_document(dosya, caption=_rapor(sonuc))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"🔥 Kibrit avı hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] kibrit_avi.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
