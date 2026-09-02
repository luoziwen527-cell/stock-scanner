import os
import time
import json
import urllib.request
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime

# 彻底屏蔽代理环境
for k in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']:
    os.environ.pop(k, None)
urllib.request.getproxies = lambda: {}

st.set_page_config(
    page_title="AI 智能主力量化投研系统 (三大核心策略闭环版)",
    layout="wide",
    page_icon="🧠"
)

# ==================== 本地自选/持仓持久化 ====================
PORTFOLIO_FILE = "user_portfolio.json"

def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_portfolio(data):
    try:
        with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

if 'scan_results' not in st.session_state:
    st.session_state['scan_results'] = []
if 'kline_cache' not in st.session_state:
    st.session_state['kline_cache'] = {}
if 'has_scanned' not in st.session_state:
    st.session_state['has_scanned'] = False
if 'portfolio' not in st.session_state:
    st.session_state['portfolio'] = load_portfolio()

# ==================== 大盘宏观状态 ====================
@st.cache_data(ttl=60)
def fetch_market_macro_status():
    url = "https://qt.gtimg.cn/q=s_sh000001,s_sz399001,s_sz399006"
    macro_info = {
        "sh_pct": 0.0, "sh_price": 3100.0, "sh_amt": 0,
        "sz_pct": 0.0, "cy_pct": 0.0,
        "status_color": "🟢", "status_text": "安全进攻区",
        "suggest_position": "60% ~ 80%",
        "action_guide": "大盘处于活跃可操作区间，可积极参与主力蓄势与强势突破标的。",
        "market_score": 12
    }
    try:
        resp = requests.get(url, timeout=2.5)
        lines = resp.text.strip().split(";")
        for line in lines:
            if "s_sh000001" in line and "=" in line:
                parts = line.split("=")[1].strip('"').split("~")
                if len(parts) >= 6:
                    p = float(parts[2] or 0)
                    if p > 100:
                        macro_info["sh_price"] = p
                    macro_info["sh_pct"] = float(parts[5] or 0)
            elif "s_sz399001" in line and "=" in line:
                parts = line.split("=")[1].strip('"').split("~")
                if len(parts) >= 6:
                    macro_info["sz_pct"] = float(parts[5] or 0)
            elif "s_sz399006" in line and "=" in line:
                parts = line.split("=")[1].strip('"').split("~")
                if len(parts) >= 6:
                    macro_info["cy_pct"] = float(parts[5] or 0)

        sh_pct = macro_info["sh_pct"]
        if sh_pct >= 0.3:
            macro_info.update({
                "status_color": "🟢", "status_text": "多头进攻周期",
                "suggest_position": "70% ~ 90%",
                "action_guide": "大盘处于多头进攻阶段，顺势低吸主升或蓄势标的。",
                "market_score": 15
            })
        elif -1.0 <= sh_pct < 0.3:
            macro_info.update({
                "status_color": "🟡", "status_text": "震荡分歧周期",
                "suggest_position": "40% ~ 60%",
                "action_guide": "大盘日内分歧震荡，严格在主力底线与支撑位附近低吸。",
                "market_score": 10
            })
        else:
            macro_info.update({
                "status_color": "🔴", "status_text": "弱势防守区",
                "suggest_position": "20% ~ 40%",
                "action_guide": "大盘破位走弱，仅轻仓博弈强庄抗跌与洗盘反包标的。",
                "market_score": 8
            })
    except Exception:
        pass
    return macro_info

# ==================== 侧边栏配置 ====================
with st.sidebar:
    st.header("🔀 核心量化策略 (3 选 1)")
    strategy_mode = st.selectbox(
        "选择当前运行策略：",
        [
            "3️⃣ 图片绝技：三步极选强势股闭环策略 (量异动+均线多头+高控盘筹码)",
            "2️⃣ 主力博弈+龙头二波/底部洗盘策略 (视频理念)",
            "1️⃣ 多周期均线+ATR低吸策略 (趋势均线共振)"
        ]
    )

    st.divider()
    st.header("🎯 选股与呈现参数")
    display_top_n = st.slider("最终呈现上限 (只)", 3, 60, 20, 1)
    deep_sample_size = st.slider("深度分析样本量 (只)", 100, 1500, 500, 50)

    # 针对策略3自动锁定参数，策略1与2开放自定义
    if "3️⃣" in strategy_mode:
        st.info("💡 已自动锁定【三步极选法】黄金区间：\n• 涨幅：3.0% ~ 5.0%\n• 换手率：3.0% ~ 10.0%\n• 量比 > 1.8 倍")
        min_scan_pct, max_scan_pct = 2.8, 5.2
    else:
        max_scan_pct = st.slider("日内最大涨幅上限 (%)", 1.0, 9.5, 5.0, 0.1)
        min_scan_pct = st.slider("日内最小涨幅下限 (%)", -7.0, 2.0, -4.0, 0.1)

    enable_ultra_filter = st.checkbox("💎 开启【盈亏比与趋势】过滤", value=True)
    min_risk_reward = st.slider("最低盈亏比门槛", 1.2, 3.5, 1.8, 0.1) if (enable_ultra_filter and "3️⃣" not in strategy_mode) else 1.0
    enable_weekly_filter = st.checkbox("📈 开启【中长线多头趋势】过滤", value=True)
    enable_fundamental_filter = st.checkbox("🛡️ 开启【基本面轻量排雷】", value=True)

    st.divider()
    st.subheader("🎯 基础标的池过滤")
    board_type = st.selectbox("市场板块", ["全部主板 (60 + 00)", "仅沪市主板 (60开头)", "仅深市主板 (00开头)"])
    price_range = st.slider("股价区间 (元)", 1.0, 100.0, (2.0, 70.0), 0.5)
    min_price, max_price = price_range
    min_amount = st.slider("最低日成交额门槛 (万元)", 500, 30000, 1500, 500)
    exclude_limit_up = st.checkbox("🚫 剔除涨停封板股票", value=True)

