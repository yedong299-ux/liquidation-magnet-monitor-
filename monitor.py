# -*- coding: utf-8 -*-
"""
清算磁区反弹监控脚本（Binance USDC永续版）
==========================================
用途：监控 ETH / BTC 的 USDC 本位永续合约（Binance Futures: ETHUSDC / BTCUSDC），
自建"清算价格聚类"估算（无需付费清算热力图API），当价格双向接近估算出的"强磁区"（±1%以内）时，
计算基于ATR的动态挂单缓冲价格，通过 Bark 推送提醒。

核心假设与局限（务必了解，这不是真实清算数据，是基于公开数据的近似估算）：
1. 无法拿到交易所真实的逐仓位持仓分布，用"历史K线收盘价 + 成交量"作为"可能的开仓价格"代理，
   越新的K线权重越高（假设近期开的仓大概率还没平/没爆）。
2. 对每个候选开仓价格，假设多档常见杠杆分布（5x/10x/20x/25x/50x/75x/100x），分别计算多头/空头强平价，
   按"杠杆流行度权重"和"K线权重"加总，形成价格轴上的清算密度分布。
3. 密度分布中的局部高峰 = "强磁区"。这是概率意义上的估算，不代表真实爆仓价。

部署：设计为通过 GitHub Actions 定时调用（见 .github/workflows/monitor.yml），
每次运行读取/写回 state.json 以维持冷却状态（触发后需价格拉开2%才重新武装）。

依赖：pip install -r requirements.txt (仅 requests)
"""

import json
import os
import statistics
import urllib.parse

import requests

# ============================================================
# 配置区 —— 按需修改
# ============================================================

BINANCE_FAPI_BASE = "https://fapi.binance.com"

# 监控的标的：Binance USDⓈ-M 期货上的 USDC 本位永续合约
INSTRUMENTS = [
    {"symbol": "ETH", "futures_symbol": "ETHUSDC"},
    {"symbol": "BTC", "futures_symbol": "BTCUSDC"},
]

# K线参数（用于估算"历史开仓价格分布"以及计算ATR）
CANDLE_INTERVAL = "1h"     # 1小时K线
CANDLE_LIMIT = 200         # 拉取200根，约8天多，足够覆盖近期开仓分布
RECENCY_DECAY = 0.985      # 每往前一根K线权重衰减，越靠近当前权重越高

# 假设的杠杆分布权重（近似零售偏好：10~25x最常见，极端杠杆较少）
LEVERAGE_WEIGHTS = {
    5: 0.05,
    10: 0.20,
    20: 0.25,
    25: 0.20,
    50: 0.15,
    75: 0.10,
    100: 0.05,
}

MAINTENANCE_MARGIN_RATE = 0.005  # 简化维持保证金率假设 0.5%，实际按仓位规模是阶梯式的

# 清算密度分桶宽度（相对当前价的百分比），越小越精细但噪声越大
BUCKET_PCT = 0.001  # 0.1%

# 触发距离阈值：价格与磁区距离 <= 1% 时触发提醒
TRIGGER_THRESHOLD = 0.01

# 解除冷却阈值：价格从磁区拉开 >= 2% 后，允许对同一磁区再次提醒
RELEASE_THRESHOLD = 0.02

# ATR 动态 buffer 参数：buffer_pct = ATR_K * ATR14 / current_price
ATR_PERIOD = 14
ATR_K = 0.5

# 磁区强度阈值：密度超过 均值 + STRENGTH_STD_MULT * 标准差 才算"强磁区"
STRENGTH_STD_MULT = 1.5

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# Bark 推送：从 GitHub Actions Secrets 注入的环境变量读取，不要硬编码进代码
BARK_KEY = os.environ.get("BARK_KEY", "")
# 如果你自建了 Bark 服务器而非用官方 api.day.app，可以覆盖这个环境变量
BARK_SERVER = os.environ.get("BARK_SERVER", "https://api.day.app")


