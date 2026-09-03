"""
abd_sosyal_duygu.py — ABD HİSSELERİ SOSYAL DUYGU TAKİPÇİSİ
============================================================
2026-09-02 — Kullanıcının fikri: "insanların konuştuklarını analiz
edelim, hisseyi değil İNSANI analiz edelim." Telegram tarafı zahmetli
çıktı (grup önizlemeleri kapalı, hesap girişi gerekiyor). ABD için
çok daha temiz kaynaklar var - bu dosya onları kullanıyor.

⚠️ SADECE ABD — BIST bu dosyada YOK (kullanıcının isteği).

KAYNAKLAR (ikisi de ÜCRETSİZ, anahtar/giriş GEREKTİRMEZ):

1) STOCKTWITS  (stocktwits.com)
   Tamamen borsa için kurulmuş sosyal ağ. EN BÜYÜK AVANTAJI:
   kullanıcılar mesajlarını KENDİLERİ "Bullish"/"Bearish" diye
   etiketliyor. Yani benim kelime listemle tahmin yürütmeme gerek
   yok - GERÇEK duygu verisi geliyor. Ayrıca her hissenin "watchlist"
   (takip eden kişi) sayısı da var.

2) APEWISDOM  (apewisdom.io)
   Reddit'i (r/wallstreetbets, r/stocks vb.) tarayıp hangi hissenin
   kaç kez konuşulduğunu sayan ücretsiz API. Biz saymıyoruz, hazır
   geliyor. "24 saatte bahsedilme değişimi" gibi hazır ölçüler var.

NE ÖLÇÜYOR:
  • Bahsedilme sayısı ve 24 saatlik DEĞİŞİMİ (ani artış = dikkat)
  • Bullish/Bearish oranı (StockTwits'in kendi etiketlerinden)
  • Reddit sıralaması ve sıralama değişimi
  • Hangi hisse birden gündeme girdi (yeni girenler)

⚠️ ARAŞTIRMA UYARISI: Akademik çalışmalar bireysel yatırımcı
coşkusunun genelde TEPE noktalarında zirve yaptığını gösteriyor -
"herkes konuşuyor" çoğu zaman GEÇ kalındığının işaretidir. Bu yüzden
sistem AL/SAT sinyali ÜRETMİYOR - sadece ölçüyor ve raporluyor.
Kullanıcı kendi gözlemini yapacak (kendisi böyle istedi).

KURULUM: Hiçbir ayar gerekmiyor. Dosyayı yükle, Start Command'ı
"python abd_sosyal_duygu.py" yap, deploy et. O kadar.

Start Command:  python abd_sosyal_duygu.py
"""
import os
import time
import threading
from collections import defaultdict
from datetime import datetime, timezone

import requests
from flask import Flask

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", ".")
PORT = int(os.environ.get("PORT", "10000"))
KOD_SURUMU = "abd-sosyal-duygu-v1-2026-09-02"

KONTROL_ARALIGI_SN = int(os.environ.get("KONTROL_ARALIGI_SN", "1800"))  # 30 dk
RAPOR_ARALIGI_SN = int(os.environ.get("RAPOR_ARALIGI_SN", "3600"))      # 1 saat
ANI_ARTIS_KATI = float(os.environ.get("ANI_ARTIS_KATI", "2.0"))         # 2x artis = dikkat

BASLIK = {"User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36"}

# StockTwits'te tek tek sorgulanacak hisseler (populer + oynak)
IZLENEN = [
    "TSLA", "NVDA", "AAPL", "AMD", "PLTR", "SOFI", "COIN", "HOOD", "MSTR",
    "SMCI", "GME", "AMC", "MARA", "RIOT", "LCID", "RIVN", "NIO", "INTC",
    "META", "AMZN", "GOOGL", "MSFT", "NFLX", "BABA", "SPY", "QQQ",
    "IONQ", "RGTI", "BBAI", "SOUN", "ACHR", "JOBY", "CVNA", "AFRM", "UPST",
]

_gecmis = defaultdict(list)      # hisse -> [(zaman, bahsedilme)]
_son_veri = {}
_kilit = threading.Lock()
_durum = {"son_kontrol": None, "apewisdom": "-", "stocktwits": "-", "tur": 0}


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram kapalı] {text}", flush=True)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[Telegram hata] {e}", flush=True)


def _apewisdom_cek():
    """Reddit bahsedilme sayilari - hazir geliyor, biz saymiyoruz."""
    sonuc = {}
    try:
        r = requests.get("https://apewisdom.io/api/v1.0/filter/all-stocks/page/1",
                         headers=BASLIK, timeout=25)
        if r.status_code != 200:
            _durum["apewisdom"] = f"HTTP {r.status_code}"
            return sonuc
        d = r.json()
        for x in d.get("results", []):
            kod = str(x.get("ticker", "")).upper()
            if not kod:
                continue
            try:
                bahis = int(x.get("mentions") or 0)
                onceki = int(x.get("mentions_24h_ago") or 0)
            except (TypeError, ValueError):
                continue
            sonuc[kod] = {
                "reddit_bahis": bahis,
                "reddit_onceki": onceki,
                "reddit_degisim": round((bahis - onceki) / onceki * 100, 1) if onceki > 0 else None,
                "reddit_sira": x.get("rank"),
                "reddit_sira_onceki": x.get("rank_24h_ago"),
                "isim": x.get("name", ""),
            }
        _durum["apewisdom"] = f"✓ {len(sonuc)} hisse"
    except Exception as e:
        _durum["apewisdom"] = f"hata: {str(e)[:40]}"
    return sonuc


