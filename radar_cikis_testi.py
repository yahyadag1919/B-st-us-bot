"""
radar_cikis_testi.py — FİLTRE + ÇIKIŞ KURALI TESTİ
===================================================
Onceki test (radar_onculu_test.py) sunu buldu:
  * Filtreli hisselerin +%2'ye ulasma orani %24.1, kontrol grubunda %12.5
    -> yukari potansiyel gercekten 1.93 KAT yuksek.
  * AMA ortalama en dusuk nokta -%1.56 (kontrol -%1.15), seans sonu
    ortalamasi -%0.30 (kontrol -%0.13), artida kapanan %39 (kontrol %44.8).

Yani filtre hisseyi "daha cok yukselecek" hale getirmiyor, "DAHA COK
OYNAYACAK" hale getiriyor. Al-ve-bekle yaklasimiyla kontrol grubundan DAHA
KOTU sonuc veriyor. Kullanilabilir olmasi tek bir seye bagli: DISIPLINLI
CIKIS.

BU TESTIN SORUSU:
  "Hangi cikis kurali bu filtreyi kara cevirir - ve o kural kontrol
   grubuna uygulandiginda ayni sonucu vermiyor mu?"

EN KRITIK NOKTA — AYNI CIKIS KURALI KONTROL GRUBUNA DA UYGULANIYOR.
Bir cikis kurali filtreli grupta kar ediyorsa ama kontrol grubunda da ayni
kari ediyorsa, kazanci saglayan FILTRE degil CIKIS KURALIDIR. Filtrenin
katkisini ancak bu karsilastirma gosterir. Onceki testte bu ayrimi
yapmamistim; sonuc "filtre bilgi tasiyor" derken aslinda sadece yukari
hedefleri olcuyordu ve asagi tarafi hic dikkate almiyordu.

MUHAFAZAKARLIK KURALLARI (onceki turnuvalardan aynen):
  * Ileriye bakma yok: sinyal barin kapanisinda, simulasyon sonraki bardan.
  * Ayni mumda hem stop hem hedef gorulurse ZARAR sayilir.
  * Maliyet her islemden dusulur.
  * Gun ici: seans sonunda zorla kapanis.
  * Goreli guc AYNI BARDAKI endeks degisimiyle olculur (ileriye bakma yok).

CALISTIRMA (Render): Start Command -> python radar_cikis_testi.py
Sure: ~10 dk (100 hisse; bu test gunluk veri cekmiyor, o yuzden hizli).
"""

import os
import time
import threading
import requests
import numpy as np
import pandas as pd
import yfinance as yf

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

TICKER_LIMIT = int(os.environ.get("RADAR_TICKER_LIMIT", "100"))

# --- Filtre esikleri (Gemini'nin tasarimi, degistirilmedi) ---
VOLUME_MULT = 2.0
BREAKOUT_LOOKBACK = 20
CATCH_MIN_PCT = 0.5
CATCH_MAX_PCT = 1.0

# --- Maliyet ---
FEE_PCT_PER_SIDE = float(os.environ.get("BIST_FEE_PCT", "0.05"))
SLIPPAGE_PCT = float(os.environ.get("BIST_SLIPPAGE_PCT", "0.05"))
TOTAL_COST_PCT = FEE_PCT_PER_SIDE * 2 + SLIPPAGE_PCT

# --- Test edilecek cikis kurallari: (hedef%, stop%) ---
# Kombinasyon sayisi bilerek az: cok varyant denenirse biri sans eseri iyi
# cikar. Burada hepsi ayni soruyu soruyor: hedef/stop dengesi nerede olmali?
EXIT_RULES = [
    ("Hedef %1.0 / Stop %0.5", 1.0, 0.5),
    ("Hedef %1.5 / Stop %1.0", 1.5, 1.0),
    ("Hedef %2.0 / Stop %1.0", 2.0, 1.0),
    ("Hedef %2.0 / Stop %1.5", 2.0, 1.5),
    ("Hedef %3.0 / Stop %1.5", 3.0, 1.5),
    ("Seans sonuna kadar tut", None, None),
]