# ============================================================
# 工具函数
# ============================================================

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def binance_get(path, params=None):
    url = BINANCE_FAPI_BASE + path
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_candles(futures_symbol, interval=CANDLE_INTERVAL, limit=CANDLE_LIMIT):
    """返回按时间正序排列的K线列表 [{ts, o, h, l, c, vol}]"""
    raw = binance_get("/fapi/v1/klines", {
        "symbol": futures_symbol, "interval": interval, "limit": limit
    })
    candles = []
    for row in raw:
        # Binance格式: [openTime, open, high, low, close, volume, closeTime, ...]
        candles.append({
            "ts": int(row[0]),
            "o": float(row[1]),
            "h": float(row[2]),
            "l": float(row[3]),
            "c": float(row[4]),
            "vol": float(row[5]),
        })
    candles.sort(key=lambda x: x["ts"])
    return candles


def get_current_price(futures_symbol):
    raw = binance_get("/fapi/v1/ticker/price", {"symbol": futures_symbol})
    return float(raw["price"])


def get_funding_rate(futures_symbol):
    try:
        raw = binance_get("/fapi/v1/premiumIndex", {"symbol": futures_symbol})
        return float(raw["lastFundingRate"])
    except Exception:
        return 0.0


def compute_atr(candles, period=ATR_PERIOD):
    """标准ATR计算"""
    if len(candles) < period + 1:
        raise ValueError("K线数量不足以计算ATR")
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["h"]
        l = candles[i]["l"]
        prev_c = candles[i - 1]["c"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    return sum(trs[-period:]) / period


# ============================================================
# 清算密度估算
# ============================================================

def build_liquidation_density(candles, current_price, funding_rate):
    """
    基于历史K线（作为开仓价格代理）+ 杠杆分布权重，构建清算价格密度分布。
    返回: dict {bucket_price: weight}
    """
    density = {}
    n = len(candles)

    # 资金费率方向性修正：费率持续偏正 -> 多头拥挤，多头清算权重上调；反之空头权重上调
    long_bias = 1.0
    short_bias = 1.0
    if funding_rate > 0.0003:
        long_bias = 1.0 + min(funding_rate * 500, 0.5)  # 最多放大50%
    elif funding_rate < -0.0003:
        short_bias = 1.0 + min(abs(funding_rate) * 500, 0.5)

    for idx, candle in enumerate(candles):
        entry_price = candle["c"]
        vol = candle["vol"] if candle["vol"] > 0 else 1.0
        age = n - idx  # 距离现在多少根K线
        recency_weight = RECENCY_DECAY ** age
        base_weight = vol * recency_weight

        for lev, lev_weight in LEVERAGE_WEIGHTS.items():
            long_liq = entry_price * (1 - 1.0 / lev + MAINTENANCE_MARGIN_RATE)
            short_liq = entry_price * (1 + 1.0 / lev - MAINTENANCE_MARGIN_RATE)

            w_long = base_weight * lev_weight * long_bias
            w_short = base_weight * lev_weight * short_bias

            _add_to_bucket(density, long_liq, current_price, w_long)
            _add_to_bucket(density, short_liq, current_price, w_short)

    return density


def _add_to_bucket(density, price, current_price, weight):
    bucket_width = current_price * BUCKET_PCT
    if bucket_width <= 0:
        return
    bucket_key = round(price / bucket_width) * bucket_width
    density[bucket_key] = density.get(bucket_key, 0.0) + weight


def find_magnet_zones(density, current_price):
    """
    从密度分布中找出显著高于均值的"强磁区"，分别返回价格低于current_price和高于current_price中
    离现价最近的一个强磁区（如果存在）。
    """
    if not density:
        return None, None

    values = list(density.values())
    mean_v = statistics.mean(values)
    std_v = statistics.pstdev(values) if len(values) > 1 else 0.0
    threshold = mean_v + STRENGTH_STD_MULT * std_v

    strong_zones = [(p, w) for p, w in density.items() if w >= threshold]
    if not strong_zones:
        # 没有明显超阈值的，退化为取权重最高的前5个，保证监控有对象
        strong_zones = sorted(density.items(), key=lambda x: -x[1])[:5]

    below = [p for p, w in strong_zones if p < current_price]
    above = [p for p, w in strong_zones if p > current_price]

    nearest_below = max(below) if below else None
    nearest_above = min(above) if above else None

    return nearest_below, nearest_above


# ============================================================
# 触发判断 + 挂单价格计算
# ============================================================

def compute_order_price(zone_price, atr, current_price, direction):
    """direction: 'long' (下方磁区，做多反弹) 或 'short' (上方磁区，做空反弹)"""
    buffer_pct = ATR_K * atr / current_price
    if direction == "long":
        return zone_price * (1 + buffer_pct), buffer_pct
    else:
        return zone_price * (1 - buffer_pct), buffer_pct


def check_and_alert(symbol, direction, zone_price, current_price, atr, state):
    if zone_price is None:
        return False, None

    if direction == "long":
        distance = (current_price - zone_price) / current_price
    else:
        distance = (zone_price - current_price) / current_price

    if distance < 0:
        distance = abs(distance)

    key = f"{symbol}_{direction}"
    zone_state = state.get(key, {})
    is_cooling_down = zone_state.get("alert_active", False)
    last_zone_price = zone_state.get("zone_price")

    if is_cooling_down and last_zone_price is not None:
        release_distance = abs(current_price - last_zone_price) / current_price
        if release_distance >= RELEASE_THRESHOLD:
            state[key] = {"alert_active": False, "zone_price": None}
            is_cooling_down = False

    should_push = False
    message = None

    if distance <= TRIGGER_THRESHOLD and not is_cooling_down:
        order_price, buffer_pct = compute_order_price(zone_price, atr, current_price, direction)
        direction_cn = "做多反弹" if direction == "long" else "做空反弹"
        message = (
            f"当前价: {current_price:.2f}\n"
            f"磁区价格(估算): {zone_price:.2f}\n"
            f"距离: {distance*100:.2f}%\n"
            f"ATR14动态buffer: {buffer_pct*100:.2f}%\n"
            f"建议挂单价: {order_price:.2f}\n"
            f"(注: 磁区为基于成交量/资金费率的自建估算，非真实清算数据，仅供参考)"
        )
        should_push = True
        state[key] = {"alert_active": True, "zone_price": zone_price}
        title = f"{symbol} {'下方' if direction == 'long' else '上方'}磁区提醒 - {direction_cn}"
        return should_push, (title, message)

    return should_push, None


# ============================================================
# 推送 (Bark)
# ============================================================

def send_bark(title, body):
    if not BARK_KEY:
        print(f"[未配置BARK_KEY，仅打印] {title}\n{body}")
        return
    try:
        url = f"{BARK_SERVER}/{BARK_KEY}/{urllib.parse.quote(title)}/{urllib.parse.quote(body)}"
        requests.get(url, timeout=10)
    except Exception as e:
        print(f"Bark推送失败: {e}")


# ============================================================
# 主流程
# ============================================================

def run():
    state = load_state()

    for inst in INSTRUMENTS:
        symbol = inst["symbol"]
        futures_symbol = inst["futures_symbol"]

        try:
            candles = get_candles(futures_symbol)
            current_price = get_current_price(futures_symbol)
            funding_rate = get_funding_rate(futures_symbol)
            atr = compute_atr(candles)
        except Exception as e:
            print(f"[{symbol}] 数据获取失败: {e}")
            continue

        density = build_liquidation_density(candles, current_price, funding_rate)
        zone_below, zone_above = find_magnet_zones(density, current_price)

        _, result_long = check_and_alert(symbol, "long", zone_below, current_price, atr, state)
        _, result_short = check_and_alert(symbol, "short", zone_above, current_price, atr, state)

        if result_long:
            send_bark(*result_long)
        if result_short:
            send_bark(*result_short)

        print(f"[{symbol}] price={current_price:.2f} atr={atr:.2f} "
              f"zone_below={zone_below} zone_above={zone_above} funding={funding_rate}")

    save_state(state)


if __name__ == "__main__":
    run()
