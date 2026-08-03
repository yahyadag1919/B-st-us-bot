"""
football_config.py
Maç analiz botunun (SPO-QUANT) ayarlari. stock_screener_bot.py'nin duz
dosya yapisina uygun olarak ayri bir modul, tek repo icinde.

Mevcut hisse botunun degiskenleriyle (TELEGRAM_TOKEN vb.) CARISMAMASI icin
tum degiskenler FOOTBALL_ / bu dosyaya ozel isimler kullaniyor.
"""

import os

# --- API anahtarlari ---
FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_KEY", "")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
# API-Football (dashboard.api-football.com uzerinden dogrudan alindi, RapidAPI degil).
# SADECE Sueper Lig icin kullanilir - gunluk kota 100 istek, dikkatli kullanilmali.
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")

# --- Ayri Telegram botu (hisse botundan farkli token/chat) ---
FOOTBALL_TELEGRAM_TOKEN = os.environ.get("FOOTBALL_TELEGRAM_TOKEN", "")
FOOTBALL_TELEGRAM_CHAT_ID = os.environ.get("FOOTBALL_TELEGRAM_CHAT_ID", "")

# --- Kalici depolama ---
# Ayni Railway Volume'u (stock_screener_bot.py ile paylasilan DATA_DIR) kullanir,
# ama dosya adlari football_ onekiyle ayristirilir (bkz. football_data_fetcher.py
# ve sonraki adimlarda yazilacak takip/state dosyalari).
DATA_DIR = os.environ.get("DATA_DIR", ".")

# --- Value bet ayarlari ---
EV_THRESHOLD = float(os.environ.get("FOOTBALL_EV_THRESHOLD", "0.05"))     # %5 uzeri edge -> sinyal
KELLY_FRACTION = float(os.environ.get("FOOTBALL_KELLY_FRACTION", "0.25"))  # tam Kelly riskli, fraksiyonel kullaniyoruz

# --- Takip edilecek ligler (football-data.org kodlari) ---
# DOGRULANDI (2026-08-03 arastirmasi): football-data.org ucretsiz plani
# TAM OLARAK bu 9 lig + WC/EC/BSA'yi icerir. Sueper Lig ve Avrupa Ligi (EL)
# bu ucretsiz planda YOK - Gemini'nin PRD'sinde ikisi de istenmisti ama
# mevcut kaynaklarla karsilanamiyor. Sueper Lig ayri bir kaynaktan
# (football_data_fetcher_apifootball.py) cekiliyor; Avrupa Ligi icin
# simdilik ucretsiz bir kaynak yok, kapsam disi birakildi.
TRACKED_COMPETITIONS = [
    "PL",   # Premier League
    "PD",   # La Liga
    "SA",   # Serie A
    "BL1",  # Bundesliga
    "FL1",  # Ligue 1
    "CL",   # Sampiyonlar Ligi
    "DED",  # Eredivisie
    "PPL",  # Primeira Liga
    "ELC",  # EFL Championship
]

# --- Bildirim seli onlemi ---
FOOTBALL_NOTIFY_THROTTLE_MINUTES = int(os.environ.get("FOOTBALL_NOTIFY_THROTTLE_MINUTES", "60"))

# --- Ayristirilmis frekans mimarisi (Gemini'nin onayladigi tasarim) ---
# Fikstur + Poisson modeli sik calisir (football-data.org bol kotali).
FOOTBALL_MODEL_SCAN_INTERVAL_MINUTES = int(os.environ.get("FOOTBALL_MODEL_SCAN_INTERVAL_MINUTES", "10"))
# Oran (odds) cekme + value bet hesabi seyrek calisir (The Odds API 500/ay,
# API-Football 100/gun kotasi icin).
FOOTBALL_ODDS_SCAN_INTERVAL_MINUTES = int(os.environ.get("FOOTBALL_ODDS_SCAN_INTERVAL_MINUTES", "240"))

REQUIRED_ENV_VARS = [
    "FOOTBALL_DATA_KEY",
    "ODDS_API_KEY",
    "FOOTBALL_TELEGRAM_TOKEN",
    "FOOTBALL_TELEGRAM_CHAT_ID",
    "API_FOOTBALL_KEY",
]


def validate_football_config():
    """
    Zorunlu env variable'larin dolu olup olmadigini kontrol eder.
    Eksik olanlarin isim listesini doner (bos liste = her sey tamam).
    _self_check() bunu acilista cagiracak (Adim 5'te ana botla birlestirilecek).
    """
    return [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