INDEX_TICKER = "XU100.IS"

BIST_TICKERS = [
    "AEFES.IS", "AGHOL.IS", "AKBNK.IS", "AKCNS.IS", "AKFGY.IS", "AKSA.IS",
    "AKSEN.IS", "ALARK.IS", "ALBRK.IS", "ALFAS.IS", "ANSGR.IS", "ARCLK.IS",
    "ASELS.IS", "ASTOR.IS", "BAGFS.IS", "BERA.IS", "BIMAS.IS", "BOBET.IS",
    "BRSAN.IS", "BRYAT.IS", "BUCIM.IS", "CANTE.IS", "CCOLA.IS", "CIMSA.IS",
    "CWENE.IS", "DOAS.IS", "DOHOL.IS", "ECILC.IS", "ECZYT.IS", "EGEEN.IS",
    "EKGYO.IS", "ENERY.IS", "ENJSA.IS", "ENKAI.IS", "EREGL.IS", "EUPWR.IS",
    "FROTO.IS", "GARAN.IS", "GESAN.IS", "GLYHO.IS", "GUBRF.IS", "GWIND.IS",
    "HALKB.IS", "HEKTS.IS", "ISCTR.IS", "ISDMR.IS", "ISGYO.IS", "ISMEN.IS",
    "IZMDC.IS", "KARSN.IS", "KCHOL.IS", "KERVT.IS", "KONTR.IS", "KONYA.IS",
    "KORDS.IS", "KRDMD.IS", "KZBGY.IS", "MAVI.IS", "MGROS.IS", "MPARK.IS",
    "ODAS.IS", "OTKAR.IS", "OYAKC.IS", "PENTA.IS", "PETKM.IS", "PGSUS.IS",
    "QUAGR.IS", "SAHOL.IS", "SASA.IS", "SDTTR.IS", "SISE.IS", "SKBNK.IS",
    "SMRTG.IS", "SOKM.IS", "TAVHL.IS", "TCELL.IS", "THYAO.IS", "TKFEN.IS",
    "TOASO.IS", "TSKB.IS", "TTKOM.IS", "TTRAK.IS", "TUKAS.IS", "TUPRS.IS",
    "ULKER.IS", "VAKBN.IS", "VESBE.IS", "VESTL.IS", "YKBNK.IS", "YYLGD.IS",
    "ZOREN.IS", "AHGAZ.IS", "ALTNY.IS", "BINHO.IS", "CVKMD.IS", "EFORC.IS",
    "GOLTS.IS", "KLKIM.IS", "OBAMS.IS", "PEKGY.IS", "REEDR.IS", "KTLEV.IS",
]


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("(Telegram ayarli degil)\n" + text, flush=True)
        return
    for i in range(0, len(text), 3900):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text[i:i + 3900]},
                timeout=20)
        except Exception as e:
            print(f"Telegram gonderilemedi: {e}", flush=True)


def fetch_15m(ticker):
    df = yf.Ticker(ticker).history(period="60d", interval="15m")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.reset_index().rename(columns={
        "Datetime": "ts", "Date": "ts", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "volume"})
    need = ["ts", "open", "high", "low", "close", "volume"]
    if not all(c in df.columns for c in need):
        return pd.DataFrame()
    return df[need].assign(session=pd.to_datetime(df["ts"]).dt.date)


def apply_exit(day, k, giris, hedef_pct, stop_pct):
    """Bir giristen sonra cikis kuralini uygular, NET yuzde sonuc doner.
    Stop HEDEFTEN ONCE kontrol edilir: ayni mumda ikisi de gorulurse zarar
    sayilir (mum ici sirayi bilemeyiz; iyimser varsayim sonuclari sisirir)."""
    if hedef_pct is None:
        cikis = float(day.iloc[-1]["close"])
        return (cikis - giris) / giris * 100 - TOTAL_COST_PCT

    hedef_fiyat = giris * (1 + hedef_pct / 100)
    stop_fiyat = giris * (1 - stop_pct / 100)
    for j in range(k + 1, len(day)):
        bar = day.iloc[j]
        if float(bar["low"]) <= stop_fiyat:
            return -stop_pct - TOTAL_COST_PCT
        if float(bar["high"]) >= hedef_fiyat:
            return hedef_pct - TOTAL_COST_PCT
    # Seans bitti, hedefe de stopa da ulasilmadi
    cikis = float(day.iloc[-1]["close"])
    return (cikis - giris) / giris * 100 - TOTAL_COST_PCT


