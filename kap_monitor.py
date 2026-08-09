"""
kap_monitor.py — PASİF KAP TAZELİK GÖZLEMCİSİ
================================================
AMAÇ: Canlı sinyal sistemine (stock_screener_bot.py) DOKUNMAZ, hiçbir
taramayı etkilemez, engelleme yapmaz, sinyal üretmez. Tek işi:
kap_pazartesi_testi.py'de test edilmiş kanalları (yayıncı RSS +
Google News, aynı sorgular/regex) periyodik olarak arka planda
yoklayıp KAP-kalıplı başlıkları zaman damgasıyla biriktirmek.

/kap komutu, o ana kadar biriken veriden GERÇEK tazelik istatistiği
üretir — tek seferlik bir test yerine, günler boyunca birikmiş çoklu
gerçek bildirim üzerinden. Ne kadar uzun toplanırsa o kadar güvenilir.

İZOLASYON: stock_screener_bot.py bu modülü try/except içinde import
eder — bu dosyada bir hata olsa bile ana sistem (BIST/ABD taramaları,
çıkış uyarıları, self-check) etkilenmez. Kendi try/except'i ile
run_forever döngüsüne eklenir, kendi zamanlayıcısı vardır.

KARAR EŞİKLERİ (kap_pazartesi_testi.py ile aynı):
  < 30 dk   -> radar için uygun (15 dk'lık doğrulama anlamlı olur)
  30-180 dk -> sınırda, "gün içi bağlam" olarak kullanılabilir
  > 180 dk  -> anlık doğrulama için yetersiz
"""

import os
import re
import csv
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

DATA_DIR = os.environ.get("DATA_DIR", ".")


def _data_path(filename: str) -> str:
    return os.path.join(DATA_DIR, filename)


HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "text/xml, application/rss+xml, */*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9",
}

# kap_pazartesi_testi.py'de test edilmiş, aynen kullanılıyor.
GOOGLE_SORGULARI = [
    ("KAP Özel Durum", "KAP+%22%C3%96zel+Durum+A%C3%A7%C4%B1klamas%C4%B1%22"),
    ("KAP *** kalıbı", "%22KAP+***%22"),
]

YAYINCI_ADAYLARI = [
    ("Yeni Şafak ekonomi", "https://www.yenisafak.com/rss?xml=ekonomi"),
    ("Yeni Şafak genel", "https://www.yenisafak.com/rss"),
    ("Yeni Şafak borsa", "https://www.yenisafak.com/rss?xml=borsa"),
    ("GZT ekonomi", "https://www.gzt.com/rss/ekonomi"),
    ("GZT genel", "https://www.gzt.com/rss"),
    ("GZT finans", "https://www.gzt.com/rss/finans"),
]

KOD_KALIBI = re.compile(r"KAP\s*\*\*\*.*?\*\*\*\s*([A-Z]{4,6})\s*\*\*\*", re.S)

KAP_LOG_FILE = _data_path("kap_freshness_log.csv")
KAP_LOG_FIELDS = ["kaynak", "kod", "baslik", "pub_time", "gorulme_time"]
KAP_LOG_MAX_ROWS = 3000  # dosya sinirsiz buyumesin

KAP_COLLECT_INTERVAL_MINUTES = int(os.environ.get("KAP_COLLECT_INTERVAL_MINUTES", "20"))

_last_collect_time = None
_last_collect_error = None


