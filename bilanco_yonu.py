"""
bilanco_yonu.py — İKİ BİLANÇO ARASI HABER AKIŞI, SONRAKİ BİLANÇONUN
                   YÖNÜNÜ TAHMİN EDİYOR MU?
====================================================================
2026-08-19 — Kullanıcının fikri: "bir hissenin son bilanço tarihinden
mevcut bilanço tarihine kadar çıkan iyi ve kötü haberlerin toplamıyla,
açıklanacak bilançonun hangi yönde olacağını tahmin edelim."

Bu, finans literatüründe gerçekten test edilmiş bir yaklaşım. Ama
dürüstlükle başlayalım - NEYİ ÖLÇEBİLİYORUZ, NEYİ ÖLÇEMİYORUZ:

ÖLÇEMEDİĞİMİZ: Gerçek haber başlıkları + duygu analizi. Geçmişe dönük
ücretsiz/güvenilir kaynak yok (bugün doğruladık, yfinance sadece güncel
haberi veriyor).

ÖLÇEBİLDİĞİMİZ (fikrin özünü test eden vekiller):
  1. 8-K TÜRÜ SKORU: iki bilanço arasındaki SEC 8-K bildirimlerini
     türüne göre iyi/kötü puanlıyoruz:
        İYİ  : 1.01 (önemli sözleşme), 2.01 (satın alma tamamlandı)
        KÖTÜ : 3.02 (hisse sulandırma), 5.02 (yönetici ayrılışı),
               1.02 (sözleşme feshi), 4.01 (denetçi değişikliği)
        NÖTR : diğerleri
     ("Kibrit avı" testinde 1.01'in %29.5, 5.02'nin %9.0 patlama
      oranı vermesi, bu türlerin gerçekten bilgi taşıdığını gösterdi.)
  2. DÖNEM İÇİ FİYAT: önceki bilançodan bu bilançoya kadar getiri
     (piyasa zaten bir şey biliyor olabilir)
  3. BİLANÇO ÖNCESİ SON 20 GÜN: son anda hızlanma var mı
  4. ANALİST BEKLENTİ DEĞİŞİMİ: EPS tahmini yükseldi mi düştü mü
     (literatürde en güçlü belgelenmiş öngörücülerden biri)

HEDEF: bilanço tepkisi = (bilançodan sonraki gün kapanış) /
       (bilançodan önceki gün kapanış) - 1  → YUKARI mı AŞAĞI mı?

KRİTİK KONTROL: Taban oran. Bilançoların zaten %X'i yukarı çıkıyorsa,
%X isabet tutturmak HİÇBİR ŞEY değildir. Başarı, taban oranı GEÇMEKTİR.

Start Command:  python bilanco_yonu.py
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
KOD_SURUMU = "bilanco-yonu-v1-2026-08-19"

EDGAR_HEADERS = {"User-Agent": "arastirma-botu yahyadag1919@gmail.com"}

IYI_ITEMLER = {"1.01", "2.01"}
KOTU_ITEMLER = {"3.02", "5.02", "1.02", "4.01"}

# Buyuk + volatil karisik - bilanco gecmisi olan hisseler
HISSELER = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "JPM", "V",
    "UNH", "HD", "PG", "COST", "XOM", "JNJ", "AVGO", "PEP", "KO", "BAC",
    "WMT", "CRM", "ADBE", "AMD", "NFLX", "DIS", "CSCO", "ORCL", "INTC",
    "QCOM", "TXN", "PFE", "NKE", "MCD", "GS", "CAT", "BA", "LLY", "ABT",
    "PLTR", "SOFI", "LCID", "RIVN", "NIO", "COIN", "HOOD", "ROKU", "SNAP",
    "SMCI", "UPST", "AFRM", "CVNA", "DKNG", "PLUG", "IONQ", "MARA", "RIOT",
    "MSTR", "GME", "AMC", "CHPT", "QS", "BBAI", "SOUN", "CRSP", "TLRY",
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


def _sert(fonk, sure=30, etiket=""):
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
        return requests.get("https://www.sec.gov/files/company_tickers.json",
                            headers=EDGAR_HEADERS, timeout=20).json()
    d = _sert(_f, 40, "CIK haritası")
    return {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in d.values()} if d else {}


def _8k_listesi(cik, ticker):
    """{tarih: [item kodlari]} - TEK istek."""
    def _f():
        return requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                            headers=EDGAR_HEADERS, timeout=20).json()
    d = _sert(_f, 40, f"8-K {ticker}")
    if not d:
        return {}
    son = d.get("filings", {}).get("recent", {})
    formlar, tarihler = son.get("form", []), son.get("filingDate", [])
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


def _bilancolar(ticker):
    """Gecmis bilanco tarihleri + EPS tahmin/gerceklesen."""
    def _f():
        import yfinance as yf
        df = yf.Ticker(ticker).earnings_dates
        if df is None or df.empty:
            return []
        out = []
        for ts, satir in df.iterrows():
            try:
                t = pd.to_datetime(ts).tz_localize(None).date()
            except Exception:
                try:
                    t = pd.to_datetime(ts).date()
                except Exception:
                    continue
            out.append({
                "tarih": t,
                "eps_tahmin": float(satir["EPS Estimate"]) if pd.notna(satir.get("EPS Estimate")) else None,
                "eps_gerceklesen": float(satir["Reported EPS"]) if pd.notna(satir.get("Reported EPS")) else None,
                "surpriz_pct": float(satir["Surprise(%)"]) if pd.notna(satir.get("Surprise(%)")) else None,
            })
        return sorted(out, key=lambda x: x["tarih"])
    return _sert(_f, 30, f"bilanço {ticker}") or []


def _fiyat(ticker):
    def _f():
        import yfinance as yf
        return yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)
    return _sert(_f, 30, f"fiyat {ticker}")


def calistir():
    cik_harita = _cik_haritasi()
    if not cik_harita:
        return None, "SEC CIK haritası alınamadı."

    kayitlar = []
    for n_i, ticker in enumerate(HISSELER, 1):
        print(f"[Bilanço Yönü {n_i}/{len(HISSELER)}] {ticker}...", flush=True)
        ham = _fiyat(ticker)
        if ham is None or ham.empty or len(ham) < 100:
            time.sleep(0.4)
            continue
        bilancolar = _bilancolar(ticker)
        time.sleep(0.3)
        if len(bilancolar) < 2:
            time.sleep(0.3)
            continue
        cik = cik_harita.get(ticker.upper())
        sekiz_k = _8k_listesi(cik, ticker) if cik else {}
        time.sleep(0.4)

        try:
            fiyat = ham.rename(columns={"Close": "close"})
            fiyat.index = pd.to_datetime(fiyat.index).tz_localize(None)
            tarihler = [d.date() for d in fiyat.index]
            kapanis = fiyat["close"].values

            def _konum(hedef):
                """hedef tarihe en yakin (>=) index."""
                for i, t in enumerate(tarihler):
                    if t >= hedef:
                        return i
                return None

            for bi in range(1, len(bilancolar)):
                onceki_b = bilancolar[bi - 1]
                bu_b = bilancolar[bi]
                i_bu = _konum(bu_b["tarih"])
                i_onceki = _konum(onceki_b["tarih"])
                if i_bu is None or i_onceki is None:
                    continue
                if i_bu <= i_onceki + 5 or i_bu + 1 >= len(kapanis) or i_bu - 1 < 0:
                    continue

                # HEDEF: bilanco tepkisi (once-sonra)
                tepki = (kapanis[i_bu + 1] - kapanis[i_bu - 1]) / kapanis[i_bu - 1] * 100

                # OZELLIKLER - hepsi bilanco ONCESI bilgiden
                iyi = kotu = toplam = 0
                for t, kodlar in sekiz_k.items():
                    if onceki_b["tarih"] < t < bu_b["tarih"]:
                        for kod in kodlar:
                            toplam += 1
                            if kod in IYI_ITEMLER:
                                iyi += 1
                            elif kod in KOTU_ITEMLER:
                                kotu += 1

                donem_getiri = (kapanis[i_bu - 1] - kapanis[i_onceki + 1]) / kapanis[i_onceki + 1] * 100
                son20 = (kapanis[i_bu - 1] - kapanis[max(0, i_bu - 21)]) / kapanis[max(0, i_bu - 21)] * 100

                tahmin_degisim = None
                if onceki_b.get("eps_tahmin") and bu_b.get("eps_tahmin"):
                    try:
                        if abs(onceki_b["eps_tahmin"]) > 1e-9:
                            tahmin_degisim = (bu_b["eps_tahmin"] - onceki_b["eps_tahmin"]) / abs(onceki_b["eps_tahmin"]) * 100
                    except Exception:
                        pass

                kayitlar.append({
                    "ticker": ticker, "bilanco_tarihi": str(bu_b["tarih"]),
                    "8k_iyi": iyi, "8k_kotu": kotu, "8k_toplam": toplam,
                    "8k_net_skor": iyi - kotu,
                    "donem_getiri_pct": round(float(donem_getiri), 2),
                    "son20gun_getiri_pct": round(float(son20), 2),
                    "eps_tahmin_degisim_pct": round(float(tahmin_degisim), 2) if tahmin_degisim is not None else None,
                    "onceki_surpriz_pct": onceki_b.get("surpriz_pct"),
                    "tepki_pct": round(float(tepki), 2),
                    "yon": "YUKARI" if tepki > 0 else "ASAGI",
                })
        except Exception as e:
            print(f"[Bilanço Yönü] {ticker} hata: {e}", flush=True)
        time.sleep(0.4)

    if not kayitlar:
        return None, "Hiç bilanço olayı üretilemedi."
    tum = pd.DataFrame(kayitlar)
    dosya = os.path.join(DATA_DIR, "bilanco_yonu.csv")
    tum.to_csv(dosya, index=False, encoding="utf-8-sig")

    taban = float((tum.tepki_pct > 0).mean() * 100)
    ozet = {"n": len(tum), "taban_yukari_oran_pct": round(taban, 2), "ozellikler": []}

    for kol in ["8k_net_skor", "8k_iyi", "8k_kotu", "8k_toplam",
                "donem_getiri_pct", "son20gun_getiri_pct",
                "eps_tahmin_degisim_pct", "onceki_surpriz_pct"]:
        alt = tum[[kol, "tepki_pct"]].dropna()
        if len(alt) < 30:
            continue
        # korelasyon
        try:
            r, pv = _stats.spearmanr(alt[kol], alt["tepki_pct"])
        except Exception:
            continue
        # ozellik POZITIF iken yukari orani vs NEGATIF iken
        poz = alt[alt[kol] > 0]
        neg = alt[alt[kol] < 0]
        ozet["ozellikler"].append({
            "ozellik": kol, "n": len(alt),
            "korelasyon": round(float(r), 4), "p": float(pv),
            "pozitifken_yukari_pct": round(float((poz.tepki_pct > 0).mean() * 100), 1) if len(poz) >= 15 else None,
            "pozitifken_n": len(poz),
            "negatifken_yukari_pct": round(float((neg.tepki_pct > 0).mean() * 100), 1) if len(neg) >= 15 else None,
            "negatifken_n": len(neg),
        })
    ozet["ozellikler"].sort(key=lambda x: x["p"])
    return dosya, ozet


def _rapor(o):
    s = [f"📊 BİLANÇO YÖNÜ TAHMİNİ — {KOD_SURUMU}",
         f"Toplam bilanço olayı: {o['n']}",
         f"TABAN ORAN: bilançoların %{o['taban_yukari_oran_pct']}'i YUKARI tepki verdi",
         f"→ Başarı sayılması için bir özelliğin bunu belirgin GEÇMESİ lazım\n",
         "ÖZELLİKLER (bilanço tepkisiyle ilişkisi, p'ye göre sıralı):"]
    for f in o["ozellikler"]:
        s.append(f"\n  {f['ozellik']} (n={f['n']}):")
        s.append(f"    korelasyon={f['korelasyon']:+.4f}, p={f['p']:.3e}")
        if f["pozitifken_yukari_pct"] is not None:
            s.append(f"    POZİTİF iken yukarı: %{f['pozitifken_yukari_pct']} (n={f['pozitifken_n']})")
        if f["negatifken_yukari_pct"] is not None:
            s.append(f"    NEGATİF iken yukarı: %{f['negatifken_yukari_pct']} (n={f['negatifken_n']})")
    s.append(f"\n⚠️ Okuma rehberi: 'POZİTİF iken yukarı' oranı taban orandan "
             f"(%{o['taban_yukari_oran_pct']}) belirgin YÜKSEK ve 'NEGATİF iken' "
             f"belirgin DÜŞÜKSE → fikir işe yarıyor. İkisi de tabana yakınsa "
             f"→ haber akışı bilanço yönünü öngörmüyor.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (bilanco yonu)", 200


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
        f"📊 BİLANÇO YÖNÜ TAHMİNİ başlıyor — {KOD_SURUMU}\n\n"
        f"Fikir: iki bilanço arasındaki haber akışı, sonraki bilançonun "
        f"yönünü öngörüyor mu?\n"
        f"{len(HISSELER)} hisse × ~2 yıl bilanço geçmişi taranıyor.\n\n"
        f"Ölçülen: 8-K türü skoru (sözleşme=iyi, sulandırma/istifa=kötü), "
        f"dönem içi getiri, bilanço öncesi son 20 gün, analist EPS "
        f"beklenti değişimi, önceki bilanço sürprizi.\n\n"
        f"⚠️ Gerçek haber başlıkları ücretsiz alınamadığı için 8-K türleri "
        f"vekil olarak kullanılıyor - kapsam kısmi.\n"
        f"Bitince CSV + özet göndereceğim."
    )
    try:
        dosya, sonuc = calistir()
        if dosya is None:
            send_telegram_message(f"📊 Analiz başarısız: {sonuc}")
            return
        send_telegram_document(dosya, caption=_rapor(sonuc))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"📊 Analiz hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] bilanco_yonu.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
