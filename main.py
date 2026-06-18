import os
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from flask import Flask, jsonify, Response, request

app = Flask(__name__)
KL_TZ = ZoneInfo("Asia/Kuala_Lumpur")

CACHE_FILE = "scan_cache.json"
PROGRESS_FILE = "scan_progress.json"

# Railway Variables 可调整：如果还是被限流，改小 CHUNK_SIZE、改大 SLEEP_SEC
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "25"))
SLEEP_SEC = float(os.environ.get("SLEEP_SEC", "6"))
RETRY_SLEEP_SEC = float(os.environ.get("RETRY_SLEEP_SEC", "20"))
MAX_RETRY = int(os.environ.get("MAX_RETRY", "1"))

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
RKLB LUNR ASTS RDW SPIR SATS IRDM
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
U CPNG GRAB SE BILI FUTU TIGR
ANF GPS URBN CROX DECK ONON
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
        yield i, items[i:i + size]


def save_json(path, payload):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Save json error {path}: {e}")


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def normalize_sub_df(sub):
    sub = sub.copy()
    sub.columns = [str(c).lower().replace(" ", "_") for c in sub.columns]
    needed = ["open", "high", "low", "close", "volume"]
    for col in needed:
        if col not in sub.columns:
            return None
    return sub[needed].dropna()


def extract_ticker_df(raw, symbol):
    try:
        if raw is None or raw.empty:
            return None

        if hasattr(raw.columns, "nlevels") and raw.columns.nlevels == 2:
            level0 = list(raw.columns.get_level_values(0))
            level1 = list(raw.columns.get_level_values(1))
            if symbol in level0:
                sub = raw[symbol]
            elif symbol in level1:
                sub = raw.xs(symbol, axis=1, level=1)
            else:
                return None
        else:
            sub = raw

        sub = normalize_sub_df(sub)
        if sub is None or len(sub) < 80:
            return None
        return sub
    except Exception:
        return None


def download_batch(batch, period="6mo", interval="1d"):
    return yf.download(
        tickers=" ".join(batch),
        period=period,
        interval=interval,
        auto_adjust=True,
        group_by="ticker",
        progress=False,
        threads=False,
    )


def fetch_batches_stage(symbols, chunk_size=CHUNK_SIZE, sleep_sec=SLEEP_SEC):
    data = {}
    failed = []
    batches = list(chunks(symbols, chunk_size))
    total_batches = len(batches)

    for batch_no, (start_index, batch) in enumerate(batches, 1):
        save_json(PROGRESS_FILE, {
            "status": "running",
            "updated_at": now_kl(),
            "batch_no": batch_no,
            "total_batches": total_batches,
            "current_batch_size": len(batch),
            "processed_symbols": start_index,
            "total_symbols": len(symbols),
            "downloaded_so_far": len(data),
            "failed_so_far": len(failed),
            "current_batch": batch,
        })

        raw = None
        for attempt in range(MAX_RETRY + 1):
            try:
                raw = download_batch(batch)
                if raw is not None and not raw.empty:
                    break
            except Exception as e:
                print(f"Batch {batch_no}/{total_batches} attempt {attempt + 1} error: {e}")
            if attempt < MAX_RETRY:
                time.sleep(RETRY_SLEEP_SEC)

        if raw is None or getattr(raw, "empty", True):
            failed.extend(batch)
        else:
            for symbol in batch:
                df = extract_ticker_df(raw, symbol)
                if df is None:
                    failed.append(symbol)
                else:
                    data[symbol] = df

        if batch_no < total_batches:
            time.sleep(sleep_sec)

    save_json(PROGRESS_FILE, {
        "status": "done",
        "updated_at": now_kl(),
        "total_batches": total_batches,
        "total_symbols": len(symbols),
        "downloaded_count": len(data),
        "failed_count": len(set(failed)),
    })

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
    df["green_light"] = (df["trend_score"] >= 3) & (df["momentum_3d_pct"] > 0) & (df["rvol"] >= 1.2)
    df["four_light_buy"] = (df["trend_score"] >= 3) & (df["momentum_3d_pct"] >= 2) & (df["rvol"] >= 1.5) & (df["breakout_10"])
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
    market_data, failed = fetch_batches_stage(WATCHLIST)
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
        "failed_symbols": failed[:120],
        "result_count": len(results),
        "scan_seconds": round(time.time() - start, 2),
        "chunk_size": CHUNK_SIZE,
        "sleep_sec": SLEEP_SEC,
        "results": results,
    }
    save_json(CACHE_FILE, payload)
    return payload


