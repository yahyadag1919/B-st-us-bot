"""
abd_akilli_para.py — SEC İÇERİDEN İŞLEM + HABER + FİYAT TEYİDİ
============================================================
2026-09-11 — Kullanıcının kararıyla kuruldu.

NE İŞE YARAR:
ABD'de BIST'teki "emir defteri/aracı kurum" arayışının yerini tutacak,
tamamen ÜCRETSİZ 3 kaynaklı bir "akıllı para" izleme sistemi:

  1) SEC EDGAR (Form 4) — bir şirketin CEO/CFO/yönetim kurulu üyesi
     kendi hissesini açık piyasadan ALDIĞINDA veya SATTIĞINDA, bu
     bildirim SEC'e düştüğü an (genelde saatler içinde) yakalanır.
     Kayıt/API key gerekmez, tamamen resmi ve ücretsiz.
  2) Finnhub — takip edilen hisselerde çıkan güncel haberler.
  3) Alpaca (IEX borsası) — yukarıdaki iki sinyalden biri tetiklendiğinde
     hissenin O ANKİ gerçek zamanlı fiyatını ekleyerek bağlam katar.

abd_sosyal_duygu.py İLE AYNI Telegram bot/sohbeti (TELEGRAM_TOKEN /
TELEGRAM_CHAT_ID) kullanılıyor - o modülün HİÇ getUpdates/komut dinleme
döngüsü olmadığı doğrulandı (sadece tek yönlü mesaj gönderiyor), bu
yüzden 409 Conflict riski YOK, iki modül rahatça aynı sohbete yazabilir.

NE YAPMAZ:
- Level 2 / emir defteri derinliği vermez (ABD'de ücretsiz hiçbir yerde
  bulunmuyor, araştırmayla doğrulandı)
- Otomatik emir vermez, sadece Telegram'a bilgi/uyarı gönderir
- Form 4'teki her kod tipini (grant, opsiyon kullanımı, hediye vb.)
  bildirmez - sadece açık piyasa ALIM (P) ve SATIM (S) kodlarını, gerçek
  "akıllı para" sinyali sayılan türler bunlar
"""
import os
import re
import ast
import time
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

# =============================================================================
# YAPILANDIRMA
# =============================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
SEC_EDGAR_EMAIL = os.environ.get("SEC_EDGAR_EMAIL", "kripto-bot-user@example.com")
DATA_DIR = os.environ.get("DATA_DIR", ".")

# SEC, User-Agent başlığında gerçek bir e-posta ister (fair-use politikası).
# Render'da SEC_EDGAR_EMAIL değişkenini kendi e-postanla tanımlaman önerilir,
# tanımlamazsan yukarıdaki genel değerle çalışır (SEC bunu engellemez ama
# kendi e-postan daha doğru bir uygulama olur).
_SEC_HEADERS = {"User-Agent": f"BistUsBot Yahya {SEC_EDGAR_EMAIL}"}

AKILLI_PARA_SURUM = "abd-akilli-para-v1-2026-09-11"

FORM4_GORULEN_DOSYASI = os.path.join(DATA_DIR, "form4_gorulen.json")
HABER_GORULEN_DOSYASI = os.path.join(DATA_DIR, "haber_gorulen.json")

FORM4_TARAMA_ARALIGI_SN = 20 * 60   # tüm liste her 20 dakikada bir taranır
FORM4_TICKER_ARASI_BEKLEME_SN = 0.3  # SEC nazik kullanım için (limit: 10/sn)

HABER_TICKER_ARASI_BEKLEME_SN = 1.2  # Finnhub 60/dk limitini rahat karşılar
HABER_TUR_ARASI_BEKLEME_SN = 60      # bir tam tur bitince ek bekleme

_durum = {"form4_tur": 0, "haber_tur": 0, "son_form4": None, "son_haber": None}

REQUIRED_ENV_VARS = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"]


def validate_akilli_para_config():
    eksik = [n for n in REQUIRED_ENV_VARS if not os.environ.get(n)]
    if not FINNHUB_API_KEY:
        print("[AkilliPara] FINNHUB_API_KEY yok - haber katmanı devre dışı.", flush=True)
    if not (ALPACA_API_KEY and ALPACA_SECRET_KEY):
        print("[AkilliPara] Alpaca anahtarları yok - fiyat teyidi devre dışı.", flush=True)
    return eksik


