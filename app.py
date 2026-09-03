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
    page_title="AI 智能主力量化投研系统 v6.4 (大盘全景版)",
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

# ==================== 东财全市场行业映射拉取 ====================
@st.cache_data(ttl=86400)
def fetch_reliable_industry_mapping():
    mapping = {}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/"
    }
    for p in range(1, 12):
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": str(p), "pz": "500", "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2", "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f100",
            "_": str(int(time.time() * 1000))
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=3.0).json()
            diff = resp.get("data", {}).get("diff", [])
            if not diff:
                break
            for item in diff:
                code = str(item.get("f12", "")).zfill(6)
                ind = item.get("f100", "")
                if ind and ind != "-" and ind != "None":
                    mapping[code] = ind
        except Exception:
            break

    fallback_rules = {
        "600519": "白酒", "000858": "白酒", "600036": "银行", "601318": "保险",
        "002475": "消费电子", "600226": "通信设备", "002741": "光伏设备", "002584": "化学制品",
        "603993": "有色金属", "601899": "有色金属", "002460": "能源金属", "002466": "能源金属",
        "688981": "半导体", "603501": "半导体", "002371": "半导体", "300750": "电池",
        "600111": "稀土永磁", "600089": "特高压", "601127": "汽车整车", "002594": "汽车整车"
    }
    for c, ind in fallback_rules.items():
        if c not in mapping:
            mapping[c] = ind
    return mapping

industry_map = fetch_reliable_industry_mapping()
all_sectors_list = sorted(list(set([v for v in industry_map.values() if v and v != "未知"])))

# ==================== 腾讯实时主力资金流向（分批安全拉取） ====================
def fetch_money_flow_safe_batched(codes):
    flow_map = {}
    if not codes:
        return flow_map

    chunk_size = 60
    code_chunks = [codes[i:i + chunk_size] for i in range(0, len(codes), chunk_size)]

    for chunk in code_chunks:
        symbols = [f"ff_{'sh' if str(c).startswith('60') else 'sz'}{str(c).zfill(6)}" for c in chunk]
        url = f"https://qt.gtimg.cn/q={','.join(symbols)}"
        try:
            resp = requests.get(url, timeout=2.5)
            for line in resp.text.strip().split(";"):
                if not line or "=" not in line:
                    continue
                parts = line.split("=")
                code_key = parts[0].split("ff_")[-1][2:]
                data_fields = parts[1].strip('"').split("~")
                if len(data_fields) >= 5:
                    net_main_wan = float(data_fields[3] or 0)
                    main_ratio = float(data_fields[4] or 0)
                    flow_map[code_key] = {
                        "主力净流入": round(net_main_wan, 1),
                        "主力净占比": round(main_ratio, 1)
                    }
        except Exception:
            continue

    return flow_map

# ==================== 侧边栏配置 ====================
with st.sidebar:
    st.header("🔀 核心量化策略 (4 选 1)")
    strategy_mode = st.selectbox(
        "选择当前运行策略：",
        [
            "4️⃣ 推文精髓：四重共振主线战法 (板块+龙头+资金+个股确定性加仓)",
            "3️⃣ 图片绝技：三步极选强势股闭环策略 (量异动+均线多头+高控盘筹码)",
            "2️⃣ 主力博弈+龙头二波/底部洗盘策略 (视频理念)",
            "1️⃣ 多周期均线+ATR低吸策略 (趋势均线共振)"
        ]
    )

    st.divider()
    st.header("🏷️ 行业板块指定扫描")
    selected_sectors = st.multiselect(
        "🎯 锁定特定板块 (留空则全市场扫描)", 
        options=all_sectors_list, 
        placeholder="如：半导体, 有色金属, 证券, 消费电子...",
        help="选定后只分析该板块，极大提速并锁定板块风口！"
    )

    st.divider()
    st.header("⚡ 实时动态跳动配置")
    enable_auto_live = st.toggle("🔄 开启盘中秒级自动盯盘刷新", value=True)
    live_interval = st.slider("动态刷新频率 (秒)", 3, 30, 5, 1) if enable_auto_live else 60

    st.divider()
    st.header("🎯 选股与呈现参数")
    display_top_n = st.slider("最终呈现上限 (只)", 3, 60, 20, 1)
    deep_sample_size = st.slider("深度分析样本量 (只)", 100, 1500, 500, 50)

    if "3️⃣" in strategy_mode:
        st.info("💡 已自动锁定【三步极选法】黄金区间：\n• 涨幅：3.0% ~ 5.0%\n• 换手率：3.0% ~ 10.0%\n• 量比 > 1.8 倍")
        min_scan_pct, max_scan_pct = 2.8, 5.2
    else:
        max_scan_pct = st.slider("日内最大涨幅上限 (%)", 1.0, 9.5, 5.5, 0.1)
        min_scan_pct = st.slider("日内最小涨幅下限 (%)", -7.0, 2.0, -4.0, 0.1)

    enable_ultra_filter = st.checkbox("💎 开启【盈亏比与趋势】过滤", value=True)
    min_risk_reward = st.slider("最低盈亏比门槛", 1.2, 3.5, 1.8, 0.1) if (enable_ultra_filter and "3️⃣" not in strategy_mode) else 1.0
    enable_weekly_filter = st.checkbox("📈 开启【中长线多头趋势】过滤", value=True)
    enable_fundamental_filter = st.checkbox("🛡️ 开启【基本面轻量排雷】", value=True)

    st.divider()
    st.subheader("🎯 基础标的池过滤")
    board_type = st.selectbox("市场板块", ["全部主板 (60 + 00)", "仅沪市主板 (60开头)", "仅深市主板 (00开头)"])
    price_range = st.slider("股价区间 (元)", 1.0, 100.0, (2.0, 80.0), 0.5)
    min_price, max_price = price_range
    min_amount = st.slider("最低日成交额门槛 (万元)", 500, 30000, 1500, 500)
    exclude_limit_up = st.checkbox("🚫 剔除涨停封板股票", value=True)

