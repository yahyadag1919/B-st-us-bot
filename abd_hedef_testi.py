"""
abd_hedef_testi.py — %1 HEDEFİ ANLAMSIZ MI? SABİT %5 HEDEFİ NE VERİR?
=======================================================================
2026-09-01 — Kullanıcının haklı şikayeti:

  "Ana sistemde çoğunlukla +%1 olanlar tutuyor. 10 gün bekleyeceksem
   +%1 anlamsız, en az +%5 almam lazım. Ayrıca günde 100+ bildirim
   geliyor."

SORUNUN KAYNAĞI - MEVCUT SİSTEMİN TASARIMI:
Şu an hedefler kademeli: 1 günde %1, 3 günde %2, 5 günde %3, 10 günde %5.
HERHANGİ biri tutunca "HEDEF TUTTU" deniyor. Doğal olarak en kolayı
(%1) tutuyor ve sistem başarılı görünüyor - ama kullanıcı için 10 gün
bekleyip %1 almak anlamsız. Haklı.

BU TEST ŞUNU ÖLÇÜYOR:
  Kademeli sistem yerine TEK SABİT HEDEF kullansak ne olur?
  Denenen hedefler: %3, %5, %7, %10
  Denenen süreler: 5, 10, 20 iş günü
  Ölçülen: hedefe ulaşma oranı VE net getiri (maliyet düşülmüş)

  Ayrıca KARŞILAŞTIRMA için:
    - Mevcut kademeli sistem (%1/%2/%3/%5)
    - KÖR temel çizgi (koşulsuz al) - her zamanki gibi, çünkü bir
      hedefin "iyi" görünmesi tek başına anlam taşımaz

GÖSTERGELER: Donchian-20, EMA9/21, ADX+DI, Awesome Oscillator,
Bollinger, CCI, MACD (canlı sistemdeki 8'liden VWAP hariç - o gün-içi
göstergesi, günlük barda anlamlı olmaz).

⚠️ DÜRÜST FARK: Canlı sistem sinyalleri 15dk barlarda üretiyor, ama
15dk verisi sadece 60 gün geriye gidiyor. Büyük örneklem için bu test
GÜNLÜK barlarda sinyal üretiyor. Sinyal zamanlaması birebir aynı
değil - ama asıl soru ("hangi hedef daha iyi") için bu yaklaşım
geçerli, çünkü hedef/süre ilişkisi zaman diliminden bağımsız.

HİSSE EVRENİ: ~300 likit ABD hissesi (büyük + orta ölçek). Sorunlu/
işlem görmeyen hisseler bilinçli olarak DIŞARIDA - veri gelmeyen
otomatik atlanıyor ve raporda kaç hissenin işlendiği belirtiliyor.

Start Command:  python abd_hedef_testi.py
Bu deploy'da SADECE bu analiz çalışır.
"""
import os
import time
import threading

import numpy as np
import pandas as pd
import requests
from flask import Flask

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")
PORT = int(os.environ.get("PORT", "10000"))
KOD_SURUMU = "abd-hedef-testi-v3-numpy-hizlandirma-2026-09-02"

MALIYET_PCT = float(os.environ.get("MALIYET_PCT", "0.10"))
HEDEFLER = [3.0, 5.0, 7.0, 10.0]
SURELER = [5, 10, 20]
KADEMELI = [(1, 1.0), (3, 2.0), (5, 3.0), (10, 5.0)]   # mevcut sistem