def _stocktwits_cek(kod):
    """Bir hissenin StockTwits akisi. Kullanicilarin KENDI etiketleri
    (Bullish/Bearish) okunuyor - kelime tahmini yapmiyoruz."""
    try:
        r = requests.get(
            f"https://api.stocktwits.com/api/2/streams/symbol/{kod}.json",
            headers=BASLIK, timeout=20)
        if r.status_code != 200:
            return None
        d = r.json()
        mesajlar = d.get("messages", [])
        if not mesajlar:
            return None
        bull = bear = notr = 0
        for m in mesajlar:
            ent = m.get("entities") or {}
            duy = (ent.get("sentiment") or {})
            b = (duy.get("basic") or "").lower() if duy else ""
            if b == "bullish":
                bull += 1
            elif b == "bearish":
                bear += 1
            else:
                notr += 1
        sembol = (d.get("symbol") or {})
        return {"st_bullish": bull, "st_bearish": bear, "st_notr": notr,
                "st_toplam": len(mesajlar),
                "st_oran": round(bull / max(bull + bear, 1) * 100, 1),
                "st_takipci": sembol.get("watchlist_count")}
    except Exception:
        return None


def _turu_calistir():
    _durum["tur"] += 1
    ape = _apewisdom_cek()
    st_basarili = 0
    birlesik = {}

    for kod in IZLENEN:
        st = _stocktwits_cek(kod)
        if st:
            st_basarili += 1
            birlesik[kod] = dict(st)
        time.sleep(1.5)   # nazik bekleme - hiz sinirina takilmamak icin
    _durum["stocktwits"] = f"✓ {st_basarili}/{len(IZLENEN)}"

    # apewisdom'daki HER hisseyi de ekle (izlenen listede olmasa bile -
    # boylece "birden gundeme giren" hisseleri de yakalariz)
    for kod, v in ape.items():
        birlesik.setdefault(kod, {}).update(v)

    simdi = datetime.now(timezone.utc)
    with _kilit:
        for kod, v in birlesik.items():
            bahis = v.get("reddit_bahis") or v.get("st_toplam") or 0
            _gecmis[kod].append((simdi, bahis))
            if len(_gecmis[kod]) > 200:
                _gecmis[kod] = _gecmis[kod][-200:]
        _son_veri.clear()
        _son_veri.update(birlesik)
        _durum["son_kontrol"] = simdi.strftime("%H:%M")
    print(f"[Tur {_durum['tur']}] ApeWisdom: {_durum['apewisdom']} | "
          f"StockTwits: {_durum['stocktwits']} | toplam {len(birlesik)} hisse",
          flush=True)


def _kontrol_dongusu():
    while True:
        try:
            _turu_calistir()
        except Exception as e:
            print(f"[Kontrol hatası] {e}", flush=True)
        time.sleep(KONTROL_ARALIGI_SN)


def _analiz():
    """Rapor icin siralanmis liste + ani artis yapanlar."""
    with _kilit:
        veri = dict(_son_veri)
        gecmis = {k: list(v) for k, v in _gecmis.items()}
    satirlar = []
    for kod, v in veri.items():
        bahis = v.get("reddit_bahis") or v.get("st_toplam") or 0
        if bahis <= 0:
            continue
        # kendi olctugumuz artis (son tur vs 4 tur once)
        g = gecmis.get(kod, [])
        kendi_artis = None
        if len(g) >= 2:
            eski = g[max(0, len(g) - 5)][1]
            if eski > 0:
                kendi_artis = round((g[-1][1] - eski) / eski * 100, 1)
        satirlar.append({
            "hisse": kod, "bahis": bahis,
            "reddit_degisim": v.get("reddit_degisim"),
            "reddit_sira": v.get("reddit_sira"),
            "kendi_artis": kendi_artis,
            "st_oran": v.get("st_oran"),
            "st_toplam": v.get("st_toplam"),
            "st_takipci": v.get("st_takipci"),
        })
    satirlar.sort(key=lambda x: -x["bahis"])
    aniler = [x for x in satirlar
              if (x["reddit_degisim"] is not None and x["reddit_degisim"] >= ANI_ARTIS_KATI * 100)
              or (x["kendi_artis"] is not None and x["kendi_artis"] >= ANI_ARTIS_KATI * 100)]
    return satirlar, aniler


