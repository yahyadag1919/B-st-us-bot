"""
bist_tavan.py — BIST'TE TAVAN YAPAN HİSSELER ERTESİ GÜN NE YAPIYOR?
=====================================================================
2026-08-19 — Kullanıcının sorusu: "geçmişte tavan yapan hisseler ertesi
gün piyasa açıldıktan sonra yükselişle mi başladı yoksa düşüşle mi, ve
ortalama yüzde kaçla?"

TAVAN TANIMI: BIST'te günlük fiyat limiti %10'dur. Günlük getirisi
>= %9.5 olan günler "tavan" sayılıyor (yuvarlama payı bırakıldı).

ERTESİ GÜN ÖLÇÜLEN 5 ŞEY (hepsi ayrı ayrı önemli):
  1. AÇILIŞ BOŞLUĞU: (ertesi açılış - tavan kapanış) / tavan kapanış
     → gece boyunca ne oldu, yukarı mı aşağı mı açtı
  2. AÇILIŞ SONRASI: (ertesi kapanış - ertesi açılış) / ertesi açılış
     → KULLANICININ ASIL SORUSU: açıldıktan SONRA yükseldi mi düştü mü
  3. AÇILIŞ→ZİRVE: gün içinde açılıştan en fazla ne kadar yükseldi
  4. AÇILIŞ→DİP: gün içinde açılıştan en fazla ne kadar düştü
  5. TOPLAM: (ertesi kapanış - tavan kapanış) / tavan kapanış

AYRICA: ardışık kaçıncı tavan olduğu da kaydediliyor (BIST'te üst üste
tavan sık görülür ve 1. tavan ile 3. tavan sonrası davranış farklı
olabilir - ayrı ayrı raporlanıyor).

KONTROL GRUBU: tavan OLMAYAN günlerin ertesi günü de aynı şekilde
ölçülüyor. Çünkü "tavan sonrası ortalama %X" tek başına anlamsızdır -
sıradan bir günün ertesinden farklı mı, asıl soru bu.

DÜRÜST NOT: yfinance'in BIST verisi zaman zaman eksik/tutarsız olabiliyor
(bugün KOZAL.IS'te veri hatası aldık). Veri gelmeyen hisseler atlanıyor,
kaç hissenin işlendiği raporda belirtiliyor.

Start Command:  python bist_tavan.py
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

TELEGRAM_TOKEN = os.environ.get("ARGE_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("ARGE_TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")
PORT = int(os.environ.get("PORT", "10000"))
KOD_SURUMU = "bist-tavan-v1-2026-08-19"

TAVAN_ESIK_PCT = 9.5   # BIST gunluk limit %10, yuvarlama payi

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


def _veri_cek(ticker, sert_sure=30):
    import concurrent.futures
    import yfinance as yf

    def _cek():
        return yf.Ticker(ticker).history(period="2y", interval="1d", timeout=20)

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


def calistir():
    kayitlar = []
    islenen, atlanan = 0, 0

    for n_i, ticker in enumerate(BIST_HISSELER, 1):
        print(f"[Tavan {n_i}/{len(BIST_HISSELER)}] {ticker}...", flush=True)
        ham = _veri_cek(ticker)
        if ham is None or ham.empty or len(ham) < 60:
            atlanan += 1
            time.sleep(0.4)
            continue
        try:
            df = ham.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                      "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            if len(df) < 60:
                atlanan += 1
                time.sleep(0.4)
                continue
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df = df.reset_index().rename(columns={df.index.name or "index": "tarih"})
            if "tarih" not in df.columns:
                df = df.rename(columns={df.columns[0]: "tarih"})

            df["gunluk_getiri"] = df["close"].pct_change() * 100
            df["tavan_mi"] = df["gunluk_getiri"] >= TAVAN_ESIK_PCT

            # ardisik kacinci tavan
            ardisik = 0
            ardisik_liste = []
            for t in df["tavan_mi"]:
                ardisik = ardisik + 1 if t else 0
                ardisik_liste.append(ardisik)
            df["kacinci_tavan"] = ardisik_liste

            for i in range(1, len(df) - 1):
                bugun = df.iloc[i]
                ertesi = df.iloc[i + 1]
                if bugun["close"] <= 0 or ertesi["open"] <= 0:
                    continue
                kayitlar.append({
                    "ticker": ticker,
                    "tarih": str(bugun["tarih"].date()),
                    "tavan_mi": int(bool(bugun["tavan_mi"])),
                    "kacinci_tavan": int(bugun["kacinci_tavan"]),
                    "o_gun_getiri_pct": round(float(bugun["gunluk_getiri"]), 2) if pd.notna(bugun["gunluk_getiri"]) else None,
                    # 1. acilis boslugu
                    "acilis_boslugu_pct": round(float((ertesi["open"] - bugun["close"]) / bugun["close"] * 100), 2),
                    # 2. ASIL SORU: acildiktan SONRA ne oldu
                    "acilis_sonrasi_pct": round(float((ertesi["close"] - ertesi["open"]) / ertesi["open"] * 100), 2),
                    # 3-4. gun ici uc noktalar
                    "acilis_zirve_pct": round(float((ertesi["high"] - ertesi["open"]) / ertesi["open"] * 100), 2),
                    "acilis_dip_pct": round(float((ertesi["low"] - ertesi["open"]) / ertesi["open"] * 100), 2),
                    # 5. toplam
                    "toplam_ertesi_gun_pct": round(float((ertesi["close"] - bugun["close"]) / bugun["close"] * 100), 2),
                })
            islenen += 1
        except Exception as e:
            print(f"[Tavan] {ticker} hata: {e}", flush=True)
            atlanan += 1
        time.sleep(0.4)

    if not kayitlar:
        return None, "Hiç kayıt üretilemedi (BIST verisi alınamamış olabilir)."

    tum = pd.DataFrame(kayitlar)
    dosya = os.path.join(DATA_DIR, "bist_tavan.csv")
    tum.to_csv(dosya, index=False, encoding="utf-8-sig")

    tavan = tum[tum.tavan_mi == 1]
    normal = tum[tum.tavan_mi == 0]

    def _ist(grup, kol):
        s = grup[kol].dropna()
        if len(s) < 5:
            return None
        return {"n": len(s), "ort": round(float(s.mean()), 3),
                "medyan": round(float(s.median()), 3),
                "pozitif_oran_pct": round(float((s > 0).mean() * 100), 1)}

    ozet = {"islenen_hisse": islenen, "atlanan_hisse": atlanan,
            "tavan_gun": len(tavan), "normal_gun": len(normal), "olcumler": {}, "ardisik": []}

    for kol in ["acilis_boslugu_pct", "acilis_sonrasi_pct", "acilis_zirve_pct",
                "acilis_dip_pct", "toplam_ertesi_gun_pct"]:
        t = _ist(tavan, kol)
        n = _ist(normal, kol)
        pv = None
        if t and n:
            try:
                _, pv = _stats.mannwhitneyu(tavan[kol].dropna(), normal[kol].dropna(),
                                             alternative="two-sided")
            except Exception:
                pass
        ozet["olcumler"][kol] = {"tavan": t, "normal": n, "p": pv}

    for k in (1, 2, 3):
        alt = tavan[tavan.kacinci_tavan == k]
        if len(alt) >= 10:
            ozet["ardisik"].append({
                "kacinci": k, "n": len(alt),
                "acilis_boslugu_ort": round(float(alt.acilis_boslugu_pct.mean()), 2),
                "acilis_sonrasi_ort": round(float(alt.acilis_sonrasi_pct.mean()), 2),
                "acilis_sonrasi_pozitif_pct": round(float((alt.acilis_sonrasi_pct > 0).mean() * 100), 1),
                "toplam_ort": round(float(alt.toplam_ertesi_gun_pct.mean()), 2),
            })
    return dosya, ozet


def _rapor(o):
    s = [f"📈 BIST TAVAN SONRASI ERTESİ GÜN — {KOD_SURUMU}",
         f"İşlenen hisse: {o['islenen_hisse']} | Atlanan (veri yok): {o['atlanan_hisse']}",
         f"Tavan günü: {o['tavan_gun']} | Normal gün: {o['normal_gun']}\n"]
    adlar = {
        "acilis_boslugu_pct": "1) AÇILIŞ BOŞLUĞU (gece)",
        "acilis_sonrasi_pct": "2) AÇILDIKTAN SONRA (asıl soru)",
        "acilis_zirve_pct": "3) Açılış→gün içi ZİRVE",
        "acilis_dip_pct": "4) Açılış→gün içi DİP",
        "toplam_ertesi_gun_pct": "5) TOPLAM (tavan kapanışına göre)",
    }
    for kol, ad in adlar.items():
        d = o["olcumler"].get(kol) or {}
        t, n = d.get("tavan"), d.get("normal")
        if not t:
            continue
        s.append(f"{ad}:")
        s.append(f"   TAVAN sonrası: ort %{t['ort']}, medyan %{t['medyan']}, "
                 f"pozitif oran %{t['pozitif_oran_pct']} (n={t['n']})")
        if n:
            s.append(f"   Normal gün sonrası: ort %{n['ort']}, medyan %{n['medyan']}, "
                     f"pozitif oran %{n['pozitif_oran_pct']} (n={n['n']})")
        if d.get("p") is not None:
            s.append(f"   p={d['p']:.2e}")
        s.append("")
    if o["ardisik"]:
        s.append("ARDIŞIK TAVAN SAYISINA GÖRE:")
        for a in o["ardisik"]:
            s.append(f"   {a['kacinci']}. tavan (n={a['n']}): boşluk %{a['acilis_boslugu_ort']}, "
                     f"açılış sonrası %{a['acilis_sonrasi_ort']} "
                     f"(pozitif %{a['acilis_sonrasi_pozitif_pct']}), toplam %{a['toplam_ort']}")
    s.append("\n⚠️ 'Açılış sonrası' senin asıl sorunun cevabı: tavan ertesi "
             "gün AÇILDIKTAN SONRA yükseliş mi düşüş mü. Normal günlerle "
             "karşılaştırmadan bakma - fark varsa anlamlı, yoksa tavan özel değil.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (bist tavan)", 200


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
        f"📈 BIST TAVAN ANALİZİ başlıyor — {KOD_SURUMU}\n\n"
        f"{len(BIST_HISSELER)} BIST hissesinin son 2 yılı taranıyor.\n"
        f"Tavan tanımı: günlük getiri >= %{TAVAN_ESIK_PCT}\n\n"
        f"Ertesi gün ölçülenler: açılış boşluğu, AÇILDIKTAN SONRAKİ "
        f"hareket (asıl soru), gün içi zirve/dip, toplam.\n"
        f"Ayrıca ardışık kaçıncı tavan olduğuna göre ayrı ayrı + normal "
        f"günlerle karşılaştırma (kontrol grubu).\n\n"
        f"⚠️ Bu deploy'da SADECE bu analiz çalışıyor.\n"
        f"Bitince CSV + özet göndereceğim."
    )
    try:
        dosya, sonuc = calistir()
        if dosya is None:
            send_telegram_message(f"📈 Analiz başarısız: {sonuc}")
            return
        send_telegram_document(dosya, caption=_rapor(sonuc))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"📈 Analiz hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] bist_tavan.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