US_TICKERS = [
    # Mega/large cap
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "JPM",
    "V", "UNH", "MA", "HD", "PG", "COST", "XOM", "JNJ", "ABBV", "MRK",
    "AVGO", "PEP", "KO", "BAC", "WMT", "CRM", "ADBE", "AMD", "NFLX", "DIS",
    "CSCO", "ORCL", "INTC", "QCOM", "TXN", "PFE", "NKE", "MCD", "GS", "CAT",
    "BA", "LLY", "TMO", "ABT", "DHR", "ACN", "LIN", "MDT", "NEE", "PM",
    "UNP", "RTX", "HON", "SBUX", "LOW", "INTU", "AMGN", "IBM", "GE", "CVX",
    "WFC", "MS", "SCHW", "BLK", "SPGI", "AXP", "C", "T", "VZ", "CMCSA",
    "AMAT", "MU", "LRCX", "ADI", "KLAC", "SNPS", "CDNS", "PANW", "CRWD",
    "NOW", "UBER", "ABNB", "PYPL", "SHOP", "COIN", "MRNA", "GILD", "BMY",
    "CVS", "CI", "ELV", "HCA", "DE", "MMM", "LMT", "NOC", "GD", "EOG",
    "SLB", "COP", "PSX", "MPC", "VLO", "NEM", "FCX", "DOW", "DD", "PPG",
    "SHW", "ECL", "APD", "NUE", "STLD", "CLF", "X", "AA", "MOS", "CF",
    # Financials / insurance
    "USB", "PNC", "TFC", "COF", "AIG", "MET", "PRU", "ALL", "TRV", "PGR",
    "CB", "AFL", "HIG", "SYF", "DFS", "AMP", "BK", "STT", "NTRS", "FITB",
    "KEY", "RF", "CFG", "HBAN", "MTB", "ZION", "CMA", "ICE", "CME", "NDAQ",
    # Tech / software / semis
    "AVGO", "NXPI", "MCHP", "ON", "SWKS", "QRVO", "MPWR", "TER", "ENTG",
    "ANET", "FTNT", "ZS", "OKTA", "DDOG", "NET", "TEAM", "WDAY", "SNOW",
    "MDB", "HUBS", "TWLO", "DOCU", "ZM", "RNG", "SQ", "AFRM", "UPST",
    "ROKU", "SPOT", "PINS", "SNAP", "MTCH", "EA", "TTWO", "RBLX", "U",
    "PLTR", "SMCI", "DELL", "HPQ", "HPE", "NTAP", "WDC", "STX", "JNPR",
    "CIEN", "GLW", "APH", "TEL", "KEYS", "TDY", "GRMN", "ZBRA", "TRMB",
    # Healthcare / biotech
    "ISRG", "SYK", "BSX", "BDX", "ZBH", "EW", "BAX", "RMD", "DXCM", "PODD",
    "ALGN", "IDXX", "WST", "STE", "HOLX", "VTRS", "OGN", "REGN", "VRTX",
    "BIIB", "ILMN", "INCY", "EXAS", "NBIX", "SRPT", "ALNY", "BMRN", "UTHR",
    "MCK", "COR", "CAH", "ZTS", "IQV", "A", "MTD", "WAT", "PKI", "CRL",
    # Consumer / retail / industrials
    "TGT", "DG", "DLTR", "ROST", "TJX", "BURL", "ULTA", "LULU", "DECK",
    "CROX", "SKX", "VFC", "PVH", "RL", "TPR", "CPRI", "GPS", "ANF", "AEO",
    "YUM", "CMG", "DRI", "DPZ", "QSR", "WEN", "SHAK", "TXRH", "EAT",
    "MAR", "HLT", "H", "WH", "RCL", "CCL", "NCLH", "LVS", "WYNN", "MGM",
    "DAL", "UAL", "AAL", "LUV", "ALK", "JBLU", "FDX", "UPS", "CHRW", "EXPD",
    "ODFL", "XPO", "SAIA", "KNX", "CSX", "NSC", "UNP", "WAB", "TRN",
    "EMR", "ETN", "PH", "ROK", "DOV", "ITW", "IR", "CMI", "PCAR", "TT",
    "JCI", "CARR", "OTIS", "AME", "FTV", "XYL", "PNR", "SWK", "MAS", "AOS",
    # Energy / utilities / REIT
    "OXY", "DVN", "FANG", "HES", "APA", "MRO", "CTRA", "EQT", "AR", "RRC",
    "HAL", "BKR", "NOV", "FTI", "CHX", "WMB", "OKE", "KMI", "TRGP", "LNG",
    "DUK", "SO", "D", "AEP", "EXC", "XEL", "ED", "WEC", "ES", "PEG",
    "SRE", "PCG", "EIX", "FE", "AEE", "CMS", "DTE", "CNP", "NI", "LNT",
    "AMT", "PLD", "CCI", "EQIX", "PSA", "SPG", "O", "WELL", "VTR", "DLR",
    "AVB", "EQR", "MAA", "ESS", "UDR", "CPT", "INVH", "AMH", "ARE", "BXP",
    # Popular / high-beta
    "GME", "AMC", "MARA", "RIOT", "MSTR", "SOFI", "LCID", "RIVN", "NIO",
    "HOOD", "DKNG", "PENN", "CHWY", "CVNA", "W", "ETSY", "EBAY", "BABA",
    "JD", "PDD", "SE", "MELI", "GRAB", "TCOM", "BIDU", "NTES", "TME",
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


def _toplu_veri_cek(tickers, sert_sure=120):
    """2026-09-02 DEGISTI: onceden hisse basina AYRI istek atiliyordu -
    401 hisse x ~30sn = 3+ saat suruyordu. Artik yf.download ile TOPLU
    cekiliyor (50'lik gruplar), bu 9 istege dusuruyor."""
    import concurrent.futures
    import yfinance as yf

    def _cek():
        return yf.download(tickers, period="2y", interval="1d",
                           group_by="ticker", auto_adjust=True,
                           progress=False, threads=True, timeout=30)

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(_cek).result(timeout=sert_sure)
    except concurrent.futures.TimeoutError:
        print(f"[SERT zaman asimi] {len(tickers)} hisselik grup", flush=True)
        return None
    except Exception as e:
        print(f"[Veri hatasi] grup: {e}", flush=True)
        return None
    finally:
        ex.shutdown(wait=False)



def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _gostergeler(df):
    df["ema9"] = _ema(df["close"], 9)
    df["ema21"] = _ema(df["close"], 21)
    df["donch_ust"] = df["high"].rolling(20).max()
    df["donch_alt"] = df["low"].rolling(20).min()
    macd = _ema(df["close"], 12) - _ema(df["close"], 26)
    df["macd"], df["macd_sig"] = macd, _ema(macd, 9)
    med = (df["high"] + df["low"]) / 2
    df["ao"] = med.rolling(5).mean() - med.rolling(34).mean()
    orta = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()
    df["bb_alt"], df["bb_ust"] = orta - 2 * std, orta + 2 * std
    tp = (df["high"] + df["low"] + df["close"]) / 3
    sma = tp.rolling(20).mean()
    md = tp.rolling(20).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    df["cci"] = (tp - sma) / (0.015 * md.replace(0, np.nan))
    up, dn = df["high"].diff(), -df["low"].diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    ndm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/14, adjust=False).mean()
    df["pdi"] = 100 * pdm.ewm(alpha=1/14, adjust=False).mean() / atr.replace(0, np.nan)
    df["ndi"] = 100 * ndm.ewm(alpha=1/14, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (df["pdi"] - df["ndi"]).abs() / (df["pdi"] + df["ndi"]).replace(0, np.nan)
    df["adx"] = dx.ewm(alpha=1/14, adjust=False).mean()
    return df


def _sinyaller(r, o):
    """Bir bardaki tetiklenmeleri döner: [(gösterge, yön), ...]"""
    s = []
    if pd.notna(r["donch_ust"]):
        if r["close"] >= r["donch_ust"]:
            s.append(("Donchian-20", "LONG"))
        elif r["close"] <= r["donch_alt"]:
            s.append(("Donchian-20", "SHORT"))
    if pd.notna(r["ema9"]) and pd.notna(o["ema9"]):
        if o["ema9"] <= o["ema21"] and r["ema9"] > r["ema21"]:
            s.append(("EMA9/21", "LONG"))
        elif o["ema9"] >= o["ema21"] and r["ema9"] < r["ema21"]:
            s.append(("EMA9/21", "SHORT"))
    if pd.notna(r["adx"]) and r["adx"] >= 25 and pd.notna(o["pdi"]):
        if o["pdi"] <= o["ndi"] and r["pdi"] > r["ndi"]:
            s.append(("ADX+DI", "LONG"))
        elif o["pdi"] >= o["ndi"] and r["pdi"] < r["ndi"]:
            s.append(("ADX+DI", "SHORT"))
    if pd.notna(r["ao"]) and pd.notna(o["ao"]):
        if o["ao"] <= 0 < r["ao"]:
            s.append(("Awesome Osc", "LONG"))
        elif o["ao"] >= 0 > r["ao"]:
            s.append(("Awesome Osc", "SHORT"))
    if pd.notna(r["bb_alt"]):
        if r["close"] <= r["bb_alt"]:
            s.append(("Bollinger", "LONG"))
        elif r["close"] >= r["bb_ust"]:
            s.append(("Bollinger", "SHORT"))
    if pd.notna(r["cci"]):
        if r["cci"] <= -100:
            s.append(("CCI", "LONG"))
        elif r["cci"] >= 100:
            s.append(("CCI", "SHORT"))
    if pd.notna(r["macd"]) and pd.notna(o["macd"]):
        if o["macd"] <= o["macd_sig"] and r["macd"] > r["macd_sig"]:
            s.append(("MACD", "LONG"))
        elif o["macd"] >= o["macd_sig"] and r["macd"] < r["macd_sig"]:
            s.append(("MACD", "SHORT"))
    return s


def _sabit_hedef(hi, lo, cl, i, yon, hedef, sure):
    """Tek sabit hedef. 2026-09-02: pandas .iloc yerine NUMPY dizileri.
    .iloc her çağrıda çok yavaş ve bu fonksiyon milyonlarca kez
    çağrılıyor - kullanıcı testin saatlerce sürdüğünü bildirdi.
    Döner (getiri_pct, tuttu_mu)."""
    giris = cl[i]
    if giris <= 0:
        return None
    son = min(i + sure, len(cl) - 1)
    if son <= i:
        return None
    if yon == "LONG":
        if hi[i + 1:son + 1].max() >= giris * (1 + hedef / 100):
            return hedef, True
    else:
        if lo[i + 1:son + 1].min() <= giris * (1 - hedef / 100):
            return hedef, True
    g = (cl[son] - giris) / giris * 100
    return (g if yon == "LONG" else -g), False


def _kademeli(hi, lo, cl, i, yon):
    """Mevcut sistemin kademeli hedefi - hangisi önce tutarsa o."""
    giris = cl[i]
    if giris <= 0:
        return None
    for gun, hedef in KADEMELI:
        j = i + gun
        if j >= len(cl):
            break
        if yon == "LONG" and hi[j] >= giris * (1 + hedef / 100):
            return hedef, True
        if yon == "SHORT" and lo[j] <= giris * (1 - hedef / 100):
            return hedef, True
    son = min(i + 10, len(cl) - 1)
    if son <= i:
        return None
    g = (cl[son] - giris) / giris * 100
    return (g if yon == "LONG" else -g), False


def calistir():
    """2026-09-02 YENIDEN YAZILDI - BELLEK TASMASI DUZELTMESI.
    Onceki surum TUM sinyal kayitlarini (yuz binlerce, her biri 29
    alanli) bellekte biriktiriyordu. Render'in 512MB sinirini asinca
    servis olduruluyor ve bastan basliyordu - kullanici 3 saat boyunca
    testi bitiremedi, surekli 148/401 civarinda yeniden basliyordu.
    Artik kayitlar biriktirilmiyor: her sinyal ISLENIR ISLENMEZ
    toplayicilara (accumulator) ekleniyor. Bellek kullanimi sabit."""
    # toplayicilar: anahtar -> [n, getiri_toplami, tutma_toplami]
    topla = {}

    def _ekle(gosterge, sistem, hedef, sure, getiri, tuttu):
        a = (gosterge, sistem, hedef, sure)
        if a not in topla:
            topla[a] = [0, 0.0, 0]
        topla[a][0] += 1
        topla[a][1] += getiri
        topla[a][2] += int(tuttu)

    islenen, atlanan, toplam_sinyal = 0, 0, 0
    GRUP = 50
    gruplar = [US_TICKERS[i:i + GRUP] for i in range(0, len(US_TICKERS), GRUP)]

    for gi, grup in enumerate(gruplar, 1):
        print(f"[Grup {gi}/{len(gruplar)}] {len(grup)} hisse cekiliyor...", flush=True)
        veri = _toplu_veri_cek(grup)
        if veri is None or len(veri) == 0:
            atlanan += len(grup)
            continue
        for ticker in grup:
            try:
                if isinstance(veri.columns, pd.MultiIndex):
                    if ticker not in veri.columns.get_level_values(0):
                        atlanan += 1
                        continue
                    ham = veri[ticker]
                else:
                    ham = veri
                df = ham.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                          "Close": "close", "Volume": "volume"})
                if not set(["open", "high", "low", "close"]).issubset(df.columns):
                    atlanan += 1
                    continue
                df = df[["open", "high", "low", "close", "volume"]].dropna().reset_index(drop=True)
                if len(df) < 120:
                    atlanan += 1
                    continue
                df = _gostergeler(df)
                # numpy dizileri - ic dongulerde pandas'tan cok daha hizli
                hi = df["high"].to_numpy(dtype=float)
                lo = df["low"].to_numpy(dtype=float)
                cl = df["close"].to_numpy(dtype=float)
                satirlar_l = [df.iloc[i] for i in range(39, len(df) - 21)]

                for idx, i in enumerate(range(40, len(df) - 21)):
                    tetik = _sinyaller(satirlar_l[idx + 1], satirlar_l[idx])
                    tetik.append(("[KOR] Kosulsuz LONG", "LONG"))
                    for gosterge, yon in tetik:
                        toplam_sinyal += 1
                        k = _kademeli(hi, lo, cl, i, yon)
                        if k:
                            _ekle(gosterge, "KADEMELI (mevcut)", "1/2/3/5", 10, k[0], k[1])
                        for h in HEDEFLER:
                            for s in SURELER:
                                res = _sabit_hedef(hi, lo, cl, i, yon, h, s)
                                if res:
                                    _ekle(gosterge, "SABIT", h, s, res[0], res[1])
                islenen += 1
                if islenen % 10 == 0:
                    print(f"   ...{islenen} hisse islendi, "
                          f"{toplam_sinyal:,} sinyal", flush=True)
            except Exception as e:
                print(f"[Hedef Testi] {ticker} hata: {e}", flush=True)
                atlanan += 1
        del veri
        # ilerleme mesaji artik grup BITTIKTEN sonra - gercek sayilarla
        try:
            send_telegram_message(f"🎯 İlerleme: {gi}/{len(gruplar)} grup bitti "
                                   f"({islenen} hisse işlendi, "
                                   f"{toplam_sinyal:,} sinyal)")
        except Exception:
            pass

    if not topla:
        return None, "Hic kayit uretilemedi."

    satirlar = []
    for (gosterge, sistem, hedef, sure), (n, gt, tt) in topla.items():
        if n < 100:
            continue
        satirlar.append({"gosterge": gosterge, "sistem": sistem,
                          "hedef": hedef, "sure": sure, "n": n,
                          "net": round(gt / n - MALIYET_PCT, 4),
                          "tutma": round(tt / n * 100, 1)})
    if not satirlar:
        return None, "Yeterli ornek yok."
    ozet = pd.DataFrame(satirlar).sort_values("net", ascending=False)
    dosya = os.path.join(DATA_DIR, "abd_hedef_ozet.csv")
    ozet.to_csv(dosya, index=False, encoding="utf-8-sig")
    return dosya, {"islenen": islenen, "atlanan": atlanan,
                    "toplam_sinyal": toplam_sinyal, "satirlar": satirlar}