def collect(df, index_bar):
    """Her uygun bar icin: filtre gecti mi + her cikis kuralinin sonucu."""
    if df.empty or len(df) < 100:
        return []
    df = df.copy()
    df["vol_ma"] = df["volume"].rolling(20 * 32, min_periods=200).mean()

    out = []
    for _, day in df.groupby("session"):
        if len(day) < 6:
            continue
        acilis = float(day.iloc[0]["open"])
        if acilis <= 0:
            continue
        for k in range(1, len(day) - 1):
            r = day.iloc[k]
            fiyat = float(r["close"])
            gun_ici = (fiyat - acilis) / acilis * 100
            if not (CATCH_MIN_PCT <= gun_ici <= CATCH_MAX_PCT):
                continue

            endeks = index_bar.get(pd.Timestamp(r["ts"]))
            if endeks is None:
                continue

            vol_ma = r["vol_ma"]
            hacim = bool(pd.notna(vol_ma) and vol_ma > 0
                         and r["volume"] >= vol_ma * VOLUME_MULT)
            onceki = day.iloc[max(0, k - BREAKOUT_LOOKBACK):k]
            kirilim = bool(not onceki.empty and fiyat > float(onceki["high"].max()))
            ayrisma = bool(gun_ici > endeks)

            sonuclar = {ad: apply_exit(day, k, fiyat, h, s)
                        for ad, h, s in EXIT_RULES}
            out.append({"gecti": hacim and kirilim and ayrisma, "sonuc": sonuclar})
    return out


def istatistik(kayitlar, kural):
    vals = np.array([kk["sonuc"][kural] for kk in kayitlar]) if kayitlar else np.array([])
    if vals.size == 0:
        return None
    return {"n": int(vals.size), "ort": float(vals.mean()),
            "isabet": float((vals > 0).mean() * 100), "toplam": float(vals.sum())}