# =============================================================================
# TELEGRAM
# =============================================================================
def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[AkilliPara devre dışı] {text}", flush=True)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print(f"[AkilliPara Telegram hata] {e}", flush=True)


# =============================================================================
# HİSSE LİSTESİ — us_sinyal_botu.py'den METİN olarak okunuyor (canlı import
# YOK - projenin standart kuralı: import, o dosyanın tüm üst-seviye kodunu
# çalıştırır, istenmeyen yan etkiler doğurur).
# =============================================================================
def _load_us_tickers():
    varsayilan = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]
    try:
        with open("us_sinyal_botu.py", "r", encoding="utf-8") as f:
            kaynak = f.read()
        agac = ast.parse(kaynak)
        for node in ast.walk(agac):
            if isinstance(node, ast.Assign):
                for hedef in node.targets:
                    if isinstance(hedef, ast.Name) and hedef.id == "US_TICKERS":
                        return ast.literal_eval(node.value)
    except Exception as e:
        print(f"[AkilliPara] US_TICKERS okunamadı, varsayılan liste kullanılıyor: {e}",
              flush=True)
    return varsayilan


US_TICKERS = _load_us_tickers()

# Bu modüle ÖZEL, ayrı ve daha geniş bir hisse listesi - kanıtlanmış
# sinyal sistemlerinin (ATR kırılımı, RSI vb.) kullandığı 106'lık
# US_TICKERS'a HİÇ dokunmuyoruz (o listeler test edilmiş, değiştirmek
# riskli). Burada amaç sadece "önemli haber/içeriden işlem" taraması
# için tanınmış, likit, büyük/orta ölçekli ~300 şirket - küçük/şüpheli
# hisseler kasıtlı olarak dışarıda.
AKILLI_PARA_TICKERS = sorted(set([
    # Teknoloji
    "AAPL","MSFT","GOOGL","GOOG","AMZN","NVDA","META","TSLA","AVGO","ORCL",
    "CRM","ADBE","AMD","CSCO","INTC","QCOM","TXN","IBM","NOW","INTU",
    "AMAT","MU","ADI","LRCX","KLAC","SNPS","CDNS","PANW","FTNT","CRWD",
    "PLTR","SNOW","NET","DDOG","ZS","MDB","TEAM","WDAY","ANSS","ROP",
    "APH","GLW","HPQ","DELL","NXPI","MCHP","ON","SWKS","TER","KEYS",
    "UBER","LYFT","ABNB","BKNG","EBAY","ETSY","SHOP","SPOT","PYPL","SQ",
    "NFLX","DIS","CMCSA","CHTR","TMUS","VZ","T",
    # Finans
    "JPM","BAC","WFC","C","GS","MS","SCHW","BLK","AXP","USB",
    "PNC","TFC","COF","BK","STT","SPGI","MCO","ICE","CME","CB",
    "MMC","AON","AJG","PGR","TRV","ALL","MET","PRU","AIG","V",
    "MA","FIS","FISV","PAYX","ADP",
    # Sağlık
    "UNH","JNJ","LLY","PFE","MRK","ABBV","TMO","ABT","DHR","BMY",
    "AMGN","GILD","CVS","CI","ELV","HUM","MDT","ISRG","SYK","BSX",
    "REGN","VRTX","ZTS","BDX","EW","IDXX","MRNA","BIIB",
    # Tüketici
    "WMT","PG","KO","PEP","COST","MCD","NKE","SBUX","TGT","LOW",
    "HD","TJX","BKNG","MAR","CMG","YUM","DG","DLTR","ROST","ULTA",
    "EL","CL","KMB","GIS","KHC","MDLZ","MNST","STZ","HSY","KR",
    # Sanayi
    "BA","CAT","GE","HON","UPS","UNP","LMT","RTX","DE","MMM",
    "NOC","GD","EMR","ETN","ITW","PH","CSX","NSC","FDX","WM",
    "PCAR","CMI","ROK","DOV","XYL","IR","JCI","CARR","OTIS",
    # Enerji
    "XOM","CVX","COP","SLB","EOG","PXD","OXY","WMB","KMI","PSX",
    "VLO","MPC","HAL","BKR","DVN","FANG","HES",
    # Malzeme/Emlak/Kamu Hizmetleri
    "LIN","APD","SHW","ECL","FCX","NEM","DOW","DD","NUE","VMC",
    "NEE","DUK","SO","D","AEP","EXC","SRE","XEL","ED","PEG",
    "PLD","AMT","EQIX","PSA","O","SPG","WELL","DLR","AVB","EQR",
    # Havacılık/Savunma ek + diğer tanınmış büyükler
    "COIN","MSTR","SOFI","ROKU","PINS","SNAP","RBLX","DASH","MRVL",
    "ARM","SMCI","VST","CEG","TTD","APP","AXON","DXCM","ALGN",
]))

