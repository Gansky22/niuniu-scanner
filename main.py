import os
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf
from flask import Flask, jsonify, Response, request

app = Flask(__name__)
KL_TZ = ZoneInfo("Asia/Kuala_Lumpur")
CACHE_FILE = "scan_cache.json"

RAW_WATCHLIST = """
AAPL MSFT NVDA AMD AVGO TSM ASML ARM INTC MU AMAT LRCX KLAC MRVL ON QCOM TXN
ADI MCHP MPWR NXPI STM WOLF COHR TER ASX UMC GFS SMCI DELL HPE ANET CSCO
PLTR AI BBAI SOUN PATH SNOW MDB DDOG ESTC HCP CFLT NOW CRM ORCL SAP TEAM
ADBE INTU APP NET CRWD PANW ZS FTNT CYBR OKTA S TENB VRNS GEN RPD SPLK
GOOGL GOOG META AMZN NFLX SHOP SPOT UBER LYFT DASH ABNB BKNG EXPE RBLX
TTD ROKU PINS SNAP MTCH DUOL FVRR UPWK ETSY SE MELI BABA JD PDD BIDU TME
TSLA RIVN LCID NIO XPEV LI F GM FSR CHPT BLNK EVGO QS STEM ENPH SEDG RUN
FSLR BE PLUG FCEL NEE DUK SO AEP XOM CVX OXY COP SLB HAL LNG
IONQ RGTI QBTS QUBT ARQQ
RKLB LUNR ASTS RDW SPIR SATS IRDM MAXR
HOOD SOFI COIN MSTR AFRM UPST PYPL SQ NU LC TREE
MARA RIOT CLSK IREN HUT BTBT CAN WULF CIFR BITF HIVE
HIMS TEM RXRX DNA BEAM EDIT CRSP NTLA VRTX REGN AMGN GILD BIIB MRNA BNTX
NVAX SAVA IOVA VKTX ALT TMDX AXON ISRG SYK MDT BSX EW ZBH PODD
LLY NVO PFE MRK JNJ ABBV BMY TMO DHR UNH CVS HUM ELV
JPM BAC C WFC GS MS SCHW BLK BX V MA AXP DFS
DIS CMCSA WBD PARA FOX NKE LULU SBUX MCD CMG YUM DPZ CELH MNST KO PEP
WMT COST TGT HD LOW TJX ROST ULTA ELF
CAT DE GE HON RTX LMT BA NOC GD ETN EMR PH MMM UPS FDX
SPY QQQ DIA IWM TQQQ SQQQ SOXL SOXS TECL FNGU ARKK ARKW ARKG
ZM DOCU ZI TWLO BILL HUBS WDAY PAYC NETS ALKT PCOR GTLB
U PATH CPNG GRAB SEA BILI FUTU TIGR
ROST TJX ANF GPS URBN CROX DECK ONON
PANW CRWD ZS NET S FTNT CYBR OKTA TENB VRNS
AAL DAL UAL LUV CCL RCL NCLH MAR HLT
""".split()


def unique_symbols(symbols):
    seen = set()
    output = []
    for s in symbols:
        s = s.strip().upper()
        if s and s not in seen:
            output.append(s)
            seen.add(s)
    return output


WATCHLIST = unique_symbols(RAW_WATCHLIST)


