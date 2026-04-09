#!/usr/bin/env python3
import json
import math
import os
import sys
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

BASE_URL = "https://data-api.binance.vision"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8776695041:AAGhYc-SDcrm6CrrPXGsmJf0netTfBhcio8")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "127278288")
INTERVAL = "4h"
CANDLE_MS = 4 * 60 * 60 * 1000
MIN_ACCUMULATION_CANDLES = int(os.getenv("MIN_ACCUMULATION_CANDLES", "4"))
MAX_ACCUMULATION_CANDLES = int(os.getenv("MAX_ACCUMULATION_CANDLES", "20"))
SCAN_POLL_SECONDS = int(os.getenv("SCAN_POLL_SECONDS", "30"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
STATE_FILE = os.getenv("STATE_FILE", "scanner_state.json")

STABLE_BASE_ASSETS = {
    "USDT", "USDC", "BUSD", "FDUSD", "TUSD", "USDP", "USDS", "USDD", "USDJ", "DAI", "EUR", "EURI",
    "AEUR", "EURC", "TRY", "BIDR", "BRL", "IDRT", "UAH", "NGN", "ZAR", "AUD", "GBP", "RUB",
}


@dataclass
class Candle:
    open_time: int
    high: float
    low: float
    close: float


def log(message: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)


def http_get_json(path: str, params: Optional[Dict[str, str]] = None):
    query = urllib.parse.urlencode(params or {})
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(url=url, method="GET", headers={"User-Agent": "SpotScannerBot/1.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def telegram_send(text: str) -> None:
    endpoint = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(endpoint, data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT):
        return


def load_state() -> Dict[str, str]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        log("تعذر قراءة ملف الحالة، سيتم إنشاء ملف جديد.")
    return {}


def save_state(state: Dict[str, str]) -> None:
    temp_file = f"{STATE_FILE}.tmp"
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    os.replace(temp_file, STATE_FILE)


def get_spot_usdt_symbols() -> List[str]:
    exchange_info = http_get_json("/api/v3/exchangeInfo")
    symbols = []
    for item in exchange_info.get("symbols", []):
        if item.get("status") != "TRADING":
            continue
        if item.get("quoteAsset") != "USDT":
            continue
        if item.get("isSpotTradingAllowed") is not True:
            continue
        base = str(item.get("baseAsset", "")).upper()
        if base in STABLE_BASE_ASSETS:
            continue
        symbol = str(item.get("symbol", "")).upper()
        if symbol.endswith("USDT"):
            symbols.append(symbol)
    symbols.sort()
    return symbols


def parse_klines(raw_klines: List[List]) -> List[Candle]:
    candles: List[Candle] = []
    for row in raw_klines:
        candles.append(Candle(open_time=int(row[0]), high=float(row[2]), low=float(row[3]), close=float(row[4])))
    return candles


def fetch_symbol_candles(symbol: str, closed_candle_open_time: int) -> List[Candle]:
    limit = MAX_ACCUMULATION_CANDLES + 5
    end_time = closed_candle_open_time + CANDLE_MS - 1
    data = http_get_json(
        "/api/v3/klines",
        {
            "symbol": symbol,
            "interval": INTERVAL,
            "limit": str(limit),
            "endTime": str(end_time),
        },
    )
    return parse_klines(data)


def detect_signal(candles: List[Candle]) -> Tuple[Optional[str], int]:
    if len(candles) < MIN_ACCUMULATION_CANDLES + 1:
        return None, 0

    signal_candle = candles[-1]

    max_n = min(MAX_ACCUMULATION_CANDLES, len(candles) - 1)
    for n in range(max_n, MIN_ACCUMULATION_CANDLES - 1, -1):
        acc = candles[-(n + 1):-1]
        range_high = max(x.high for x in acc)
        range_low = min(x.low for x in acc)

        if signal_candle.high > range_high and signal_candle.close > range_high:
            return "BREAK_HIGH", n
        if signal_candle.low < range_low and signal_candle.close < range_low:
            return "BREAK_LOW", n

    return None, 0


def format_alert(symbol: str, signal: str, price: float, accumulation_count: int, candle_open: int) -> str:
    candle_time = datetime.fromtimestamp(candle_open / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if signal == "BREAK_HIGH":
        icon = "🟢"
        signal_text = "اختراق القمة"
    else:
        icon = "🔴"
        signal_text = "كسر القاع"

    return (
        f"{icon} تنبيه سبوت Binance\n"
        f"الزوج: {symbol}\n"
        f"نوع الإشارة: {signal_text}\n"
        f"السعر الحالي: {price:.8f}\n"
        f"عدد شموع التجميع: {accumulation_count}\n"
        f"الفريم: 4H\n"
        f"الشمعة: {candle_time}"
    )


def scan_once(symbols: List[str], closed_candle_open_time: int, state: Dict[str, str]) -> int:
    sent_count = 0
    for index, symbol in enumerate(symbols, start=1):
        try:
            candles = fetch_symbol_candles(symbol, closed_candle_open_time)
            if not candles:
                continue
            last = candles[-1]
            if last.open_time != closed_candle_open_time:
                continue

            signal, accumulation_count = detect_signal(candles)
            if signal is None:
                continue

            dedupe_key = f"{symbol}:{closed_candle_open_time}"
            if state.get(dedupe_key):
                continue

            message = format_alert(symbol, signal, last.close, accumulation_count, closed_candle_open_time)
            telegram_send(message)
            state[dedupe_key] = signal
            sent_count += 1
            log(f"تم إرسال تنبيه {symbol} | {signal} | تجميع={accumulation_count}")
            save_state(state)
            time.sleep(0.08)
        except Exception as error:
            log(f"خطأ في {symbol}: {error}")
            continue

        if index % 50 == 0:
            log(f"تقدم الفحص: {index}/{len(symbols)}")

    return sent_count


def get_last_closed_candle_open_time(now_ms: int) -> int:
    current_open = math.floor(now_ms / CANDLE_MS) * CANDLE_MS
    return current_open - CANDLE_MS


def run() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("يجب ضبط TELEGRAM_TOKEN و TELEGRAM_CHAT_ID")

    state = load_state()
    last_scanned_candle = -1

    log("بدء تشغيل Binance Spot Scanner Bot (4H)...")
    while True:
        try:
            symbols = get_spot_usdt_symbols()
            if not symbols:
                log("لم يتم العثور على أزواج USDT Spot.")
                time.sleep(60)
                continue

            now_ms = int(time.time() * 1000)
            closed_candle_open_time = get_last_closed_candle_open_time(now_ms)

            if closed_candle_open_time != last_scanned_candle:
                log(f"بدء فحص شمعة 4H المغلقة: {datetime.fromtimestamp(closed_candle_open_time / 1000, tz=timezone.utc)}")
                log(f"عدد الأزواج بعد التصفية: {len(symbols)}")
                sent_count = scan_once(symbols, closed_candle_open_time, state)
                last_scanned_candle = closed_candle_open_time
                log(f"انتهى الفحص. عدد التنبيهات المرسلة: {sent_count}")
            else:
                log("لا توجد شمعة 4H جديدة بعد. في وضع الانتظار...")

            time.sleep(SCAN_POLL_SECONDS)
        except KeyboardInterrupt:
            log("تم إيقاف البوت يدوياً.")
            return
        except Exception as error:
            log(f"خطأ عام: {error}")
            traceback.print_exc()
            time.sleep(20)


def main() -> None:
    run()


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        log(f"فشل التشغيل: {ex}")
        sys.exit(1)
