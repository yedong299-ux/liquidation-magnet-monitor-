# -*- coding: utf-8 -*-
"""
清算磁区反弹监控脚本（Binance USDC永续版）
==========================================
用途：监控 ETH / BTC （对标 Binance USDC本位永续合约的价格区间：ETHUSDC / BTCUSDC），
自建"清算价格聚类"估算（无需付费清算热力图API），当价格双向接近估算出的"强磁区"（±1%以内）时，
计算基于ATR的动态挂单缓冲价格，通过 Bark 推送提醒。

重要说明（2026-09更新）：Binance合约接口(fapi.binance.com)对GitHub Actions所在的美国机房IP
返回451拒绝访问，这是Binance的地区合规限制，不是账号或代码问题。改用Binance现货的不限地区镜像站
(data-api.binance.vision) 获取 ETHUSDC/BTCUSDC 现货价格与K线作为替代数据源——现货价格和合约价格
在ETH/BTC这类主流币上几乎没有偏差，不影响磁区估算的准确性。唯一的代价：现货没有"资金费率"这个概念，
所以脚本里"多空拥挤度权重修正"这部分功能被禁用（对应变量固定为0），核心的清算聚类估算逻辑不受影响。

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

# 注意：不用 fapi.binance.com（合约接口，美国IP会被451拒绝），改用不限地区的现货镜像站
BINANCE_BASE = "https://data-api.binance.vision"

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

# ATR 计算周期
ATR_PERIOD = 14

# 只在现价正负 MAGNET_RANGE_PCT 范围内找磁区，范围外的一律忽略（去掉远端噪音）
MAGNET_RANGE_PCT = 0.05  # 5%

# 磁区强度阈值：密度超过 (区间内局部均值 + STRENGTH_STD_MULT * 局部标准差) 才算"强磁区"
# 注意：均值/标准差只在 MAGNET_RANGE_PCT 范围内计算，不再拿全局密度分布做参照，
# 这样"够不够强"是跟现价附近的其他磁区比，而不是跟很远的历史噪声比。
STRENGTH_STD_MULT = 1.5

# 密集簇判定：从最强磁区(anchor)往价格更远的方向走，只要相邻两个"小磁区"之间的价格间隔
# 不超过 CLUSTER_GAP_PCT，就算同一簇，一直往外延伸到间隔断开或触达 MAGNET_RANGE_PCT 边界为止。
# 挂单价会参考这一整簇最外沿的价格（近似"插针反弹最低价"），而不是只看anchor单点。
CLUSTER_GAP_PCT = 0.003  # 0.3%

# 挂单价相对簇最外沿的微调系数：order_offset_pct = ORDER_EDGE_ATR_K * ATR14 / current_price
# 这个值比之前小很多，因为目标点已经是簇的最深处，不需要再叠加很大的缓冲
ORDER_EDGE_ATR_K = 0.15

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
    url = BINANCE_BASE + path
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_candles(futures_symbol, interval=CANDLE_INTERVAL, limit=CANDLE_LIMIT):
    """返回按时间正序排列的K线列表 [{ts, o, h, l, c, vol}]（走现货镜像站，字段结构与合约K线一致）"""
    raw = binance_get("/api/v3/klines", {
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
    raw = binance_get("/api/v3/ticker/price", {"symbol": futures_symbol})
    return float(raw["price"])


def get_funding_rate(futures_symbol):
    # 现货镜像站没有资金费率数据（合约概念，fapi接口被地区限制拦截），固定返回0，
    # 相当于禁用"多空拥挤度权重修正"这个次要功能，不影响核心清算聚类估算。
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


def find_magnet_zone(density, current_price, direction):
    """
    只在 current_price 正负 MAGNET_RANGE_PCT 范围内找磁区（范围外一律忽略，去掉远端噪音）。
    direction: 'below' (找现价下方) 或 'above' (找现价上方)

    步骤：
    1. 在范围内用局部均值+STRENGTH_STD_MULT*局部标准差筛出"够强"的候选点，取其中最强的一个作为anchor。
       如果范围内没有任何点够强，直接返回None——不再退化去凑一个弱磁区，避免噪声。
    2. 从anchor开始，往"离现价更远"的方向延伸：只要相邻两个磁区点（含未达阈值的小磁区）价格间隔
       不超过CLUSTER_GAP_PCT，就并入同一簇，一直到间隔断开或触达范围边界为止。
    3. 簇最外沿的价格 = edge_price，近似"插针反弹最低/最高价"，后面用来算挂单价。

    返回 dict {"anchor_price", "anchor_weight", "edge_price", "cluster_size"} 或 None
    """
    if not density:
        return None

    lo = current_price * (1 - MAGNET_RANGE_PCT)
    hi = current_price * (1 + MAGNET_RANGE_PCT)

    if direction == "below":
        candidates = {p: w for p, w in density.items() if lo <= p < current_price}
    else:
        candidates = {p: w for p, w in density.items() if current_price < p <= hi}

    if not candidates:
        return None

    weights = list(candidates.values())
    mean_v = statistics.mean(weights)
    std_v = statistics.pstdev(weights) if len(weights) > 1 else 0.0
    threshold = mean_v + STRENGTH_STD_MULT * std_v

    strong = {p: w for p, w in candidates.items() if w >= threshold}
    if not strong:
        return None  # 范围内没有显著磁区，跳过，不勉强凑一个

    anchor_price = max(strong.items(), key=lambda kv: kv[1])[0]
    anchor_weight = strong[anchor_price]

    all_prices_sorted = sorted(candidates.keys())
    max_gap = current_price * CLUSTER_GAP_PCT
    idx = all_prices_sorted.index(anchor_price)
    cluster = [anchor_price]

    if direction == "below":
        # 往更远离现价的方向 = 数组中更靠左（价格更小）
        i = idx
        while i > 0 and (all_prices_sorted[i] - all_prices_sorted[i - 1]) <= max_gap:
            i -= 1
            cluster.append(all_prices_sorted[i])
        edge_price = min(cluster)
    else:
        i = idx
        while i < len(all_prices_sorted) - 1 and (all_prices_sorted[i + 1] - all_prices_sorted[i]) <= max_gap:
            i += 1
            cluster.append(all_prices_sorted[i])
        edge_price = max(cluster)

    return {
        "anchor_price": anchor_price,
        "anchor_weight": anchor_weight,
        "edge_price": edge_price,
        "cluster_size": len(cluster),
    }


# ============================================================
# 触发判断 + 挂单价格计算
# ============================================================

def compute_order_price(edge_price, atr, current_price, direction):
    """
    direction: 'long' (下方簇，做多反弹) 或 'short' (上方簇，做空反弹)
    挂单价放在簇最外沿附近（插针反弹最低/最高价的估算），只叠加一个很小的ATR微调用于容错，
    并强制限制在"现价这一侧"，避免挂单价反而穿到现价对面这种不合理结果。
    """
    edge_offset_pct = ORDER_EDGE_ATR_K * atr / current_price
    if direction == "long":
        raw_order = edge_price * (1 + edge_offset_pct)
        order_price = min(raw_order, current_price * 0.999)
    else:
        raw_order = edge_price * (1 - edge_offset_pct)
        order_price = max(raw_order, current_price * 1.001)
    return order_price, edge_offset_pct


def check_and_alert(symbol, direction, zone, current_price, atr, state):
    """direction: 'below' 或 'above'（对应 find_magnet_zone 的方向）"""
    if zone is None:
        return False, None

    anchor_price = zone["anchor_price"]
    edge_price = zone["edge_price"]
    cluster_size = zone["cluster_size"]
    dir_key = "long" if direction == "below" else "short"

    distance = abs(current_price - anchor_price) / current_price

    key = f"{symbol}_{dir_key}"
    zone_state = state.get(key, {})
    is_cooling_down = zone_state.get("alert_active", False)
    last_zone_price = zone_state.get("zone_price")

    if is_cooling_down and last_zone_price is not None:
        release_distance = abs(current_price - last_zone_price) / current_price
        if release_distance >= RELEASE_THRESHOLD:
            state[key] = {"alert_active": False, "zone_price": None}
            is_cooling_down = False

    if distance <= TRIGGER_THRESHOLD and not is_cooling_down:
        order_price, offset_pct = compute_order_price(edge_price, atr, current_price, dir_key)
        direction_cn = "做多反弹" if dir_key == "long" else "做空反弹"
        cluster_note = f"（含{cluster_size}个密集小磁区，已取簇最外沿）" if cluster_size > 1 else ""
        message = (
            f"当前价: {current_price:.2f}\n"
            f"强磁区(anchor): {anchor_price:.2f}\n"
            f"磁区簇最外沿: {edge_price:.2f}{cluster_note}\n"
            f"距离(现价→anchor): {distance*100:.2f}%\n"
            f"建议挂单价(近簇外沿): {order_price:.2f}\n"
            f"(注: 磁区为基于成交量的自建估算，非真实清算数据，仅供参考)"
        )
        state[key] = {"alert_active": True, "zone_price": anchor_price}
        title = f"{symbol} {'下方' if dir_key == 'long' else '上方'}磁区提醒 - {direction_cn}"
        return True, (title, message)

    return False, None


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
        zone_below = find_magnet_zone(density, current_price, "below")
        zone_above = find_magnet_zone(density, current_price, "above")

        _, result_long = check_and_alert(symbol, "below", zone_below, current_price, atr, state)
        _, result_short = check_and_alert(symbol, "above", zone_above, current_price, atr, state)

        if result_long:
            send_bark(*result_long)
        if result_short:
            send_bark(*result_short)

        below_desc = f"anchor={zone_below['anchor_price']:.2f}/edge={zone_below['edge_price']:.2f}" if zone_below else "无"
        above_desc = f"anchor={zone_above['anchor_price']:.2f}/edge={zone_above['edge_price']:.2f}" if zone_above else "无"
        print(f"[{symbol}] price={current_price:.2f} atr={atr:.2f} "
              f"zone_below=({below_desc}) zone_above=({above_desc})")

    save_state(state)


if __name__ == "__main__":
    run()