def main():
    tickers = BIST_TICKERS[:TICKER_LIMIT]
    print(f"CIKIS TESTI BASLIYOR: {len(tickers)} hisse", flush=True)
    send_telegram_message(
        "🎯 [RADAR ÇIKIŞ KURALI TESTİ] Başladı.\n"
        f"{len(tickers)} hisse | 60 günlük 15dk verisi | {len(EXIT_RULES)} çıkış kuralı\n"
        f"Maliyet dahil (%{TOTAL_COST_PCT:.2f} gidiş-dönüş)\n"
        "Soru: hangi çıkış kuralı bu filtreyi kâra çevirir?\n"
        "~10 dakika sürebilir...")

    index_bar = {}
    for deneme in range(4):
        try:
            idx = yf.Ticker(INDEX_TICKER).history(period="60d", interval="15m")
            if idx is None or idx.empty:
                raise ValueError("bos endeks verisi")
            idx = idx.reset_index().rename(columns={"Datetime": "ts", "Date": "ts",
                                                    "Open": "open", "Close": "close"})
            idx["session"] = pd.to_datetime(idx["ts"]).dt.date
            for _, day in idx.groupby("session"):
                a = float(day.iloc[0]["open"])
                if a <= 0:
                    continue
                for _, br in day.iterrows():
                    index_bar[pd.Timestamp(br["ts"])] = (float(br["close"]) - a) / a * 100
            break
        except Exception as e:
            print(f"Endeks denemesi {deneme + 1}: {e}", flush=True)
            if deneme < 3:
                time.sleep(15 * (deneme + 1))
    if not index_bar:
        send_telegram_message("🚨 Endeks verisi alınamadı — test iptal.")
        return

    gecen, kontrol = [], []
    ok, hata = 0, 0
    for n, tk in enumerate(tickers, 1):
        try:
            df = fetch_15m(tk)
            if df.empty:
                hata += 1
                continue
            for g in collect(df, index_bar):
                (gecen if g["gecti"] else kontrol).append(g)
            ok += 1
            print(f"[{n}/{len(tickers)}] {tk} tamam", flush=True)
        except Exception as e:
            hata += 1
            print(f"[{n}/{len(tickers)}] {tk} HATA: {e}", flush=True)
        time.sleep(0.3)

    lines = ["🎯 [ÇIKIŞ KURALI TESTİ SONUÇLARI]",
             f"Taranan: {ok}/{len(tickers)} hisse | maliyet dahil",
             f"Filtreli gözlem: {len(gecen)} | kontrol: {len(kontrol)}", ""]

    kazananlar = []
    for ad, _, _ in EXIT_RULES:
        f = istatistik(gecen, ad)
        c = istatistik(kontrol, ad)
        lines.append(f"▸ {ad}")
        if f:
            lines.append(f"   FİLTRELİ : ort {f['ort']:+.3f}% | isabet %{f['isabet']:.1f} "
                         f"| toplam {f['toplam']:+.0f}%")
        if c:
            lines.append(f"   KONTROL  : ort {c['ort']:+.3f}% | isabet %{c['isabet']:.1f}")
        if f and c:
            fark = f["ort"] - c["ort"]
            lines.append(f"   FARK     : {fark:+.3f}% "
                         f"({'filtre katkı sağlıyor' if fark > 0 else 'filtre katkı sağlamıyor'})")
            # Kural: filtreli POZITIF olmali VE kontrolden BELIRGIN iyi olmali.
            # Ikisi birden saglanmazsa kazanci saglayan filtre degildir.
            if f["ort"] > 0 and fark > 0.05 and f["n"] >= 100:
                kazananlar.append((f["ort"], ad, fark, f["n"]))
        lines.append("")

    lines.append("📊 SONUÇ")
    if kazananlar:
        kazananlar.sort(reverse=True)
        for ort, ad, fark, n in kazananlar:
            lines.append(f"  ✅ {ad}: {ort:+.3f}% ort, kontrolden {fark:+.3f}% iyi (n={n})")
        lines.append("")
        lines.append("Bu kural(lar) hem kâr üretiyor hem de kazancı filtreden alıyor.")
    else:
        lines.append("  ❌ Hiçbir çıkış kuralı iki şartı birden sağlamadı:")
        lines.append("     (1) maliyet sonrası pozitif olmak")
        lines.append("     (2) kontrol grubundan belirgin şekilde iyi olmak")
        lines.append("  Bir kural kârlı ama kontrol grubunda da kârlıysa, kazancı")
        lines.append("  sağlayan filtre değil çıkış kuralıdır — filtreye gerek yok.")

    lines.append("")
    lines.append("ℹ️ 60 günlük veri = TEK piyasa rejimi. Sonuç bu dönem için geçerlidir.")
    lines.append("ℹ️ KAP/haber bileşeni yine test edilmedi.")
    if hata:
        lines.append(f"ℹ️ Veri alınamayan: {hata} hisse")

    msg = "\n".join(lines)
    send_telegram_message(msg)
    print(msg, flush=True)


if __name__ == "__main__":
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _P(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"test calisiyor")

        def log_message(self, *a):
            pass

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))),
                                  _P).serve_forever(), daemon=True).start()

    def _keep_awake():
        url = os.environ.get("RENDER_EXTERNAL_URL")
        if not url:
            return
        time.sleep(60)
        while True:
            try:
                requests.get(url.rstrip("/"), timeout=20)
            except Exception:
                pass
            time.sleep(600)

    threading.Thread(target=_keep_awake, daemon=True).start()

    main()
    print("Test bitti. Start Command'i 'python main.py' yapabilirsin.", flush=True)
    while True:
        time.sleep(3600)