FINNHUB_TICKERS = AKILLI_PARA_TICKERS
FORM4_TICKERS = AKILLI_PARA_TICKERS


# =============================================================================
# BASİT JSON DURUM DOSYASI YARDIMCILARI
# =============================================================================
def _json_yukle(yol, varsayilan):
    if not os.path.exists(yol):
        return varsayilan
    try:
        import json
        with open(yol, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return varsayilan


def _json_kaydet(yol, veri):
    try:
        import json
        with open(yol, "w", encoding="utf-8") as f:
            json.dump(veri, f)
    except OSError as e:
        print(f"[AkilliPara] {yol} kaydedilemedi: {e}", flush=True)


# =============================================================================
# ALPACA — FİYAT TEYİDİ (IEX borsası, ücretsiz plan)
# =============================================================================
def _alpaca_fiyat_al(ticker: str):
    if not (ALPACA_API_KEY and ALPACA_SECRET_KEY):
        return None
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{ticker}/trades/latest",
            params={"feed": "iex"},
            headers={"APCA-API-KEY-ID": ALPACA_API_KEY,
                     "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY},
            timeout=10)
        if r.status_code != 200:
            return None
        return r.json().get("trade", {}).get("p")
    except Exception:
        return None


# =============================================================================
# SEC EDGAR — TICKER -> CIK EŞLEŞTİRME
# =============================================================================
_cik_map = {}


def _cik_map_yukle():
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json",
                          headers=_SEC_HEADERS, timeout=20)
        r.raise_for_status()
        veri = r.json()
        harita = {}
        for kayit in veri.values():
            sembol = kayit.get("ticker", "").upper()
            cik = kayit.get("cik_str")
            if sembol and cik:
                harita[sembol] = str(cik).zfill(10)
        print(f"[AkilliPara] CIK haritası yüklendi ({len(harita)} şirket).", flush=True)
        return harita
    except Exception as e:
        print(f"[AkilliPara] CIK haritası alınamadı: {e}", flush=True)
        return {}


# =============================================================================
# SEC EDGAR — FORM 4 (İÇERİDEN İŞLEM) TARAMA
# =============================================================================
def _form4_xml_url_bul(cik_no_lead: str, accession_dashli: str):
    """Bir Form 4 dosyasının içindeki ham XML belgesinin adresini bulur."""
    accession_nodash = accession_dashli.replace("-", "")
    index_url = (f"https://www.sec.gov/Archives/edgar/data/{cik_no_lead}/"
                 f"{accession_nodash}/{accession_dashli}-index.json")
    r = requests.get(index_url, headers=_SEC_HEADERS, timeout=15)
    r.raise_for_status()
    veri = r.json()
    for item in veri.get("directory", {}).get("item", []):
        ad = item.get("name", "")
        # xslF345X05/... klasörü XSLT görünüm dosyasıdır, ham veri değil -
        # onu değil, asıl XML veri dosyasını arıyoruz.
        if ad.lower().endswith(".xml") and not ad.lower().startswith("xslf345"):
            return (f"https://www.sec.gov/Archives/edgar/data/{cik_no_lead}/"
                     f"{accession_nodash}/{ad}")
    return None


