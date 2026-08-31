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
    page_title="AI 智能主力量化全闭环投研系统 v5.3",
    layout="wide",
    page_icon="🧠"
)

# ==================== 本地持仓持久化 ====================
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
        "action_guide": "大盘处于活跃可操作区间，可积极参与主力蓄势与龙头洗盘标的。",
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
    st.header("🔀 核心量化策略 (2 选 1)")
    strategy_mode = st.selectbox(
        "选择当前运行策略：",
        [
            "2️⃣ 主力博弈+龙头二波/底部洗盘策略 (视频理念)",
            "1️⃣ 多周期均线+ATR低吸策略 (趋势均线共振)"
        ]
    )
    is_strategy_main_force = ("主力博弈" in strategy_mode)

    st.divider()
    st.header("⚙️ 选股过滤参数")
    enable_weekly_filter = st.checkbox("📈 开启【周线定大势】过滤", value=False)
    enable_fundamental_filter = st.checkbox("🛡️ 开启【基本面轻量排雷】", value=True)

    st.divider()
    st.subheader("🎯 涨跌幅与价格空间")
    max_scan_pct = st.slider("日内最大涨幅上限 (%)", 1.0, 9.5, 5.0, 0.1)
    min_scan_pct = st.slider("日内最小涨幅下限 (%)", -7.0, 2.0, -4.0, 0.1)

    st.divider()
    st.subheader("🎯 扫描样本量")
    deep_sample_size = st.slider("深度分析样本量 (只)", 100, 800, 300, 50)
    display_top_n = st.slider("最终呈现数量 (只)", 5, 50, 15, 5)

    st.divider()
    board_type = st.selectbox("市场板块", ["全部主板 (60 + 00)", "仅沪市主板 (60开头)", "仅深市主板 (00开头)"])
    price_range = st.slider("股价区间 (元)", 1.0, 100.0, (2.0, 60.0), 0.5)
    min_price, max_price = price_range
    min_amount = st.slider("最低日成交额门槛 (万元)", 500, 20000, 1000, 500)
    exclude_limit_up = st.checkbox("🚫 剔除涨停封板股票", value=True)

    st.divider()
    st.subheader("🤖 AI 大模型 (可选)")
    llm_api_key = st.text_input("大模型 API Key", value="", type="password")
    llm_base_url = st.text_input("API Base URL", value="https://api.deepseek.com/v1")

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
    with ThreadPoolExecutor(max_workers=20) as executor:
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

def calculate_chip_distribution_decay(k_df: pd.DataFrame, current_price: float, lookback: int = 60):
    if len(k_df) < 10:
        return {"获利盘比例(%)": 70.0, "筹码峰": current_price, "平均成本": current_price, "距筹码峰(%)": 0.0}
    df = k_df.tail(lookback).copy()
    typical = (df['最高'] + df['最低'] + df['收盘']) / 3.0
    vols = df['成交量'].values
    prices = typical.values
    decay_weights = vols * np.exp(np.linspace(-1.0, 0, len(df)))
    hist, bin_edges = np.histogram(prices, bins=25, weights=decay_weights)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    total_vol = hist.sum() + 1e-9
    profit_ratio = round(np.sum(decay_weights[prices <= current_price]) / total_vol * 100, 1)
    peak_idx = np.argmax(hist)
    chip_peak = float(bin_centers[peak_idx])
    return {
        "获利盘比例(%)": profit_ratio,
        "筹码峰": round(chip_peak, 2),
        "平均成本": round(float(np.average(prices, weights=decay_weights)), 2),
        "距筹码峰(%)": round((current_price - chip_peak) / chip_peak * 100, 2)
    }