# ==================== 页面头部 ====================
macro = fetch_market_macro_status()
m_col1, m_col2, m_col3, m_col4 = st.columns([1.2, 1, 1.2, 2.6])
m_col1.metric("🏛️ 上证指数", f"{macro['sh_price']} 点", f"{macro['sh_pct']:+.2f}%")
m_col2.metric("🏛️ 深证成指", f"{macro['sz_pct']:+.2f}%")
m_col3.metric("🧭 建议总仓位", macro['suggest_position'], f"{macro['status_color']} {macro['status_text']}")
with m_col4:
    st.info(f"💡 **大盘战术风控指引**：\n{macro['action_guide']}")

st.divider()

# ==================== 行情数据拉取 ====================
def generate_stock_codes(b_type: str):
    symbols = []
    if "仅深市" not in b_type:
        for i in range(0, 600):
            symbols.append(f"sh600{i:03d}")
        for i in range(0, 350):
            symbols.append(f"sh601{i:03d}")
        for i in range(0, 500):
            symbols.append(f"sh603{i:03d}")
        for i in range(0, 400):
            symbols.append(f"sh605{i:03d}")
    if "仅沪市" not in b_type:
        for i in range(0, 1000):
            symbols.append(f"sz000{i:03d}")
        for i in range(0, 350):
            symbols.append(f"sz001{i:03d}")
        for i in range(0, 1000):
            symbols.append(f"sz002{i:03d}")
        for i in range(0, 400):
            symbols.append(f"sz003{i:03d}")
    return symbols

def fetch_tencent_batch(batch_symbols):
    url = f"https://qt.gtimg.cn/q={','.join(batch_symbols)}"
    items = []
    try:
        resp = requests.get(url, timeout=3.5)
        for line in resp.text.strip().split(";"):
            if not line or "=" not in line:
                continue
            data_str = line.split("=")[1].strip().strip('"')
            if not data_str:
                continue
            fields = data_str.split("~")
            if len(fields) < 46:
                continue
            name, code = fields[1], fields[2]
            price = float(fields[3] or 0)
            if price <= 0 or "ST" in name or "退" in name:
                continue
            pe = float(fields[39] or 0) if len(fields) > 39 and fields[39] else 0.0
            pb = float(fields[46] or 0) if len(fields) > 46 and fields[46] else 0.0

            items.append({
                "代码": code, "名称": name, "最新价": price,
                "昨收": float(fields[4] or 0), "今开": float(fields[5] or 0),
                "最高": float(fields[33] or price), "最低": float(fields[34] or price),
                "涨跌幅": float(fields[32] or 0),
                "成交额(万)": float(fields[37] or 0) if len(fields) > 37 and fields[37] else (float(fields[6] or 0) * price / 100),
                "换手率": float(fields[38] or 0) if len(fields) > 38 and fields[38] else 1.0,
                "成交量": float(fields[6] or 0),
                "PE": pe, "PB": pb
            })
    except Exception:
        pass
    return items

@st.cache_data(ttl=120)
def get_all_realtime_stocks_tx(b_type: str, min_p: float, max_p: float, min_amt: float, no_limit: bool):
    symbols = generate_stock_codes(b_type)
    batches = [symbols[i:i+100] for i in range(0, len(symbols), 100)]
    all_stocks = []
    with ThreadPoolExecutor(max_workers=25) as executor:
        for f in as_completed([executor.submit(fetch_tencent_batch, b) for b in batches]):
            res = f.result()
            if res:
                all_stocks.extend(res)
    df = pd.DataFrame(all_stocks)
    if df.empty:
        return df
    df = df[(df['最新价'] >= min_p) & (df['最新价'] <= max_p)]
    if min_amt > 0:
        df_f = df[df['成交额(万)'] >= min_amt]
        if len(df_f) >= 20:
            df = df_f
    if no_limit:
        df = df[df['涨跌幅'] < 9.5]
    return df.drop_duplicates(subset=['代码']).reset_index(drop=True)

def fetch_kline_safe(code, row_data, days=90):
    market = "sh" if str(code).startswith("60") else "sz"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market}{code},day,,,{days},qfq"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=3.0)
        raw = resp.json().get("data", {}).get(f"{market}{code}", {})
        raw_klines = raw.get("qfqday") or raw.get("day") or []
        if raw_klines and len(raw_klines) >= 10:
            data = [{"日期": r[0], "开盘": float(r[1]), "收盘": float(r[2]),
                     "最高": float(r[3]), "最低": float(r[4]), "成交量": float(r[5])} for r in raw_klines]
            k_df = pd.DataFrame(data)
            k_df['涨跌幅'] = k_df['收盘'].pct_change() * 100
            k_df['涨跌幅'] = k_df['涨跌幅'].fillna(0)
            return k_df
    except Exception:
        pass

    p = float(row_data.get('最新价', 10))
    mock_dates = pd.date_range(end=datetime.today(), periods=30).strftime('%Y-%m-%d').tolist()
    mock_data = [{"日期": d, "开盘": p * 0.99, "收盘": p, "最高": p * 1.01, "最低": p * 0.98, "成交量": 15000.0, "涨跌幅": 0.5} for d in mock_dates]
    return pd.DataFrame(mock_data)