# ==================== 深度大盘全景变动数据抓取 ====================
def fetch_realtime_macro_deep():
    """
    抓取指数行情以及东财/腾讯全市场涨跌统计，形成全景大盘变动
    """
    url_tx = "https://qt.gtimg.cn/q=s_sh000001,s_sz399001,s_sz399006"
    macro_info = {
        "sh_pct": 0.0, "sh_price": 3100.0, "sh_amt_yi": 0.0,
        "sz_pct": 0.0, "sz_price": 10000.0, "sz_amt_yi": 0.0,
        "cy_pct": 0.0, "cy_price": 2000.0,
        "total_amt_yi": 0.0,
        "up_count": 0, "down_count": 0, "flat_count": 0,
        "limit_up": 0, "limit_down": 0,
        "status_color": "🟢", "status_text": "安全进攻区",
        "suggest_position": "60% ~ 80%",
        "action_guide": "大盘处于活跃可操作区间，可积极参与主力蓄势与强势突破标的。",
        "market_score": 12,
        "update_time": datetime.now().strftime("%H:%M:%S")
    }

    # 1. 抓取指数点位与成交额
    try:
        resp = requests.get(url_tx, timeout=2.0)
        lines = resp.text.strip().split(";")
        for line in lines:
            if "s_sh000001" in line and "=" in line:
                parts = line.split("=")[1].strip('"').split("~")
                if len(parts) >= 6:
                    p = float(parts[2] or 0)
                    if p > 100:
                        macro_info["sh_price"] = p
                    macro_info["sh_pct"] = float(parts[5] or 0)
                    macro_info["sh_amt_yi"] = round(float(parts[7] or 0) / 10000, 1) if len(parts) > 7 else 0.0
            elif "s_sz399001" in line and "=" in line:
                parts = line.split("=")[1].strip('"').split("~")
                if len(parts) >= 6:
                    macro_info["sz_price"] = float(parts[2] or 0)
                    macro_info["sz_pct"] = float(parts[5] or 0)
                    macro_info["sz_amt_yi"] = round(float(parts[7] or 0) / 10000, 1) if len(parts) > 7 else 0.0
            elif "s_sz399006" in line and "=" in line:
                parts = line.split("=")[1].strip('"').split("~")
                if len(parts) >= 6:
                    macro_info["cy_price"] = float(parts[2] or 0)
                    macro_info["cy_pct"] = float(parts[5] or 0)
        macro_info["total_amt_yi"] = round(macro_info["sh_amt_yi"] + macro_info["sz_amt_yi"], 1)
    except Exception:
        pass

    # 2. 抓取全市场涨跌家数与涨跌停统计 (东财大盘情绪接口)
    try:
        url_em = "https://push2.eastmoney.com/api/qt/ulist.np/get"
        params = {
            "fltt": "2", "invt": "2",
            "fields": "f3,f104,f105,f106",  # f104: 涨, f105: 跌, f106: 平
            "secids": "1.000001,0.399001"
        }
        r_em = requests.get(url_em, params=params, timeout=2.0).json()
        diff = r_em.get("data", {}).get("diff", [])
        if diff:
            up = sum(int(x.get("f104", 0) or 0) for x in diff)
            down = sum(int(x.get("f105", 0) or 0) for x in diff)
            flat = sum(int(x.get("f106", 0) or 0) for x in diff)
            if up > 0 or down > 0:
                macro_info["up_count"] = up
                macro_info["down_count"] = down
                macro_info["flat_count"] = flat
    except Exception:
        # 兜底默认值
        macro_info["up_count"] = 2800
        macro_info["down_count"] = 2100
        macro_info["flat_count"] = 150

    # 3. 综合评估全景大盘状态
    sh_pct = macro_info["sh_pct"]
    up_cnt = macro_info["up_count"]
    down_cnt = macro_info["down_count"]

    if sh_pct >= 0.3 and up_cnt > down_cnt:
        macro_info.update({
            "status_color": "🟢", "status_text": "多头进攻周期 (放量普涨)",
            "suggest_position": "70% ~ 90%",
            "action_guide": "大盘与市场情绪高度共振，赚钱效应极佳，顺势重仓做主线，利润依托5日线奔跑。",
            "market_score": 15
        })
    elif up_cnt > down_cnt * 1.2:
        macro_info.update({
            "status_color": "🟢", "status_text": "结构性赚钱周期",
            "suggest_position": "60% ~ 75%",
            "action_guide": "指数震荡但个股普涨，题材板块活跃，可积极做多四重共振标的。",
            "market_score": 13
        })
    elif -0.8 <= sh_pct < 0.3:
        macro_info.update({
            "status_color": "🟡", "status_text": "震荡分歧周期 (存量博弈)",
            "suggest_position": "40% ~ 55%",
            "action_guide": "大盘分歧轮动快，严控追高，仅在主力底线与支撑位附近分批低吸，有浮盈及时落袋。",
            "market_score": 10
        })
    else:
        macro_info.update({
            "status_color": "🔴", "status_text": "弱势防守区 (泥沙俱下)",
            "suggest_position": "10% ~ 30%",
            "action_guide": "大盘破位或个股大面积普跌，主力资金大幅退潮。赚到钱立刻清仓离场，轻仓或空仓防守！",
            "market_score": 7
        })

    return macro_info