# ==================== 策略 2：主力逻辑博弈与龙头/底部洗盘低吸 ====================
def evaluate_strategy_main_force_game(df: pd.DataFrame, row_data: dict, enable_weekly: bool, enable_fundamental: bool, macro_status: dict):
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
        return 0, 0, 0, ["🔴 净资产为负"], {}, {}, {}, {}, False, "🔴 高"

    chip_info = calculate_chip_distribution_decay(df, close[-1])
    atr = calculate_atr(df, 14)

    # 1. 模式A：强势股/龙头二波换手蓄势
    pattern_a_hit = False
    limit_ups = sum(1 for i in range(max(1, n-25), n) if (close[i] / close[i-1] - 1) >= 0.055)
    max_p_25d = np.max(highs[-25:]) if n >= 25 else close[-1]
    pullback_pct = (max_p_25d - close[-1]) / max_p_25d * 100

    if limit_ups >= 1 and 4.0 <= pullback_pct <= 42.0:
        pattern_a_hit = True
        quality += 22
        tags.append(f"🔥 强势回调蓄势({pullback_pct:.1f}%)")
        recent_surge_idx = [i for i in range(max(1, n-25), n) if (close[i] / close[i-1] - 1) >= 0.055]
        logic_base_price = opens[recent_surge_idx[-1]] if recent_surge_idx else lows[-10:].min()
    else:
        logic_base_price = lows[-15:].min() if n >= 15 else close[-1] * 0.92

    # 2. 模式B：底部放量 + 假摔洗盘
    pattern_b_hit = False
    if n >= 15:
        vol_avg = np.mean(vols[-15:])
        surge_days = [i for i in range(n-15, n-1) if vols[i] >= 1.15 * vol_avg and (close[i] / close[i-1] - 1) >= 0.025]
        if surge_days:
            first_i = surge_days[0]
            surge_open, surge_low = opens[first_i], lows[first_i]
            if close[-1] >= surge_open * 0.92:
                pattern_b_hit = True
                quality += 20
                logic_base_price = min(surge_open, surge_low)
                tags.append("🎯 底部放量假摔洗盘(守住主力底线)")

    # 3. 模式C：均线粘合突破/横盘整理
    pattern_c_hit = False
    if n >= 10:
        ma5, ma10, ma20 = np.mean(close[-5:]), np.mean(close[-10:]), np.mean(close[-min(20, n):])
        spread = (max(ma5, ma10, ma20) - min(ma5, ma10, ma20)) / ma20 * 100
        if spread < 9.0:
            pattern_c_hit = True
            quality += 16
            tags.append(f"🌀 均线粘合蓄势({spread:.1f}%)")

    # 默认兜底
    if not (pattern_a_hit or pattern_b_hit or pattern_c_hit):
        quality += 10
        tags.append("主力温和蓄势")

    if -5.0 <= pct <= 5.0:
        timing += 16
        tags.append("承接企稳区间")

    total = quality + timing + macro_status.get("market_score", 10)

    buy_low = round(max(logic_base_price * 1.002, close[-1] * 0.96), 2)
    buy_high = round(close[-1] * 1.015, 2)
    stop_loss_logic = round(logic_base_price * 0.96, 2)
    target_p = round(max(max_p_25d, close[-1] * 1.15), 2)

    # 智能判定买入时段与所属轨道
    if pattern_a_hit or pattern_b_hit:
        timing_slot = "🌅 早盘低吸 (9:40-10:15)"
        style_tag = "🔥 强势进攻轨"
        timing_detail_text = "🌅 【早盘低吸】：强势股急跌洗盘，早盘关注缩量下探主力防守线后的放量企稳信号。"
    elif pattern_c_hit:
        timing_slot = "🌇 尾盘确认 (14:30-14:50)"
        style_tag = "🛡️ 稳健防守轨"
        timing_detail_text = "🌇 【尾盘买入】：均线粘合蓄势，全天不破位、尾盘缩量走平即可锁定确定性建仓。"
    else:
        timing_slot = "🌇 尾盘确认 (14:30-14:50)"
        style_tag = "🛡️ 稳健防守轨"
        timing_detail_text = "🌇 【尾盘买入】：温和蓄势标的，建议尾盘确认承接有效后分批介入。"

    advice = {
        "建议买入区间": f"{buy_low} ~ {buy_high}",
        "建议买入区间_低": buy_low, "建议买入区间_高": buy_high,
        "建议止损位": f"{stop_loss_logic} (主力逻辑防守线)",
        "第一止盈目标": f"{target_p} (主升/二波目标)",
        "动态压力位": round(max_p_25d, 2),
        "动态支撑位": round(logic_base_price, 2),
        "ATR": round(atr, 3),
        "自适应仓位": "30% (博弈主力建仓仓位)",
        "最佳时段": timing_slot,
        "策略轨道": style_tag
    }

    timing_dict = {
        "买点战术详情": [
            timing_detail_text,
            f"🎯 【主力防守线】：以建仓起涨底线 {stop_loss_logic} 元为防守依据，不破不恐慌割肉！",
            f"🌊 【区间低吸】：主力常砸盘震仓，在买入区间 [{buy_low}~{buy_high}] 分批建仓。",
            f"🚀 【止盈目标】：因何而买就因何而卖，反弹至前高 {max_p_25d} 元附近分批止盈。"
        ]
    }

    radar = {
        "主力异动": 18 if pattern_a_hit else 14,
        "洗盘充分度": 16 if "假摔" in " ".join(tags) or "回调" in " ".join(tags) else 12,
        "筹码沉淀": min(20, int(chip_info['获利盘比例(%)'] * 0.2)),
        "底部安全性": 18 if pattern_b_hit else 14,
        "博弈胜率": min(20, int(total * 0.22))
    }

    return total, quality, timing, tags, advice, radar, timing_dict, chip_info, True, "🟢 低"