def now_kl():
    return datetime.now(KL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def get_col(df, ticker, field):
    """Safely extract one ticker OHLCV series from a yfinance batch dataframe."""
    try:
        if hasattr(df.columns, "nlevels") and df.columns.nlevels == 2:
            # group_by="ticker" usually gives columns like (TICKER, Open)
            if ticker in df.columns.get_level_values(0):
                sub = df[ticker].copy()
            # fallback for layout like (Open, TICKER)
            elif ticker in df.columns.get_level_values(1):
                sub = df.xs(ticker, axis=1, level=1).copy()
            else:
                return None
        else:
            sub = df.copy()

        # normalize column names
        sub.columns = [str(c).lower().replace(" ", "_") for c in sub.columns]
        field = field.lower()
        if field not in sub.columns:
            return None
        return sub[field]
    except Exception:
        return None


def fetch_batch(symbols, period="6mo", interval="1d", chunk_size=60, sleep_sec=1.0):
    """
    Batch download to reduce Yahoo requests.
    300 stocks become about 5 requests instead of 300 requests.
    """
    data = {}
    failed = []

    for batch in chunks(symbols, chunk_size):
        try:
            raw = yf.download(
                tickers=" ".join(batch),
                period=period,
                interval=interval,
                auto_adjust=True,
                group_by="ticker",
                progress=False,
                threads=True,
            )

            if raw is None or raw.empty:
                failed.extend(batch)
                continue

            for symbol in batch:
                try:
                    open_s = get_col(raw, symbol, "open")
                    high_s = get_col(raw, symbol, "high")
                    low_s = get_col(raw, symbol, "low")
                    close_s = get_col(raw, symbol, "close")
                    volume_s = get_col(raw, symbol, "volume")

                    if close_s is None or volume_s is None or high_s is None or low_s is None or open_s is None:
                        failed.append(symbol)
                        continue

                    sub = {
                        "open": open_s,
                        "high": high_s,
                        "low": low_s,
                        "close": close_s,
                        "volume": volume_s,
                    }

                    import pandas as pd
                    df = pd.DataFrame(sub).dropna()

                    if len(df) < 80:
                        failed.append(symbol)
                        continue

                    data[symbol] = df
                except Exception:
                    failed.append(symbol)

        except Exception as e:
            print(f"Batch fetch error {batch[:3]}...: {e}")
            failed.extend(batch)

        time.sleep(sleep_sec)

    return data, sorted(set(failed))


def calculate_signal(df):
    df = df.copy()

    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()

    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["rvol"] = df["volume"] / df["vol_ma20"]

    df["momentum_3d_pct"] = (df["close"] / df["close"].shift(3) - 1) * 100
    df["change_1d_pct"] = (df["close"] / df["close"].shift(1) - 1) * 100
    df["range_pct"] = ((df["high"] - df["low"]) / df["close"]) * 100
    df["dollar_volume"] = df["close"] * df["volume"]

    df["recent_high_10"] = df["high"].rolling(10).max().shift(1)
    df["breakout_10"] = df["close"] > df["recent_high_10"]

    df["trend_score"] = 0
    df.loc[df["close"] > df["ma5"], "trend_score"] += 1
    df.loc[df["ma5"] > df["ma10"], "trend_score"] += 1
    df.loc[df["ma10"] > df["ma20"], "trend_score"] += 1
    df.loc[df["ma20"] > df["ma60"], "trend_score"] += 1

    df["green_light"] = (
        (df["trend_score"] >= 3) &
        (df["momentum_3d_pct"] > 0) &
        (df["rvol"] >= 1.2)
    )

    df["four_light_buy"] = (
        (df["trend_score"] >= 3) &
        (df["momentum_3d_pct"] >= 2) &
        (df["rvol"] >= 1.5) &
        (df["breakout_10"])
    )

    return df


def get_grade(latest):
    score = 0

    trend = latest.get("trend_score", 0)
    rvol = latest.get("rvol", 0)
    momentum = latest.get("momentum_3d_pct", 0)
    breakout = latest.get("breakout_10", False)
    range_pct = latest.get("range_pct", 0)
    dollar_volume = latest.get("dollar_volume", 0)

    if trend >= 4:
        score += 30
    elif trend >= 3:
        score += 22

    if rvol >= 2:
        score += 25
    elif rvol >= 1.5:
        score += 18
    elif rvol >= 1.2:
        score += 10

    if momentum >= 5:
        score += 20
    elif momentum >= 2:
        score += 12
    elif momentum > 0:
        score += 6

    if breakout:
        score += 18

    if range_pct <= 8:
        score += 7
    else:
        score -= 5

    if dollar_volume >= 100_000_000:
        score += 8
    elif dollar_volume >= 50_000_000:
        score += 4

    if score >= 88:
        grade = "A+"
    elif score >= 75:
        grade = "A"
    elif score >= 60:
        grade = "B"
    elif score >= 45:
        grade = "C"
    else:
        grade = "弱"

    return score, grade


def analyze_symbol(symbol, df):
    df = calculate_signal(df)
    latest = df.iloc[-1]

    if not bool(latest["green_light"]) and not bool(latest["four_light_buy"]):
        return None

    score, grade = get_grade(latest)

    return {
        "symbol": symbol,
        "price": round(float(latest["close"]), 2),
        "change_1d_pct": round(float(latest["change_1d_pct"]), 2),
        "rvol": round(float(latest["rvol"]), 2),
        "volume": int(latest["volume"]),
        "dollar_volume_m": round(float(latest["dollar_volume"]) / 1_000_000, 1),
        "momentum_3d_pct": round(float(latest["momentum_3d_pct"]), 2),
        "range_pct": round(float(latest["range_pct"]), 2),
        "trend_score": int(latest["trend_score"]),
        "breakout_10": bool(latest["breakout_10"]),
        "four_light_buy": bool(latest["four_light_buy"]),
        "score": round(float(score), 1),
        "grade": grade,
        "signal": "🟢 四灯转绿 + 突破" if bool(latest["four_light_buy"]) else "🟡 四灯转绿观察",
    }


def scan_four_light():
    start = time.time()
    market_data, failed = fetch_batch(WATCHLIST)
    results = []

    for symbol, df in market_data.items():
        item = analyze_symbol(symbol, df)
        if item:
            results.append(item)

    results.sort(key=lambda x: x["score"], reverse=True)

    payload = {
        "updated_at": now_kl(),
        "watchlist_count": len(WATCHLIST),
        "downloaded_count": len(market_data),
        "failed_count": len(failed),
        "failed_symbols": failed[:80],
        "result_count": len(results),
        "scan_seconds": round(time.time() - start, 2),
        "results": results,
    }

    save_cache(payload)
    return payload


def save_cache(payload):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Cache save error: {e}")


def load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def render_text(payload, title="🟢 Four Light 美股短线扫描"):
    lines = []
    lines.append(title)
    lines.append(f"更新时间：{payload.get('updated_at', '-')}")
    lines.append(f"股票池：{payload.get('watchlist_count', len(WATCHLIST))} 只")
    lines.append(f"成功下载：{payload.get('downloaded_count', 0)} 只｜失败：{payload.get('failed_count', 0)} 只")
    lines.append(f"扫描耗时：{payload.get('scan_seconds', '-')} 秒")
    lines.append("")

    results = payload.get("results", [])
    if not results:
        lines.append("今天暂时没有符合 Four Light 条件的股票。")
        lines.append("")
        lines.append("主要条件：")
        lines.append("1. 收盘价 > MA5")
        lines.append("2. MA5 > MA10")
        lines.append("3. MA10 > MA20")
        lines.append("4. RVOL >= 1.2")
        lines.append("5. 3日动能转正")
        lines.append("6. 突破10日高点会加分")
        return "\n".join(lines)

    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['symbol']}｜{r['signal']}")
        lines.append(f"等级：{r['grade']}｜分数：{r['score']}")
        lines.append(f"价格：${r['price']}｜日涨跌：{r['change_1d_pct']}%")
        lines.append(f"RVOL：{r['rvol']}｜成交额：${r['dollar_volume_m']}M")
        lines.append(f"3日动能：{r['momentum_3d_pct']}%｜日振幅：{r['range_pct']}%")
        lines.append(f"趋势灯：{r['trend_score']}/4｜突破10日高点：{'是' if r['breakout_10'] else '否'}")
        lines.append("")

    lines.append("⚠️ 这是扫描提醒，不是买入建议。")
    lines.append("建议再确认：VWAP、止损位、大盘方向、是否已经追高。")
    return "\n".join(lines)


