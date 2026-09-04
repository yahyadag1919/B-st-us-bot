"""
abd_sosyal_duygu.py — ABD SOSYAL DUYGU (SEANS DİLİMLERİNE GÖRE)
=================================================================
2026-09-02 — Kullanıcının isteğiyle YENİDEN KURULDU.

ÖNCEKİ SÜRÜMÜN SORUNU:
ApeWisdom (Reddit) verisi 24 SAATLİK TOPLAM veriyor - gece, seans
öncesi, seans hepsi bir arada. Kullanıcı ise şunu istedi:
  "İnsanlar ASIL SEANS için ne düşünüyor - seans BAŞLAMADAN ÖNCE ve
   seans BAŞLADIĞINDA hisselerdeki düşünceler benim için önemli."

ÇÖZÜM: StockTwits her mesajın SAATİNİ veriyor. Mesajlar New York
saatine çevrilip seans dilimlerine ayrılıyor:

   🌙 GECE          : 20:00 – 04:00 NY  (TR 03:00 – 11:00)
   🌅 SEANS ÖNCESİ  : 04:00 – 09:30 NY  (TR 11:00 – 16:30)  ← ÖNEMLİ
   📈 ASIL SEANS    : 09:30 – 16:00 NY  (TR 16:30 – 23:00)  ← ÖNEMLİ
   🌆 SEANS SONRASI : 16:00 – 20:00 NY  (TR 23:00 – 03:00)

Böylece "seans açılmadan önce ne düşünüyorlardı" ve "seans başlayınca
ne düşünüyorlar" ayrı görülüyor - ve aradaki DEĞİŞİM raporlanıyor.

BİRİKTİRME: StockTwits her istekte son ~30 mesajı veriyor. Sistem sık
aralıklarla kontrol edip mesajları KİMLİĞİNE göre biriktiriyor (tekrar
saymıyor). Gün boyunca tam tablo böyle oluşuyor.

ApeWisdom hâlâ var ama SADECE "hangi hisseler gündemde" için - onun
verisi zaman dilimine ayrılamıyor.

⚠️ AL/SAT sinyali üretmez. Araştırmalar bireysel coşkunun TEPE
noktalarında zirve yaptığını gösteriyor. Kullanıcı kendi gözlemini
yapacak (kendisi böyle istedi).

Start Command:  python abd_sosyal_duygu.py
"""
import os
import time
import threading
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PORT = int(os.environ.get("PORT", "10000"))
KOD_SURUMU = "abd-sosyal-duygu-v3-basit-rapor-2026-09-02"

SIK_ARALIK_SN = int(os.environ.get("SIK_ARALIK_SN", "600"))
SEYREK_ARALIK_SN = int(os.environ.get("SEYREK_ARALIK_SN", "1800"))
TUR_BASI_SORGU = int(os.environ.get("TUR_BASI_SORGU", "45"))
RAPOR_ARALIGI_SN = int(os.environ.get("RAPOR_ARALIGI_SN", "1800"))  # 30 dk
MIN_MESAJ = 5

BASLIK = {"User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36"}