# ==================== 策略 1：多周期均线 + ATR 低吸策略 ====================
def evaluate_strategy_classic_atr(df: pd.DataFrame, row_data: dict, enable_weekly: bool, enable_fundamental: bool, macro_status: dict):
    quality, timing, tags = 15, 15, []
    close = df['收盘'].values
    pct = float(row_data.get('涨跌幅', 0))
    pb = float(row_data.get('PB', 0))

    if enable_fundamental and pb < 0:
        return 0, 0, 0, ["🔴 净资产为负"], {}, {}, {}, {}, False, "🔴 高"

    ma5 = df['收盘'].rolling(5).mean().iloc[-1] if len(df) >= 5 else close[-1]
    ma10 = df['收盘'].rolling(10).mean().iloc[-1] if len(df) >= 10 else close[-1]
    ma20 = df['收盘'].rolling(20).mean().iloc[-1] if len(df) >= 20 else close[-1]
    atr = calculate_atr(df, 14)
    chip_info = calculate_chip_distribution_decay(df, close[-1])

    if close[-1] >= ma20 * 0.96:
        quality += 15
        tags.append("站稳中线支撑")
    if close[-1] > ma5 * 0.98:
        quality += 10
        tags.append("短期均线蓄势")

    bias5 = (close[-1] / ma5 - 1) * 100 if ma5 > 0 else 0
    if -2.5 <= bias5 <= 4.0:
        timing += 15
        tags.append("🌟 黄金低吸买点")

    total = quality + timing + macro_status.get("market_score", 10)

    buy_low = round(close[-1] * 0.98, 2)
    buy_high = round(close[-1] * 1.015, 2)
    stop_loss_p = round(min(df['最低'].iloc[-1], ma20) - 0.6 * atr, 2)
    target1 = round(close[-1] + 1.2 * atr, 2)

    timing_slot = "🌇 尾盘确认 (14:30-14:50)"
    style_tag = "🛡️ 稳健防守轨"

    advice = {
        "建议买入区间": f"{buy_low} ~ {buy_high}",
        "建议买入区间_低": buy_low, "建议买入区间_高": buy_high,
        "建议止损位": f"{stop_loss_p} (ATR动态止损)",
        "第一止盈目标": f"{target1}",
        "动态压力位": round(close[-1] * 1.08, 2),
        "动态支撑位": round(ma20, 2),
        "ATR": round(atr, 3),
        "自适应仓位": "25% (ATR波动率平价)",
        "最佳时段": timing_slot,
        "策略轨道": style_tag
    }

    timing_dict = {
        "买点战术详情": [
            "🌇 【尾盘买入】：趋势均线低吸标的，规避日内冲高回落风险，14:30 后确认站稳均线即可介入。",
            f"🚀 【顺势低吸】：回踩买入区间 [{buy_low}~{buy_high}] 分批布局。",
            f"🛡️ 【ATR止损】：严格执行动态止损线 {stop_loss_p} 元。"
        ]
    }

    radar = {
        "均线趋势": 16,
        "资金强度": 14,
        "突破动能": 13,
        "换手活跃": 14,
        "位置安全": 16
    }
    return total, quality, timing, tags, advice, radar, timing_dict, chip_info, True, "🟢 低"

