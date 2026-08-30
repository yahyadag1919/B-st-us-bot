"""
bist_ertesi_gun_saatleri.py — TAVAN OLMAYANLAR: ERTESİ GÜN SAAT SAAT NE OLUYOR?
=================================================================================
2026-08-28 — Kullanıcının haklı itirazı.

ÖNCEKİ TESTİN EKSİĞİ:
bist_guclu_kapanis.py şunu bulmuştu: %6-9.5 arası kapananlar ertesi gün
açılıştan sonra gün içinde ortalama %3.17 yükseliyor. Yani hareket VAR.
Ama bist_plan_testi.py "ertesi sabah +%2 hedefle sat" senaryosunu test
edip "kâr bırakmıyor" dedi.

BU İKİSİ ÇELİŞİYOR GİBİ - ve sebebi ölçmediğimiz bir şey:
    O %3.17'lik yükseliş GÜNÜN HANGİ SAATİNDE oluyor?
Eğer öğleden sonra oluyorsa, sabah koyduğun +%2 emri tutmadan önce
hisse düşüp seni zarara sokmuş olabilir. Zirveye ULAŞMA SAATİNİ hiç
ölçmedik - bu gerçek bir boşluktu.

BU DOSYA ONU ÖLÇÜYOR:
Tavan OLMADAN %6-9.5 arası kapanan hisselerin ERTESİ GÜNÜNÜ 15dk
barlarla saat saat takip ediyor:
  1. Açılıştan itibaren her saat dilimindeki ortalama getiri
  2. Gün içi ZİRVE hangi saatte oluşuyor (dağılım)
  3. Gün içi DİP hangi saatte oluşuyor
  4. ★ HER SAAT İÇİN: "o saatte satsaydım" net getirim ne olurdu
  5. ★ SABİT HEDEFLER: +%1 / +%1.5 / +%2 hedefler günün hangi
     saatinde tutuyor (tutuyorsa) - ve tutmadan önce ne kadar düşüyor

Böylece net göreceğiz: sabah erken çıkmak mı, öğlene kadar beklemek mi,
yoksa hiç girmemek mi doğru.

Start Command:  python bist_ertesi_gun_saatleri.py
Bu deploy'da SADECE bu analiz çalışır.
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
KOD_SURUMU = "ertesi-gun-saatleri-v1-2026-08-28"

TAVAN_ESIK = 9.5
BANT_ALT, BANT_UST = 6.0, 9.49
MALIYET_PCT = 0.30
HEDEFLER = [1.0, 1.5, 2.0]

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
        return yf.Ticker(ticker).history(period="60d", interval="15m", timeout=20)

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


def _saat_dilimi(bar_no):
    """Açılıştan itibaren bar numarası → saat dilimi adı."""
    if bar_no < 2:
        return "1) ilk 30 dk"
    if bar_no < 4:
        return "2) 30-60 dk"
    if bar_no < 8:
        return "3) 2. saat"
    if bar_no < 12:
        return "4) 3. saat"
    if bar_no < 16:
        return "5) 4. saat"
    if bar_no < 24:
        return "6) 5-6. saat"
    return "7) son saatler"


def calistir():
    olaylar = []
    islenen, atlanan = 0, 0

    for n_i, ticker in enumerate(BIST_HISSELER, 1):
        print(f"[Saatler {n_i}/{len(BIST_HISSELER)}] {ticker}...", flush=True)
        ham = _veri_cek(ticker)
        if ham is None or ham.empty or len(ham) < 30:
            atlanan += 1
            time.sleep(0.35)
            continue
        try:
            df = ham.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                      "Close": "close", "Volume": "volume"})
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            if len(df) < 30:
                atlanan += 1
                time.sleep(0.35)
                continue
            idx = pd.to_datetime(df.index)
            try:
                idx = idx.tz_localize(None)
            except (TypeError, AttributeError):
                pass
            df.index = idx
            df["gun"] = df.index.date

            gunler = sorted(set(df["gun"]))
            for gi in range(1, len(gunler) - 1):
                dun = df[df["gun"] == gunler[gi - 1]]
                bugun = df[df["gun"] == gunler[gi]]
                ertesi = df[df["gun"] == gunler[gi + 1]]
                if dun.empty or bugun.empty or len(ertesi) < 8:
                    continue
                dun_kap = float(dun.iloc[-1]["close"])
                bugun_kap = float(bugun.iloc[-1]["close"])
                if dun_kap <= 0 or bugun_kap <= 0:
                    continue
                getiri = (bugun_kap - dun_kap) / dun_kap * 100
                # SADECE tavan OLMAYAN guclu kapanislar
                if not (BANT_ALT <= getiri < BANT_UST):
                    continue

                alis = bugun_kap                      # aksam kapanista aldik
                e_acilis = float(ertesi.iloc[0]["open"])
                if e_acilis <= 0:
                    continue
                # veri hatasi filtresi (BIST limiti +-%10)
                if abs((e_acilis - alis) / alis * 100) > 12:
                    continue

                zirve_bar = int(np.argmax(ertesi["high"].values))
                dip_bar = int(np.argmin(ertesi["low"].values))

                kayit = {
                    "ticker": ticker, "tarih": str(gunler[gi]),
                    "kapanis_getirisi": round(getiri, 2),
                    "bosluk_pct": round((e_acilis - alis) / alis * 100, 3),
                    "zirve_bar": zirve_bar, "zirve_dilimi": _saat_dilimi(zirve_bar),
                    "zirve_saati": ertesi.index[zirve_bar].strftime("%H:%M"),
                    "dip_bar": dip_bar, "dip_dilimi": _saat_dilimi(dip_bar),
                    "gun_zirve_pct": round((float(ertesi["high"].max()) - alis) / alis * 100, 2),
                    "gun_dip_pct": round((float(ertesi["low"].min()) - alis) / alis * 100, 2),
                }
                # her saat diliminin SONUNDA satsaydik (alisa gore, NET)
                for sinir, ad in [(2, "ilk30dk"), (4, "60dk"), (8, "2saat"),
                                  (12, "3saat"), (16, "4saat"), (24, "6saat")]:
                    if len(ertesi) > min(sinir, len(ertesi)) - 1:
                        j = min(sinir, len(ertesi)) - 1
                        kayit[f"sat_{ad}_net"] = round(
                            (float(ertesi.iloc[j]["close"]) - alis) / alis * 100 - MALIYET_PCT, 3)
                kayit["sat_kapanis_net"] = round(
                    (float(ertesi.iloc[-1]["close"]) - alis) / alis * 100 - MALIYET_PCT, 3)

                # SABIT HEDEFLER: kacinci barda tuttu, tutmadan once ne kadar dustu
                for h in HEDEFLER:
                    hedef_f = alis * (1 + h / 100)
                    tuttu_bar = None
                    for j in range(len(ertesi)):
                        if float(ertesi.iloc[j]["high"]) >= hedef_f:
                            tuttu_bar = j
                            break
                    kayit[f"hedef{h}_tuttu"] = int(tuttu_bar is not None)
                    kayit[f"hedef{h}_bar"] = tuttu_bar if tuttu_bar is not None else None
                    kayit[f"hedef{h}_dilim"] = _saat_dilimi(tuttu_bar) if tuttu_bar is not None else None
                    if tuttu_bar is not None:
                        oncesi = ertesi.iloc[:tuttu_bar + 1]
                        kayit[f"hedef{h}_once_dip_pct"] = round(
                            (float(oncesi["low"].min()) - alis) / alis * 100, 2)
                        kayit[f"hedef{h}_net"] = round(h - MALIYET_PCT, 3)
                    else:
                        kayit[f"hedef{h}_once_dip_pct"] = round(
                            (float(ertesi["low"].min()) - alis) / alis * 100, 2)
                        kayit[f"hedef{h}_net"] = round(
                            (float(ertesi.iloc[-1]["close"]) - alis) / alis * 100 - MALIYET_PCT, 3)
                olaylar.append(kayit)
            islenen += 1
        except Exception as e:
            print(f"[Saatler] {ticker} hata: {e}", flush=True)
            atlanan += 1
        time.sleep(0.35)

    if not olaylar:
        return None, "Hiç olay üretilemedi."
    tum = pd.DataFrame(olaylar)
    dosya = os.path.join(DATA_DIR, "bist_ertesi_gun_saatleri.csv")
    tum.to_csv(dosya, index=False, encoding="utf-8-sig")

    sonuc = {"islenen": islenen, "n": len(tum),
             "ort_bosluk": round(float(tum.bosluk_pct.mean()), 3),
             "ort_gun_zirve": round(float(tum.gun_zirve_pct.mean()), 2),
             "ort_gun_dip": round(float(tum.gun_dip_pct.mean()), 2)}

    sirali = ["1) ilk 30 dk", "2) 30-60 dk", "3) 2. saat", "4) 3. saat",
              "5) 4. saat", "6) 5-6. saat", "7) son saatler"]
    sonuc["zirve_dagilimi"] = []
    for d in sirali:
        n = int((tum.zirve_dilimi == d).sum())
        if n:
            sonuc["zirve_dagilimi"].append({"ad": d, "n": n,
                                             "oran": round(n / len(tum) * 100, 1)})
    sonuc["dip_dagilimi"] = []
    for d in sirali:
        n = int((tum.dip_dilimi == d).sum())
        if n:
            sonuc["dip_dagilimi"].append({"ad": d, "n": n,
                                           "oran": round(n / len(tum) * 100, 1)})
    sonuc["satis_saatleri"] = []
    for ad, kol in [("ilk 30 dk sonunda", "sat_ilk30dk_net"),
                    ("1 saat sonunda", "sat_60dk_net"),
                    ("2 saat sonunda", "sat_2saat_net"),
                    ("3 saat sonunda", "sat_3saat_net"),
                    ("4 saat sonunda", "sat_4saat_net"),
                    ("6 saat sonunda", "sat_6saat_net"),
                    ("kapanışta", "sat_kapanis_net")]:
        if kol in tum and tum[kol].notna().sum() >= 30:
            s = tum[kol].dropna()
            sonuc["satis_saatleri"].append({
                "ad": ad, "n": len(s), "net": round(float(s.mean()), 3),
                "kazanma": round(float((s > 0).mean() * 100), 1)})
    sonuc["hedefler"] = []
    for h in HEDEFLER:
        tk, nk, dk = f"hedef{h}_tuttu", f"hedef{h}_net", f"hedef{h}_once_dip_pct"
        if tk not in tum:
            continue
        tutan = tum[tum[tk] == 1]
        sonuc["hedefler"].append({
            "hedef": h, "tutma_orani": round(float(tum[tk].mean() * 100), 1),
            "net": round(float(tum[nk].mean()), 3),
            "kazanma": round(float((tum[nk] > 0).mean() * 100), 1),
            "medyan_bar": float(tutan[f"hedef{h}_bar"].median()) if len(tutan) else None,
            "en_sik_dilim": tutan[f"hedef{h}_dilim"].mode().iloc[0] if len(tutan) else None,
            "once_dip": round(float(tum[dk].mean()), 2)})
    return dosya, sonuc


def _rapor(o):
    s = [f"⏰ TAVAN OLMAYANLAR: ERTESİ GÜN SAAT SAAT — {KOD_SURUMU}",
         f"İşlenen: {o['islenen']} hisse | Olay: {o['n']}",
         f"Ortalama açılış boşluğu: %{o['ort_bosluk']}",
         f"Gün içi zirve: %{o['ort_gun_zirve']} | Gün içi dip: %{o['ort_gun_dip']}\n",
         "🔺 ZİRVE HANGİ SAATTE OLUŞUYOR:"]
    for z in o["zirve_dagilimi"]:
        s.append(f"   {z['ad']:<16} {z['oran']:>5.1f}%  (n={z['n']})")
    s.append("\n🔻 DİP HANGİ SAATTE OLUŞUYOR:")
    for z in o["dip_dagilimi"]:
        s.append(f"   {z['ad']:<16} {z['oran']:>5.1f}%  (n={z['n']})")
    s.append("\n💰 O SAATTE SATSAYDIM (net, maliyet düşülmüş):")
    for x in o["satis_saatleri"]:
        s.append(f"   {x['ad']:<20} net %{x['net']:>7.3f}  kazanma %{x['kazanma']}")
    s.append("\n🎯 SABİT HEDEFLER:")
    for h in o["hedefler"]:
        s.append(f"   +%{h['hedef']}: tutma %{h['tutma_orani']}, NET %{h['net']}, "
                 f"kazanma %{h['kazanma']}")
        if h["en_sik_dilim"]:
            s.append(f"       en sık {h['en_sik_dilim']}'de tutuyor "
                     f"(medyan {h['medyan_bar']:.0f}. bar)")
        s.append(f"       tutmadan önce ortalama dip: %{h['once_dip']}")
    s.append("\n⚠️ ASIL SORU: 'O saatte satsaydım' satırlarından herhangi biri "
             "POZİTİF mi? Pozitifse o saatte çıkmak kâr bırakıyor demektir - "
             "önceki testte sadece +%2 hedefini denemiştik, saat bazlı "
             "çıkışı hiç ölçmemiştik.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (ertesi gun saatleri)", 200


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
        f"⏰ ERTESİ GÜN SAAT SAAT ANALİZİ başlıyor — {KOD_SURUMU}\n\n"
        f"Senin itirazın test ediliyor: tavan olmayanlar sabah gapsız "
        f"açıyor ama sonradan yükseliyordu - peki bu yükseliş SAAT KAÇTA?\n\n"
        f"Önceki testte sadece '+%2 hedefle sat' senaryosunu denemiştik, "
        f"yükselişin saatini hiç ölçmemiştik. Bu gerçek bir boşluktu.\n\n"
        f"Ölçülecek: zirve/dip hangi saatte oluşuyor, her saat diliminde "
        f"satsan net getirin ne olurdu, sabit hedefler günün hangi "
        f"saatinde tutuyor ve tutmadan önce ne kadar düşüyor.\n\n"
        f"{len(BIST_HISSELER)} hisse × 60 günlük 15dk verisi.\n"
        f"⚠️ Bu deploy'da SADECE bu analiz çalışıyor.\nBitince CSV + özet göndereceğim."
    )
    try:
        dosya, sonuc = calistir()
        if dosya is None:
            send_telegram_message(f"⏰ Analiz başarısız: {sonuc}")
            return
        send_telegram_document(dosya, caption=_rapor(sonuc))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"⏰ Analiz hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] bist_ertesi_gun_saatleri.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