# ==================== 顶部大盘全景动态变动看板 ====================
@st.fragment(run_every=live_interval if enable_auto_live else None)
def render_live_macro_header():
    macro = fetch_realtime_macro_deep()

    # 第一行：三大指数 + 两市总成交额
    c1, c2, c3, c4 = st.columns([1.1, 1.1, 1.1, 1.5])
    c1.metric("🏛️ 上证指数", f"{macro['sh_price']} 点", f"{macro['sh_pct']:+.2f}%")
    c2.metric("🏛️ 深证成指", f"{macro['sz_price']} 点", f"{macro['sz_pct']:+.2f}%")
    c3.metric("🏛️ 创业板指", f"{macro['cy_price']} 点", f"{macro['cy_pct']:+.2f}%")
    c4.metric("💰 两市总成交额", f"{macro['total_amt_yi']} 亿元", f"沪:{macro['sh_amt_yi']}亿 | 深:{macro['sz_amt_yi']}亿")

    # 第二行：全市场赚钱效应仪表盘 + 风控动作指引
    info_col1, info_col2 = st.columns([2.2, 2.8])
    with info_col1:
        total_stocks = max(1, macro['up_count'] + macro['down_count'] + macro['flat_count'])
        up_pct = round(macro['up_count'] / total_stocks * 100, 1)
        down_pct = round(macro['down_count'] / total_stocks * 100, 1)
        st.markdown(f"""
        <div style="background-color:rgba(255,255,255,0.04); padding:10px 14px; border-radius:8px; border-left:4px solid #ff9800;">
            <div style="font-size:14px; font-weight:bold; color:#ddd;">📊 全市场即时赚钱效应 ({macro['update_time']})：</div>
            <div style="margin-top:6px; font-size:15px;">
                <span style="color:#ef5350; font-weight:bold;">🔺 上涨: {macro['up_count']} 家 ({up_pct}%)</span> &nbsp;|&nbsp; 
                <span style="color:#26a69a; font-weight:bold;">🔻 下跌: {macro['down_count']} 家 ({down_pct}%)</span> &nbsp;|&nbsp; 
                <span style="color:#aaa;">➖ 平盘: {macro['flat_count']} 家</span>
            </div>
            <div style="font-size:12px; color:#aaa; margin-top:4px;">
                情绪判定：<b>{'🔥 多头极强' if up_pct > 65 else ('⚡ 分歧轮动' if up_pct > 40 else '❄️ 冰点低迷')}</b> | 建议总仓位：<b style="color:#00e676;">{macro['suggest_position']}</b>
            </div>
        </div>
        """, unsafe_allow_html=True)

    with info_col2:
        live_tag = "🟢 实时盯盘" if enable_auto_live else "⚪ 静止"
        st.markdown(f"""
        <div style="background-color:rgba(255,255,255,0.04); padding:10px 14px; border-radius:8px; border-left:4px solid {macro['status_color']=='🟢' and '#00e676' or (macro['status_color']=='🟡' and '#ffd600' or '#ff1744')};">
            <div style="font-size:14px; font-weight:bold;">🧭 实时宏观风控指令 [{live_tag}]：<span style="color:#ffd600;">{macro['status_text']}</span></div>
            <div style="font-size:13px; color:#eee; margin-top:4px; line-height:1.4;">
                {macro['action_guide']}
            </div>
        </div>
        """, unsafe_allow_html=True)

render_live_macro_header()
st.divider()

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
        resp = requests.get(url, timeout=3.0)
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

@st.cache_data(ttl=90)
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

def calculate_dynamic_win_rate(net_main_wan, main_ratio, pos_desc, rr_ratio, quality_score, chip_w70, macro_score, sector_rank_score=0):
    base_score = 40.0
    base_score += min(15.0, (quality_score / 45.0) * 15.0)

    if chip_w70 < 10.0:
        base_score += 15.0
    elif chip_w70 < 14.0:
        base_score += 10.0
    elif chip_w70 < 17.0:
        base_score += 6.0

    if rr_ratio >= 3.0:
        base_score += 12.0
    elif rr_ratio >= 2.2:
        base_score += 8.0
    elif rr_ratio >= 1.8:
        base_score += 4.0

    if "黄金买点" in pos_desc:
        base_score += 12.0
    elif "刚起跑" in pos_desc:
        base_score += 9.0
    elif "防守区" in pos_desc:
        base_score += 5.0
    elif "超买" in pos_desc:
        base_score -= 15.0
    elif "破位" in pos_desc:
        base_score -= 25.0

    if macro_score >= 14:
        base_score += 6.0
    elif macro_score <= 8:
        base_score -= 6.0

    base_score += sector_rank_score

    flow_status = "🟡 资金平衡"
    flow_reason = "资金平稳，多为空头试探与均线承接"
    if net_main_wan > 800 and main_ratio > 3.0:
        base_score += 12.0
        flow_status = f"🟢 主力抢筹 (+{net_main_wan}万)"
        flow_reason = "主力大资金逆势净流入建仓，洗盘反包确定性极高！"
    elif net_main_wan > 0:
        base_score += 6.0
        flow_status = f"🟢 主力微买 (+{net_main_wan}万)"
        flow_reason = "主力资金保持温和净买入，下方支撑坚固。"
    elif net_main_wan < -1500 and main_ratio < -6.0:
        base_score -= 14.0
        flow_status = f"🔴 主力离场 ({net_main_wan}万)"
        flow_reason = "主力大单正在离场，警惕冲高回落，必须严控仓位！"
    elif net_main_wan < 0:
        base_score -= 4.0
        flow_status = f"🟡 散户博弈 ({net_main_wan}万)"
        flow_reason = "主力资金静默，等待放量突破信号。"

    final_buy_prob = int(np.clip(round(base_score), 25, 95))
    sell_risk_prob = 100 - final_buy_prob

    return final_buy_prob, sell_risk_prob, flow_status, flow_reason

def diagnose_position_and_action(current_p, b_low, b_high, stop_loss, target_p, ma5, ma10):
    bias5 = (current_p / ma5 - 1) * 100 if ma5 > 0 else 0
    dist_sl = (current_p - stop_loss) / current_p * 100

    if b_low <= current_p <= b_high * 1.008:
        pos_desc = "🟢 黄金买点区 (回踩支撑位)"
        action = "👉 尾盘分批建仓，破防守线止损"
        action_color = "#00e676"
    elif current_p > b_high * 1.008 and bias5 <= 3.5:
        pos_desc = "🟡 刚起跑临界点 (轻度突破)"
        action = "👉 盘中小幅回踩可打入底仓，切忌追高"
        action_color = "#ffd600"
    elif bias5 > 3.5:
        pos_desc = f"🟠 脱离成本超买区 (偏离MA5 {bias5:.1f}%)"
        action = "✋ 严禁追买！已有底仓等放量冲高止盈"
        action_color = "#ff9100"
    elif current_p < stop_loss:
        pos_desc = "🔴 跌破防守线 (破位区)"
        action = "🚨 坚决不买！若已持仓次日早盘冲高无条件清仓"
        action_color = "#ff1744"
    else:
        pos_desc = f"⚪ 蓄势防守区 (距止损仅 {dist_sl:.1f}%)"
        action = "👀 观察承接，不破支撑线可轻仓试探"
        action_color = "#e0e0e0"

    return pos_desc, action, action_color