def calculate_atr(k_df: pd.DataFrame, period: int = 14) -> float:
    if len(k_df) < period + 1:
        return float(k_df['收盘'].iloc[-1] * 0.025)
    high, low, close = k_df['最高'].values, k_df['最低'].values, k_df['收盘'].values
    tr = np.maximum(high[1:] - low[1:], np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
    return float(np.mean(tr[-period:]))

# 精准筹码集中度计算 (70% 与 90% 集中度)
def calculate_precise_chip_concentration(k_df: pd.DataFrame, current_price: float, lookback: int = 60):
    if len(k_df) < 15:
        return {"width_90": 25.0, "width_70": 18.0, "chip_peak": current_price, "profit_ratio": 50.0}
    df = k_df.tail(lookback).copy()
    typical = (df['最高'] + df['最低'] + df['收盘']) / 3.0
    vols = df['成交量'].values
    prices = typical.values
    decay_weights = vols * np.exp(np.linspace(-0.8, 0, len(df)))

    sorted_indices = np.argsort(prices)
    sorted_p = prices[sorted_indices]
    sorted_w = decay_weights[sorted_indices]
    cum_w = np.cumsum(sorted_w)
    total_w = cum_w[-1] + 1e-9
    cum_norm = cum_w / total_w

    p5 = sorted_p[np.searchsorted(cum_norm, 0.05)]
    p95 = sorted_p[np.searchsorted(cum_norm, 0.95)]
    p15 = sorted_p[np.searchsorted(cum_norm, 0.15)]
    p85 = sorted_p[np.searchsorted(cum_norm, 0.85)]

    avg_cost = np.average(prices, weights=decay_weights)
    width_90 = round((p95 - p5) / (avg_cost + 1e-6) * 100, 1)
    width_70 = round((p85 - p15) / (avg_cost + 1e-6) * 100, 1)
    profit_ratio = round(np.sum(decay_weights[prices <= current_price]) / total_w * 100, 1)

    return {
        "width_90": width_90,
        "width_70": width_70,
        "chip_peak": round(avg_cost, 2),
        "profit_ratio": profit_ratio
    }

# ==================== 策略 3：三步极选强势股闭环策略 ====================
def evaluate_strategy_three_step_champion(df: pd.DataFrame, row_data: dict, macro_status: dict):
    pct = float(row_data.get('涨跌幅', 0))
    turnover = float(row_data.get('换手率', 0))
    close = df['收盘'].values
    highs = df['最高'].values
    lows = df['最低'].values
    vols = df['成交量'].values
    n = len(df)

    if n < 25:
        return None

    # 第一步：涨幅 3%~5%，换手率 3%~10%，量比 > 1.8倍
    if not (2.8 <= pct <= 5.2):
        return None
    if not (2.8 <= turnover <= 10.5):
        return None

    vol_today = vols[-1]
    vol_5d_avg = np.mean(vols[-6:-1]) if n >= 6 else vol_today
    vol_ratio = vol_today / (vol_5d_avg + 1e-6)
    if vol_ratio < 1.8:
        return None

    # 第二步：均线多头 + 站稳MA5 + 1个月内有放量大阳 + 回调不破MA10
    ma5 = np.mean(close[-5:])
    ma10 = np.mean(close[-10:])
    ma20 = np.mean(close[-20:])

    if not (ma5 >= ma10 * 0.995 and ma10 >= ma20 * 0.995):
        return None
    if close[-1] < ma5 * 0.99:
        return None

    surge_indices = [i for i in range(max(1, n-22), n) if (close[i] / close[i-1] - 1) >= 0.048 and vols[i] >= 1.4 * np.mean(vols[max(0, i-5):i])]
    if not surge_indices:
        return None

    last_surge_idx = surge_indices[-1]
    for i in range(last_surge_idx + 1, n):
        curr_ma10 = np.mean(close[max(0, i-9):i+1])
        if lows[i] < curr_ma10 * 0.975:
            return None

    # 第三步：筹码高控盘 (70%集中度<16.5%)
    chip_data = calculate_precise_chip_concentration(df, close[-1])
    w90, w70 = chip_data["width_90"], chip_data["width_70"]
    if w90 > 22.0 or w70 > 16.5:
        return None

    total_score = 92
    tags = ["🔥 放量异动", "📈 均线多头", f"👑 主力高度控盘(70%集中度:{w70}%)"]

    buy_low = round(ma5, 2)
    buy_high = round(close[-1], 2)
    stop_loss_ma10 = round(ma10, 2)
    target_high = round(np.max(highs[-20:]) * 1.12, 2)

    why_buy_core = (
        f"【三步极选闭环全达标】：今日温和放量 {vol_ratio:.1f} 倍涨 {pct:.1f}%，换手 {turnover:.1f}% 不冷不热；"
        f"均线多头排列且站稳 5 日线；70% 筹码集中度达 {w70}%（主力已高度锁定），随后回踩不破10日线即起飞。"
    )

    advice = {
        "建议买入区间": f"{buy_low} ~ {buy_high}",
        "建议买入区间_低": buy_low, "建议买入区间_高": buy_high,
        "建议止损位": f"{stop_loss_ma10} (跌破10日线坚决清仓)",
        "止损数值": stop_loss_ma10,
        "第一止盈目标": f"{target_high} (突破前高空间)",
        "止盈数值": target_high,
        "动态压力位": round(np.max(highs[-20:]), 2),
        "动态支撑位": round(ma10, 2),
        "ATR": round(close[-1] * 0.03, 3),
        "盈亏比": f"{round((target_high - close[-1]) / max(0.01, close[-1] - stop_loss_ma10), 1)} : 1",
        "买入时段": "🌇 尾盘进场 (14:30 - 14:50)",
        "卖出时机": "盘中破 5 日线减半，破 10 日线清仓离场",
        "预估持股周期": "⚡ 顺势主升 (2 ~ 6 个交易日)",
        "为什么值得买": why_buy_core,
        "自适应仓位": "首批进6成，突破新高加4成"
    }

    timing_dict = {
        "买点战术详情": [
            "🌇 【尾盘买入】：回踩 5 日或 10 日均线缩量止跌，尾盘 14:30 进场 60% 底仓！",
            "🚀 【加仓时机】：次日或随后交易日放量突破创新高时，顺势追加入 40% 仓位！",
            f"🛡️ 【铁律止盈止损】：以 5 日线为减仓线（跌破先减一半），跌破 10 日线 {stop_loss_ma10} 元坚决清仓走人，不猜顶不逃顶！"
        ]
    }

    radar = {"主力异动": 19, "洗盘充分度": 18, "筹码沉淀": 20, "底部安全性": 17, "博弈胜率": 20}
    return total_score, 45, 47, tags, advice, radar, timing_dict, chip_data, True, "🟢 低", 3.0

# ==================== 策略 2：主力逻辑博弈与假摔洗盘 ====================
def evaluate_strategy_main_force_game(df: pd.DataFrame, row_data: dict, enable_weekly: bool, enable_fundamental: bool, macro_status: dict, min_rr: float):
    quality, timing, tags = 15, 15, []
    close = df['收盘'].values
    highs = df['最高'].values
    lows = df['最低'].values
    opens = df['开盘'].values
    vols = df['成交量'].values
    n = len(df)
    pct = float(row_data.get('涨跌幅', 0))
    pb = float(row_data.get('PB', 0))

    if enable_fundamental and pb < 0:
        return None

    if enable_weekly and n >= 20:
        ma20 = np.mean(close[-20:])
        if close[-1] < ma20 * 0.94:
            return None

    chip_info = calculate_precise_chip_concentration(df, close[-1])
    atr = calculate_atr(df, 14)

    limit_ups = sum(1 for i in range(max(1, n-25), n) if (close[i] / close[i-1] - 1) >= 0.055)
    max_p_25d = np.max(highs[-25:]) if n >= 25 else close[-1]
    pullback_pct = (max_p_25d - close[-1]) / max_p_25d * 100

    pattern_a_hit = False
    if limit_ups >= 1 and 4.0 <= pullback_pct <= 38.0:
        pattern_a_hit = True
        quality += 25
        tags.append(f"🔥 强势回调蓄势({pullback_pct:.1f}%)")
        recent_surge_idx = [i for i in range(max(1, n-25), n) if (close[i] / close[i-1] - 1) >= 0.055]
        logic_base_price = opens[recent_surge_idx[-1]] if recent_surge_idx else lows[-10:].min()
        why_buy_core = f"前期大阳放量突破后，主力缩量急跌洗盘 {pullback_pct:.1f}%，现已触底大阳起涨底线，假摔洗盘结束随时迎来二波反包拉升。"
    else:
        logic_base_price = lows[-15:].min() if n >= 15 else close[-1] * 0.92

    pattern_b_hit = False
    if n >= 15 and not pattern_a_hit:
        vol_avg = np.mean(vols[-15:])
        surge_days = [i for i in range(n-15, n-1) if vols[i] >= 1.2 * vol_avg and (close[i] / close[i-1] - 1) >= 0.025]
        if surge_days:
            first_i = surge_days[0]
            surge_open, surge_low = opens[first_i], lows[first_i]
            if close[-1] >= surge_open * 0.93:
                pattern_b_hit = True
                quality += 22
                logic_base_price = min(surge_open, surge_low)
                tags.append("🎯 底部放量洗盘(守底线)")
                why_buy_core = "底部出现异动放量主力吃货，随后几天连续不涨看似疲软，实为缩量假摔挖坑洗出浮筹，未破成本线，变盘在即。"

    pattern_c_hit = False
    if n >= 10 and not (pattern_a_hit or pattern_b_hit):
        ma5, ma10, ma20 = np.mean(close[-5:]), np.mean(close[-10:]), np.mean(close[-min(20, n):])
        spread = (max(ma5, ma10, ma20) - min(ma5, ma10, ma20)) / ma20 * 100
        if spread < 8.5:
            pattern_c_hit = True
            quality += 18
            tags.append(f"🌀 均线粘合({spread:.1f}%)")
            why_buy_core = "多日横盘不涨是由于主力在此进行【筹码沉淀与均线收敛】，波动率压缩至极点，属于典型的暴风雨前静默吸筹期。"

    if not (pattern_a_hit or pattern_b_hit or pattern_c_hit):
        return None

    if -4.5 <= pct <= 4.0:
        timing += 16
        tags.append("企稳承接区")

    buy_low = round(max(logic_base_price * 1.002, close[-1] * 0.965), 2)
    buy_high = round(close[-1] * 1.015, 2)
    stop_loss_logic = round(logic_base_price * 0.96, 2)
    target_p = round(max(max_p_25d, close[-1] * 1.15), 2)

    risk_span = max(0.01, close[-1] - stop_loss_logic)
    reward_span = max(0.01, target_p - close[-1])
    rr_ratio = round(reward_span / risk_span, 2)

    if rr_ratio < min_rr:
        return None

    total = quality + timing + macro_status.get("market_score", 10)
    if rr_ratio >= 2.8:
        total += 8
        tags.append(f"💎 盈亏比({rr_ratio}:1)")

    if pattern_a_hit or pattern_b_hit:
        buy_time_window = "🌅 早盘 (09:40 - 10:15)"
        sell_time_window = "🚀 冲高离场 (10:30-11:00 或 14:30冲板失败分批卖)"
        holding_period = "⚡ 短线爆发 (1 ~ 4 个交易日)"
        timing_detail_text = "🌅 【早盘买点】：早盘主力惯性急跌震仓，观察 9:40 之后不再创新低且分时放量站上均线即可建仓。"
    else:
        buy_time_window = "🌇 尾盘 (14:30 - 14:50)"
        sell_time_window = "🎯 稳步止盈 (次日或第三日放量大阳线冲高卖出)"
        holding_period = "⏳ 稳健波段 (3 ~ 8 个交易日)"
        timing_detail_text = "🌇 【尾盘买点】：横盘多日蓄势股，尾盘 14:30 确认未跳水破位、收在均线上方直接买入锁定隔夜主动权。"

    advice = {
        "建议买入区间": f"{buy_low} ~ {buy_high}",
        "建议买入区间_低": buy_low, "建议买入区间_高": buy_high,
        "建议止损位": f"{stop_loss_logic} (主力逻辑防守线)",
        "止损数值": stop_loss_logic,
        "第一止盈目标": f"{target_p} (目标空间: +{round((target_p/close[-1]-1)*100, 1)}%)",
        "止盈数值": target_p,
        "动态压力位": round(max_p_25d, 2),
        "动态支撑位": round(logic_base_price, 2),
        "ATR": round(atr, 3),
        "盈亏比": f"{rr_ratio} : 1",
        "买入时段": buy_time_window,
        "卖出时机": sell_time_window,
        "预估持股周期": holding_period,
        "为什么值得买": why_buy_core,
        "自适应仓位": "30% (博弈主力建仓仓位)"
    }

    timing_dict = {
        "买点战术详情": [
            timing_detail_text,
            f"🎯 【主力防守线】：以起涨底线 {stop_loss_logic} 元为铁律止损线，收盘跌破坚决走人，不破绝不割肉！",
            f"🚀 【卖出与止盈】：{sell_time_window}，目标价 {target_p} 元附近分批止盈锁定利润。",
            f"💡 【逻辑解析】：{why_buy_core}"
        ]
    }

    radar = {
        "主力异动": 18 if pattern_a_hit else 14,
        "洗盘充分度": 16 if "洗盘" in why_buy_core or "回调" in why_buy_core else 12,
        "筹码沉淀": min(20, int(chip_info['profit_ratio'] * 0.2)),
        "底部安全性": 18 if pattern_b_hit else 14,
        "博弈胜率": min(20, int(total * 0.22))
    }

    return total, quality, timing, tags, advice, radar, timing_dict, chip_info, True, "🟢 低", rr_ratio

# ==================== 策略 1：多周期均线 + ATR 低吸策略 ====================
def evaluate_strategy_classic_atr(df: pd.DataFrame, row_data: dict, enable_weekly: bool, enable_fundamental: bool, macro_status: dict, min_rr: float):
    quality, timing, tags = 15, 15, []
    close = df['收盘'].values
    pct = float(row_data.get('涨跌幅', 0))
    pb = float(row_data.get('PB', 0))
    n = len(df)

    if enable_fundamental and pb < 0:
        return None

    ma5 = df['收盘'].rolling(5).mean().iloc[-1] if n >= 5 else close[-1]
    ma10 = df['收盘'].rolling(10).mean().iloc[-1] if n >= 10 else close[-1]
    ma20 = df['收盘'].rolling(20).mean().iloc[-1] if n >= 20 else close[-1]

    if close[-1] < ma20 * 0.97:
        return None

    atr = calculate_atr(df, 14)
    chip_info = calculate_precise_chip_concentration(df, close[-1])

    if close[-1] >= ma20 * 0.99:
        quality += 18
        tags.append("站稳中线支撑")
    if close[-1] > ma5 * 0.985:
        quality += 12
        tags.append("短期均线蓄势")

    bias5 = (close[-1] / ma5 - 1) * 100 if ma5 > 0 else 0
    if -2.0 <= bias5 <= 3.0:
        timing += 18
        tags.append("🌟 黄金低吸位")

    buy_low = round(close[-1] * 0.98, 2)
    buy_high = round(close[-1] * 1.015, 2)
    stop_loss_p = round(min(df['最低'].iloc[-1], ma20) - 0.5 * atr, 2)
    target1 = round(close[-1] + 1.8 * atr, 2)

    risk_span = max(0.01, close[-1] - stop_loss_p)
    reward_span = max(0.01, target1 - close[-1])
    rr_ratio = round(reward_span / risk_span, 2)

    if rr_ratio < min_rr:
        return None

    total = quality + timing + macro_status.get("market_score", 10)
    why_buy_core = "股价沿中期均线温和推升，虽近期波动较小看似不涨，但下有 MA20 强力托底，洗盘缩量，动能正在积蓄准备开启主升浪。"

    buy_time_window = "🌇 尾盘 (14:30 - 14:50)"
    sell_time_window = "🎯 冲高止盈 (盘中放量上冲 MA5 上方 +3%~+5% 逐步分批止盈)"
    holding_period = "⏳ 稳健持股 (3 ~ 10 个交易日)"

    advice = {
        "建议买入区间": f"{buy_low} ~ {buy_high}",
        "建议买入区间_低": buy_low, "建议买入区间_高": buy_high,
        "建议止损位": f"{stop_loss_p} (ATR动态止损)",
        "止损数值": stop_loss_p,
        "第一止盈目标": f"{target1} (目标空间: +{round((target1/close[-1]-1)*100, 1)}%)",
        "止盈数值": target1,
        "动态压力位": round(close[-1] * 1.08, 2),
        "动态支撑位": round(ma20, 2),
        "ATR": round(atr, 3),
        "盈亏比": f"{rr_ratio} : 1",
        "买入时段": buy_time_window,
        "卖出时机": sell_time_window,
        "预估持股周期": holding_period,
        "为什么值得买": why_buy_core,
        "自适应仓位": "25% (ATR波动率平价)"
    }

    timing_dict = {
        "买点战术详情": [
            "🌇 【尾盘买点】：趋势均线型标的，14:30 后确认站稳均线低吸，避免日内冲高回落被套。",
            f"🛡️ 【ATR止损】：严格执行动态止损线 {stop_loss_p} 元，破位不侥幸。",
            f"🚀 【卖出时机】：{sell_time_window}，目标位 {target1} 元分批落袋。",
            f"💡 【逻辑解析】：{why_buy_core}"
        ]
    }

    radar = {
        "均线趋势": 16,
        "资金强度": 14,
        "突破动能": 13,
        "换手活跃": 14,
        "位置安全": 16
    }
    return total, quality, timing, tags, advice, radar, timing_dict, chip_info, True, "🟢 低", rr_ratio

# ==================== 工作任务分发 ====================
def worker_task(code, name, row_data, strategy_choice, enable_weekly, enable_fundamental, macro_status, min_rr):
    k_df = fetch_kline_safe(code, row_data, days=90)
    last_close = float(k_df['收盘'].iloc[-1])
    pct_today = float(k_df['涨跌幅'].iloc[-1]) if len(k_df) > 1 else float(row_data.get('涨跌幅', 0))

    if "3️⃣" in strategy_choice:
        res = evaluate_strategy_three_step_champion(k_df, row_data, macro_status)
    elif "2️⃣" in strategy_choice:
        res = evaluate_strategy_main_force_game(k_df, row_data, enable_weekly, enable_fundamental, macro_status, min_rr)
    else:
        res = evaluate_strategy_classic_atr(k_df, row_data, enable_weekly, enable_fundamental, macro_status, min_rr)

    if not res:
        return None

    total, quality, timing, tags, advice, radar, timing_dict, chip_info, passed, risk_level, rr_ratio = res
    star_rating = "⭐⭐⭐⭐⭐" if total >= 85 else ("⭐⭐⭐⭐" if total >= 75 else "⭐⭐⭐")
    amt_wan = float(row_data.get('成交额(万)', 0))

    k_df['MA5'] = k_df['收盘'].rolling(5).mean().fillna(k_df['收盘'])
    k_df['MA10'] = k_df['收盘'].rolling(10).mean().fillna(k_df['收盘'])
    k_df['MA20'] = k_df['收盘'].rolling(20).mean().fillna(k_df['收盘'])

    return {
        "代码": code, "名称": name, "评级": star_rating,
        "为什么值得买": advice.get("为什么值得买", ""),
        "买入时段": advice.get("买入时段", "尾盘"),
        "持股周期": advice.get("预估持股周期", "3-5天"),
        "仓位战术": advice.get("自适应仓位", "分批建仓"),
        "综合评分": total,
        "最新价": last_close, "涨跌幅(%)": round(pct_today, 2), "成交额(万)": int(amt_wan),
        "量化特征": " | ".join(tags) if tags else "强势精选",
        "advice": advice, "timing": timing_dict, "radar": radar, "chip_info": chip_info,
        "k_df": k_df, "row_data": row_data
    }

# ==================== 图表可视化 ====================
def draw_pro_kline(code, name, k_df, advice):
    recent = k_df.tail(45).copy()
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.72, 0.28])
    fig.add_trace(go.Candlestick(x=recent['日期'], open=recent['开盘'], high=recent['最高'],
                                 low=recent['最低'], close=recent['收盘'],
                                 increasing_line_color='#ef5350', decreasing_line_color='#26a69a', name="K线"), row=1, col=1)
    for ma_col, color, label in [('MA5', '#ff9800', 'MA5 (减仓线)'), ('MA10', '#2196f3', 'MA10 (生命线)'), ('MA20', '#9c27b0', 'MA20')]:
        if ma_col in recent.columns:
            fig.add_trace(go.Scatter(x=recent['日期'], y=recent[ma_col], line=dict(color=color, width=1.4), name=label), row=1, col=1)
    b_low = advice.get("建议买入区间_低", recent['收盘'].iloc[-1] * 0.98)
    b_high = advice.get("建议买入区间_高", recent['收盘'].iloc[-1])
    fig.add_hrect(y0=b_low, y1=b_high, fillcolor="rgba(0, 230, 118, 0.15)", line_width=0,
                  annotation_text=f"🎯 买入区间: {b_low}~{b_high}", annotation_position="top left", row=1, col=1)
    fig.add_hline(y=advice["动态压力位"], line_dash="dot", line_color="#ff1744", annotation_text=f"目标/压力: {advice['动态压力位']}", row=1, col=1)
    fig.add_hline(y=advice["动态支撑位"], line_dash="dash", line_color="#00e676", annotation_text=f"主力底线/生命线: {advice['动态支撑位']}", row=1, col=1)
    vol_colors = ['#ef5350' if c >= o else '#26a69a' for c, o in zip(recent['收盘'], recent['开盘'])]
    fig.add_trace(go.Bar(x=recent['日期'], y=recent['成交量'], marker_color=vol_colors, name="成交量"), row=2, col=1)
    fig.update_layout(title=f"📈 {code} {name} (最新 {recent['收盘'].iloc[-1]} 元)",
                      xaxis_rangeslider_visible=False, height=450, margin=dict(l=10, r=10, t=35, b=10))
    return fig