def _rapor(o):
    s = [f"🎯 ABD HEDEF TESTİ — {KOD_SURUMU}",
         f"İşlenen: {o['islenen']} hisse | Atlanan: {o['atlanan']} | "
         f"Sinyal: {o['toplam_sinyal']}",
         f"Maliyet: -%{MALIYET_PCT}\n"]
    kor = [x for x in o["satirlar"] if "KÖR" in x["gosterge"]]
    if kor:
        b = max(kor, key=lambda y: y["net"])
        s.append(f"KÖR ÇİZGİ en iyi: hedef %{b['hedef']} / {b['sure']}g → "
                 f"net %{b['net']:.3f} (tutma %{b['tutma']})\n")
    s.append("EN İYİ 20 KOMBİNASYON (net getiriye göre):")
    s.append(f"{'gösterge':<16}{'hedef':>8}{'süre':>6}{'n':>7}{'NET':>9}{'tutma':>8}")
    for x in sorted(o["satirlar"], key=lambda y: -y["net"])[:20]:
        s.append(f"{x['gosterge'][:15]:<16}{str(x['hedef']):>8}{x['sure']:>6}"
                 f"{x['n']:>7}{x['net']:>8.3f}%{x['tutma']:>7.1f}%")
    s.append("\nMEVCUT KADEMELİ SİSTEM (%1/2/3/5) — kıyas:")
    for x in [y for y in o["satirlar"] if y["sistem"] == "KADEMELİ (mevcut)"]:
        s.append(f"   {x['gosterge'][:20]:<22} n={x['n']:<6} net %{x['net']:.3f} "
                 f"tutma %{x['tutma']}")
    s.append("\n⚠️ SORUNUN CEVABI:\n"
             "  'tutma' = hedefe ulaşma oranı. %5 hedefinde bu oran düşük "
             "çıkacak (doğal) - ama NET getiri daha yüksekse yine de daha "
             "iyidir.\n"
             "  Karar ölçütü: NET sütunu. Kademeli sistemden yüksek net "
             "veren sabit hedef varsa, ona geçmek mantıklı.\n"
             "  KÖR çizgiyi geçemeyen hiçbir kombinasyon gerçek değildir.")
    return "\n".join(s)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (abd hedef testi)", 200


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
        f"🎯 ABD HEDEF TESTİ başlıyor — {KOD_SURUMU}\n\n"
        f"Senin şikayetin test ediliyor: '10 gün bekleyip %1 almak "
        f"anlamsız, en az %5 lazım.'\n\n"
        f"Mevcut kademeli sistem (%1/2/3/5) ile SABİT hedefler "
        f"(%3/%5/%7/%10 × 5/10/20 gün) karşılaştırılıyor.\n"
        f"{len(US_TICKERS)} likit ABD hissesi × 2 yıl, 7 gösterge + "
        f"kör temel çizgi.\n\n"
        f"⚠️ Bu deploy'da SADECE bu analiz çalışıyor. Uzun sürebilir.\n"
        f"Bitince CSV + özet göndereceğim."
    )
    try:
        dosya, sonuc = calistir()
        if dosya is None:
            send_telegram_message(f"🎯 Analiz başarısız: {sonuc}")
            return
        send_telegram_document(dosya, caption=_rapor(sonuc))
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_telegram_message(f"🎯 Analiz hatası: {e}")


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] abd_hedef_testi.py — {KOD_SURUMU}", flush=True)
    threading.Thread(target=_calis, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