# ==================== 策略 4️⃣：推文精髓·四重共振主线战法 ====================
def evaluate_strategy_quad_resonance(df: pd.DataFrame, row_data: dict, macro_status: dict, flow_info: dict, sector_stats: dict, sector_name: str):
    close = df['收盘'].values
    highs = df['最高'].values
    lows = df['最低'].values
    vols = df['成交量'].values
    n = len(df)
    pct = float(row_data.get('涨跌幅', 0))

    if n < 20:
        return None

    sec_info = sector_stats.get(sector_name, {"avg_pct": 0.0, "strong_count": 0, "rank": 99})
    is_sector_strong = (sec_info["avg_pct"] >= 0.5) or (sec_info["strong_count"] >= 3)
    sector_resonance = "🔥🔥 主线强势共振" if is_sector_strong else "⚪ 板块轮动试探"

    ma5 = np.mean(close[-5:])
    ma10 = np.mean(close[-10:])
    ma20 = np.mean(close[-20:])
    if close[-1] < ma20 * 0.96:
        return None

    chip_info = calculate_precise_chip_concentration(df, close[-1])
    atr = calculate_atr(df, 14)

    buy_low = round(max(ma10, close[-1] * 0.97), 2)
    buy_high = round(close[-1] * 1.015, 2)
    stop_loss = round(ma10 * 0.97, 2)
    target_p = round(max(np.max(highs[-20:]), close[-1] * 1.15), 2)
    rr_ratio = round((target_p - close[-1]) / max(0.01, close[-1] - stop_loss), 1)

    pos_desc, action_cmd, action_color = diagnose_position_and_action(close[-1], buy_low, buy_high, stop_loss, target_p, ma5, ma10)

    net_wan = flow_info.get("主力净流入", 0.0)
    ratio = flow_info.get("主力净占比", 0.0)
    is_fund_strong = (net_wan > 300 or ratio > 2.0)

    sector_add_score = 10 if is_sector_strong else 0
    b_prob, s_prob, flow_status, flow_reason = calculate_dynamic_win_rate(net_wan, ratio, pos_desc, rr_ratio, 40, chip_info['width_70'], macro_status.get("market_score", 12), sector_add_score)

    if is_sector_strong and is_fund_strong and (close[-1] >= ma5):
        resonance_tag = "🔥🔥🔥 四重共振(主线领跑)"
        position_rule = "50%~60% 重仓 (主线确定性)"
        add_rule = "盈利突破前高加仓40%，破10日线清仓，严禁亏损加仓！"
        why_buy_core = f"【四重共振达成】：所属板块【{sector_name}】集体走强（同梯队强势标的共振）；主力资金持续流入；个股稳站5日均线上方。逻辑极其硬朗，按博主原则给予重仓投票！"
    elif is_sector_strong or is_fund_strong:
        resonance_tag = "⚡⚡ 双重共振(梯队跟进)"
        position_rule = "20%~30% 底仓 (试错观察)"
        add_rule = "底仓试错，待放量拉升得到验证后再加仓，绝不逆势摊平！"
        why_buy_core = f"【双重共振】：板块有异动或主力资金介入，但个股处于洗盘分歧期。属于博主倡导的“底仓试错”区间，有盈利再追胜，不盲目重仓。"
    else:
        resonance_tag = "⚪ 孤立博弈(信号偏弱)"
        position_rule = "10% 轻仓博弈"
        add_rule = "纯题材脉冲，快进快出，不加仓，破位即走！"
        why_buy_core = "缺乏板块集群效应，单兵突进易受情绪退潮影响，仅适合小仓位低吸做T。"

    advice = {
        "建议买入区间": f"{buy_low} ~ {buy_high}",
        "建议买入区间_低": buy_low, "建议买入区间_高": buy_high,
        "建议止损位": f"{stop_loss} (博主铁律：破10日均线离场)",
        "止损数值": stop_loss,
        "第一止盈目标": f"{target_p}",
        "止盈数值": target_p,
        "动态压力位": round(target_p, 2),
        "动态支撑位": round(ma10, 2),
        "ATR": round(atr, 3),
        "盈亏比": f"{rr_ratio} : 1",
        "买入时段": "🌇 尾盘确认 (14:30-14:50)",
        "卖出时机": "破 5 日线减半，破 10 日线清仓离场",
        "预估持股周期": "2 ~ 6 个交易日 (跟随主线)",
        "为什么值得买": why_buy_core,
        "自适应仓位": position_rule,
        "当前位置描述": pos_desc,
        "具体操作指令": action_cmd,
        "指令颜色": action_color,
        "主力资金状态": flow_status,
        "买入概率": f"{b_prob}%",
        "卖出/风险概率": f"{s_prob}%",
        "共振等级": resonance_tag,
        "加仓铁律": add_rule
    }

    timing_dict = {
        "买点战术详情": [
            f"📍 【共振级别】：{resonance_tag} | 仓位配置：{position_rule}",
            f"⚡ 【加仓与风控铁律】：{add_rule}",
            f"💰 【资金动向】：{flow_status} (胜率估算: {b_prob}%)",
            f"🎯 【离场信号】：板块走弱/龙头退潮/跌破10日均线，执行无条件止损！"
        ]
    }

    radar = {"主力异动": 18, "洗盘充分度": 16, "筹码沉淀": min(20, int(chip_info['profit_ratio'] * 0.2)), "底部安全性": 17, "博弈胜率": int(b_prob * 0.2)}
    total_score = 90 if "四重" in resonance_tag else 78
    return total_score, 42, 45, [resonance_tag, position_rule[:8]], advice, radar, timing_dict, chip_info, True, "🟢 低", rr_ratio