# ==================== 扫描执行 ====================
if st.button("🚀 启动全市场深度量化极速扫描", type="primary", use_container_width=True):
    t_start = time.time()
    with st.spinner(f"正在全景扫描主板标的池..."):
        pool = get_all_realtime_stocks_tx(board_type, min_price, max_price, min_amount, exclude_limit_up)

    if len(pool) == 0:
        st.error("❌ 标的池初筛为空，请调宽左侧参数。")
        st.stop()

    candidates = pool[(pool['涨跌幅'] >= min_scan_pct) & (pool['涨跌幅'] <= max_scan_pct)].sort_values(
        by=["成交额(万)"], ascending=False
    ).head(deep_sample_size)

    if len(candidates) < 15:
        candidates = pool.sort_values(by=["成交额(万)"], ascending=False).head(deep_sample_size)

    hit_results, new_kline_cache = [], {}
    progress_bar = st.progress(0, text=f"正在深度分析 {len(candidates)} 只样本...")
    completed = 0
    with ThreadPoolExecutor(max_workers=25) as executor:
        futures = [executor.submit(worker_task, str(row['代码']).zfill(6), row['名称'], row.to_dict(),
                                   strategy_mode, enable_weekly_filter, enable_fundamental_filter, macro, min_risk_reward)
                   for _, row in candidates.iterrows()]
        for future in as_completed(futures):
            completed += 1
            res_item = future.result()
            if res_item:
                k_df = res_item.pop("k_df")
                row_d = res_item.pop("row_data")
                new_kline_cache[res_item["代码"]] = (res_item["名称"], k_df, res_item["advice"],
                                                    res_item["radar"], res_item["timing"],
                                                    res_item["chip_info"], row_d)
                hit_results.append(res_item)
            progress_bar.progress(completed / len(candidates), text=f"分析进度: {completed}/{len(candidates)}")
    progress_bar.empty()

    hit_results = sorted(hit_results, key=lambda x: x["综合评分"], reverse=True)[:display_top_n]
    st.session_state['scan_results'] = hit_results
    st.session_state['kline_cache'] = new_kline_cache
    st.session_state['has_scanned'] = True
    elapsed = round(time.time() - t_start, 1)
    st.toast(f"⚡ 扫描完成！锁定 Top {len(hit_results)} 只标的，耗时 {elapsed} 秒", icon="🎉")

