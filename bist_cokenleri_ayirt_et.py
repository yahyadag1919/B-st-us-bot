"""
bist_cokenleri_ayirt_et.py — ERTESİ GÜN SERT DÜŞENLERİ ÖNCEDEN AYIRT EDEBİLİR MİYİZ?
======================================================================================
2026-08-31 — Kullanıcının sorusu, ve şimdiye kadar HİÇ sormadığımız açı.

DURUM: %6-9.5 arası kapanan hisseler ertesi gün hem SERT YÜKSELİYOR
(ortalama gün içi zirve +%3.79) hem SERT DÜŞÜYOR (ortalama dip -%2.85).
İkisi birbirini götürdüğü için kenar ince kalıyor (+%0.28).

ŞİMDİYE KADAR HEP "hangisi yükselecek" diye baktık.
BU DOSYA TERSİNİ SORUYOR: "hangisi ÇÖKECEK, önceden belli mi?"

Eğer çökenleri ayırt edebilirsek, onları ELEYEREK kalan grubun
ortalamasını yükseltebiliriz. Bu, kâr aramaktan çok ZARARDAN KAÇINMA
yaklaşımı - kullanıcının para yönetimi mantığına da uyuyor.

YÖNTEM:
  Ertesi günü sonucuna göre üç gruba ayır:
    ÇÖKEN   : ertesi gün dibi <= -%3 (sert düşüş yaşadı)
    NORMAL  : arada
    PATLAYAN: ertesi gün zirvesi >= +%3 VE dibi > -%3 (temiz yükseliş)
  Sonra ÖNCEKİ GÜNÜN özelliklerini karşılaştır - çökenler farklı
  görünüyor muydu?

ÖLÇÜLEN ÖNCEKİ GÜN ÖZELLİKLERİ (hepsi o gün kapanışında bilinebilir):
  • Kapanış konumu (zirvede mi, gerilemiş mi)
  • Hacim oranı
  • Gün içi tavana değip düştü mü
  • Üst üste kaçıncı yükselen gün
  • Getiri seviyesi
  • Gün içi aralık genişliği (sert dalgalanma mı, sakin yükseliş mi)
  • O günkü açılış boşluğu
  • Son 5 günlük toplam getiri (çok mu koştu)
  • Fiyat seviyesi

DÜRÜST BEKLENTİ: Bu ayrım kolay olmayabilir. Önceki testlerde
filtrelerin çoğu işe yaramadı. Ama bu soruyu DOĞRUDAN hiç sormadık -
denemeye değer. Kontrol grubu ve p-değeri ile bakılacak, tesadüfi
farkları gerçek sanmayalım.

Start Command:  python bist_cokenleri_ayirt_et.py
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
KOD_SURUMU = "cokenleri-ayirt-v1-2026-08-31"

TAVAN_ESIK = 9.5
BANT_ALT, BANT_UST = 6.0, 9.49
COKME_ESIK = -3.0      # ertesi gun dibi bunun altindaysa COKEN
PATLAMA_ESIK = 3.0     # ertesi gun zirvesi bunun ustundeyse PATLAYAN

BIST_HISSELER = [
    "THYAO.IS", "GARAN.IS", "AKBNK.IS", "ISCTR.IS", "YKBNK.IS", "VAKBN.IS",
    "HALKB.IS", "SISE.IS", "EREGL.IS", "KRDMD.IS", "KRDMA.IS", "TUPRS.IS",
    "PETKM.IS", "ASELS.IS", "TCELL.IS", "TTKOM.IS", "BIMAS.IS", "MGROS.IS",
    "SOKM.IS", "FROTO.IS", "TOASO.IS", "ARCLK.IS", "VESTL.IS", "TAVHL.IS",
    "PGSUS.IS", "KCHOL.IS", "SAHOL.IS", "DOHOL.IS", "ALARK.IS", "ENKAI.IS",
    "TKFEN.IS", "KOZAA.IS", "ODAS.IS", "ZOREN.IS", "AKSEN.IS", "EKGYO.IS",
    "ISGYO.IS", "TRGYO.IS", "HEKTS.IS", "SASA.IS", "GUBRF.IS", "AEFES.IS",
    "ULKER.IS", "CCOLA.IS", "TATGD.IS", "BANVT.IS", "PENTA.IS", "SMRTG.IS",
    "ALFAS.IS", "ASTOR.IS", "EUPWR.IS", "CWENE.IS", "GESAN.IS", "KONTR.IS",
    "ISDMR.IS", "CIMSA.IS", "AKCNS.IS", "OYAKC.IS", "BRSAN.IS", "AGHOL.IS",
    "AKFGY.IS", "ALBRK.IS", "ANSGR.IS", "ARDYZ.IS", "AYDEM.IS", "BERA.IS",
    "BIOEN.IS", "BRYAT.IS", "BUCIM.IS", "CANTE.IS", "CEMTS.IS", "DOAS.IS",
    "ECILC.IS", "EGEEN.IS", "ENJSA.IS", "ESEN.IS", "EUREN.IS", "GENIL.IS",
    "GLYHO.IS", "GWIND.IS", "HATSN.IS", "IEYHO.IS", "IZMDC.IS", "KARSN.IS",
    "KAYSE.IS", "KLSER.IS", "KORDS.IS", "MAVI.IS", "MPARK.IS", "NTHOL.IS",
    "OTKAR.IS", "PAPIL.IS", "QUAGR.IS", "SELEC.IS", "SKBNK.IS", "SNGYO.IS",
    "TMSN.IS", "TSKB.IS", "TTRAK.IS", "TURSG.IS", "ULUUN.IS", "VESBE.IS",
    "YATAS.IS", "YEOTK.IS", "YYLGD.IS", "ZRGYO.IS", "MAALT.IS", "PSDTC.IS",
    "PRZMA.IS", "AVOD.IS", "BEYAZ.IS", "BURVA.IS", "DAGI.IS", "DERIM.IS",
    "DGATE.IS", "DITAS.IS", "EGEPO.IS", "EMKEL.IS", "ERSU.IS", "FONET.IS",
    "GEDIK.IS", "INTEM.IS", "KAPLM.IS", "KRSTL.IS", "KUTPO.IS", "LINK.IS",
    "MERKO.IS", "MTRKS.IS", "NIBAS.IS", "ORCAY.IS", "PKENT.IS", "SANFM.IS",
    "SEKUR.IS", "TEKTU.IS", "ULAS.IS", "VANGD.IS", "YAYLA.IS", "MEGAP.IS",
    "DEVA.IS", "ADEL.IS", "ALCTL.IS", "BFREN.IS", "ISATR.IS", "KRDMB.IS",
    "AKSA.IS", "ASUZU.IS", "BAGFS.IS", "KARTN.IS", "KATMR.IS", "KLMSN.IS",
    "KONYA.IS", "LOGO.IS", "NUHCM.IS", "PARSN.IS", "SARKY.IS", "TUKAS.IS",
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
        print(f"[Çökenler {n_i}/{len(BIST_HISSELER)}] {ticker}...", flush=True)
        ham = _veri_cek(ticker)
        if ham is None or ham.empty or len(ham) < 40:
            atlanan += 1
            time.sleep(0.35)
            continue
        try:
            df = ham.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                      "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].dropna().reset_index(drop=True)
            if len(df) < 40:
                atlanan += 1
                time.sleep(0.35)
                continue
            df["getiri"] = df["close"].pct_change() * 100
            df["hacim_ort20"] = df["volume"].rolling(20).mean()
            ardisik, liste = 0, []
            for g in df["getiri"]:
                ardisik = ardisik + 1 if (pd.notna(g) and g > 0) else 0
                liste.append(ardisik)
            df["ardisik"] = liste

            for i in range(21, len(df) - 1):
                bugun, ertesi = df.iloc[i], df.iloc[i + 1]
                onceki_kap = float(df.iloc[i - 1]["close"])
                g = bugun["getiri"]
                if pd.isna(g) or not (BANT_ALT <= g < BANT_UST):
                    continue
                alis = float(bugun["close"])
                if alis <= 0 or ertesi["open"] <= 0 or onceki_kap <= 0:
                    continue
                # veri hatasi filtresi (BIST limiti +-%10)
                if abs((ertesi["open"] - alis) / alis * 100) > 12:
                    continue

                e_dip = (float(ertesi["low"]) - alis) / alis * 100
                e_zirve = (float(ertesi["high"]) - alis) / alis * 100
                if e_dip <= COKME_ESIK:
                    etiket = "ÇÖKEN"
                elif e_zirve >= PATLAMA_ESIK:
                    etiket = "PATLAYAN"
                else:
                    etiket = "NORMAL"

                aralik = float(bugun["high"] - bugun["low"])
                bes_gun_once = float(df.iloc[max(0, i - 5)]["close"])
                kayitlar.append({
                    "ticker": ticker, "etiket": etiket,
                    # --- ONCEKI GUN OZELLIKLERI (kapanista bilinebilir) ---
                    "getiri_pct": round(float(g), 2),
                    "kapanis_konumu": round((alis - float(bugun["low"])) / aralik, 3) if aralik > 0 else None,
                    "hacim_orani": round(float(bugun["volume"] / bugun["hacim_ort20"]), 2) if bugun["hacim_ort20"] > 0 else None,
                    "tavana_degdi": int((float(bugun["high"]) - onceki_kap) / onceki_kap * 100 >= TAVAN_ESIK),
                    "ardisik_yukselen": int(bugun["ardisik"]),
                    "gun_araligi_pct": round(aralik / alis * 100, 2),
                    "acilis_boslugu_pct": round((float(bugun["open"]) - onceki_kap) / onceki_kap * 100, 2),
                    "son5gun_getiri_pct": round((alis - bes_gun_once) / bes_gun_once * 100, 2) if bes_gun_once > 0 else None,
                    "fiyat": round(alis, 2),
                    # --- ERTESI GUN SONUCU ---
                    "ertesi_dip_pct": round(e_dip, 2),
                    "ertesi_zirve_pct": round(e_zirve, 2),
                })
            islenen += 1
        except Exception as e:
            print(f"[Çökenler] {ticker} hata: {e}", flush=True)
            atlanan += 1
        time.sleep(0.35)

    if not kayitlar:
        return None, "Hiç kayıt üretilemedi."
    tum = pd.DataFrame(kayitlar)
    dosya = os.path.join(DATA_DIR, "bist_cokenler.csv")
    tum.to_csv(dosya, index=False, encoding="utf-8-sig")

    cok = tum[tum.etiket == "ÇÖKEN"]
    pat = tum[tum.etiket == "PATLAYAN"]
    sonuc = {"islenen": islenen, "toplam": len(tum),
             "coken": len(cok), "patlayan": len(pat),
             "normal": int((tum.etiket == "NORMAL").sum()),
             "karsilastirma": [], "filtreler": []}

    kolonlar = ["getiri_pct", "kapanis_konumu", "hacim_orani", "tavana_degdi",
                "ardisik_yukselen", "gun_araligi_pct", "acilis_boslugu_pct",
                "son5gun_getiri_pct", "fiyat"]
    for kol in kolonlar:
        a, b = cok[kol].dropna(), pat[kol].dropna()
        if len(a) < 20 or len(b) < 20:
            continue
        try:
            _, pv = _stats.mannwhitneyu(a, b, alternative="two-sided")
        except Exception:
            continue
        sonuc["karsilastirma"].append({
            "ozellik": kol, "coken": round(float(a.median()), 3),
            "patlayan": round(float(b.median()), 3), "p": float(pv)})
    sonuc["karsilastirma"].sort(key=lambda x: x["p"])

    # ELEME DENEMESI: bir filtre uygulanirsa cokme orani duser mi
    taban_cokme = float((tum.etiket == "ÇÖKEN").mean() * 100)
    sonuc["taban_cokme"] = round(taban_cokme, 1)
    denemeler = [
        ("Zirvede kapandı (≥0.9)", tum[tum.kapanis_konumu >= 0.9]),
        ("Zirveden gerilemiş (<0.5)", tum[tum.kapanis_konumu < 0.5]),
        ("Hacim ≥3x", tum[tum.hacim_orani >= 3]),
        ("Hacim <1.5x", tum[tum.hacim_orani < 1.5]),
        ("Gün içi tavana değdi", tum[tum.tavana_degdi == 1]),
        ("İlk yükseliş günü", tum[tum.ardisik_yukselen == 1]),
        ("3+ gün üst üste", tum[tum.ardisik_yukselen >= 3]),
        ("Gün aralığı dar (<%5)", tum[tum.gun_araligi_pct < 5]),
        ("Gün aralığı geniş (≥%9)", tum[tum.gun_araligi_pct >= 9]),
        ("Son 5 gün <%15", tum[tum.son5gun_getiri_pct < 15]),
        ("Son 5 gün ≥%25 (çok koşmuş)", tum[tum.son5gun_getiri_pct >= 25]),
        ("★ Zirvede + hacim≥2x", tum[(tum.kapanis_konumu >= 0.9) & (tum.hacim_orani >= 2)]),
        ("★ Zirvede + son5gün<%20", tum[(tum.kapanis_konumu >= 0.9) & (tum.son5gun_getiri_pct < 20)]),
    ]
    for ad, alt in denemeler:
        if len(alt) < 50:
            continue
        sonuc["filtreler"].append({
            "ad": ad, "n": len(alt),
            "cokme": round(float((alt.etiket == "ÇÖKEN").mean() * 100), 1),
            "patlama": round(float((alt.etiket == "PATLAYAN").mean() * 100), 1),
            "ort_dip": round(float(alt.ertesi_dip_pct.mean()), 2),
        })
    sonuc["filtreler"].sort(key=lambda x: x["cokme"])
    return dosya, sonuc


def _rapor(o):
    s = [f"💥 ERTESİ GÜN ÇÖKENLERİ AYIRT EDEBİLİR MİYİZ? — {KOD_SURUMU}",
         f"İşlenen: {o['islenen']} hisse | Toplam olay: {o['toplam']}",
         f"ÇÖKEN (dip ≤%{COKME_ESIK}): {o['coken']} | "
         f"PATLAYAN (zirve ≥+%{PATLAMA_ESIK}): {o['patlayan']} | NORMAL: {o['normal']}",
         f"Taban çökme oranı: %{o['taban_cokme']}\n",
         "ÖNCEKİ GÜN ÖZELLİKLERİ — ÇÖKEN vs PATLAYAN (medyan):",
         f"{'özellik':<22}{'ÇÖKEN':>10}{'PATLAYAN':>11}{'p-değeri':>12}"]
    for k in o["karsilastirma"]:
        s.append(f"{k['ozellik']:<22}{k['coken']:>10.3f}{k['patlayan']:>11.3f}{k['p']:>12.2e}")
    s.append(f"\nFİLTRE DENEMELERİ (çökme oranı en düşükten sıralı, taban %{o['taban_cokme']}):")
    s.append(f"{'filtre':<30}{'n':>6}{'çökme':>8}{'patlama':>9}{'ort dip':>9}")
    for f in o["filtreler"]:
        s.append(f"{f['ad'][:29]:<30}{f['n']:>6}{f['cokme']:>7.1f}%{f['patlama']:>8.1f}%{f['ort_dip']:>8.2f}%")
    s.append("\n⚠️ NASIL OKUNMALI:\n"
             f"  Bir filtrenin çökme oranı tabandan (%{o['taban_cokme']}) BELİRGİN "
             "düşükse, o filtre riski azaltıyor demektir.\n"
             "  p-değeri küçük olan özellikler, çöken ve patlayanı gerçekten "
             "ayırıyor demektir.\n"
             "  Hepsi tabana yakınsa → çökenler önceden ayırt edilemiyor, "
             "bunu dürüstçe kabul ederiz.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (cokenleri ayirt et)", 200


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
        f"💥 ÇÖKENLERİ AYIRT ETME ANALİZİ başlıyor — {KOD_SURUMU}\n\n"
        f"Senin sorun: '%6-9.5 kapananlar hem sert yükseliyor hem sert "
        f"düşüyor - düşenleri nasıl tespit ederiz?'\n\n"
        f"Şimdiye kadar hep 'hangisi yükselecek' diye baktık, bu soruyu "
        f"HİÇ sormadık.\n\n"
        f"Ertesi günü sonucuna göre ÇÖKEN (dip ≤%{COKME_ESIK}) ve PATLAYAN "
        f"(zirve ≥+%{PATLAMA_ESIK}) diye ayırıp, ÖNCEKİ GÜNÜN özelliklerini "
        f"karşılaştırıyoruz: kapanış konumu, hacim, gün aralığı, ardışık "
        f"yükseliş, son 5 gün getirisi, açılış boşluğu.\n\n"
        f"{len(BIST_HISSELER)} hisse × 2 yıl.\n"
        f"⚠️ Bu deploy'da SADECE bu analiz çalışıyor.\nBitince CSV + özet göndereceğim."
    )
    try:
        dosya, sonuc = calistir()
        if dosya is None:
            send_telegram_message(f"💥 Analiz başarısız: {sonuc}")
            return
        send_telegram_document(dosya, caption=_rapor(sonuc))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"💥 Analiz hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] bist_cokenleri_ayirt_et.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