IZLENEN = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "JPM",
    "V", "MA", "UNH", "HD", "PG", "COST", "XOM", "JNJ", "ABBV", "MRK", "LLY",
    "PEP", "KO", "BAC", "WMT", "CRM", "ADBE", "AMD", "NFLX", "DIS", "CSCO",
    "ORCL", "INTC", "QCOM", "TXN", "PFE", "NKE", "MCD", "GS", "CAT", "BA",
    "TMO", "ABT", "ACN", "MDT", "NEE", "UNP", "RTX", "HON", "SBUX", "LOW",
    "INTU", "AMGN", "IBM", "GE", "CVX", "WFC", "MS", "SCHW", "BLK", "AXP",
    "C", "T", "VZ", "CMCSA", "TMUS", "GILD", "AMAT", "MU", "LRCX", "ADI",
    "KLAC", "SNPS", "CDNS", "NXPI", "MCHP", "ON", "ARM", "ASML", "TSM",
    "PANW", "CRWD", "NOW", "SNOW", "DDOG", "NET", "ZS", "MDB", "TEAM",
    "WDAY", "HUBS", "TWLO", "ZM", "SQ", "SHOP", "SPOT", "ANET", "FTNT",
    "DELL", "HPQ", "HPE", "NTAP", "WDC", "STX", "SMCI", "PLTR", "SOFI",
    "COIN", "HOOD", "MSTR", "MARA", "RIOT", "CLSK", "GME", "AMC", "LCID",
    "RIVN", "NIO", "XPEV", "LI", "F", "GM", "UBER", "LYFT", "ABNB", "DKNG",
    "SNAP", "PINS", "ROKU", "RBLX", "AFRM", "UPST", "CVNA", "OPEN", "ETSY",
    "EBAY", "CHWY", "BABA", "JD", "PDD", "SE", "MELI", "BIDU", "RDDT",
    "IONQ", "RGTI", "QBTS", "BBAI", "SOUN", "AI", "PATH", "LAZR", "ACHR",
    "JOBY", "RKLB", "ASTS", "QS", "CHPT", "BLNK", "EVGO", "PLUG", "FCEL",
    "RUN", "ENPH", "SEDG", "FSLR", "EOSE", "CRSP", "NTLA", "BEAM", "RXRX",
    "MRNA", "NVAX", "BNTX", "OCGN", "TLRY", "CGC", "SNDL",
    "SPY", "QQQ", "IWM", "DIA", "ARKK", "TQQQ", "SQQQ", "SOXL", "UVXY",
    "GLD", "SLV", "TLT", "XLE", "XLF", "XLK", "OXY", "DVN", "FANG", "SLB",
    "COP", "MPC", "VLO", "KMI", "FCX", "NEM", "AA", "X", "CLF", "NUE",
    "DE", "MMM", "LMT", "NOC", "UPS", "FDX", "DAL", "UAL", "AAL", "LUV",
    "CCL", "RCL", "NCLH", "MGM", "LVS", "WYNN", "MAR", "TGT", "DG", "DLTR",
    "TJX", "ULTA", "LULU", "DECK", "CROX", "YUM", "CMG", "DPZ", "HTZ",
]
IZLENEN = list(dict.fromkeys(IZLENEN))

DILIMLER = ["GECE", "SEANS_ONCESI", "ASIL_SEANS", "SEANS_SONRASI"]
DILIM_ADI = {"GECE": "🌙 Gece", "SEANS_ONCESI": "🌅 Seans öncesi",
             "ASIL_SEANS": "📈 ASIL SEANS", "SEANS_SONRASI": "🌆 Seans sonrası"}

_sayac = defaultdict(lambda: {"bullish": 0, "bearish": 0, "notr": 0})
_gorulen_mesaj = set()
_reddit_gundem = {}
_kilit = threading.Lock()
_sira_imleci = 0
_durum = {"son_kontrol": None, "tur": 0, "apewisdom": "-",
          "stocktwits": "-", "kapsam": "-", "toplam_mesaj": 0}


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram kapalı] {text}", flush=True)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[Telegram hata] {e}", flush=True)


def _ny_fark(ay):
    """ABD yaz saati (Mart-Kasım) UTC-4, kışın UTC-5."""
    return -4 if 3 <= ay <= 11 else -5


def _ny_simdi():
    u = datetime.now(timezone.utc)
    return u + timedelta(hours=_ny_fark(u.month))


def _dilim_bul(ny):
    dk = ny.hour * 60 + ny.minute
    if dk < 4 * 60:
        return "GECE"
    if dk < 9 * 60 + 30:
        return "SEANS_ONCESI"
    if dk < 16 * 60:
        return "ASIL_SEANS"
    if dk < 20 * 60:
        return "SEANS_SONRASI"
    return "GECE"


def _seans_saatinde_mi():
    ny = _ny_simdi()
    if ny.weekday() >= 5:
        return False
    dk = ny.hour * 60 + ny.minute
    return 4 * 60 <= dk < 16 * 60


