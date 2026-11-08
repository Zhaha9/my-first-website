#!/usr/bin/env python3
"""本机 Investment Halo ↔ WindPy 只读行情桥接服务。"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import date, datetime, time as datetime_time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from WindPy import w


HOST = "127.0.0.1"
PORT = 8765
INDEX = Path(__file__).with_name("index.html")
DATA_DIR = Path(__file__).with_name("data") / "daily"
LATEST_RESULTS = DATA_DIR / "latest.json"
SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_RUN_TIME = datetime_time(16, 5)
DAILY_CUTOFF_TIME = datetime_time(21, 0)
ASSETS = {
    "510300.SH": {"name": "沪深300ETF", "kind": "场内 ETF"},
    "510500.SH": {"name": "中证500ETF", "kind": "场内 ETF"},
    "600519.SH": {"name": "贵州茅台", "kind": "A 股股票"},
    "601318.SH": {"name": "中国平安", "kind": "A 股股票"},
    "511010.SH": {"name": "国债ETF", "kind": "债券 ETF"},
    "019547.SH": {"name": "国开债 7-10年", "kind": "交易所债券"},
}
FUNDAMENTAL_ASSETS = {"600519.SH", "601318.SH"}
FUNDAMENTAL_FIELDS = {"pe_ttm": "PE_TTM", "pb_lf": "PB_LF", "roe_ttm": "ROE_TTM"}
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
wind_lock = threading.Lock()
quotes_lock = threading.Lock()
wind_started = False


def ensure_wind() -> None:
    global wind_started
    with wind_lock:
        if wind_started and w.isconnected():
            return
        result = w.start()
        if result.ErrorCode != 0 or not w.isconnected():
            raise RuntimeError(f"WindPy 启动失败：{result}")
        wind_started = True


def _fetch_quotes() -> dict:
    ensure_wind()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    start = today - timedelta(days=365 * 3 + 5)
    output = []
    for code, meta in ASSETS.items():
        try:
            with wind_lock:
                result = w.wsd(code, "close", start.isoformat(), today.isoformat(), "")
            if result.ErrorCode != 0:
                output.append({"code": code, **meta, "ok": False, "error": str(result.Data[0][0]) if result.Data else f"Wind 错误码 {result.ErrorCode}"})
                continue
            dates = result.Times or []
            values = result.Data[0] if result.Data else []
            points = [{"date": d.isoformat() if isinstance(d, date) else str(d), "close": float(v)}
                      for d, v in zip(dates, values) if v is not None]
            if not points:
                output.append({"code": code, **meta, "ok": False, "error": "授权范围内没有返回收盘数据"})
            else:
                quote = {"code": code, **meta, "ok": True, "latest": points[-1],
                         "previous": points[-2] if len(points) > 1 else None,
                         "series": points}
                if code in FUNDAMENTAL_ASSETS:
                    fundamentals = {}
                    for field, key in FUNDAMENTAL_FIELDS.items():
                        try:
                            with wind_lock:
                                fr = w.wsd(code, field, start.isoformat(), today.isoformat(), "")
                            if fr.ErrorCode == 0:
                                fdates = fr.Times or []
                                fvalues = fr.Data[0] if fr.Data else []
                                fundamentals[key] = [
                                    {"date": d.isoformat() if isinstance(d, date) else str(d), "value": float(v)}
                                    for d, v in zip(fdates, fvalues) if v is not None
                                ]
                            else:
                                fundamentals[key] = {"error": f"Wind 错误码 {fr.ErrorCode}"}
                        except Exception as field_exc:
                            fundamentals[key] = {"error": str(field_exc)[:200]}
                    quote["fundamentals"] = fundamentals
                else:
                    quote["fundamentals"] = {"supported": False, "reason": "该资产不适用公司 PE/ROE 指标"}
                output.append(quote)
        except Exception as exc:  # A single unavailable instrument must not hide other quotes.
            logging.exception("Wind quote failed for %s", code)
            output.append({"code": code, **meta, "ok": False, "error": str(exc)[:240]})
    market_dates = [q["latest"]["date"] for q in output if q.get("ok") and q.get("latest")]
    as_of = max(market_dates, default=today.isoformat())
    return {"ok": any(q["ok"] for q in output), "source": "WindPy", "asOf": as_of, "quotes": output}


def get_quotes() -> dict:
    with quotes_lock:
        return _fetch_quotes()


def build_signals(quotes: list[dict]) -> list[dict]:
    signals = []
    for quote in quotes:
        series = quote.get("series", [])
        if not quote.get("ok") or len(series) < 2:
            signals.append({"code": quote["code"], "signal": "未覆盖", "momentum": None,
                            "reason": quote.get("error", "有效历史行情不足")})
            continue
        ref = series[max(0, len(series) - 21)]
        latest = series[-1]
        momentum = latest["close"] / ref["close"] - 1 if ref["close"] else 0
        signal = "买入" if momentum > 0.01 else "卖出" if momentum < -0.01 else "持有"
        signals.append({"code": quote["code"], "signal": signal, "momentum": momentum,
                        "reason": f"20 日收盘动量 {momentum:+.2%}", "dataDate": latest["date"]})
    return signals


def _solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [row[:] + [value] for row, value in zip(matrix, vector)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(augmented[row][col]))
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        divisor = augmented[col][col]
        if abs(divisor) < 1e-24:
            raise ArithmeticError("风险预算协方差矩阵不可逆")
        for j in range(col, size + 1):
            augmented[col][j] /= divisor
        for row in range(size):
            if row == col:
                continue
            factor = augmented[row][col]
            for j in range(col, size + 1):
                augmented[row][j] -= factor * augmented[col][j]
    return [row[-1] for row in augmented]


def calculate_risk_parity(quotes: list[dict]) -> dict:
    usable = [q for q in quotes if q.get("ok") and len(q.get("series", [])) >= 61]
    if len(usable) < 2:
        return {"ok": False, "reason": "至少两项资产各需 61 个以上收盘点"}
    returns = []
    for quote in usable:
        series = quote["series"]
        returns.append({series[i]["date"]: series[i]["close"] / series[i - 1]["close"] - 1
                        for i in range(1, len(series)) if series[i - 1]["close"]})
    dates = sorted(set.intersection(*(set(values) for values in returns)))
    n = len(usable)
    if len(dates) < 60:
        return {"ok": False, "reason": "共同有效收益样本不足 60 个交易日"}
    means = [sum(values[d] for d in dates) / len(dates) for values in returns]
    covariance = [[sum((returns[i][d] - means[i]) * (returns[j][d] - means[j]) for d in dates) /
                   (len(dates) - 1) for j in range(n)] for i in range(n)]
    # 10% diagonal shrinkage stabilizes the optimizer while preserving each asset variance.
    covariance = [[covariance[i][j] * (0.9 if i != j else 1.0) for j in range(n)] for i in range(n)]
    budget = 1 / n
    x = [1 / math.sqrt(max(covariance[i][i], 1e-16)) for i in range(n)]
    total = sum(x)
    x = [value / total for value in x]

    def objective(values: list[float]) -> float:
        variance_term = 0.5 * sum(values[i] * covariance[i][j] * values[j]
                                  for i in range(n) for j in range(n))
        return variance_term - budget * sum(math.log(value) for value in values)

    iterations = 0
    for iterations in range(1, 101):
        gradient = [sum(covariance[i][j] * x[j] for j in range(n)) - budget / x[i]
                    for i in range(n)]
        hessian = [[covariance[i][j] + (budget / (x[i] * x[i]) if i == j else 0)
                    for j in range(n)] for i in range(n)]
        step = _solve_linear(hessian, [-value for value in gradient])
        old_objective = objective(x)
        alpha = 1.0
        while alpha > 1e-10:
            candidate = [x[i] + alpha * step[i] for i in range(n)]
            if min(candidate) > 0 and objective(candidate) < old_objective:
                break
            alpha *= 0.5
        if alpha <= 1e-10:
            break
        x = candidate
        if max(abs(value) for value in gradient) < 1e-10:
            break
    total = sum(x)
    weights = [value / total for value in x]
    portfolio_variance = sum(weights[i] * covariance[i][j] * weights[j]
                             for i in range(n) for j in range(n))
    rows = []
    for i, quote in enumerate(usable):
        marginal = sum(covariance[i][j] * weights[j] for j in range(n))
        rows.append({"code": quote["code"], "name": quote["name"],
                     "annualizedVolatility": math.sqrt(covariance[i][i] * 252),
                     "weight": weights[i],
                     "riskContribution": weights[i] * marginal / portfolio_variance})
    equity_codes = {"510300.SH", "510500.SH", "600519.SH", "601318.SH"}
    bond_codes = {"511010.SH", "019547.SH"}
    class_weights = {
        "equity": sum(row["weight"] for row in rows if row["code"] in equity_codes),
        "bonds": sum(row["weight"] for row in rows if row["code"] in bond_codes),
        "multiStrategy": 0.0,
    }
    return {"ok": True, "method": "equal-risk-contribution", "lookbackStart": dates[0],
            "lookbackEnd": dates[-1], "observations": len(dates), "riskBudget": budget,
            "shrinkage": 0.1, "iterations": iterations, "assets": rows,
            "classWeights": class_weights}


def write_daily_results() -> dict:
    quotes = get_quotes()
    today = datetime.now(SHANGHAI).date().isoformat()
    if not quotes.get("ok") or quotes.get("asOf") != today:
        raise RuntimeError(f"Wind 尚未提供 {today} 收盘行情；最近行情日为 {quotes.get('asOf', '无数据')}，本次稍后重试")
    result = {**quotes, "status": "complete",
              "generatedAt": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
              "signals": build_signals(quotes["quotes"]),
              "riskParity": calculate_risk_parity(quotes["quotes"])}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    day_file = DATA_DIR / f"{today}.json"
    temp_file = LATEST_RESULTS.with_suffix(".tmp")
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    temp_file.write_bytes(encoded)
    os.replace(temp_file, LATEST_RESULTS)
    day_file.write_bytes(encoded)
    logging.info("每日收盘结果已保存：日期=%s 行情=%d 风险平价=%s", today,
                 len(result["quotes"]), result["riskParity"].get("ok"))
    return result


def has_current_close(today: date) -> bool:
    ensure_wind()
    start = today - timedelta(days=10)
    with wind_lock:
        result = w.wsd("510300.SH", "close", start.isoformat(), today.isoformat(), "")
    if result.ErrorCode != 0 or not result.Times:
        return False
    latest = result.Times[-1]
    latest_date = latest.isoformat() if isinstance(latest, date) else str(latest)
    return latest_date == today.isoformat()


def daily_scheduler() -> None:
    last_attempt_date = ""
    last_attempt_at = 0.0
    while True:
        now = datetime.now(SHANGHAI)
        today = now.date().isoformat()
        if now.weekday() < 5 and DAILY_RUN_TIME <= now.time() <= DAILY_CUTOFF_TIME:
            if today != last_attempt_date or time.monotonic() - last_attempt_at >= 600:
                last_attempt_date, last_attempt_at = today, time.monotonic()
                try:
                    if not has_current_close(now.date()):
                        logging.info("Wind 尚无今日收盘数据，稍后检查；日期=%s", today)
                        time.sleep(600)
                        continue
                    result = write_daily_results()
                    if result["asOf"] == today:
                        last_attempt_date = today
                        last_attempt_at = float("inf")
                except Exception as exc:
                    logging.warning("每日收盘任务未完成，将稍后重试：%s", exc)
        time.sleep(60)


class Handler(BaseHTTPRequestHandler):
    server_version = "InvestmentHaloLocal/1.0"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            try:
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            except OSError as exc:
                self._send(500, str(exc).encode(), "text/plain; charset=utf-8")
            return
        if self.path == "/api/health":
            connected = False
            try:
                ensure_wind()
                connected = bool(w.isconnected())
            except Exception:
                logging.exception("Wind health check failed")
            payload = json.dumps({"ok": connected, "windConnected": connected}, ensure_ascii=False).encode()
            self._send(200 if connected else 503, payload, "application/json; charset=utf-8")
            return
        if self.path == "/api/daily-results":
            try:
                payload = json.loads(LATEST_RESULTS.read_text(encoding="utf-8"))
                self._send(200, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(),
                           "application/json; charset=utf-8")
            except FileNotFoundError:
                payload = {"ok": False, "status": "waiting", "scheduledTime": "工作日 16:05（北京时间）"}
                self._send(200, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            except Exception as exc:
                logging.exception("Daily result read failed")
                payload = {"ok": False, "status": "error", "error": str(exc)[:240]}
                self._send(500, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if self.path == "/api/quotes":
            try:
                payload = json.dumps(get_quotes(), ensure_ascii=False, allow_nan=False).encode()
                self._send(200, payload, "application/json; charset=utf-8")
            except Exception as exc:
                logging.exception("Wind quote request failed")
                payload = json.dumps({"ok": False, "error": str(exc)[:300]}, ensure_ascii=False).encode()
                self._send(503, payload, "application/json; charset=utf-8")
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:
        logging.info("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    if not INDEX.is_file():
        raise SystemExit(f"找不到网页文件：{INDEX}")
    ensure_wind()
    threading.Thread(target=daily_scheduler, name="wind-daily-close", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    logging.info("Investment Halo 本机服务已启动：http://%s:%d/", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("服务正在停止")
    finally:
        server.server_close()
        try:
            w.stop()
        except Exception:
            logging.exception("WindPy 停止时发生错误")


if __name__ == "__main__":
    main()