def _temizle(s: str) -> str:
    s = re.sub(r"<!\[CDATA\[|\]\]>", "", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _item_ayikla(xml: str):
    cikti = []
    for o in re.findall(r"<item>(.*?)</item>", xml, re.S | re.I):
        b = re.search(r"<title[^>]*>(.*?)</title>", o, re.S | re.I)
        t = re.search(r"<pubDate[^>]*>(.*?)</pubDate>", o, re.S | re.I)
        if not b:
            continue
        tarih = None
        if t:
            try:
                tarih = parsedate_to_datetime(_temizle(t.group(1)))
                if tarih.tzinfo is None:
                    tarih = tarih.replace(tzinfo=timezone.utc)
            except Exception:
                pass
        cikti.append((_temizle(b.group(1)), tarih))
    return cikti


def _fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text


def _append_log(rows):
    exists = os.path.exists(KAP_LOG_FILE)
    with open(KAP_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=KAP_LOG_FIELDS)
        if not exists:
            w.writeheader()
        for row in rows:
            w.writerow(row)


def _read_log():
    if not os.path.exists(KAP_LOG_FILE):
        return []
    with open(KAP_LOG_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _trim_log_if_needed():
    rows = _read_log()
    if len(rows) > KAP_LOG_MAX_ROWS:
        rows = rows[-KAP_LOG_MAX_ROWS:]
        with open(KAP_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=KAP_LOG_FIELDS)
            w.writeheader()
            w.writerows(rows)


def collect_once() -> int:
    """Tum kanallari bir kez yoklar, KAP-kalipli basliklari zaman
    damgasiyla loglar. Sinyal uretmez, hicbir seyi tetiklemez/engellemez
    - sadece kayit. Donen deger: bu turda eklenen yeni satir sayisi."""
    now = datetime.now(timezone.utc)
    yeni = []

    for ad, url in YAYINCI_ADAYLARI:
        try:
            xml = _fetch(url)
        except Exception:
            continue
        for baslik, pub in _item_ayikla(xml):
            m = KOD_KALIBI.search(baslik)
            if not m:
                continue
            yeni.append({
                "kaynak": ad, "kod": m.group(1), "baslik": baslik[:150],
                "pub_time": pub.isoformat() if pub else "",
                "gorulme_time": now.isoformat(),
            })

    for ad, q in GOOGLE_SORGULARI:
        url = f"https://news.google.com/rss/search?q={q}&hl=tr&gl=TR&ceid=TR:tr"
        try:
            xml = _fetch(url)
        except Exception:
            continue
        for baslik, pub in _item_ayikla(xml):
            m = KOD_KALIBI.search(baslik)
            if not m:
                continue
            yeni.append({
                "kaynak": ad, "kod": m.group(1), "baslik": baslik[:150],
                "pub_time": pub.isoformat() if pub else "",
                "gorulme_time": now.isoformat(),
            })

    if yeni:
        _append_log(yeni)
        _trim_log_if_needed()
    return len(yeni)


def maybe_collect():
    """run_forever donguesunden HER TURDA cagrilmasi guvenlidir - kendi
    zamanlayicisina gore KAP_COLLECT_INTERVAL_MINUTES'da bir gercekten
    calisir. Piyasa acik/kapali kontrolu YOK: amac zaten kaynaklarin
    7/24 davranisini gormek, sadece seans ici olani degil."""
    global _last_collect_time, _last_collect_error
    if (_last_collect_time is not None and
            (datetime.now() - _last_collect_time).total_seconds()
            < KAP_COLLECT_INTERVAL_MINUTES * 60):
        return
    _last_collect_time = datetime.now()
    try:
        collect_once()
        _last_collect_error = None
    except Exception as e:
        _last_collect_error = str(e)
        raise


def build_kap_report() -> str:
    """/kap komutu icin - simdiye kadar biriken veriden coklu-gozlem
    tazelik istatistigi ve radar-uygunluk karari uretir."""
    rows = _read_log()
    if not rows:
        eksik = f"\n⚠️ Son toplama hatası: {_last_collect_error}" if _last_collect_error else ""
        return (
            "📡 [KAP GÖZLEMCİSİ] Henüz veri toplanmadı.\n"
            f"Her {KAP_COLLECT_INTERVAL_MINUTES} dakikada bir arka planda "
            "yokluyor, sinyal sistemini etkilemiyor. Birkaç tur sonra tekrar sor."
            + eksik
        )

    now = datetime.now(timezone.utc)
    yaslar_by_kaynak = {}
    bozuk = 0
    for r in rows:
        if not r.get("pub_time") or not r.get("gorulme_time"):
            continue
        try:
            pub = datetime.fromisoformat(r["pub_time"])
            gorulme = datetime.fromisoformat(r["gorulme_time"])
        except Exception:
            bozuk += 1
            continue
        # Gorulme aninda bildirim ne kadar "yasli"ydi - asil olcmek istedigimiz bu.
        yas_dk = (gorulme - pub).total_seconds() / 60
        if yas_dk < 0:
            continue  # saat kaymasi / bozuk veri
        yaslar_by_kaynak.setdefault(r["kaynak"], []).append(yas_dk)

    ilk_kayit = min((r["gorulme_time"] for r in rows if r.get("gorulme_time")), default="")
    lines = [
        "📡 [KAP GÖZLEMCİSİ] Biriken veri analizi",
        f"Toplam kayıt: {len(rows)} | İzleme başlangıcı: {ilk_kayit[:16].replace('T', ' ')}",
    ]

    if not yaslar_by_kaynak:
        lines.append("⚠️ Tarihli hiçbir kayıt yok — kanallar başlık veriyor ama pubDate boş/bozuk geliyor olabilir.")
        return "\n".join(lines)

    en_iyi_kaynak, en_iyi_medyan = None, None
    for kaynak, yaslar in sorted(yaslar_by_kaynak.items()):
        yaslar_sirali = sorted(yaslar)
        medyan = yaslar_sirali[len(yaslar_sirali) // 2]
        en_taze = min(yaslar_sirali)
        lines.append(f"  {kaynak}: {len(yaslar)} örnek | medyan {medyan:.0f} dk | en taze {en_taze:.0f} dk")
        if en_iyi_medyan is None or medyan < en_iyi_medyan:
            en_iyi_medyan = medyan
            en_iyi_kaynak = kaynak

    lines.append("")
    lines.append(f"🏆 En iyi kanal: {en_iyi_kaynak} (medyan {en_iyi_medyan:.0f} dk gecikme)")
    if en_iyi_medyan < 30:
        lines.append("✅ RADAR İÇİN UYGUN — 15 dk'lık doğrulama penceresi anlamlı olur.")
    elif en_iyi_medyan < 180:
        lines.append("⚠️ SINIRDA — anlık teyit yerine 'gün içi bağlam' olarak kullanılabilir.")
    else:
        lines.append("❌ ANLIK DOĞRULAMA İÇİN YETERSİZ — günlük özet için değerli olabilir, radar için değil.")

    lines.append("")
    lines.append(
        f"ℹ️ Tek seferlik testten farkı: {len(rows)} kayıt, birden fazla gerçek "
        "bildirim üzerinden ölçülüyor. Ne kadar uzun toplanırsa o kadar güvenilir "
        "— birkaç gün sonra tekrar sorman önerilir."
    )
    if bozuk:
        lines.append(f"({bozuk} kayıt tarih hatası yüzünden sayıma dahil edilmedi)")
    return "\n".join(lines)