def _apewisdom_cek():
    sonuc = {}
    try:
        r = requests.get("https://apewisdom.io/api/v1.0/filter/all-stocks/page/1",
                         headers=BASLIK, timeout=25)
        if r.status_code != 200:
            _durum["apewisdom"] = f"HTTP {r.status_code}"
            return sonuc
        for x in r.json().get("results", []):
            kod = str(x.get("ticker", "")).upper()
            if not kod:
                continue
            try:
                sonuc[kod] = {"konusma": int(x.get("mentions") or 0)}
            except (TypeError, ValueError):
                continue
        _durum["apewisdom"] = f"✓ {len(sonuc)}"
    except Exception as e:
        _durum["apewisdom"] = f"hata: {str(e)[:30]}"
    return sonuc


def _stocktwits_isle(kod):
    """Mesajları çekip SAATİNE göre seans dilimlerine ayırır."""
    try:
        r = requests.get(
            f"https://api.stocktwits.com/api/2/streams/symbol/{kod}.json",
            headers=BASLIK, timeout=20)
        if r.status_code != 200:
            return 0
        mesajlar = r.json().get("messages", [])
    except Exception:
        return 0

    yeni = 0
    for m in mesajlar:
        mid = m.get("id")
        if mid is None:
            continue
        anahtar = (kod, mid)
        if anahtar in _gorulen_mesaj:
            continue
        _gorulen_mesaj.add(anahtar)
        try:
            z = datetime.strptime(m.get("created_at", ""), "%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            continue
        ny = z + timedelta(hours=_ny_fark(z.month))
        dilim = _dilim_bul(ny)
        ent = m.get("entities") or {}
        duy = ent.get("sentiment") or {}
        b = (duy.get("basic") or "").lower() if duy else ""
        tur = "bullish" if b == "bullish" else ("bearish" if b == "bearish" else "notr")
        with _kilit:
            _sayac[(kod, str(ny.date()), dilim)][tur] += 1
            _durum["toplam_mesaj"] += 1
        yeni += 1
    return yeni


def _bu_turun_hisseleri(ape):
    global _sira_imleci
    secim = []
    gundem = sorted(ape.items(), key=lambda x: -(x[1].get("konusma") or 0))
    for kod, _ in gundem[:TUR_BASI_SORGU // 3]:
        if kod.isalpha() and len(kod) <= 5:
            secim.append(kod)
    kalan = TUR_BASI_SORGU - len(secim)
    if kalan > 0 and IZLENEN:
        for i in range(kalan):
            secim.append(IZLENEN[(_sira_imleci + i) % len(IZLENEN)])
        _sira_imleci = (_sira_imleci + kalan) % len(IZLENEN)
    return list(dict.fromkeys(secim))


def _turu_calistir():
    _durum["tur"] += 1
    ape = _apewisdom_cek()
    with _kilit:
        _reddit_gundem.clear()
        _reddit_gundem.update(ape)
    hedefler = _bu_turun_hisseleri(ape)
    yeni_toplam = 0
    for kod in hedefler:
        yeni_toplam += _stocktwits_isle(kod)
        time.sleep(1.2)
    _durum["stocktwits"] = f"✓ {len(hedefler)} sorgu"
    _durum["kapsam"] = f"{_sira_imleci}/{len(IZLENEN)}"
    _durum["son_kontrol"] = _ny_simdi().strftime("%H:%M NY")
    if len(_gorulen_mesaj) > 60000:
        with _kilit:
            _gorulen_mesaj.clear()
    print(f"[Tur {_durum['tur']}] ApeWisdom {_durum['apewisdom']} | "
          f"{len(hedefler)} hisse sorgulandı | {yeni_toplam} yeni mesaj", flush=True)


def _kontrol_dongusu():
    while True:
        try:
            _turu_calistir()
        except Exception as e:
            print(f"[Kontrol hatası] {e}", flush=True)
        time.sleep(SIK_ARALIK_SN if _seans_saatinde_mi() else SEYREK_ARALIK_SN)


def _bugun_ozet():
    bugun = str(_ny_simdi().date())
    with _kilit:
        veri = {k: dict(v) for k, v in _sayac.items() if k[1] == bugun}
        gundem = dict(_reddit_gundem)
    hisseler = defaultdict(lambda: {d: {"bullish": 0, "bearish": 0, "notr": 0}
                                     for d in DILIMLER})
    for (kod, _, dilim), v in veri.items():
        for t in ("bullish", "bearish", "notr"):
            hisseler[kod][dilim][t] += v[t]
    satirlar = []
    for kod, d in hisseler.items():
        toplam = sum(d[x]["bullish"] + d[x]["bearish"] + d[x]["notr"] for x in DILIMLER)
        if toplam < MIN_MESAJ:
            continue
        satir = {"hisse": kod, "toplam": toplam,
                 "reddit": gundem.get(kod, {}).get("konusma")}
        for dl in DILIMLER:
            b, s = d[dl]["bullish"], d[dl]["bearish"]
            satir[dl + "_n"] = b + s + d[dl]["notr"]
            satir[dl + "_long"] = b
            satir[dl + "_short"] = s
            satir[dl + "_oran"] = round(b / (b + s) * 100, 1) if (b + s) else None
        o, a = satir["SEANS_ONCESI_oran"], satir["ASIL_SEANS_oran"]
        satir["degisim"] = round(a - o, 1) if (o is not None and a is not None) else None
        satirlar.append(satir)
    satirlar.sort(key=lambda x: -x["toplam"])
    return satirlar


def rapor_gonder():
    """2026-09-02: kullanıcı raporun kafa karıştırıcı olduğunu söyledi.
    Artık tablo/yüzde yığını yerine, hisse hisse DÜZ TÜRKÇE cümle:
    kaç kişi LONG dedi, kaç kişi SHORT dedi, seans açılınca ne değişti."""
    satirlar = _bugun_ozet()
    ny = _ny_simdi()
    tr = ny + timedelta(hours=7)
    if not satirlar:
        send_telegram_message(
            f"💬 Sosyal duygu ({tr.strftime('%H:%M')} TR)\n"
            f"Bugün henüz yeterli mesaj toplanmadı. "
            f"Toplanan: {_durum['toplam_mesaj']}")
        return

    s = [f"💬 İNSANLAR NE DİYOR? — {tr.strftime('%d.%m %H:%M')} TR",
         f"Şu an: {DILIM_ADI[_dilim_bul(ny)]}\n"]

    for x in satirlar[:12]:
        ob, os_ = x["SEANS_ONCESI_long"], x["SEANS_ONCESI_short"]
        ab, as_ = x["ASIL_SEANS_long"], x["ASIL_SEANS_short"]
        s.append(f"━━━ {x['hisse']} ━━━")
        if ob + os_ > 0:
            s.append(f"🌅 Seans öncesi: {ob} kişi LONG, {os_} kişi SHORT")
        if ab + as_ > 0:
            s.append(f"📈 Seans içinde: {ab} kişi LONG, {as_} kişi SHORT")
        # tek cumlelik yorum
        o, a = x["SEANS_ONCESI_oran"], x["ASIL_SEANS_oran"]
        if o is not None and a is not None:
            fark = a - o
            if fark >= 15:
                s.append(f"   → Seans açılınca fikir İYİLEŞTİ "
                         f"(%{o:.0f} → %{a:.0f} long)")
            elif fark <= -15:
                s.append(f"   → Seans açılınca fikir BOZULDU "
                         f"(%{o:.0f} → %{a:.0f} long)")
            else:
                s.append(f"   → Fikir değişmedi (%{a:.0f} long)")
        elif a is not None:
            if a >= 70:
                s.append(f"   → Çoğunluk LONG tarafında (%{a:.0f})")
            elif a <= 30:
                s.append(f"   → Çoğunluk SHORT tarafında (%{a:.0f} long)")
            else:
                s.append(f"   → Görüşler bölünmüş (%{a:.0f} long)")
        s.append("")

    s.append("ℹ️ Bu rakamlar StockTwits kullanıcılarının kendi "
             "işaretlediği LONG/SHORT etiketleridir - tahmin değil.\n"
             "⚠️ AL/SAT tavsiyesi DEĞİL. Kalabalığın coşkusu genelde "
             "tepe noktalarında en yüksektir.")
    send_telegram_message("\n".join(s))


def _rapor_dongusu():
    """2026-09-02: önceden günde 3 kez sabit saatlerde gönderiyordu,
    kullanıcı daha sık istedi. Artık 30 dakikada bir - ama SADECE
    seans öncesi ve seans saatlerinde (gece boşuna bildirim gelmesin)."""
    while True:
        try:
            if _seans_saatinde_mi():
                rapor_gonder()
        except Exception as e:
            print(f"[Rapor hatası] {e}", flush=True)
        time.sleep(RAPOR_ARALIGI_SN)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (abd sosyal duygu)", 200


@app.route("/")
def ana():
    satirlar = _bugun_ozet()
    ny = _ny_simdi()
    h = ["<h2>ABD Sosyal Duygu — Seans Dilimlerine Göre</h2>",
         f"<p>NY {ny.strftime('%H:%M')} | Şu an: {DILIM_ADI[_dilim_bul(ny)]} | "
         f"Tur {_durum['tur']} | Mesaj {_durum['toplam_mesaj']}</p>",
         f"<p>ApeWisdom {_durum['apewisdom']} | havuz taraması "
         f"{_durum['kapsam']}</p>",
         "<table border=1 cellpadding=5><tr><th>hisse</th><th>gece</th>"
         "<th>seans öncesi</th><th>ASIL SEANS</th><th>seans sonrası</th>"
         "<th>değişim</th><th>mesaj</th></tr>"]
    for x in satirlar[:60]:
        def f(k):
            v = x[k + "_oran"]
            return f"{v:.0f}% ({x[k+'_n']})" if v is not None else "-"
        d = f"{x['degisim']:+.0f}" if x["degisim"] is not None else "-"
        h.append(f"<tr><td><b>{x['hisse']}</b></td><td>{f('GECE')}</td>"
                 f"<td>{f('SEANS_ONCESI')}</td><td>{f('ASIL_SEANS')}</td>"
                 f"<td>{f('SEANS_SONRASI')}</td><td>{d}</td>"
                 f"<td>{x['toplam']}</td></tr>")
    h.append("</table><p><a href='/rapor'>Raporu gönder</a> | "
             "<a href='/simdi'>Şimdi kontrol et</a></p>")
    return "\n".join(h)


@app.route("/rapor")
def rapor_gor():
    rapor_gonder()
    return "Gönderildi. <a href='/'>Geri</a>"


@app.route("/simdi")
def simdi():
    threading.Thread(target=_turu_calistir, daemon=True).start()
    return "Kontrol başlatıldı. <a href='/'>Geri</a>"


def _ping():
    harici = (os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
              or os.environ.get("HARICI_URL", "").rstrip("/"))
    time.sleep(30)
    while True:
        try:
            if harici:
                requests.get(f"{harici}/health", timeout=20)
        except Exception:
            pass
        time.sleep(600)


if __name__ == "__main__":
    print(f"[BAŞLANGIÇ] abd_sosyal_duygu.py — {KOD_SURUMU}", flush=True)
    send_telegram_message(
        f"💬 ABD SOSYAL DUYGU v2 — SEANS DİLİMLERİNE GÖRE\n\n"
        f"Artık 24 saatlik toplam değil, seans dilimlerine ayrılmış:\n"
        f"  🌅 Seans öncesi (TR 11:00-16:30)\n"
        f"  📈 ASIL SEANS (TR 16:30-23:00)\n"
        f"  🌙 Gece  🌆 Seans sonrası\n\n"
        f"Ayrıca 'seans açılınca fikir değişti mi' ölçülüyor.\n\n"
        f"Havuz {len(IZLENEN)} hisse, her turda {TUR_BASI_SORGU} tanesi "
        f"(gündemdekiler + sırayla dönen dilim).\n"
        f"Kontrol: seans saatlerinde {SIK_ARALIK_SN//60} dk, dışında "
        f"{SEYREK_ARALIK_SN//60} dk.\n"
        f"Rapor: {RAPOR_ARALIGI_SN//60} dakikada bir (sadece seans öncesi ve seans saatlerinde).")
    threading.Thread(target=_kontrol_dongusu, daemon=True).start()
    threading.Thread(target=_rapor_dongusu, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