def rapor_gonder():
    satirlar, aniler = _analiz()
    if not satirlar:
        send_telegram_message(
            f"💬 ABD sosyal duygu raporu\n"
            f"Henüz veri yok.\nApeWisdom: {_durum['apewisdom']} | "
            f"StockTwits: {_durum['stocktwits']}")
        return
    s = [f"💬 ABD SOSYAL DUYGU RAPORU",
         f"Son kontrol: {_durum['son_kontrol']} | Tur: {_durum['tur']}",
         f"Kaynak: ApeWisdom(Reddit) {_durum['apewisdom']} | "
         f"StockTwits {_durum['stocktwits']}\n"]
    if aniler:
        s.append(f"🔥 ANİ KONUŞMA ARTIŞI (≥%{int(ANI_ARTIS_KATI*100)}):")
        for x in aniler[:10]:
            d = x["reddit_degisim"] if x["reddit_degisim"] is not None else x["kendi_artis"]
            ek = f" | boğa %{x['st_oran']}" if x.get("st_oran") is not None else ""
            s.append(f"   {x['hisse']:<6} bahis {x['bahis']:<5} (+%{d}){ek}")
        s.append("")
    s.append("EN ÇOK KONUŞULANLAR:")
    s.append(f"{'hisse':<7}{'bahis':>7}{'24s δ':>8}{'boğa%':>7}{'mesaj':>7}")
    for x in satirlar[:20]:
        d = f"{x['reddit_degisim']:+.0f}%" if x["reddit_degisim"] is not None else "-"
        o = f"{x['st_oran']:.0f}%" if x.get("st_oran") is not None else "-"
        m = str(x.get("st_toplam") or "-")
        s.append(f"{x['hisse']:<7}{x['bahis']:>7}{d:>8}{o:>7}{m:>7}")
    s.append("\n⚠️ Bu AL/SAT sinyali DEĞİL - sadece konuşma sayımı.\n"
             "'boğa%' = StockTwits kullanıcılarının KENDİ etiketi "
             "(bullish/bearish), tahmin değil gerçek veri.\n"
             "Araştırmalar bireysel coşkunun TEPE noktalarında zirve "
             "yaptığını gösteriyor - 'ani artış' geç kalmışlık işareti "
             "de olabilir. Kendi gözlemini yap.")
    send_telegram_message("\n".join(s))


def _rapor_dongusu():
    while True:
        time.sleep(RAPOR_ARALIGI_SN)
        try:
            rapor_gonder()
        except Exception as e:
            print(f"[Rapor hatası] {e}", flush=True)


app = Flask(__name__)


@app.route("/health")
def health():
    return "OK (abd sosyal duygu)", 200


@app.route("/")
def ana():
    satirlar, aniler = _analiz()
    h = ["<h2>ABD Sosyal Duygu Takipçisi</h2>",
         f"<p>Son kontrol: {_durum['son_kontrol'] or '-'} | Tur: {_durum['tur']}</p>",
         f"<p>ApeWisdom: {_durum['apewisdom']} | StockTwits: {_durum['stocktwits']}</p>"]
    if aniler:
        h.append("<h3>🔥 Ani artış</h3><ul>")
        for x in aniler[:10]:
            h.append(f"<li>{x['hisse']} — bahis {x['bahis']}</li>")
        h.append("</ul>")
    h.append("<h3>En çok konuşulanlar</h3><table border=1 cellpadding=4>"
             "<tr><th>hisse</th><th>bahis</th><th>24s δ</th><th>boğa%</th>"
             "<th>StockTwits mesaj</th><th>takipçi</th></tr>")
    for x in satirlar[:40]:
        h.append(f"<tr><td>{x['hisse']}</td><td>{x['bahis']}</td>"
                 f"<td>{x['reddit_degisim'] if x['reddit_degisim'] is not None else '-'}</td>"
                 f"<td>{x['st_oran'] if x.get('st_oran') is not None else '-'}</td>"
                 f"<td>{x.get('st_toplam') or '-'}</td>"
                 f"<td>{x.get('st_takipci') or '-'}</td></tr>")
    h.append("</table><p><a href='/rapor'>Raporu Telegram'a gönder</a> | "
             "<a href='/simdi'>Şimdi kontrol et</a></p>")
    return "\n".join(h)


@app.route("/rapor")
def rapor_gor():
    rapor_gonder()
    return "Gönderildi. <a href='/'>Geri</a>"


@app.route("/simdi")
def simdi():
    threading.Thread(target=_turu_calistir, daemon=True).start()
    return "Kontrol başlatıldı (1-2 dk sürer). <a href='/'>Geri</a>"


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
        f"💬 ABD SOSYAL DUYGU TAKİPÇİSİ başladı — {KOD_SURUMU}\n\n"
        f"Kaynaklar (ikisi de ücretsiz, anahtar gerekmiyor):\n"
        f"  • StockTwits — kullanıcıların KENDİ bullish/bearish "
        f"etiketleri ({len(IZLENEN)} hisse)\n"
        f"  • ApeWisdom — Reddit bahsedilme sayıları (tüm hisseler)\n\n"
        f"Kontrol: {KONTROL_ARALIGI_SN//60} dk | Rapor: {RAPOR_ARALIGI_SN//60} dk\n"
        f"İlk veri birkaç dakika içinde gelir.")
    threading.Thread(target=_kontrol_dongusu, daemon=True).start()
    threading.Thread(target=_rapor_dongusu, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
