"""
bist_tavan_kilitlenme.py — RADAR SİNYALİ TAVANA KİLİTLENİYOR MU?
==================================================================
2026-08-28 — Önceki testlerin sonucu netti:
    TAVAN olarak kapananlar → ertesi gün +%2.51 net (%90 kazanma) ✅
    %6-9.5 kapananlar       → ~%0.05 net, yani sıfır ❌

Yani tüm değer, hissenin KAPANIŞTA TAVANA KİLİTLENMESİNDE.

BU DOSYANIN CEVAPLADIĞI SORU:
    Radar bana saat 17:00'de %8'lik bir hisse gösterdiğinde, o hissenin
    KAPANIŞTA tavan yapma ihtimali nedir?

Bu sayı doğrudan işine yarar:
  - Yüksekse (%40+) → radar bildirimleri gerçekten değerli, gir
  - Düşükse (%10) → çoğu sönüyor demektir, çok seçici olmak gerek

ALT KIRILIMLAR (hangi bildirimler daha güvenilir):
  - Kapanışa kaç bar kaldığında yakalandı (erken mi, son dakika mı)
  - Yakalandığındaki seviye (%6-7 / %7-8 / %8-9 / %9-9.5)
  - Hacim durumu
  - Son 1 saatteki hız

ZAMAN ÖLÇÜMÜ HAKKINDA: saat dilimi karışıklığından kaçınmak için
"kapanışa kalan bar sayısı" kullanılıyor (1 bar = 15 dk). Bu, saat
diliminden bağımsız ve güvenilir. Referans olsun diye barın kendi
saati de kaydediliyor.

VERİ SINIRI: yfinance 15dk verisini sadece 60 gün geriye veriyor,
yani ~40 işlem günü. Örneklem bu yüzden sınırlı - sonucu buna göre
yorumla (kesin sayı değil, büyüklük mertebesi fikri verir).

Start Command:  python bist_tavan_kilitlenme.py
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
KOD_SURUMU = "tavan-kilitlenme-v1-2026-08-28"

TAVAN_ESIK = 9.5
BANT_ALT, BANT_UST = 6.0, 9.5

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


def _kalan_bar_grubu(k):
    if k <= 2:
        return "son 30 dk"
    if k <= 4:
        return "30-60 dk kala"
    if k <= 8:
        return "1-2 saat kala"
    if k <= 16:
        return "2-4 saat kala"
    return "4+ saat kala"


def _seviye_grubu(g):
    if g < 7:
        return "%6-7"
    if g < 8:
        return "%7-8"
    if g < 9:
        return "%8-9"
    return "%9-9.5"


def calistir():
    kayitlar = []
    islenen, atlanan = 0, 0

    for n_i, ticker in enumerate(BIST_HISSELER, 1):
        print(f"[Kilitlenme {n_i}/{len(BIST_HISSELER)}] {ticker}...", flush=True)
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
            for gi in range(1, len(gunler)):
                gun = gunler[gi]
                dun_barlar = df[df["gun"] == gunler[gi - 1]]
                bugun = df[df["gun"] == gun]
                if dun_barlar.empty or len(bugun) < 5:
                    continue
                dun_kapanis = float(dun_barlar.iloc[-1]["close"])
                if dun_kapanis <= 0:
                    continue

                gun_kapanis = float(bugun.iloc[-1]["close"])
                gun_sonu_getiri = (gun_kapanis - dun_kapanis) / dun_kapanis * 100
                kilitlendi = int(gun_sonu_getiri >= TAVAN_ESIK)

                ort_hacim = float(dun_barlar["volume"].mean()) or 1.0
                # AYNI GUN icin her seviye+zaman grubundan SADECE ILK kayit
                gorulen = set()
                n_bar = len(bugun)
                for bi in range(2, n_bar):
                    bar = bugun.iloc[bi]
                    getiri = (float(bar["close"]) - dun_kapanis) / dun_kapanis * 100
                    if not (BANT_ALT <= getiri < BANT_UST):
                        continue
                    kalan = n_bar - 1 - bi
                    zg = _kalan_bar_grubu(kalan)
                    sg = _seviye_grubu(getiri)
                    anahtar = (zg, sg)
                    if anahtar in gorulen:
                        continue
                    gorulen.add(anahtar)

                    onceki4 = bugun.iloc[max(0, bi - 4):bi + 1]["close"]
                    hiz = ((float(onceki4.iloc[-1]) - float(onceki4.iloc[0]))
                           / float(onceki4.iloc[0]) * 100) if len(onceki4) >= 2 and float(onceki4.iloc[0]) > 0 else None
                    hacim_orani = float(bar["volume"]) / ort_hacim if ort_hacim > 0 else None

                    kayitlar.append({
                        "ticker": ticker, "tarih": str(gun),
                        "bar_saati": bugun.index[bi].strftime("%H:%M"),
                        "kalan_bar": kalan, "zaman_grubu": zg,
                        "getiri_pct": round(getiri, 2), "seviye_grubu": sg,
                        "hiz_1saat_pct": round(hiz, 2) if hiz is not None else None,
                        "hacim_orani": round(hacim_orani, 2) if hacim_orani else None,
                        "gun_sonu_getiri_pct": round(gun_sonu_getiri, 2),
                        "tavana_kilitlendi": kilitlendi,
                    })
            islenen += 1
        except Exception as e:
            print(f"[Kilitlenme] {ticker} hata: {e}", flush=True)
            atlanan += 1
        time.sleep(0.35)

    if not kayitlar:
        return None, "Hiç kayıt üretilemedi (BIST 15dk verisi alınamamış olabilir)."
    tum = pd.DataFrame(kayitlar)
    dosya = os.path.join(DATA_DIR, "bist_tavan_kilitlenme.csv")
    tum.to_csv(dosya, index=False, encoding="utf-8-sig")

    sonuc = {"islenen": islenen, "atlanan": atlanan, "toplam": len(tum),
             "genel_oran": round(float(tum.tavana_kilitlendi.mean() * 100), 1),
             "zaman": [], "seviye": [], "capraz": [], "hiz": [], "hacim": []}

    zaman_sira = ["4+ saat kala", "2-4 saat kala", "1-2 saat kala", "30-60 dk kala", "son 30 dk"]
    for z in zaman_sira:
        g = tum[tum.zaman_grubu == z]
        if len(g) >= 15:
            sonuc["zaman"].append({"ad": z, "n": len(g),
                                    "oran": round(float(g.tavana_kilitlendi.mean() * 100), 1)})
    for s in ["%6-7", "%7-8", "%8-9", "%9-9.5"]:
        g = tum[tum.seviye_grubu == s]
        if len(g) >= 15:
            sonuc["seviye"].append({"ad": s, "n": len(g),
                                     "oran": round(float(g.tavana_kilitlendi.mean() * 100), 1)})
    for z in zaman_sira:
        for s in ["%6-7", "%7-8", "%8-9", "%9-9.5"]:
            g = tum[(tum.zaman_grubu == z) & (tum.seviye_grubu == s)]
            if len(g) >= 15:
                sonuc["capraz"].append({"zaman": z, "seviye": s, "n": len(g),
                                         "oran": round(float(g.tavana_kilitlendi.mean() * 100), 1)})
    for ad, alt in [("hız ≥%2", tum[tum.hiz_1saat_pct >= 2]),
                    ("hız %0-2", tum[(tum.hiz_1saat_pct >= 0) & (tum.hiz_1saat_pct < 2)]),
                    ("hız <%0 (düşüyor)", tum[tum.hiz_1saat_pct < 0])]:
        if len(alt) >= 15:
            sonuc["hiz"].append({"ad": ad, "n": len(alt),
                                  "oran": round(float(alt.tavana_kilitlendi.mean() * 100), 1)})
    for ad, alt in [("hacim ≥3x", tum[tum.hacim_orani >= 3]),
                    ("hacim 1-3x", tum[(tum.hacim_orani >= 1) & (tum.hacim_orani < 3)]),
                    ("hacim <1x", tum[tum.hacim_orani < 1])]:
        if len(alt) >= 15:
            sonuc["hacim"].append({"ad": ad, "n": len(alt),
                                    "oran": round(float(alt.tavana_kilitlendi.mean() * 100), 1)})
    return dosya, sonuc


def _rapor(o):
    s = [f"🔒 RADAR SİNYALİ TAVANA KİLİTLENİYOR MU? — {KOD_SURUMU}",
         f"İşlenen: {o['islenen']} hisse | Örnek: {o['toplam']} sinyal anı",
         f"\n★ GENEL: %6-9.5 bandında yakalanan sinyallerin "
         f"%{o['genel_oran']}'i kapanışta TAVANA KİLİTLENİYOR\n"]
    if o["zaman"]:
        s.append("KAPANIŞA NE KADAR KALA YAKALANDIĞINA GÖRE:")
        for z in o["zaman"]:
            s.append(f"   {z['ad']:<16} n={z['n']:<5} kilitlenme %{z['oran']}")
        s.append("")
    if o["seviye"]:
        s.append("YAKALANDIĞINDAKİ SEVİYEYE GÖRE:")
        for x in o["seviye"]:
            s.append(f"   {x['ad']:<8} n={x['n']:<5} kilitlenme %{x['oran']}")
        s.append("")
    if o["hiz"]:
        s.append("SON 1 SAATTEKİ HIZA GÖRE:")
        for x in o["hiz"]:
            s.append(f"   {x['ad']:<18} n={x['n']:<5} kilitlenme %{x['oran']}")
        s.append("")
    if o["hacim"]:
        s.append("HACME GÖRE:")
        for x in o["hacim"]:
            s.append(f"   {x['ad']:<12} n={x['n']:<5} kilitlenme %{x['oran']}")
        s.append("")
    if o["capraz"]:
        s.append("ZAMAN × SEVİYE (en yüksek 8):")
        for x in sorted(o["capraz"], key=lambda y: -y["oran"])[:8]:
            s.append(f"   {x['zaman']:<16} {x['seviye']:<8} n={x['n']:<5} %{x['oran']}")
    s.append("\n⚠️ NEDEN ÖNEMLİ: Tavana kilitlenirse ertesi gün +%2.51 net "
             "(%90 kazanma). Kilitlenmezse ~sıfır. Yani bu oran, radar "
             "bildirimlerinin GERÇEK değerini gösteriyor.\n"
             "Örnek: kilitlenme %30 ise, 10 bildirimin 3'ü kazandırır, "
             "7'si başa baş → yine de artıda olursun.\n"
             "⚠️ Veri sınırı: 15dk verisi sadece 60 gün geriye gidiyor, "
             "örneklem sınırlı - kesin sayı değil, mertebe fikri.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (tavan kilitlenme)", 200


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
        f"🔒 TAVANA KİLİTLENME ANALİZİ başlıyor — {KOD_SURUMU}\n\n"
        f"Soru: radar sana %8'lik bir hisse gösterdiğinde, o hisse "
        f"KAPANIŞTA tavan yapma ihtimali nedir?\n\n"
        f"Bu sayı doğrudan işine yarar - çünkü önceki testler gösterdi ki "
        f"tüm kâr tavana kilitlenmede (+%2.51 net, %90 kazanma), "
        f"kilitlenmezse sıfır.\n\n"
        f"{len(BIST_HISSELER)} hisse × 60 günlük 15dk verisi. Kırılımlar: "
        f"kapanışa kalan süre, yakalandığı seviye, hız, hacim.\n\n"
        f"⚠️ Bu deploy'da SADECE bu analiz çalışıyor.\nBitince CSV + özet göndereceğim."
    )
    try:
        dosya, sonuc = calistir()
        if dosya is None:
            send_telegram_message(f"🔒 Analiz başarısız: {sonuc}")
            return
        send_telegram_document(dosya, caption=_rapor(sonuc))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"🔒 Analiz hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] bist_tavan_kilitlenme.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