# ==================== 策略 3：三步极选强势股闭环策略 ====================
def evaluate_strategy_three_step_champion(df: pd.DataFrame, row_data: dict, macro_status: dict, flow_info: dict):
    pct = float(row_data.get('涨跌幅', 0))
    turnover = float(row_data.get('换手率', 0))
    close = df['收盘'].values
    highs = df['最高'].values
    lows = df['最低'].values
    vols = df['成交量'].values
    n = len(df)

    if n < 25:
        return None

    if not (2.8 <= pct <= 5.2):
        return None
    if not (2.8 <= turnover <= 10.5):
        return None

    vol_today = vols[-1]
    vol_5d_avg = np.mean(vols[-6:-1]) if n >= 6 else vol_today
    vol_ratio = vol_today / (vol_5d_avg + 1e-6)
    if vol_ratio < 1.8:
        return None

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

    chip_data = calculate_precise_chip_concentration(df, close[-1])
    w90, w70 = chip_data["width_90"], chip_data["width_70"]
    if w90 > 22.0 or w70 > 16.5:
        return None

    buy_low = round(ma5, 2)
    buy_high = round(close[-1], 2)
    stop_loss_ma10 = round(ma10, 2)
    target_high = round(np.max(highs[-20:]) * 1.12, 2)
    rr_ratio = round((target_high - close[-1]) / max(0.01, close[-1] - stop_loss_ma10), 1)

    pos_desc, action_cmd, action_color = diagnose_position_and_action(close[-1], buy_low, buy_high, stop_loss_ma10, target_high, ma5, ma10)

    net_wan = flow_info.get("主力净流入", 0.0)
    ratio = flow_info.get("主力净占比", 0.0)
    m_score = macro_status.get("market_score", 12)
    b_prob, s_prob, flow_status, flow_reason = calculate_dynamic_win_rate(net_wan, ratio, pos_desc, rr_ratio, 45, w70, m_score)

    why_buy_core = (
        f"今日温和放量 {vol_ratio:.1f} 倍涨 {pct:.1f}%，换手 {turnover:.1f}%；"
        f"均线多头排列；70% 筹码集中度达 {w70}%（主力高度控盘锁定）。资金面：{flow_reason}"
    )

    advice = {
        "建议买入区间": f"{buy_low} ~ {buy_high}",
        "建议买入区间_低": buy_low, "建议买入区间_高": buy_high,
        "建议止损位": f"{stop_loss_ma10} (破10日线清仓)",
        "止损数值": stop_loss_ma10,
        "第一止盈目标": f"{target_high}",
        "止盈数值": target_high,
        "动态压力位": round(np.max(highs[-20:]), 2),
        "动态支撑位": round(ma10, 2),
        "ATR": round(close[-1] * 0.03, 3),
        "盈亏比": f"{rr_ratio} : 1",
        "买入时段": "🌇 尾盘进场 (14:30 - 14:50)",
        "卖出时机": "破 5 日线减半，破 10 日线清仓",
        "预估持股周期": "⚡ 顺势主升 (2 ~ 6 个交易日)",
        "为什么值得买": why_buy_core,
        "自适应仓位": "首批进6成，突破新高加4成",
        "当前位置描述": pos_desc,
        "具体操作指令": action_cmd,
        "指令颜色": action_color,
        "主力资金状态": flow_status,
        "买入概率": f"{b_prob}%",
        "卖出/风险概率": f"{s_prob}%"
    }

    timing_dict = {
        "买点战术详情": [
            f"📍 【当前位置定位】：{pos_desc}",
            f"💰 【主力资金流向】：{flow_status} (胜率: {b_prob}%)",
            f"⚡ 【实操动作指引】：{action_cmd}",
            "🌇 【尾盘买入】：回踩 5 日或 10 日均线缩量止跌，尾盘 14:30 进场 60% 底仓！",
            f"🛡️ 【铁律止盈止损】：以 5 日线为减仓线，跌破 10 日线 {stop_loss_ma10} 元坚决清仓走人！"
        ]
    }

    radar = {"主力异动": 19, "洗盘充分度": 18, "筹码沉淀": 20, "底部安全性": 17, "博弈胜率": int(b_prob * 0.2)}
    return 92, 45, 47, ["🔥 放量异动", "📈 均线多头", flow_status[:10]], advice, radar, timing_dict, chip_data, True, "🟢 低", 3.0

# ==================== 策略 2：主力逻辑博弈与假摔洗盘 ====================
def evaluate_strategy_main_force_game(df: pd.DataFrame, row_data: dict, enable_weekly: bool, enable_fundamental: bool, macro_status: dict, min_rr: float, flow_info: dict):
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
        why_buy_core = f"前期大阳放量突破后缩量急跌洗盘 {pullback_pct:.1f}%，现已触底起涨底线，假摔结束随时反包拉升。"
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
                why_buy_core = "底部异动放量主力吃货，随后几天连续不涨看似疲软，实为缩量假摔挖坑洗出浮筹，未破成本线。"

    pattern_c_hit = False
    if n >= 10 and not (pattern_a_hit or pattern_b_hit):
        ma5, ma10, ma20 = np.mean(close[-5:]), np.mean(close[-10:]), np.mean(close[-min(20, n):])
        spread = (max(ma5, ma10, ma20) - min(ma5, ma10, ma20)) / ma20 * 100
        if spread < 8.5:
            pattern_c_hit = True
            quality += 18
            tags.append(f"🌀 均线粘合({spread:.1f}%)")
            why_buy_core = "多日横盘不涨是在进行【筹码沉淀与均线收敛】，波动率压缩至极点，属于典型的暴风雨前静默吸筹期。"

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

    ma5_val = np.mean(close[-5:])
    ma10_val = np.mean(close[-10:])
    pos_desc, action_cmd, action_color = diagnose_position_and_action(close[-1], buy_low, buy_high, stop_loss_logic, target_p, ma5_val, ma10_val)

    net_wan = flow_info.get("主力净流入", 0.0)
    ratio = flow_info.get("主力净占比", 0.0)
    m_score = macro_status.get("market_score", 12)
    b_prob, s_prob, flow_status, flow_reason = calculate_dynamic_win_rate(net_wan, ratio, pos_desc, rr_ratio, quality, chip_info['width_70'], m_score)

    why_buy_core += f" 资金面状态：{flow_reason}"
    total = quality + timing + macro_status.get("market_score", 10)

    advice = {
        "建议买入区间": f"{buy_low} ~ {buy_high}",
        "建议买入区间_低": buy_low, "建议买入区间_高": buy_high,
        "建议止损位": f"{stop_loss_logic} (主力逻辑防守线)",
        "止损数值": stop_loss_logic,
        "第一止盈目标": f"{target_p}",
        "止盈数值": target_p,
        "动态压力位": round(max_p_25d, 2),
        "动态支撑位": round(logic_base_price, 2),
        "ATR": round(atr, 3),
        "盈亏比": f"{rr_ratio} : 1",
        "买入时段": "🌅 早盘 (09:40-10:15)" if (pattern_a_hit or pattern_b_hit) else "🌇 尾盘 (14:30-14:50)",
        "卖出时机": "冲高前高压力位分批止盈",
        "预估持股周期": "2 ~ 5 个交易日",
        "为什么值得买": why_buy_core,
        "自适应仓位": "30% (博弈主力建仓仓位)",
        "当前位置描述": pos_desc,
        "具体操作指令": action_cmd,
        "指令颜色": action_color,
        "主力资金状态": flow_status,
        "买入概率": f"{b_prob}%",
        "卖出/风险概率": f"{s_prob}%"
    }

    timing_dict = {
        "买点战术详情": [
            f"📍 【当前位置定位】：{pos_desc}",
            f"💰 【主力资金流向】：{flow_status} (胜率: {b_prob}%)",
            f"⚡ 【实操动作指引】：{action_cmd}",
            f"🎯 【主力防守线】：以起涨底线 {stop_loss_logic} 元为铁律止损线，跌破坚决走人！"
        ]
    }

    radar = {"主力异动": 18, "洗盘充分度": 16, "筹码沉淀": min(20, int(chip_info['profit_ratio'] * 0.2)), "底部安全性": 18, "博弈胜率": int(b_prob * 0.2)}
    return total, quality, timing, tags, advice, radar, timing_dict, chip_info, True, "🟢 低", rr_ratio