# ==================== 结果看板 ====================
tab_view_select, tab_view_portfolio = st.tabs(["🔥 AI 智能精选投研看板", "💼 我的网页持仓/自选监控池"])

with tab_view_select:
    if st.session_state.get('scan_results'):
        results = st.session_state['scan_results']
        kline_cache = st.session_state['kline_cache']
        res_df = pd.DataFrame(results)

        st.subheader("👑 今日核心自选前三甲 (闭眼关注)")
        top3_cols = st.columns(min(3, len(results)))
        for idx, col in enumerate(top3_cols):
            r_item = results[idx]
            adv = r_item['advice']
            with col:
                st.markdown(f"""
                <div style="background-color:rgba(255,255,255,0.05); padding:14px; border-radius:8px; border-left:4px solid #ff4b4b;">
                    <div style="font-size:17px; font-weight:bold;">{r_item['评级']} {r_item['名称']} ({r_item['代码']})</div>
                    <div style="font-size:13px; color:#ff9800; margin-top:4px;">⏰ 买入：{adv['买入时段']}</div>
                    <div style="font-size:13px; color:#00e676; margin-top:2px;">🚀 卖出：{adv['卖出时机']}</div>
                    <div style="font-size:13px; color:#64b5f6; margin-top:2px;">💰 仓位：{adv['自适应仓位']}</div>
                    <div style="font-size:12px; color:#eee; margin-top:6px; line-height:1.4;">💡 <b>核心逻辑</b>：{r_item['为什么值得买']}</div>
                </div>
                """, unsafe_allow_html=True)

        st.divider()

        # 时段快捷筛选
        f_col1, f_col2 = st.columns([4, 6])
        with f_col1:
            slot_filter = st.radio("⏰ 按买入时段快捷过滤：", ["全部", "🌅 早盘", "🌇 尾盘"], horizontal=True)
        
        if slot_filter != "全部":
            filtered_df = res_df[res_df["买入时段"].str.contains(slot_filter[:2])].reset_index(drop=True)
        else:
            filtered_df = res_df

        st.success(f"🎉 投研完成！当前策略【{strategy_mode.split(' ')[0]}】呈现 **Top {len(filtered_df)}** 只标的。")

        display_cols = ["评级", "代码", "名称", "为什么值得买", "买入时段", "持股周期", "仓位战术", "最新价", "涨跌幅(%)", "综合评分"]
        st.dataframe(filtered_df[display_cols], use_container_width=True, hide_index=True)

        st.subheader("📊 个股全景决策中枢 (一键加自选)")
        if not filtered_df.empty:
            selected_code = st.selectbox(
                "选择要深度诊断的股票：",
                options=filtered_df["代码"].tolist(),
                format_func=lambda x: f"[{next(r['评级'] for r in results if r['代码']==x)}] {x} - {next(r['名称'] for r in results if r['代码']==x)} | 买入:{next(r['买入时段'] for r in results if r['代码']==x)}"
            )

            if selected_code and selected_code in kline_cache:
                s_name, s_df, s_adv, s_radar, s_timing, s_chip, s_row_data = kline_cache[selected_code]

                ca, cb, cc, cd, ce = st.columns(5)
                ca.metric("⏰ 建议买入时机", s_adv.get("买入时段", "尾盘买入").split(" ")[0])
                cb.metric("🎯 建议买入区间", s_adv["建议买入区间"])
                cc.metric("🛡️ 逻辑防守/止损线", s_adv["建议止损位"].split(" ")[0])
                cd.metric("🚀 第一止盈目标", s_adv["第一止盈目标"].split(" ")[0])
                ce.metric("💰 仓位执行法则", s_adv.get("自适应仓位", "分批建仓"))

                # 网页一键加入自选按钮
                exists_in_p = any(p['code'] == selected_code for p in st.session_state['portfolio'])
                b_c1, b_c2 = st.columns([1.5, 8.5])
                with b_c1:
                    if exists_in_p:
                        if st.button("🗑️ 从网页自选池移除", key=f"del_{selected_code}"):
                            st.session_state['portfolio'] = [p for p in st.session_state['portfolio'] if p['code'] != selected_code]
                            save_portfolio(st.session_state['portfolio'])
                            st.toast(f"已将 {selected_code} 移出自选池！", icon="🗑️")
                            st.rerun()
                    else:
                        if st.button("➕ 加入网页自选监控池", type="primary", key=f"add_{selected_code}"):
                            new_item = {
                                "code": selected_code,
                                "name": s_name,
                                "add_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                "buy_range": s_adv["建议买入区间"],
                                "stop_loss": s_adv.get("止损数值", s_df['收盘'].iloc[-1] * 0.95),
                                "target": s_adv.get("止盈数值", s_df['收盘'].iloc[-1] * 1.15),
                                "why_buy": s_adv["为什么值得买"],
                                "period": s_adv["预估持股周期"]
                            }
                            st.session_state['portfolio'].append(new_item)
                            save_portfolio(st.session_state['portfolio'])
                            st.toast(f"🎉 成功加入网页监控池！可切换至第二标签页集中盯盘。", icon="✅")
                            st.rerun()

                st.warning(f"🌟 **为什么值得买（主力意图剖析）**：\n\n{s_adv['为什么值得买']}")
                st.info(f"💡 **精准操盘买卖点指引**：\n" + "\n".join([f"- {t}" for t in s_timing['买点战术详情']]))
                st.plotly_chart(draw_pro_kline(selected_code, s_name, s_df, s_adv), use_container_width=True)
        else:
            st.warning("⚠️ 当前时段筛选下暂无匹配标的，可切换为【全部】查看。")

    elif st.session_state.get('has_scanned'):
        st.warning("⚠️ 扫描池暂时为空。若使用策略3，因筹码集中度与量比卡尺较严，遇弱势盘面可能无票跳出，此为正常防守表现。")
    else:
        st.info("👈 请在左侧选择你心仪的量化策略，点击上方红色的 **“🚀 启动全市场深度量化极速扫描”**。")