@app.route("/")
def home():
    cache = load_cache()
    cache_text = cache.get("updated_at") if cache else "还没有扫描记录"
    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Niuniu Scanner</title>
<style>
body {{ font-family: Arial, sans-serif; padding: 30px; background: #f5f5f5; }}
.card {{ background: white; padding: 22px; border-radius: 14px; max-width: 900px; margin: auto; box-shadow: 0 2px 10px rgba(0,0,0,0.12); }}
a {{ display: block; padding: 14px; margin: 12px 0; background: #111; color: white; text-decoration: none; border-radius: 8px; }}
.small {{ color:#666; font-size:14px; }}
</style>
</head>
<body>
<div class="card">
<h2>🟢 Niuniu Four Light Scanner</h2>
<p>当前时间：{now_kl()}</p>
<p>股票池数量：{len(WATCHLIST)} 只</p>
<p>最后扫描：{cache_text}</p>

<a href="/scan-now">手动扫描 300+ 股票</a>
<a href="/run-four-light">查看最新扫描结果</a>
<a href="/api/four-light">JSON 最新结果</a>
<a href="/watchlist">查看股票池</a>
<a href="/health">Health Check</a>

<h3>升级内容</h3>
<p>现在使用批量下载，不再一只一只抓。300+ 股票大约分成数批下载，比较不容易被限流。</p>
<p class="small">建议每天手动扫 1–2 次即可。/scan-now 会重新扫描，/run-four-light 只读取最新缓存。</p>
</div>
</body>
</html>
"""
    return Response(html, mimetype="text/html")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "time": now_kl(),
        "watchlist_count": len(WATCHLIST),
        "cache_exists": load_cache() is not None,
    })


@app.route("/watchlist")
def watchlist():
    lines = [f"📌 当前股票池：{len(WATCHLIST)} 只", ""]
    for i, symbol in enumerate(WATCHLIST, 1):
        lines.append(f"{i}. {symbol}")
    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")


@app.route("/scan-now")
def scan_now():
    payload = scan_four_light()
    return Response(render_text(payload, "🟢 Four Light 手动扫描完成"), mimetype="text/plain; charset=utf-8")


@app.route("/run-four-light")
def run_four_light():
    cache = load_cache()
    if not cache:
        lines = []
        lines.append("还没有扫描记录。")
        lines.append("")
        lines.append("请先打开：/scan-now")
        return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")
    return Response(render_text(cache), mimetype="text/plain; charset=utf-8")


@app.route("/api/four-light")
def api_four_light():
    cache = load_cache()
    if not cache or request.args.get("refresh") == "1":
        cache = scan_four_light()
    return jsonify(cache)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