def _form4_xml_ayristir(xml_bytes: bytes) -> dict:
    kok = ET.fromstring(xml_bytes)
    isim = kok.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName")
    unvan = kok.findtext(".//reportingOwnerRelationship/officerTitle")
    is_director = kok.findtext(".//reportingOwnerRelationship/isDirector") == "1"

    islemler = []
    for tx in kok.findall(".//nonDerivativeTransaction"):
        kod = tx.findtext(".//transactionCoding/transactionCode")
        if kod not in ("P", "S"):  # sadece açık piyasa alım/satım - grant/opsiyon vb. hariç
            continue
        lot = tx.findtext(".//transactionAmounts/transactionShares/value")
        fiyat = tx.findtext(".//transactionAmounts/transactionPricePerShare/value")
        try:
            islemler.append({
                "kod": kod,
                "lot": float(lot) if lot else None,
                "fiyat": float(fiyat) if fiyat else None,
            })
        except ValueError:
            continue

    return {"isim": isim, "unvan": unvan, "is_director": is_director,
            "islemler": islemler}


FORM4_MIN_TUTAR_USD = 100_000  # bu tutarın altındaki işlemler rutin sayılır, bildirilmez


def _form4_bildir(ticker: str, detay: dict):
    for tx in detay["islemler"]:
        if tx["lot"] is None:
            continue
        tutar = tx["lot"] * tx["fiyat"] if tx["fiyat"] else None
        # Tutar hesaplanabiliyorsa ve eşiğin altındaysa atla - hesaplanamıyorsa
        # (fiyat eksikse) emin olamadığımız için yine de bildiriyoruz.
        if tutar is not None and tutar < FORM4_MIN_TUTAR_USD:
            continue
        yon_etiketi = "🟢 ALIM (piyasadan satın aldı)" if tx["kod"] == "P" \
            else "🔴 SATIM (piyasada sattı)"
        unvan = detay["unvan"] or ("Yönetim Kurulu Üyesi" if detay["is_director"]
                                    else "İçeriden Kişi")
        tutar_str = f" (~${tutar:,.0f})" if tutar else ""
        fiyat_bilgi = _alpaca_fiyat_al(ticker)
        satirlar = [
            f"🕵️ İÇERİDEN İŞLEM — {ticker}",
            f"{yon_etiketi}",
            f"Kim: {detay['isim'] or 'Bilinmiyor'} ({unvan})",
            f"Miktar: {tx['lot']:,.0f} lot" + (f" @ ${tx['fiyat']:.2f}" if tx["fiyat"] else "")
            + tutar_str,
        ]
        if fiyat_bilgi:
            satirlar.append(f"Şu anki fiyat (Alpaca/IEX, gerçek zamanlı): ${fiyat_bilgi:.2f}")
        satirlar.append(
            f"SEC kaydı: https://www.sec.gov/cgi-bin/browse-edgar?"
            f"action=getcompany&CIK={ticker}&type=4")
        send_telegram_message("\n".join(satirlar))
        _durum["son_form4"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


FORM4_MAX_TEKRAR_DENEME = 3  # bir filing'i kaç kez daha deneyip sonra vazgeçeceğiz


def _gorulen_normalize(gorulen: dict, ticker: str) -> dict:
    """Eski format (düz accession string) ile yeni format (dict, bekleme
    kuyruğu dahil) arasında geçiş - var olan kayıtlı veriyi bozmadan yeni
    yapıya taşır."""
    deger = gorulen.get(ticker)
    if deger is None:
        return {"son": None, "bekleyen": []}
    if isinstance(deger, str):
        return {"son": deger, "bekleyen": []}
    return deger


def _form4_islem_dene(cik_no_lead: str, accession: str):
    """Bir filing'i indirip ayrıştırmayı dener. Başarılıysa (detay, None),
    başarısızsa (None, hata_mesajı) döner."""
    try:
        xml_url = _form4_xml_url_bul(cik_no_lead, accession)
        if not xml_url:
            return None, "index bulunamadı (muhtemelen SEC henüz hazırlamadı)"
        xr = requests.get(xml_url, headers=_SEC_HEADERS, timeout=15)
        xr.raise_for_status()
        return _form4_xml_ayristir(xr.content), None
    except Exception as e:
        return None, str(e)


def _form4_ticker_tara(ticker: str, cik: str, gorulen: dict) -> bool:
    """(2026-09-25 düzeltmesi) Eskiden bir filing indirilemese/ayrıştırılamasa
    BİLE 'gördüm' işareti hemen konuyordu - SEC bazen bir filing'in
    index.json'ını birkaç dakika geç hazırladığı için (404) ya da geçici
    yoğunlukta (503) bu, o CEO/CFO işlemini SONSUZA KADAR kaçırmak
    anlamına geliyordu. Artık başarısız olanlar ayrı bir 'bekleyen'
    kuyruğuna düşüyor, her turda (en fazla FORM4_MAX_TEKRAR_DENEME kez)
    tekrar deneniyor."""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    r = requests.get(url, headers=_SEC_HEADERS, timeout=15)
    if r.status_code != 200:
        return False
    veri = r.json()
    son = veri.get("filings", {}).get("recent", {})
    formlar = son.get("form", [])
    accessionlar = son.get("accessionNumber", [])

    dortler = [a for f, a in zip(formlar, accessionlar) if f == "4"]
    if not dortler:
        return False

    kayit = _gorulen_normalize(gorulen, ticker)
    onceki = kayit["son"]

    if onceki is None:
        # İlk kez görülüyor - geçmişi alarma boğmamak için sadece temel al.
        gorulen[ticker] = {"son": dortler[0], "bekleyen": []}
        return True

    cik_no_lead = str(int(cik))
    degisti = False

    # --- 1) Önce daha önce başarısız olup bekleyenleri tekrar dene ---
    hala_bekleyen = []
    for accession, deneme_sayisi in kayit["bekleyen"]:
        detay, hata = _form4_islem_dene(cik_no_lead, accession)
        if detay is not None:
            if detay["islemler"]:
                _form4_bildir(ticker, detay)
            degisti = True
        else:
            print(f"[AkilliPara] {ticker} Form4 tekrar deneme başarısız "
                  f"({accession}, {deneme_sayisi + 1}. deneme): {hata}", flush=True)
            if deneme_sayisi + 1 < FORM4_MAX_TEKRAR_DENEME:
                hala_bekleyen.append((accession, deneme_sayisi + 1))
            else:
                print(f"[AkilliPara] {ticker} Form4 {accession} - "
                      f"{FORM4_MAX_TEKRAR_DENEME} denemeden sonra vazgeçildi.", flush=True)
            degisti = True

    # --- 2) Yeni filing'leri bul ve işle ---
    yeniler = []
    for accession in dortler:
        if accession == onceki:
            break
        yeniler.append(accession)

    if yeniler:
        gorulen[ticker] = {"son": dortler[0], "bekleyen": hala_bekleyen}
        degisti = True
        for accession in reversed(yeniler):  # eskiden yeniye doğru bildir
            detay, hata = _form4_islem_dene(cik_no_lead, accession)
            if detay is not None:
                if detay["islemler"]:
                    _form4_bildir(ticker, detay)
            else:
                print(f"[AkilliPara] {ticker} Form4 detay hatası ({accession}): {hata}", flush=True)
                gorulen[ticker]["bekleyen"].append((accession, 0))
    else:
        gorulen[ticker] = {"son": onceki, "bekleyen": hala_bekleyen}

    return degisti


def _form4_kontrol_dongusu():
    global _cik_map
    _cik_map = _cik_map_yukle()
    if not _cik_map:
        print("[AkilliPara] CIK haritası yok - Form4 katmanı devre dışı kalacak.", flush=True)
        return
    gorulen = _json_yukle(FORM4_GORULEN_DOSYASI, {})
    while True:
        degisti = False
        for i, ticker in enumerate(FORM4_TICKERS):
            cik = _cik_map.get(ticker.replace("-", ".")) or _cik_map.get(ticker)
            if not cik:
                continue
            try:
                if _form4_ticker_tara(ticker, cik, gorulen):
                    degisti = True
            except Exception as e:
                print(f"[AkilliPara] {ticker} Form4 tarama hatası: {e}", flush=True)
            if degisti and i % 10 == 0:
                _json_kaydet(FORM4_GORULEN_DOSYASI, gorulen)
            time.sleep(FORM4_TICKER_ARASI_BEKLEME_SN)
        if degisti:
            _json_kaydet(FORM4_GORULEN_DOSYASI, gorulen)
        _durum["form4_tur"] += 1
        print(f"[AkilliPara] Form4 turu #{_durum['form4_tur']} tamamlandı.", flush=True)
        time.sleep(FORM4_TARAMA_ARALIGI_SN)


# =============================================================================
# FINNHUB — HABER TARAMA
# =============================================================================
def _haber_bildir(ticker: str, haber: dict, yon: str):
    baslik = haber.get("headline", "")
    kaynak = haber.get("source", "")
    url = haber.get("url", "")
    fiyat_bilgi = _alpaca_fiyat_al(ticker)
    yon_etiket = {"pozitif": "🟢 Olumlu", "negatif": "🔴 Olumsuz",
                  "belirsiz": "⚪ Belirsiz/karışık"}[yon]
    satirlar = [f"📰 HABER — {ticker} ({kaynak}) — {yon_etiket}", baslik]
    if fiyat_bilgi:
        satirlar.append(f"Şu anki fiyat (Alpaca/IEX, gerçek zamanlı): ${fiyat_bilgi:.2f}")
    if url:
        satirlar.append(url)
    send_telegram_message("\n".join(satirlar))
    _durum["son_haber"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


# Anahtar kelimeler artık YÖNÜNE göre ayrı - hem "önemli mi" hem
# "olumlu mu olumsuz mu" tek geçişte belirleniyor. Yön net değilse
# (örn. satın alma - hedef şirket için iyi, alıcı için değişken)
# BELIRSIZ_KELIMELER'e konuyor, yine bildiriliyor ama yönü iddia
# edilmiyor.
POZITIF_ONEMLI_KELIMELER = {
    "fda approval", "fda approves",
    "upgrade", "upgrades", "raises guidance", "guidance raised",
    "beats estimates", "earnings beat",
    "stock split", "buyback", "share repurchase",
}

NEGATIF_ONEMLI_KELIMELER = {
    "bankruptcy", "chapter 11", "files for bankruptcy",
    "lawsuit", "sues", "sued", "class action",
    "investigation", "probe", "sec investigates", "doj",
    "fda rejects", "recall",
    "downgrade", "downgrades", "cuts rating",
    "guidance cut", "cuts guidance",
    "misses estimates", "earnings miss",
    "layoffs", "job cuts", "workforce reduction",
    "resigns", "resignation", "steps down", "fired", "ousted", "ceo departure",
    "data breach", "hack", "cyberattack",
    "halted", "trading halt", "delisted", "delisting",
    "credit rating cut", "credit rating downgrade", "default",
}

BELIRSIZ_ONEMLI_KELIMELER = {
    "settlement", "clinical trial results",
    "acquisition", "acquires", "to acquire", "merger", "to merge", "takeover",
    "bid for", "hostile bid",
    "activist investor", "activist stake",
}

ONEMLI_HABER_ANAHTAR_KELIMELERI = (
    POZITIF_ONEMLI_KELIMELER | NEGATIF_ONEMLI_KELIMELER | BELIRSIZ_ONEMLI_KELIMELER)


def _onemli_haber_mi(baslik: str) -> bool:
    b = baslik.lower()
    if any(kelime in b for kelime in ONEMLI_HABER_ANAHTAR_KELIMELERI):
        return True
    return _kazanc_gunu_baslik_mi(baslik)


# (2026-09-23 eklendi) Kazanç açıklaması başlıkları genelde NÖTR/resmi bir
# dille geliyor ("Reports Fourth Quarter Results" gibi) - hiçbir pozitif/
# negatif kelime içermeseler bile ÖNEMLİ sayılmalı, çünkü kazanç günü
# doğası gereği piyasayı hareket ettirebilir. Bu, geriye dönük testte
# MRNA (%162) ve SNOW (%32.7) gibi devasa hareketlerin "önemli haber yok"
# diye kaçırılmasının sebebiydi - başlık beat/miss kelimesi içermiyordu
# ama yine de bir kazanç açıklamasıydı. Yönü başlıktan çıkaramıyorsak
# _haber_yonu zaten doğru şekilde "belirsiz" dönüyor - bu doğru, çünkü
# nötr bir başlıktan yön iddia etmek yanlış olur.
_KAZANC_BASLIK_KALIPLARI = [
    r"reports?\s+(its\s+)?(fourth|third|second|first)\s+quarter",
    r"reports?\s+q[1-4]\b",
    r"reports?\s+fiscal\s+(fourth|third|second|first|year|\d{4})",
    r"announces?\s+(fourth|third|second|first)\s+quarter",
    r"quarterly\s+(financial\s+)?results",
    r"full[\s-]year\s+results",
    r"reports?\s+.*\s+financial\s+results",
]


def _kazanc_gunu_baslik_mi(baslik: str) -> bool:
    b = baslik.lower()
    return any(re.search(p, b) for p in _KAZANC_BASLIK_KALIPLARI)


def _haber_yonu(baslik: str) -> str:
    b = baslik.lower()
    if any(k in b for k in NEGATIF_ONEMLI_KELIMELER):
        return "negatif"
    if any(k in b for k in POZITIF_ONEMLI_KELIMELER):
        return "pozitif"
    return "belirsiz"


# Bazı mega-cap şirketler (Microsoft, Apple, Google vb.) SIK SIK başka
# şirketlerin haberlerinde bağlam olarak anılıyor ("Microsoft-first",
# "Apple tedarikçisi" gibi) - bu onların KENDİ haberi olduğu anlamına
# gelmiyor. Bu şirketler için, isim başlığın BAŞINDA (ilk ~6 kelime)
# geçmiyorsa, haberin gerçek öznesi o şirket sayılmıyor. Listede
# olmayan tickerlar için bu kontrol uygulanmıyor (küçük şirketlerde bu
# yanlış-eşleştirme sorunu çok daha nadir).
SIRKET_ADI_ESLESTIRME = {
    "AAPL": ["apple"], "MSFT": ["microsoft"],
    "GOOGL": ["google", "alphabet"], "GOOG": ["google", "alphabet"],
    "AMZN": ["amazon"], "META": ["meta", "facebook"],
    "NVDA": ["nvidia"], "TSLA": ["tesla"],
    "NFLX": ["netflix"], "AMD": ["amd", "advanced micro devices"],
}


def _haber_konusu_dogru_mu(ticker: str, baslik: str) -> bool:
    isimler = SIRKET_ADI_ESLESTIRME.get(ticker)
    if not isimler:
        return True  # eşleştirme tanımlı değilse varsayılan: geçir
    ilk_kisim = " ".join(baslik.lower().split()[:6])
    return any(isim in ilk_kisim for isim in isimler)


# Sadece bu güvenilir tel/haber ajanslarından gelen haberler geçer -
# listede olmayan HER kaynak (blog, "analiz" sitesi, içerik fabrikası
# dahil) otomatik elenir. Bu, "kötüleri engelle" değil "sadece
# güvenilenlere izin ver" mantığı - çok daha sıkı.
GUVENILIR_HABER_KAYNAKLARI = {
    "reuters", "bloomberg", "business wire", "businesswire",
    "pr newswire", "prnewswire", "globenewswire", "globe newswire",
    "cnbc", "marketwatch", "barron's", "barrons", "the wall street journal",
    "wsj", "associated press", "ap news", "dow jones newswires",
    "yahoo", "yahoo finance",
}

GUNLUK_HISSE_BASI_HABER_LIMITI = 2  # tüm filtreler geçse bile son emniyet


def _kaynak_guvenilir_mi(kaynak: str) -> bool:
    return (kaynak or "").strip().lower() in GUVENILIR_HABER_KAYNAKLARI


def _haber_ticker_tara(ticker: str, gorulen: dict, gunluk_sayac: dict, bugun_str: str):
    if not FINNHUB_API_KEY:
        return
    # Hafta sonu piyasalar kapalı - gerçek kurumsal olay (kazanç, dava,
    # FDA onayı vb.) neredeyse hiç çıkmaz, akan şey sadece içerik
    # fabrikalarının dolgu yazıları oluyor. Taramayı tamamen atla.
    if datetime.now(timezone.utc).weekday() >= 5:  # 5=Cumartesi, 6=Pazar
        return

    r = requests.get(
        "https://finnhub.io/api/v1/company-news",
        params={"symbol": ticker, "from": bugun_str, "to": bugun_str,
                "token": FINNHUB_API_KEY},
        timeout=15)
    if r.status_code != 200:
        return
    haberler = r.json()
    if not isinstance(haberler, list):
        return

    ilk_calisma = ticker not in gorulen
    gorulmus = set(gorulen.get(ticker, []))
    yeni_idler = []
    for h in haberler:
        hid = h.get("id")
        if hid is None or hid in gorulmus:
            continue
        yeni_idler.append(hid)
        if ilk_calisma:
            continue
        if not _kaynak_guvenilir_mi(h.get("source", "")):
            continue
        if not _onemli_haber_mi(h.get("headline", "")):
            continue
        if not _haber_konusu_dogru_mu(ticker, h.get("headline", "")):
            continue
        if gunluk_sayac.get(ticker, 0) >= GUNLUK_HISSE_BASI_HABER_LIMITI:
            continue
        _haber_bildir(ticker, h, _haber_yonu(h.get("headline", "")))
        gunluk_sayac[ticker] = gunluk_sayac.get(ticker, 0) + 1

    if yeni_idler or ilk_calisma:
        birlesik = list(gorulmus) + yeni_idler
        gorulen[ticker] = birlesik[-100:]  # dosya şişmesin diye sınırlı tut


def _haber_kontrol_dongusu():
    if not FINNHUB_API_KEY:
        print("[AkilliPara] FINNHUB_API_KEY yok - haber döngüsü başlatılmadı.", flush=True)
        return
    gorulen = _json_yukle(HABER_GORULEN_DOSYASI, {})
    gunluk_sayac = {}
    gunluk_sayac_tarihi = None
    while True:
        bugun_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if gunluk_sayac_tarihi != bugun_str:
            gunluk_sayac = {}  # yeni gün - günlük limit sıfırlanır
            gunluk_sayac_tarihi = bugun_str
        for i, ticker in enumerate(FINNHUB_TICKERS):
            try:
                _haber_ticker_tara(ticker, gorulen, gunluk_sayac, bugun_str)
            except Exception as e:
                print(f"[AkilliPara] {ticker} haber tarama hatası: {e}", flush=True)
            if i % 10 == 0:
                _json_kaydet(HABER_GORULEN_DOSYASI, gorulen)
            time.sleep(HABER_TICKER_ARASI_BEKLEME_SN)
        _json_kaydet(HABER_GORULEN_DOSYASI, gorulen)
        _durum["haber_tur"] += 1
        print(f"[AkilliPara] Haber turu #{_durum['haber_tur']} tamamlandı.", flush=True)
        time.sleep(HABER_TUR_ARASI_BEKLEME_SN)


# =============================================================================
# BAŞLANGIÇ MESAJI
# =============================================================================
def baslangic():
    eksik = validate_akilli_para_config()
    if eksik:
        print(f"[AkilliPara] Eksik zorunlu ayar: {eksik}", flush=True)
        return
    katmanlar = ["✅ SEC Form 4 (içeriden işlem) - ücretsiz, kayıt gerekmiyor"]
    katmanlar.append("✅ Finnhub haberleri" if FINNHUB_API_KEY
                      else "⚠️ Finnhub haberleri - API key yok, devre dışı")
    katmanlar.append("✅ Alpaca (IEX) fiyat teyidi" if (ALPACA_API_KEY and ALPACA_SECRET_KEY)
                      else "⚠️ Alpaca fiyat teyidi - anahtar yok, devre dışı")
    send_telegram_message(
        f"🕵️ ABD Akıllı Para Takip Sistemi AKTİF — {AKILLI_PARA_SURUM}\n"
        f"Takip edilen hisse sayısı: {len(US_TICKERS)}\n\n"
        + "\n".join(katmanlar) +
        "\n\nCEO/CFO alım-satımı ve önemli haberler geldiğinde otomatik "
        "bildirim gelecek. Bu sinyaller AL/SAT komutu değil, dikkat "
        "çekmesi gereken gelişmelerin bildirimidir.")