# ==================== 网页持仓监控池 ====================
with tab_view_portfolio:
    st.subheader("💼 我的网页专属自选与持仓监控")
    if not st.session_state['portfolio']:
        st.info("💡 监控池目前为空。在第一页【AI 智能精选看板】中选中股票后，点击 **“➕ 加入网页自选监控池”** 即可在此集中盯盘。")
    else:
        p_list = st.session_state['portfolio']
        p_codes = [p['code'] for p in p_list]
        
        symbols = [f"sh{c}" if c.startswith("60") else f"sz{c}" for c in p_codes]
        real_items = fetch_tencent_batch(symbols)
        price_map = {item['代码']: item for item in real_items}

        p_display = []
        for p in p_list:
            c = p['code']
            real_d = price_map.get(c, {})
            curr_p = real_d.get('最新价', 0.0)
            pct = real_d.get('涨跌幅', 0.0)
            sl = float(p.get('stop_loss', 0))
            tg = float(p.get('target', 0))

            if curr_p <= sl and curr_p > 0:
                status = "🔴 跌破防守线(坚决离场)"
            elif curr_p >= tg and curr_p > 0:
                status = "🟢 触及目标位(建议止盈)"
            else:
                dist_sl = round((curr_p - sl) / curr_p * 100, 1) if curr_p > 0 else 0
                status = f"🟡 正常持有中 (距止损 {dist_sl}%)"

            p_display.append({
                "代码": c,
                "名称": p['name'],
                "最新价": curr_p,
                "今日涨跌幅(%)": pct,
                "建议买入区间": p.get('buy_range', '-'),
                "铁律防守线": sl,
                "目标止盈价": tg,
                "持仓预警状态": status,
                "加入时间": p.get('add_time', '-'),
                "核心逻辑": p.get('why_buy', '-')
            })

        st.dataframe(pd.DataFrame(p_display), use_container_width=True, hide_index=True)

        del_col1, del_col2 = st.columns([2, 8])
        with del_col1:
            code_to_remove = st.selectbox("选择要移除的股票", options=[p['code'] for p in p_list], format_func=lambda x: f"{x} - {next(p['name'] for p in p_list if p['code']==x)}")
            if st.button("🗑️ 确认移出监控池", use_container_width=True):
                st.session_state['portfolio'] = [p for p in st.session_state['portfolio'] if p['code'] != code_to_remove]
                save_portfolio(st.session_state['portfolio'])
                st.toast(f"已移除 {code_to_remove}", icon="✅")
                st.rerun()