def render_text(payload, title="🟢 Four Light 美股短线扫描"):
    lines = [
        title,
        f"更新时间：{payload.get('updated_at', '-')}",
        f"股票池：{payload.get('watchlist_count', len(WATCHLIST))} 只",
        f"成功下载：{payload.get('downloaded_count', 0)} 只｜失败：{payload.get('failed_count', 0)} 只",
        f"分批设置：每批 {payload.get('chunk_size', CHUNK_SIZE)} 只｜每批休息 {payload.get('sleep_sec', SLEEP_SEC)} 秒",
        f"扫描耗时：{payload.get('scan_seconds', '-')} 秒",
        "",
    ]
    results = payload.get("results", [])
    if not results:
        lines += [
            "今天暂时没有符合 Four Light 条件的股票。",
            "",
            "主要条件：",
            "1. 收盘价 > MA5",
            "2. MA5 > MA10",
            "3. MA10 > MA20",
            "4. RVOL >= 1.2",
            "5. 3日动能转正",
            "6. 突破10日高点会加分",
        ]
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
    cache = load_json(CACHE_FILE)
    progress = load_json(PROGRESS_FILE)
    cache_text = cache.get("updated_at") if cache else "还没有扫描记录"
    progress_text = progress.get("status") if progress else "无"
    html = f"""
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Niuniu Scanner</title>
<style>
body {{ font-family: Arial, sans-serif; padding: 30px; background: #f5f5f5; }}
.card {{ background: white; padding: 22px; border-radius: 14px; max-width: 900px; margin: auto; box-shadow: 0 2px 10px rgba(0,0,0,0.12); }}
a {{ display: block; padding: 14px; margin: 12px 0; background: #111; color: white; text-decoration: none; border-radius: 8px; }}
.small {{ color:#666; font-size:14px; }}
</style></head>
<body><div class="card">
<h2>🟢 Niuniu Four Light Scanner</h2>
<p>当前时间：{now_kl()}</p>
<p>股票池数量：{len(WATCHLIST)} 只</p>
<p>最后扫描：{cache_text}</p>
<p>扫描进度：{progress_text}</p>
<a href="/scan-now">手动分阶段扫描</a>
<a href="/progress">查看扫描进度</a>
<a href="/run-four-light">查看最新扫描结果</a>
<a href="/api/four-light">JSON 最新结果</a>
<a href="/watchlist">查看股票池</a>
<a href="/health">Health Check</a>
<h3>更新说明</h3>
<p>现在是分阶段扫描，不是一次过扫 300 只。默认每批 {CHUNK_SIZE} 只，每批之间休息 {SLEEP_SEC} 秒。</p>
<p class="small">如果 Yahoo 还是限流，可以在 Railway Variables 设置 CHUNK_SIZE=15、SLEEP_SEC=10。</p>
</div></body></html>
"""
    return Response(html, mimetype="text/html")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "time": now_kl(),
        "watchlist_count": len(WATCHLIST),
        "cache_exists": load_json(CACHE_FILE) is not None,
        "chunk_size": CHUNK_SIZE,
        "sleep_sec": SLEEP_SEC,
    })


@app.route("/watchlist")
def watchlist():
    lines = [f"📌 当前股票池：{len(WATCHLIST)} 只", ""]
    for i, symbol in enumerate(WATCHLIST, 1):
        lines.append(f"{i}. {symbol}")
    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")


@app.route("/progress")
def progress():
    payload = load_json(PROGRESS_FILE)
    if not payload:
        return Response("目前没有扫描进度。", mimetype="text/plain; charset=utf-8")
    lines = ["📡 扫描进度", f"状态：{payload.get('status', '-')}", f"更新时间：{payload.get('updated_at', '-')}"]
    if payload.get("status") == "running":
        lines.append(f"批次：{payload.get('batch_no', '-')}/{payload.get('total_batches', '-')}")
        lines.append(f"已处理到第：{payload.get('processed_symbols', 0)} 只")
        lines.append(f"成功下载：{payload.get('downloaded_so_far', 0)} 只")
        lines.append(f"失败：{payload.get('failed_so_far', 0)} 只")
        lines.append("")
        lines.append("当前批次：")
        lines.append(", ".join(payload.get("current_batch", [])))
    else:
        lines.append(f"总批次：{payload.get('total_batches', '-')}")
        lines.append(f"成功下载：{payload.get('downloaded_count', 0)} 只")
        lines.append(f"失败：{payload.get('failed_count', 0)} 只")
    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")


@app.route("/scan-now")
def scan_now():
    payload = scan_four_light()
    return Response(render_text(payload, "🟢 Four Light 手动分阶段扫描完成"), mimetype="text/plain; charset=utf-8")


@app.route("/run-four-light")
def run_four_light():
    cache = load_json(CACHE_FILE)
    if not cache:
        return Response("还没有扫描记录。\n\n请先打开：/scan-now", mimetype="text/plain; charset=utf-8")
    return Response(render_text(cache), mimetype="text/plain; charset=utf-8")


@app.route("/api/four-light")
def api_four_light():
    cache = load_json(CACHE_FILE)
    if not cache or request.args.get("refresh") == "1":
        cache = scan_four_light()
    return jsonify(cache)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