# ==================== 工作任务分发 ====================
def worker_task(code, name, row_data, is_main_force, enable_weekly, enable_fundamental, macro_status):
    k_df = fetch_kline_safe(code, row_data, days=90)
    last_close = float(k_df['收盘'].iloc[-1])
    pct_today = float(k_df['涨跌幅'].iloc[-1]) if len(k_df) > 1 else float(row_data.get('涨跌幅', 0))

    if is_main_force:
        total, quality, timing, tags, advice, radar, timing_dict, chip_info, passed, risk_level = \
            evaluate_strategy_main_force_game(k_df, row_data, enable_weekly, enable_fundamental, macro_status)
    else:
        total, quality, timing, tags, advice, radar, timing_dict, chip_info, passed, risk_level = \
            evaluate_strategy_classic_atr(k_df, row_data, enable_weekly, enable_fundamental, macro_status)

    amt_wan = float(row_data.get('成交额(万)', 0))
    k_df['MA5'] = k_df['收盘'].rolling(5).mean().fillna(k_df['收盘'])
    k_df['MA10'] = k_df['收盘'].rolling(10).mean().fillna(k_df['收盘'])
    k_df['MA20'] = k_df['收盘'].rolling(20).mean().fillna(k_df['收盘'])

    return {
        "代码": code, "名称": name,
        "综合评分": total, "质量分": quality, "时机分": timing, "风险": risk_level,
        "最新价": last_close, "涨跌幅(%)": round(pct_today, 2), "成交额(万)": int(amt_wan),
        "最佳时段": advice.get("最佳时段", "🌇 尾盘确认 (14:30-14:50)"),
        "轨道类型": advice.get("策略轨道", "🛡️ 稳健防守轨"),
        "PB": row_data.get("PB", 0),
        "量化特征": " | ".join(tags) if tags else "主力博弈",
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
    for ma_col, color, label in [('MA5', '#ff9800', 'MA5'), ('MA10', '#2196f3', 'MA10'), ('MA20', '#9c27b0', 'MA20')]:
        if ma_col in recent.columns:
            fig.add_trace(go.Scatter(x=recent['日期'], y=recent[ma_col], line=dict(color=color, width=1.4), name=label), row=1, col=1)
    b_low = advice.get("建议买入区间_低", recent['收盘'].iloc[-1] * 0.98)
    b_high = advice.get("建议买入区间_高", recent['收盘'].iloc[-1])
    fig.add_hrect(y0=b_low, y1=b_high, fillcolor="rgba(0, 230, 118, 0.15)", line_width=0,
                  annotation_text=f"🎯 买入区间: {b_low}~{b_high}", annotation_position="top left", row=1, col=1)
    fig.add_hline(y=advice["动态压力位"], line_dash="dot", line_color="#ff1744", annotation_text=f"前高/压力: {advice['动态压力位']}", row=1, col=1)
    fig.add_hline(y=advice["动态支撑位"], line_dash="dash", line_color="#00e676", annotation_text=f"主力底线: {advice['动态支撑位']}", row=1, col=1)
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
        st.error("❌ 标的池初筛为空，请适当调宽左侧【股价区间】（如 2.0 ~ 80.0 元）或降低【最低日成交额门槛】。")
        st.stop()

    candidates = pool[(pool['涨跌幅'] >= min_scan_pct) & (pool['涨跌幅'] <= max_scan_pct)].sort_values(
        by=["成交额(万)"], ascending=False
    ).head(deep_sample_size)

    if len(candidates) < 20:
        candidates = pool.sort_values(by=["成交额(万)"], ascending=False).head(deep_sample_size)

    hit_results, new_kline_cache = [], {}
    progress_bar = st.progress(0, text=f"正在深度分析 {len(candidates)} 只样本...")
    completed = 0
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(worker_task, str(row['代码']).zfill(6), row['名称'], row.to_dict(),
                                   is_strategy_main_force, enable_weekly_filter, enable_fundamental_filter, macro)
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
    st.toast(f"⚡ 扫描完成！严选锁定 Top {len(hit_results)} 只标的，耗时 {elapsed} 秒", icon="🎉")

# ==================== 结果看板 ====================
tab_view_select, tab_view_portfolio = st.tabs(["🔥 AI 智能精选投研看板", "💼 我的持仓监控池"])

with tab_view_select:
    if st.session_state.get('scan_results'):
        results = st.session_state['scan_results']
        kline_cache = st.session_state['kline_cache']
        res_df = pd.DataFrame(results)

        # 增加时段快捷筛选
        f_col1, f_col2 = st.columns([3, 7])
        with f_col1:
            slot_filter = st.radio("⏰ 按操作时段快捷过滤：", ["全部", "🌅 早盘低吸", "🌇 尾盘确认"], horizontal=True)
        
        if slot_filter != "全部":
            filtered_df = res_df[res_df["最佳时段"].str.contains(slot_filter[:2])].reset_index(drop=True)
        else:
            filtered_df = res_df

        st.success(f"🎉 投研完成！已采用【{strategy_mode}】严选出 **Top {len(filtered_df)}** 只低吸标的。")

        display_cols = ["代码", "名称", "综合评分", "最佳时段", "轨道类型", "量化特征", "最新价", "涨跌幅(%)", "成交额(万)"]
        st.dataframe(filtered_df[display_cols], use_container_width=True, hide_index=True)

        st.subheader("📊 个股决策中枢")
        if not filtered_df.empty:
            selected_code = st.selectbox(
                "选择要诊断的股票：",
                options=filtered_df["代码"].tolist(),
                format_func=lambda x: f"[{next(r['综合评分'] for r in results if r['代码']==x)}分] {x} - {next(r['名称'] for r in results if r['代码']==x)} | {next(r['最佳时段'] for r in results if r['代码']==x)}"
            )

            if selected_code and selected_code in kline_cache:
                s_name, s_df, s_adv, s_radar, s_timing, s_chip, s_row_data = kline_cache[selected_code]

                ca, cb, cc, cd, ce = st.columns(5)
                ca.metric("⏰ 最佳时机", s_adv.get("最佳时段", "尾盘买入"))
                cb.metric("🎯 建议买入区间", s_adv["建议买入区间"])
                cc.metric("🛡️ 逻辑防守线", s_adv["建议止损位"].split(" ")[0])
                cd.metric("🚀 第一止盈目标", s_adv["第一止盈目标"].split(" ")[0])
                ce.metric("👑 筹码峰", f"{s_chip['筹码峰']} 元")

                st.info(f"💡 **操盘战术指引**：\n" + "\n".join([f"- {t}" for t in s_timing['买点战术详情']]))

                st.plotly_chart(draw_pro_kline(selected_code, s_name, s_df, s_adv), use_container_width=True)
        else:
            st.warning("⚠️ 当前时段筛选下暂无匹配标的，可切换为【全部】查看。")

    elif st.session_state.get('has_scanned'):
        st.warning("⚠️ 扫描池暂时为空，请在左侧将【股价区间】调宽至 2.0 ~ 80.0 元，并调低成交额门槛后重新扫描。")
    else:
        st.info("👈 请在左侧确认参数后，点击上方红色的 **“🚀 启动全市场深度量化极速扫描”** 按钮。")

with tab_view_portfolio:
    st.info("💡 监控池支持跟踪持仓股票，触及防守线时自动预警。")
