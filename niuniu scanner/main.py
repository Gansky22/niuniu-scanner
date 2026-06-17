import os
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf
from flask import Flask, jsonify, Response

app = Flask(__name__)

KL_TZ = ZoneInfo("Asia/Kuala_Lumpur")

WATCHLIST = [
    "NVDA", "AMD", "TSLA", "PLTR", "IONQ", "RGTI", "QBTS",
    "SOUN", "HOOD", "COIN", "MSTR", "SMCI", "AVGO", "TSM",
    "ARM", "LUNR", "RKLB", "SOFI", "MARA", "RIOT",
    "DXYZ", "HIMS", "BBAI", "AI", "CRWD", "NET", "PANW"
]


def now_kl():
    return datetime.now(KL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def fetch_data(symbol):
    try:
        df = yf.download(
            symbol,
            period="6mo",
            interval="1d",
            auto_adjust=True,
            progress=False
        )

        if df is None or df.empty:
            return None

        df = df.reset_index()
        df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]

        if "close" not in df.columns:
            return None

        return df

    except Exception as e:
        print(f"{symbol} fetch error: {e}")
        return None


def calculate_signal(df):
    df = df.copy()

    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()

    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["rvol"] = df["volume"] / df["vol_ma20"]

    df["momentum_3d_pct"] = (df["close"] / df["close"].shift(3) - 1) * 100
    df["range_pct"] = ((df["high"] - df["low"]) / df["close"]) * 100

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

    trend_score = latest.get("trend_score", 0)
    rvol = latest.get("rvol", 0)
    momentum = latest.get("momentum_3d_pct", 0)
    breakout = latest.get("breakout_10", False)
    range_pct = latest.get("range_pct", 0)

    if trend_score >= 4:
        score += 30
    elif trend_score >= 3:
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

    if score >= 80:
        grade = "A+"
    elif score >= 70:
        grade = "A"
    elif score >= 55:
        grade = "B"
    elif score >= 40:
        grade = "C"
    else:
        grade = "弱"

    return score, grade


def scan_four_light():
    results = []

    for symbol in WATCHLIST:
        df = fetch_data(symbol)

        if df is None or len(df) < 80:
            continue

        df = calculate_signal(df)
        latest = df.iloc[-1]

        if not latest["green_light"] and not latest["four_light_buy"]:
            continue

        score, grade = get_grade(latest)

        signal = "🟢 四灯转绿 + 突破" if latest["four_light_buy"] else "🟡 四灯转绿观察"

        results.append({
            "symbol": symbol,
            "price": round(float(latest["close"]), 2),
            "rvol": round(float(latest["rvol"]), 2),
            "momentum_3d_pct": round(float(latest["momentum_3d_pct"]), 2),
            "range_pct": round(float(latest["range_pct"]), 2),
            "trend_score": int(latest["trend_score"]),
            "breakout_10": bool(latest["breakout_10"]),
            "four_light_buy": bool(latest["four_light_buy"]),
            "score": round(float(score), 1),
            "grade": grade,
            "signal": signal
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


@app.route("/")
def home():
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Four Light Scanner</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            padding: 30px;
            background: #f5f5f5;
        }}
        .card {{
            background: white;
            padding: 20px;
            border-radius: 12px;
            max-width: 800px;
            margin: auto;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        a {{
            display: block;
            padding: 12px;
            margin: 10px 0;
            background: #111;
            color: white;
            text-decoration: none;
            border-radius: 8px;
        }}
    </style>
</head>
<body>
    <div class="card">
        <h2>🟢 Four Light 美股短线扫描器</h2>
        <p>更新时间：{now_kl()}</p>

        <a href="/run-four-light">运行 Four Light 扫描</a>
        <a href="/api/four-light">JSON API</a>
        <a href="/health">Health Check</a>

        <h3>扫描逻辑</h3>
        <p>寻找：均线转强 + 动能转强 + 成交量放大 + 突破10日高点。</p>
    </div>
</body>
</html>
"""
    return Response(html, mimetype="text/html")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "time": now_kl()
    })


@app.route("/api/four-light")
def api_four_light():
    results = scan_four_light()
    return jsonify({
        "updated_at": now_kl(),
        "count": len(results),
        "results": results
    })


@app.route("/run-four-light")
def run_four_light():
    results = scan_four_light()

    lines = []
    lines.append("🟢 Four Light 美股短线扫描")
    lines.append(f"更新时间：{now_kl()}")
    lines.append("")

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
        return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")

    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['symbol']}｜{r['signal']}")
        lines.append(f"等级：{r['grade']}｜分数：{r['score']}")
        lines.append(f"价格：${r['price']}")
        lines.append(f"RVOL：{r['rvol']}")
        lines.append(f"3日动能：{r['momentum_3d_pct']}%")
        lines.append(f"日振幅：{r['range_pct']}%")
        lines.append(f"趋势灯：{r['trend_score']}/4")
        lines.append(f"突破10日高点：{'是' if r['breakout_10'] else '否'}")
        lines.append("")

    lines.append("⚠️ 这是扫描提醒，不是买入建议。")
    lines.append("建议再确认：VWAP、止损位、大盘方向、是否已经追高。")

    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)