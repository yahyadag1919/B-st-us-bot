"""
bist_guclu_kapanis.py — %6-9.5 ARASI KAPANANLAR: HANGİLERİ ERTESİ GÜN İYİ AÇIYOR?
===================================================================================
2026-08-28 — Kullanıcının gözlemi: "ben bir ara baktım, %8 civarında
olanlar bile sabah güzel başladı."

BU, ÖNCEKİ BULGUMUZLA ÇELİŞİYOR. Önceki test (bist_tavana_yakin.py):
    %8-9.5 kapananlar → ertesi gün boşluk sadece +%0.39, yukarı açma %48.1
    TAM TAVAN         → +%2.42, yukarı açma %81.1
Sonuç: "etki sadece tavana özgü" demiştik.

AMA O TESTİN İKİ ZAYIFLIĞI VARDI - kullanıcının itirazı haklı olabilir:
  1. %8-9.5 diliminde sadece n=77 örnek vardı (küçük).
  2. Sadece ORTALAMAYA baktık. Ortalama alt grupları GİZLER: belki
     %8'lerin bir kısmı gerçekten iyi açıyor, bir kısmı kötü, ortalama
     ikisini birbirine karıştırıp sıfır gösteriyor.
  3. Sadece AÇILIŞ BOŞLUĞUNU ölçtük. Kullanıcının "güzel başladı"
     dediği şey, açıldıktan SONRA ilk saatlerde yükselmek olabilir -
     onu hiç ölçmedik.

BU DOSYA ÜÇÜNÜ DE DÜZELTİYOR:
  • Daha büyük örneklem (daha çok hisse + 2 yıl)
  • ÜÇ ayrı ölçüm: (a) açılış boşluğu, (b) açılıştan gün içi ZİRVEYE
    (satış fırsatı var mıydı), (c) açılıştan kapanışa
  • ALT GRUPLARA AYIRMA - asıl mesele bu. %6-9.5 arası kapananları
    şu özelliklere göre bölüp her grubu ayrı ölçüyor:
      - Günün ZİRVESİNDE mi kapandı, yoksa geriledi mi?
      - Hacim patlaması var mıydı?
      - Gün içinde TAVANA DEĞİP geri mi düştü? (çok önemli olabilir -
        talep var ama kilitlenememiş demek)
      - Üst üste kaçıncı yükselen gün?
      - Son bölümde mi hızlandı?

Start Command:  python bist_guclu_kapanis.py
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
KOD_SURUMU = "guclu-kapanis-v1-2026-08-28"

ALT_BANT, UST_BANT = 6.0, 9.49   # incelenen dilim (tavan DEGIL)
TAVAN_ESIK = 9.5

BIST_HISSELER = [
    "THYAO.IS", "GARAN.IS", "AKBNK.IS", "ISCTR.IS", "YKBNK.IS", "VAKBN.IS",
    "HALKB.IS", "SISE.IS", "EREGL.IS", "KRDMD.IS", "TUPRS.IS", "PETKM.IS",
    "ASELS.IS", "TCELL.IS", "TTKOM.IS", "BIMAS.IS", "MGROS.IS", "SOKM.IS",
    "FROTO.IS", "TOASO.IS", "ARCLK.IS", "VESTL.IS", "TAVHL.IS", "PGSUS.IS",
    "KCHOL.IS", "SAHOL.IS", "DOHOL.IS", "ALARK.IS", "ENKAI.IS", "TKFEN.IS",
    "KOZAA.IS", "ODAS.IS", "ZOREN.IS", "AKSEN.IS", "EKGYO.IS", "ISGYO.IS",
    "TRGYO.IS", "HEKTS.IS", "SASA.IS", "GUBRF.IS", "AEFES.IS", "ULKER.IS",
    "CCOLA.IS", "TATGD.IS", "BANVT.IS", "PENTA.IS", "SMRTG.IS", "ALFAS.IS",
    "ASTOR.IS", "EUPWR.IS", "CWENE.IS", "GESAN.IS", "KONTR.IS", "ISDMR.IS",
    "CIMSA.IS", "AKCNS.IS", "OYAKC.IS", "BRSAN.IS", "AGHOL.IS", "AKFGY.IS",
    "ALBRK.IS", "ANSGR.IS", "ARDYZ.IS", "AYDEM.IS", "BERA.IS", "BIOEN.IS",
    "BRYAT.IS", "BUCIM.IS", "CANTE.IS", "CEMTS.IS", "DOAS.IS", "ECILC.IS",
    "EGEEN.IS", "ENJSA.IS", "ESEN.IS", "EUREN.IS", "GENIL.IS", "GLYHO.IS",
    "GWIND.IS", "HATSN.IS", "IEYHO.IS", "IZMDC.IS", "KARSN.IS", "KAYSE.IS",
    "KLSER.IS", "KORDS.IS", "KRDMA.IS", "MAVI.IS", "MPARK.IS", "NTHOL.IS",
    "OTKAR.IS", "PAPIL.IS", "QUAGR.IS", "SELEC.IS", "SKBNK.IS", "SNGYO.IS",
    "TMSN.IS", "TSKB.IS", "TTRAK.IS", "TURSG.IS", "ULUUN.IS", "VESBE.IS",
    "YATAS.IS", "YEOTK.IS", "YYLGD.IS", "ZRGYO.IS", "MAALT.IS", "PSDTC.IS",
    "PRZMA.IS", "AVOD.IS", "BEYAZ.IS", "BURVA.IS", "DAGI.IS", "DERIM.IS",
    "DGATE.IS", "DITAS.IS", "EGEPO.IS", "EMKEL.IS", "ERSU.IS", "FONET.IS",
    "GEDIK.IS", "INTEM.IS", "KAPLM.IS", "KRSTL.IS", "KUTPO.IS", "LINK.IS",
    "MERKO.IS", "MTRKS.IS", "NIBAS.IS", "ORCAY.IS", "PKENT.IS", "SANFM.IS",
    "SEKUR.IS", "TEKTU.IS", "ULAS.IS", "VANGD.IS", "YAYLA.IS", "MEGAP.IS",
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
        print(f"[Güçlü Kapanış {n_i}/{len(BIST_HISSELER)}] {ticker}...", flush=True)
        ham = _veri_cek(ticker)
        if ham is None or ham.empty or len(ham) < 60:
            atlanan += 1
            time.sleep(0.35)
            continue
        try:
            df = ham.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                      "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            if len(df) < 60:
                atlanan += 1
                time.sleep(0.35)
                continue
            df = df.reset_index(drop=True)
            df["getiri"] = df["close"].pct_change() * 100
            df["hacim_ort20"] = df["volume"].rolling(20).mean()

            # ardisik yukselen gun sayaci
            ardisik, ardisik_liste = 0, []
            for g in df["getiri"]:
                ardisik = ardisik + 1 if (pd.notna(g) and g > 0) else 0
                ardisik_liste.append(ardisik)
            df["ardisik_yukselen"] = ardisik_liste

            for i in range(21, len(df) - 1):
                bugun, ertesi = df.iloc[i], df.iloc[i + 1]
                onceki_kapanis = df.iloc[i - 1]["close"]
                if bugun["close"] <= 0 or ertesi["open"] <= 0 or onceki_kapanis <= 0:
                    continue
                g = bugun["getiri"]
                if pd.isna(g) or not (ALT_BANT <= g < UST_BANT):
                    continue

                aralik = bugun["high"] - bugun["low"]
                kapanis_konumu = (bugun["close"] - bugun["low"]) / aralik if aralik > 0 else None
                hacim_orani = (bugun["volume"] / bugun["hacim_ort20"]) if bugun["hacim_ort20"] > 0 else None
                # gun icinde TAVANA DEGDI MI ama kapanista altinda kaldi mi?
                tavana_degdi = int((bugun["high"] - onceki_kapanis) / onceki_kapanis * 100 >= TAVAN_ESIK)

                kayitlar.append({
                    "ticker": ticker, "getiri_pct": round(float(g), 2),
                    "kapanis_konumu": round(float(kapanis_konumu), 3) if kapanis_konumu is not None else None,
                    "hacim_orani": round(float(hacim_orani), 2) if hacim_orani is not None else None,
                    "tavana_degdi": tavana_degdi,
                    "ardisik_yukselen": int(bugun["ardisik_yukselen"]),
                    # --- 3 AYRI OLCUM ---
                    "bosluk_pct": round(float((ertesi["open"] - bugun["close"]) / bugun["close"] * 100), 3),
                    "acilis_zirve_pct": round(float((ertesi["high"] - ertesi["open"]) / ertesi["open"] * 100), 3),
                    "acilis_kapanis_pct": round(float((ertesi["close"] - ertesi["open"]) / ertesi["open"] * 100), 3),
                    "toplam_pct": round(float((ertesi["close"] - bugun["close"]) / bugun["close"] * 100), 3),
                })
            islenen += 1
        except Exception as e:
            print(f"[Güçlü Kapanış] {ticker} hata: {e}", flush=True)
            atlanan += 1
        time.sleep(0.35)

    if not kayitlar:
        return None, "Hiç kayıt üretilemedi."
    tum = pd.DataFrame(kayitlar)
    dosya = os.path.join(DATA_DIR, "bist_guclu_kapanis.csv")
    tum.to_csv(dosya, index=False, encoding="utf-8-sig")

    def _ozet(g, ad):
        if len(g) < 15:
            return None
        return {"ad": ad, "n": len(g),
                "bosluk_ort": round(float(g.bosluk_pct.mean()), 3),
                "bosluk_poz_pct": round(float((g.bosluk_pct > 0).mean() * 100), 1),
                "acilis_zirve_ort": round(float(g.acilis_zirve_pct.mean()), 3),
                "acilis_kapanis_ort": round(float(g.acilis_kapanis_pct.mean()), 3),
                "toplam_ort": round(float(g.toplam_pct.mean()), 3)}

    sonuc = {"islenen": islenen, "atlanan": atlanan, "toplam": len(tum), "gruplar": []}
    sonuc["gruplar"].append(_ozet(tum, "TÜMÜ (%6-9.5 arası)"))

    # ALT GRUPLAR - asil mesele bu
    sonuc["gruplar"].append(_ozet(tum[tum.kapanis_konumu >= 0.9], "Zirvede kapandı (≥0.9)"))
    sonuc["gruplar"].append(_ozet(tum[tum.kapanis_konumu < 0.5], "Geriledi (<0.5)"))
    sonuc["gruplar"].append(_ozet(tum[tum.tavana_degdi == 1], "Gün içi TAVANA DEĞDİ, altında kapandı"))
    sonuc["gruplar"].append(_ozet(tum[tum.tavana_degdi == 0], "Tavana hiç değmedi"))
    sonuc["gruplar"].append(_ozet(tum[tum.hacim_orani >= 3], "Hacim ≥3x"))
    sonuc["gruplar"].append(_ozet(tum[(tum.hacim_orani >= 1.5) & (tum.hacim_orani < 3)], "Hacim 1.5-3x"))
    sonuc["gruplar"].append(_ozet(tum[tum.hacim_orani < 1.5], "Hacim <1.5x"))
    sonuc["gruplar"].append(_ozet(tum[tum.ardisik_yukselen >= 3], "3+ gündür üst üste yükseliyor"))
    sonuc["gruplar"].append(_ozet(tum[tum.ardisik_yukselen == 1], "İlk yükseliş günü"))
    # EN GUCLU KOMBINASYON
    sonuc["gruplar"].append(_ozet(
        tum[(tum.kapanis_konumu >= 0.9) & (tum.hacim_orani >= 2) & (tum.tavana_degdi == 1)],
        "★ Zirvede kapandı + hacim ≥2x + tavana değdi"))
    sonuc["gruplar"] = [g for g in sonuc["gruplar"] if g]

    # istatistiksel test: zirvede kapananlar vs gerileyenler
    a = tum[tum.kapanis_konumu >= 0.9].bosluk_pct.dropna()
    b = tum[tum.kapanis_konumu < 0.5].bosluk_pct.dropna()
    if len(a) >= 20 and len(b) >= 20:
        try:
            _, pv = _stats.mannwhitneyu(a, b, alternative="two-sided")
            sonuc["kapanis_konumu_p"] = float(pv)
        except Exception:
            pass
    return dosya, sonuc


def _rapor(o):
    s = [f"📊 %{ALT_BANT}-{UST_BANT} ARASI KAPANANLAR (TAVAN DEĞİL) — {KOD_SURUMU}",
         f"İşlenen: {o['islenen']} hisse | Örnek: {o['toplam']} gün\n",
         "Kıyas için: TAM TAVAN yapanlar → boşluk +%2.42, yukarı açma %81\n",
         f"{'grup':<42}{'n':>6}{'boşluk':>9}{'poz%':>7}{'aç→zirve':>10}{'aç→kapanış':>12}"]
    for g in o["gruplar"]:
        s.append(f"{g['ad']:<42}{g['n']:>6}{g['bosluk_ort']:>8.2f}%{g['bosluk_poz_pct']:>6.1f}"
                 f"{g['acilis_zirve_ort']:>9.2f}%{g['acilis_kapanis_ort']:>11.2f}%")
    if o.get("kapanis_konumu_p") is not None:
        s.append(f"\nZirvede kapananlar vs gerileyenler (boşluk farkı): p={o['kapanis_konumu_p']:.2e}")
    s.append("\n⚠️ NASIL OKUNMALI:\n"
             "  'boşluk' = ertesi sabah ne kadar yukarıdan açtı\n"
             "  'aç→zirve' = açıldıktan sonra gün içinde en fazla ne kadar yükseldi\n"
             "     (senin 'sabah güzel başladı' gözlemin BURADA görünür)\n"
             "  Bir alt grup, TÜMÜ satırından belirgin iyiyse → o filtre işe yarıyor.\n"
             "  Hepsi birbirine benziyorsa → önceki bulgu doğruydu, etki tavana özgü.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (guclu kapanis)", 200


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
        f"📊 GÜÇLÜ KAPANIŞ ANALİZİ başlıyor — {KOD_SURUMU}\n\n"
        f"Senin gözlemini test ediyoruz: '%8 civarında olanlar bile sabah "
        f"güzel başladı.'\n\n"
        f"Önceki test %8-9.5 için sadece +%0.39 boşluk demişti, ama o testin "
        f"3 zayıflığı vardı: küçük örneklem (n=77), sadece ortalamaya bakması, "
        f"ve sadece açılış boşluğunu ölçmesi.\n\n"
        f"Bu sefer: {len(BIST_HISSELER)} hisse × 2 yıl, ÜÇ ayrı ölçüm "
        f"(boşluk / açılıştan zirveye / açılıştan kapanışa) ve ALT GRUPLARA "
        f"ayırma (zirvede mi kapandı, hacim, gün içi tavana değdi mi, "
        f"ardışık yükseliş).\n\n"
        f"⚠️ Bu deploy'da SADECE bu analiz çalışıyor.\nBitince CSV + özet göndereceğim."
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
    print(f"[BAŞLANGIÇ] bist_guclu_kapanis.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