# ==================== 策略 1：多周期均线 + ATR 低吸策略 ====================
def evaluate_strategy_classic_atr(df: pd.DataFrame, row_data: dict, enable_weekly: bool, enable_fundamental: bool, macro_status: dict, min_rr: float, flow_info: dict):
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

    buy_low = round(close[-1] * 0.98, 2)
    buy_high = round(close[-1] * 1.015, 2)
    stop_loss_p = round(min(df['最低'].iloc[-1], ma20) - 0.5 * atr, 2)
    target1 = round(close[-1] + 1.8 * atr, 2)

    risk_span = max(0.01, close[-1] - stop_loss_p)
    reward_span = max(0.01, target1 - close[-1])
    rr_ratio = round(reward_span / risk_span, 2)

    if rr_ratio < min_rr:
        return None

    pos_desc, action_cmd, action_color = diagnose_position_and_action(close[-1], buy_low, buy_high, stop_loss_p, target1, ma5, ma10)
    net_wan = flow_info.get("主力净流入", 0.0)
    ratio = flow_info.get("主力净占比", 0.0)
    m_score = macro_status.get("market_score", 12)
    b_prob, s_prob, flow_status, flow_reason = calculate_dynamic_win_rate(net_wan, ratio, pos_desc, rr_ratio, 35, chip_info['width_70'], m_score)

    why_buy_core = f"股价沿中期均线温和推升，下有 MA20 强力托底，洗盘缩量。资金面：{flow_reason}"
    total = quality + timing + macro_status.get("market_score", 10)

    advice = {
        "建议买入区间": f"{buy_low} ~ {buy_high}",
        "建议买入区间_低": buy_low, "建议买入区间_高": buy_high,
        "建议止损位": f"{stop_loss_p} (ATR动态止损)",
        "止损数值": stop_loss_p,
        "第一止盈目标": f"{target1}",
        "止盈数值": target1,
        "动态压力位": round(close[-1] * 1.08, 2),
        "动态支撑位": round(ma20, 2),
        "ATR": round(atr, 3),
        "盈亏比": f"{rr_ratio} : 1",
        "买入时段": "🌇 尾盘 (14:30 - 14:50)",
        "卖出时机": "冲高上轨 +3%~+5% 逐步止盈",
        "预估持股周期": "3 ~ 10 个交易日",
        "为什么值得买": why_buy_core,
        "自适应仓位": "25% (ATR波动率平价)",
        "当前位置描述": pos_desc,
        "具体操作指令": action_cmd,
        "指令颜色": action_color,
        "主力资金状态": flow_status,
        "买入概率": f"{b_prob}%",
        "卖出/风险概率": f"{s_prob}%"
    }

    timing_dict = {
        "买点战术详情": [
            f"📍 【当前位置定位】：{pos_desc}",
            f"💰 【主力资金流向】：{flow_status} (胜率: {b_prob}%)",
            f"⚡ 【实操动作指引】：{action_cmd}",
            "🌇 【尾盘买点】：趋势均线型标的，14:30 后确认站稳均线低吸。",
            f"🛡️ 【ATR止损】：严格执行动态止损线 {stop_loss_p} 元。"
        ]
    }

    radar = {"均线趋势": 16, "资金强度": 14, "突破动能": 13, "换手活跃": 14, "位置安全": 16}
    return total, quality, timing, tags, advice, radar, timing_dict, chip_info, True, "🟢 低", rr_ratio

# ==================== 工作任务分发 ====================
def worker_task(code, name, row_data, strategy_choice, enable_weekly, enable_fundamental, macro_status, min_rr, ind_map, flow_map, sector_stats):
    k_df = fetch_kline_safe(code, row_data, days=90)
    last_close = float(k_df['收盘'].iloc[-1])
    pct_today = float(k_df['涨跌幅'].iloc[-1]) if len(k_df) > 1 else float(row_data.get('涨跌幅', 0))
    flow_info = flow_map.get(str(code).zfill(6), {"主力净流入": 0.0, "主力净占比": 0.0})
    sector_name = ind_map.get(str(code).zfill(6), "制造综合")

    if "4️⃣" in strategy_choice:
        res = evaluate_strategy_quad_resonance(k_df, row_data, macro_status, flow_info, sector_stats, sector_name)
    elif "3️⃣" in strategy_choice:
        res = evaluate_strategy_three_step_champion(k_df, row_data, macro_status, flow_info)
    elif "2️⃣" in strategy_choice:
        res = evaluate_strategy_main_force_game(k_df, row_data, enable_weekly, enable_fundamental, macro_status, min_rr, flow_info)
    else:
        res = evaluate_strategy_classic_atr(k_df, row_data, enable_weekly, enable_fundamental, macro_status, min_rr, flow_info)

    if not res:
        return None

    total, quality, timing, tags, advice, radar, timing_dict, chip_info, passed, risk_level, rr_ratio = res
    star_rating = "⭐⭐⭐⭐⭐" if total >= 85 else ("⭐⭐⭐⭐" if total >= 75 else "⭐⭐⭐")
    amt_wan = float(row_data.get('成交额(万)', 0))

    k_df['MA5'] = k_df['收盘'].rolling(5).mean().fillna(k_df['收盘'])
    k_df['MA10'] = k_df['收盘'].rolling(10).mean().fillna(k_df['收盘'])
    k_df['MA20'] = k_df['收盘'].rolling(20).mean().fillna(k_df['收盘'])

    return {
        "代码": code, "名称": name, "板块": sector_name, "评级": star_rating,
        "当前位置": advice.get("当前位置描述", "蓄势区"),
        "主力资金": advice.get("主力资金状态", "平稳"),
        "买入胜率": advice.get("买入概率", "65%"),
        "操作指令": advice.get("具体操作指令", "等待信号"),
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
    fig.add_hline(y=advice["动态支撑位"], line_dash="dash", line_color="#00e676", annotation_text=f"底线防守: {advice['动态支撑位']}", row=1, col=1)
    vol_colors = ['#ef5350' if c >= o else '#26a69a' for c, o in zip(recent['收盘'], recent['开盘'])]
    fig.add_trace(go.Bar(x=recent['日期'], y=recent['成交量'], marker_color=vol_colors, name="成交量"), row=2, col=1)
    fig.update_layout(title=f"📈 {code} {name} (最新 {recent['收盘'].iloc[-1]} 元)",
                      xaxis_rangeslider_visible=False, height=450, margin=dict(l=10, r=10, t=35, b=10))
    return fig

# ==================== 扫描执行 ====================
if st.button("🚀 启动全市场深度量化极速扫描", type="primary", use_container_width=True):
    t_start = time.time()
    macro_now = fetch_realtime_macro_deep()
    with st.spinner(f"正在拉取标的池与行业数据..."):
        pool = get_all_realtime_stocks_tx(board_type, min_price, max_price, min_amount, exclude_limit_up)

    if len(pool) == 0:
        st.error("❌ 标的池初筛为空，请调宽左侧参数。")
        st.stop()

    pool['行业'] = pool['代码'].apply(lambda x: industry_map.get(str(x).zfill(6), "制造综合"))

    sector_stats = {}
    sec_grouped = pool.groupby('行业')['涨跌幅'].agg(['mean', 'count', lambda s: (s >= 3.0).sum()]).reset_index()
    sec_grouped.columns = ['行业', 'avg_pct', 'total_count', 'strong_count']
    sec_grouped = sec_grouped.sort_values(by=['strong_count', 'avg_pct'], ascending=False).reset_index(drop=True)
    for idx, r in sec_grouped.iterrows():
        sector_stats[r['行业']] = {"avg_pct": round(r['avg_pct'], 2), "strong_count": int(r['strong_count']), "rank": idx + 1}

    if selected_sectors:
        pool = pool[pool['行业'].isin(selected_sectors)]
        if len(pool) == 0:
            st.error("❌ 在您选定的板块中，当前没有任何符合基础条件的股票。")
            st.stop()

    candidates = pool[(pool['涨跌幅'] >= min_scan_pct) & (pool['涨跌幅'] <= max_scan_pct)].sort_values(
        by=["成交额(万)"], ascending=False
    ).head(deep_sample_size)

    if len(candidates) < 15:
        candidates = pool.sort_values(by=["成交额(万)"], ascending=False).head(deep_sample_size)

    candidate_codes = candidates['代码'].tolist()
    money_flow_data = fetch_money_flow_safe_batched(candidate_codes)

    hit_results, new_kline_cache = [], {}
    progress_bar = st.progress(0, text=f"正在深度分析 {len(candidates)} 只样本及其共振动向...")
    completed = 0
    with ThreadPoolExecutor(max_workers=25) as executor:
        futures = [executor.submit(worker_task, str(row['代码']).zfill(6), row['名称'], row.to_dict(),
                                   strategy_mode, enable_weekly_filter, enable_fundamental_filter, macro_now, min_risk_reward, industry_map, money_flow_data, sector_stats)
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
tab_view_select, tab_view_portfolio = st.tabs(["🔥 AI 智能精选投研看板", "💼 我的网页持仓/自选监控池 (动态跳动)"])

with tab_view_select:
    if st.session_state.get('scan_results'):
        results = st.session_state['scan_results']
        kline_cache = st.session_state['kline_cache']
        res_df = pd.DataFrame(results)

        st.subheader("👑 今日核心自选前三甲 (闭眼看主力资金与买入胜率)")
        top3_cols = st.columns(min(3, len(results)))
        for idx, col in enumerate(top3_cols):
            r_item = results[idx]
            adv = r_item['advice']
            with col:
                st.markdown(f"""
                <div style="background-color:rgba(255,255,255,0.05); padding:14px; border-radius:8px; border-left:4px solid {adv['指令颜色']};">
                    <div style="font-size:17px; font-weight:bold;">{r_item['评级']} {r_item['名称']} ({r_item['代码']}) <span style="font-size:12px; background-color:#2e7d32; padding:2px 6px; border-radius:4px; color:#fff; margin-left:6px;">{r_item['板块']}</span></div>
                    <div style="font-size:13px; color:#ffd600; margin-top:4px;">📍 <b>位置</b>：{adv['当前位置描述']}</div>
                    <div style="font-size:13px; color:#64b5f6; margin-top:2px;">💰 <b>主力动向</b>：{adv['主力资金状态']}</div>
                    <div style="font-size:14px; color:#00e676; margin-top:3px; font-weight:bold;">🎲 <b>买入胜率</b>：{adv['买入概率']} | <b>仓位</b>：{adv.get('自适应仓位','分批')}</div>
                    <div style="font-size:12px; color:#eee; margin-top:6px; line-height:1.4;">💡 {r_item['为什么值得买']}</div>
                </div>
                """, unsafe_allow_html=True)

        st.divider()

        display_cols = ["评级", "代码", "名称", "板块", "当前位置", "主力资金", "买入胜率", "操作指令", "仓位战术", "最新价", "涨跌幅(%)", "综合评分"]
        st.dataframe(res_df[display_cols], use_container_width=True, hide_index=True)

        st.subheader("📊 个股全景决策中枢 (买卖四问·自检执行)")
        if not res_df.empty:
            selected_code = st.selectbox(
                "选择要深度诊断的股票：",
                options=res_df["代码"].tolist(),
                format_func=lambda x: f"[{next(r['板块'] for r in results if r['代码']==x)}] {x} - {next(r['名称'] for r in results if r['代码']==x)} | 胜率:{next(r['买入胜率'] for r in results if r['代码']==x)} | {next(r['仓位战术'] for r in results if r['代码']==x)}"
            )

            if selected_code and selected_code in kline_cache:
                s_name, s_df, s_adv, s_radar, s_timing, s_chip, s_row_data = kline_cache[selected_code]

                st.markdown(f"""
                <div style="background-color:rgba(0,0,0,0.3); padding:16px; border-radius:8px; border:1px solid #444; margin-bottom:12px;">
                    <div style="font-size:16px; font-weight:bold; color:#ff9800; margin-bottom:8px;">🧠 操盘手灵魂自检【买卖 4 问】：</div>
                    <div style="font-size:13px; color:#ddd;">1️⃣ <b>板块还强吗？</b>：所属【{next(r['板块'] for r in results if r['代码']==selected_code)}】{s_adv.get('共振等级','处于主线共振')}</div>
                    <div style="font-size:13px; color:#ddd; margin-top:3px;">2️⃣ <b>主力还在流入吗？</b>：{s_adv['主力资金状态']}</div>
                    <div style="font-size:13px; color:#ddd; margin-top:3px;">3️⃣ <b>胜率与仓位匹配吗？</b>：当前胜率 {s_adv['买入概率']}，严格执行【{s_adv['自适应仓位']}】投票</div>
                    <div style="font-size:13px; color:#00e676; margin-top:3px;">4️⃣ <b>灵魂一问：如果今天没有这只股票，我现在还会买吗？</b>：{s_adv['具体操作指令']}</div>
                </div>
                """, unsafe_allow_html=True)

                ca, cb, cc, cd, ce = st.columns(5)
                ca.metric("🎯 建议买入区间", s_adv["建议买入区间"])
                cb.metric("🛡️ 10日线铁律止损", s_adv["建议止损位"].split(" ")[0])
                cc.metric("🚀 第一止盈目标", s_adv["第一止盈目标"].split(" ")[0])
                cd.metric("⏰ 最佳下单时段", s_adv.get("买入时段", "尾盘买入").split(" ")[0])
                ce.metric("💰 仓位投票法则", s_adv.get("自适应仓位", "分批建仓"))

                exists_in_p = any(p['code'] == selected_code for p in st.session_state['portfolio'])
                b_c1, b_c2 = st.columns([1.8, 8.2])
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
                            st.toast(f"🎉 成功加入网页监控池！", icon="✅")
                            st.rerun()

                st.warning(f"🌟 **逻辑与资金共振推演**：\n\n{s_adv['为什么值得买']}")
                st.info(f"💡 **精准操盘动作拆解**：\n" + "\n".join([f"- {t}" for t in s_timing['买点战术详情']]))
                st.plotly_chart(draw_pro_kline(selected_code, s_name, s_df, s_adv), use_container_width=True)
        else:
            st.warning("⚠️ 当前筛选下暂无标的。")

    elif st.session_state.get('has_scanned'):
        st.warning("⚠️ 扫描池暂时为空，建议调宽参数重新扫描。")
    else:
        st.info("👈 请在左侧确认参数后，点击上方红色的 **“🚀 启动全市场深度量化极速扫描”** 按钮。")

# ==================== 网页持仓监控池 ====================
with tab_view_portfolio:
    st.subheader("💼 我的网页专属自选与持仓监控")

    @st.fragment(run_every=live_interval if enable_auto_live else None)
    def render_live_portfolio_panel():
        if not st.session_state['portfolio']:
            st.info("💡 监控池目前为空。在第一页【AI 智能精选看板】中选中股票后，点击 **“➕ 加入网页自选监控池”** 即可在此集中盯盘。")
            return

        p_list = st.session_state['portfolio']
        p_codes = [p['code'] for p in p_list]
        symbols = [f"sh{c}" if c.startswith("60") else f"sz{c}" for c in p_codes]
        real_items = fetch_tencent_batch(symbols)
        price_map = {item['代码']: item for item in real_items}
        flow_map_p = fetch_money_flow_safe_batched(p_codes)

        now_str = datetime.now().strftime("%H:%M:%S")
        st.caption(f"⚡ 监控池状态：**实时跳动同步中** (最新刷新时间: {now_str} | 每 {live_interval} 秒轮询)")

        p_display = []
        alert_count = 0
        for p in p_list:
            c = p['code']
            real_d = price_map.get(c, {})
            curr_p = real_d.get('最新价', 0.0)
            pct = real_d.get('涨跌幅', 0.0)
            sl = float(p.get('stop_loss', 0))
            tg = float(p.get('target', 0))
            f_d = flow_map_p.get(c, {})
            f_net = f_d.get("主力净流入", 0.0)

            if curr_p <= sl and curr_p > 0:
                status = "🔴 跌破防守线 (坚决清仓)"
                alert_count += 1
            elif curr_p >= tg and curr_p > 0:
                status = "🟢 触及目标位 (分批止盈)"
            else:
                dist_sl = round((curr_p - sl) / curr_p * 100, 1) if curr_p > 0 else 0
                status = f"🟡 正常持有 (距止损 {dist_sl}%)"

            p_display.append({
                "代码": c,
                "名称": p['name'],
                "最新价": curr_p,
                "今日涨跌幅(%)": pct,
                "主力实时净流入": f"{'+' if f_net>0 else ''}{f_net}万",
                "建议买入区间": p.get('buy_range', '-'),
                "铁律防守线": sl,
                "目标止盈价": tg,
                "实时预警状态": status,
                "核心逻辑": p.get('why_buy', '-')
            })

        if alert_count > 0:
            st.error(f"🚨 **紧急风控提示**：监控池中有 {alert_count} 只股票已跌破防守线，请严格遵守纪律，次日冲高离场！")

        st.dataframe(pd.DataFrame(p_display), use_container_width=True, hide_index=True)

    render_live_portfolio_panel()

    if st.session_state['portfolio']:
        st.write("")
        p_list = st.session_state['portfolio']
        del_col1, del_col2 = st.columns([3, 7])
        with del_col1:
            code_to_remove = st.selectbox("选择要移除的股票", options=[p['code'] for p in p_list], format_func=lambda x: f"{x} - {next(p['name'] for p in p_list if p['code']==x)}")
            if st.button("🗑️ 确认移出监控池", use_container_width=True):
                st.session_state['portfolio'] = [p for p in st.session_state['portfolio'] if p['code'] != code_to_remove]
                save_portfolio(st.session_state['portfolio'])
                st.toast(f"已移除 {code_to_remove}", icon="✅")
                st.rerun()
