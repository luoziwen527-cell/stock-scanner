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

# 彻底屏蔽代理
for k in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']:
    os.environ.pop(k, None)
urllib.request.getproxies = lambda: {}

st.set_page_config(
    page_title="AI 智能主力量化全闭环投研系统 v2.6",
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

# ==================== 大盘宏观 ====================
@st.cache_data(ttl=60)
def fetch_market_macro_status():
    url = "https://qt.gtimg.cn/q=s_sh000001,s_sz399001"
    macro_info = {
        "sh_pct": 0.0, "sh_price": 0.0, "sh_amt": 0,
        "sz_pct": 0.0,
        "status_color": "🟢", "status_text": "安全进攻区",
        "suggest_position": "70% ~ 90%",
        "action_guide": "大盘趋势良好，可积极参与主线龙头突破与多头共振标的。",
        "market_score": 15
    }
    try:
        resp = requests.get(url, timeout=2.0)
        lines = resp.text.strip().split(";")
        for line in lines:
            if "s_sh000001" in line:
                parts = line.split("=")[1].strip('"').split("~")
                if len(parts) >= 6:
                    macro_info["sh_price"] = float(parts[2] or 0)
                    macro_info["sh_pct"] = float(parts[5] or 0)
                    macro_info["sh_amt"] = int(float(parts[6] or 0) / 10000)
            elif "s_sz399001" in line:
                parts = line.split("=")[1].strip('"').split("~")
                if len(parts) >= 6:
                    macro_info["sz_pct"] = float(parts[5] or 0)

        sh_pct = macro_info["sh_pct"]
        if sh_pct >= 0.3:
            macro_info.update({
                "status_color": "🟢", "status_text": "多头进攻周期",
                "suggest_position": "70% ~ 90%",
                "action_guide": "大盘处于多头进攻阶段，容错率高，可顺势操作龙头突破与主升浪。",
                "market_score": 18
            })
        elif -0.6 <= sh_pct < 0.3:
            macro_info.update({
                "status_color": "🟡", "status_text": "震荡分歧周期",
                "suggest_position": "30% ~ 50%",
                "action_guide": "大盘日内分歧震荡，盘中切忌追高！严格推迟到 14:30 尾盘低吸回踩企稳标的。",
                "market_score": 10
            })
        else:
            macro_info.update({
                "status_color": "🔴", "status_text": "弱势系统性风险区",
                "suggest_position": "0% ~ 20% (防守观望)",
                "action_guide": "大盘单边下跌破位，建议防守观望，仅轻仓关注逆势抗跌抢筹龙头。",
                "market_score": 4
            })
    except Exception:
        pass
    return macro_info

# ==================== 微信推送 ====================
def send_wechat_notification(webhook_key: str, title: str, content_list: list, macro_info: dict):
    if not webhook_key:
        return False, "未配置微信 Key 或 Webhook"

    text_content = f"### 🚀 {title}\n"
    text_content += f"**大盘环境**: {macro_info['status_color']} {macro_info['status_text']} | 上证: `{macro_info['sh_price']}点` ({macro_info['sh_pct']:+.2f}%)\n"
    text_content += f"**建议总仓位**: `{macro_info['suggest_position']}`\n"
    text_content += f"**战术指引**: {macro_info['action_guide']}\n\n---------------------------------\n"

    for idx, item in enumerate(content_list[:8]):
        text_content += (
            f"- **No.{idx+1} [{item['代码']}] {item['名称']}** | "
            f"最新价: `{item['最新价']}元` ({item['涨跌幅(%)']:+.2f}%) | "
            f"质量:{item.get('质量分',0)} 时机:{item.get('时机分',0)} | "
            f"买入区间: `{item['advice']['建议买入区间']}`\n"
            f"  *入场时机*: {item['timing']['最佳买入时机']}\n"
            f"  *特征*: {item['量化特征']}\n"
        )

    if webhook_key.startswith(("SCT", "SCU")) or "sendkey" in webhook_key.lower():
        try:
            requests.post(f"https://sctapi.ftqq.com/{webhook_key}.send",
                          data={"title": title, "desp": text_content}, timeout=4)
            return True, "Server酱微信推送成功"
        except Exception as e:
            return False, f"Server酱推送失败: {e}"
    elif "qyapi.weixin.qq.com" in webhook_key:
        try:
            requests.post(webhook_key, json={"msgtype": "markdown", "markdown": {"content": text_content}},
                          headers={"Content-Type": "application/json"}, timeout=4)
            return True, "企业微信机器人推送成功"
        except Exception as e:
            return False, f"企业微信推送失败: {e}"
    return False, "无法识别的微信推送 Key 格式"

# ==================== AI 点评（诚实版） ====================
def generate_ai_llm_analysis(code: str, name: str, item_data: dict, api_key: str, base_url: str):
    if api_key:
        try:
            url = f"{base_url.rstrip('/')}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            prompt = (
                f"你是资深量化投研专家。针对 A 股主板 [{code}] {name}，"
                f"当前价 {item_data['最新价']} 元，日内涨幅 {item_data['涨跌幅(%)']}%，"
                f"质量分 {item_data.get('质量分',0)}，时机分 {item_data.get('时机分',0)}，"
                f"技术特征 '{item_data['量化特征']}'。\n"
                f"注意：我没有给你任何公告/解禁/减持/立案数据，请不要编造“未见重大风险”。"
                f"请用 120 字以内给出：1) 技术面短线买卖建议；2) 明确提醒需要人工核查公告排雷。"
            )
            payload = {
                "model": "deepseek-chat" if "deepseek" in base_url.lower() else "qwen-turbo",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4
            }
            resp = requests.post(url, json=payload, headers=headers, timeout=6)
            return resp.json().get("choices", [{}])[0].get("message", {}).get("content", "大模型分析返回异常。")
        except Exception:
            pass

    return (
        "⚠️ **公告排雷**：当前未接入公告/解禁/减持/立案数据，无法自动判定基本面安全，请人工核查。\n\n"
        f"🎯 **技术操盘**：质量分 {item_data.get('质量分',0)} / 时机分 {item_data.get('时机分',0)}。"
        "建议在买入区间分批，止损严格按 ATR 动态位执行。"
    )

# ==================== 页面标题 ====================
st.title("🧠 AI 智能主力量化全闭环投研系统 v2.6")
st.caption("多周期共振 · 时间衰减筹码 · ATR 自适应买卖 · 质量/时机分离 · 对齐实盘评分的真实历史回测")

macro = fetch_market_macro_status()
m_col1, m_col2, m_col3, m_col4 = st.columns([1.2, 1, 1.2, 2.6])
m_col1.metric("🏛️ 上证指数", f"{macro['sh_price']} 点", f"{macro['sh_pct']:+.2f}%")
m_col2.metric("🏛️ 深证成指", f"{macro['sz_pct']:+.2f}%")
m_col3.metric("🧭 建议总仓位", macro['suggest_position'], f"{macro['status_color']} {macro['status_text']}")
with m_col4:
    st.info(f"💡 **大盘战术风控指引**：\n{macro['action_guide']}")

st.divider()

# ==================== 侧边栏 ====================
with st.sidebar:
    st.header("⚙️ 选股模式与共振参数")
    enable_weekly_filter = st.checkbox("📈 开启【周线定大势】硬核共振", value=True)

    st.divider()
    st.subheader("🎯 深度样本与精选")
    deep_sample_size = st.slider("深度分析样本量 (只)", 100, 1000, 400, 50)
    display_top_n = st.slider("最终精选呈现数量 (只)", 5, 60, 15, 5)

    st.divider()
    board_type = st.selectbox("市场板块", ["全部主板 (60 + 00)", "仅沪市主板 (60开头)", "仅深市主板 (00开头)"])
    price_range = st.slider("股价区间 (元)", 1.0, 50.0, (3.0, 25.0), 0.5)
    min_price, max_price = price_range
    min_amount = st.slider("最低日成交额门槛 (万元)", 1000, 30000, 3000, 500)
    exclude_limit_up = st.checkbox("🚫 剔除涨停封板股票 (可买入优先)", value=True)

    st.divider()
    st.subheader("🤖 AI 大模型接入 (可选)")
    llm_api_key = st.text_input("大模型 API Key (如 DeepSeek)", value="", type="password")
    llm_base_url = st.text_input("API Base URL", value="https://api.deepseek.com/v1")

    st.divider()
    st.subheader("📲 微信推送")
    wechat_key = st.text_input("微信推送 Key / Webhook", value="", type="password")

    col_t1, col_t2 = st.columns(2)
    with col_t1:
        if st.button("🧪 发送测试推送"):
            if not wechat_key:
                st.warning("请先填写微信推送 Key")
            else:
                ok, msg = send_wechat_notification(
                    wechat_key, "【AI选股测试】微信推送连接正常",
                    [{"代码": "600000", "名称": "测试股票", "最新价": 10.5, "涨跌幅(%)": 3.5,
                      "质量分": 35, "时机分": 32,
                      "advice": {"建议买入区间": "10.35~10.50"},
                      "timing": {"最佳买入时机": "14:30 尾盘低吸"},
                      "量化特征": "周线多头+日线突破"}],
                    macro)
                st.success("✅ 推送成功！") if ok else st.error(f"❌ {msg}")
    with col_t2:
        auto_push_on_scan = st.checkbox("扫描后自动推送", value=False)

    st.warning("⏰ 14:30 后台定时：Streamlit 依赖网页前台。无头自动化建议使用 APScheduler 或系统 Crontab。")

# ==================== 行情获取 ====================
def generate_stock_codes(b_type: str):
    symbols = []
    if "仅深市" not in b_type:
        for i in range(0, 700):
            symbols.append(f"sh600{i:03d}")
        for i in range(0, 400):
            symbols.append(f"sh601{i:03d}")
        for i in range(0, 600):
            symbols.append(f"sh603{i:03d}")
        for i in range(0, 600):
            symbols.append(f"sh605{i:03d}")
    if "仅沪市" not in b_type:
        for i in range(0, 1400):
            symbols.append(f"sz000{i:03d}")
        for i in range(0, 400):
            symbols.append(f"sz001{i:03d}")
        for i in range(0, 1400):
            symbols.append(f"sz002{i:03d}")
        for i in range(0, 500):
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
            if len(fields) < 40:
                continue
            name, code = fields[1], fields[2]
            price = float(fields[3] or 0)
            if price <= 0 or "ST" in name or "退" in name:
                continue
            items.append({
                "代码": code, "名称": name, "最新价": price,
                "昨收": float(fields[4] or 0), "今开": float(fields[5] or 0),
                "最高": float(fields[33] or price), "最低": float(fields[34] or price),
                "涨跌幅": float(fields[32] or 0),
                "成交额(万)": float(fields[37] or 0) if len(fields) > 37 and fields[37] else (float(fields[6] or 0) * price / 100),
                "换手率": float(fields[38] or 0) if len(fields) > 38 and fields[38] else 1.0,
                "成交量": float(fields[6] or 0)
            })
    except Exception:
        pass
    return items

@st.cache_data(ttl=180)
def get_all_realtime_stocks_tx(b_type: str, min_p: float, max_p: float, min_amt: float, no_limit: bool):
    symbols = generate_stock_codes(b_type)
    batches = [symbols[i:i+100] for i in range(0, len(symbols), 100)]
    all_stocks = []
    with ThreadPoolExecutor(max_workers=24) as executor:
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
        df = df[df['涨跌幅'] < 9.2]
    return df.drop_duplicates(subset=['代码']).reset_index(drop=True)

# ==================== K线 + ATR + 时间衰减筹码 ====================
def fetch_kline_safe(code, row_data, days=180):
    market = "sh" if str(code).startswith("60") else "sz"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market}{code},day,,,{days},qfq"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=2.8)
        raw = resp.json().get("data", {}).get(f"{market}{code}", {})
        raw_klines = raw.get("qfqday") or raw.get("day") or []
        if raw_klines and len(raw_klines) >= 25:
            data = [{"日期": r[0], "开盘": float(r[1]), "收盘": float(r[2]),
                     "最高": float(r[3]), "最低": float(r[4]), "成交量": float(r[5])} for r in raw_klines]
            k_df = pd.DataFrame(data)
            k_df['涨跌幅'] = k_df['收盘'].pct_change() * 100
            k_df['涨跌幅'] = k_df['涨跌幅'].fillna(0)
            return k_df
    except Exception:
        pass
    p = float(row_data.get('最新价', 10))
    return pd.DataFrame([{"日期": datetime.today().strftime('%Y-%m-%d'),
                          "开盘": float(row_data.get('今开', p)), "收盘": p,
                          "最高": float(row_data.get('最高', p)), "最低": float(row_data.get('最低', p)),
                          "成交量": 10000.0, "涨跌幅": float(row_data.get('涨跌幅', 0))}])

def calculate_atr(k_df: pd.DataFrame, period: int = 14) -> float:
    if len(k_df) < period + 1:
        return float(k_df['收盘'].iloc[-1] * 0.02)
    high, low, close = k_df['最高'].values, k_df['最低'].values, k_df['收盘'].values
    tr = np.maximum(high[1:] - low[1:], np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
    return float(np.mean(tr[-period:]))

def calculate_chip_distribution_decay(k_df: pd.DataFrame, current_price: float, lookback: int = 120):
    if len(k_df) < 30:
        return {"获利盘比例(%)": 70.0, "套牢盘比例(%)": 30.0, "筹码90%宽度(%)": 15.0,
                "筹码峰": current_price, "平均成本": current_price, "集中度(%)": 12.0, "距筹码峰(%)": 0.0}
    df = k_df.tail(lookback).copy()
    typical = (df['最高'] + df['最低'] + df['收盘']) / 3.0
    vols = df['成交量'].values
    prices = typical.values
    # 更强调近期：指数衰减从 -1.5 → 0
    decay_weights = vols * np.exp(np.linspace(-1.5, 0, len(df)))
    hist, bin_edges = np.histogram(prices, bins=40, weights=decay_weights)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    total_vol = hist.sum() + 1e-9
    cum_vol = np.cumsum(hist)
    low_idx = min(np.searchsorted(cum_vol, total_vol * 0.05), len(bin_centers)-1)
    high_idx = min(np.searchsorted(cum_vol, total_vol * 0.95), len(bin_centers)-1)
    p5, p95 = bin_centers[low_idx], bin_centers[high_idx]
    width_pct = ((p95 - p5) / (p95 + p5 + 1e-5)) * 100
    profit_ratio = round(np.sum(decay_weights[prices <= current_price]) / total_vol * 100, 1)
    peak_idx = np.argmax(hist)
    chip_peak = float(bin_centers[peak_idx])
    return {
        "获利盘比例(%)": profit_ratio,
        "套牢盘比例(%)": round(100.0 - profit_ratio, 1),
        "筹码90%宽度(%)": round(width_pct, 1),
        "筹码峰": round(chip_peak, 2),
        "平均成本": round(float(np.average(prices, weights=decay_weights)), 2),
        "集中度(%)": round(width_pct, 1),
        "距筹码峰(%)": round((current_price - chip_peak) / chip_peak * 100, 2)
    }

def check_weekly_resonance(daily_df: pd.DataFrame):
    if len(daily_df) < 30:
        return True, "日K样本不足"
    try:
        w_df = daily_df.copy()
        w_df['日期'] = pd.to_datetime(w_df['日期'])
        w_df.set_index('日期', inplace=True)
        weekly = w_df.resample('W-FRI').agg({
            '开盘': 'first', '最高': 'max', '最低': 'min',
            '收盘': 'last', '成交量': 'sum'
        }).dropna()
        if len(weekly) < 5:
            return True, "周线形成中"
        w_close = weekly['收盘'].values
        w_ma5 = weekly['收盘'].rolling(5).mean().iloc[-1]
        w_ma10 = weekly['收盘'].rolling(10).mean().iloc[-1] if len(weekly) >= 10 else w_ma5
        if w_close[-1] >= w_ma5 >= w_ma10:
            return True, "🌟 周线多头共振"
        elif w_close[-1] >= w_ma5 * 0.98:
            return True, "周线支撑有效"
        return False, "⚠️ 周线处于下降通道"
    except Exception:
        return True, "周线计算略过"

@st.cache_data(ttl=60)
def fetch_min_timeline(code: str):
    market = "sh" if str(code).startswith("60") else "sz"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={market}{code}"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=2.0)
        raw_data = resp.json().get("data", {}).get(f"{market}{code}", {}).get("data", {}).get("data", [])
        if not raw_data:
            return None
        records, cum_vol, cum_amt = [], 0, 0
        for item in raw_data:
            parts = item.split(" ")
            price, vol = float(parts[1]), float(parts[2])
            cum_vol += vol
            cum_amt += price * vol
            records.append({"时间": parts[0], "现价": price,
                            "均价": round(cum_amt / cum_vol if cum_vol > 0 else price, 2), "成交量": vol})
        return pd.DataFrame(records)
    except Exception:
        return None

# ==================== ATR 自适应买卖点 ====================
def generate_buy_timing_strategy(close_p, ma5, ma10, ma20, low_p, high_p, atr, macro_status, chip_info, bias5):
    atr_ratio = atr / close_p
    k_low = 0.4 if atr_ratio < 0.015 else (0.6 if atr_ratio < 0.03 else 0.85)
    buy_low = round(max(ma5 - k_low * atr, close_p * 0.97), 2)
    buy_high = round(min(close_p, ma5 + 0.2 * atr), 2)
    if buy_low > buy_high:
        buy_low, buy_high = buy_high * 0.99, buy_high
    support_p = round(min(low_p, ma10, ma20), 2)
    stop_loss_p = round(support_p - 0.8 * atr, 2)
    target1 = round(max(high_p * 1.01, close_p + 0.8 * atr), 2)
    target2 = round(close_p + 1.5 * atr, 2)
    target_pct = min(12.0, max(6.0, (target1 / close_p - 1) * 100))

    timing_tactics = []
    if chip_info.get("套牢盘比例(%)", 30) > 35:
        timing_tactics.append(f"⚠️ 上方套牢盘仍有 {chip_info['套牢盘比例(%)']}% ，突破后需观察放量消化。")
    if chip_info.get("获利盘比例(%)", 70) >= 90 and bias5 > 6:
        timing_tactics.append("🚨 获利盘极高 + 高乖离，警惕主力派发，优先等待回踩。")
    if macro_status["status_color"] == "🔴":
        timing_tactics.append("⚠️ 【大盘风险防御】：今日大盘破位，严禁早盘追高！仅 14:30 尾盘轻仓试探。")
    elif macro_status["status_color"] == "🟡":
        timing_tactics.append(f"🕒 【14:30 尾盘低吸】：震荡市首选。股价守在 MA5（约 {round(ma5,2)}）上方且分时黄线上方可打底仓。")
    else:
        timing_tactics.append(f"🚀 【多头顺势】：可在回踩买入区间 [{buy_low}~{buy_high}] 时分批。")
    timing_tactics.append("🕒 【次日 09:35~10:00】：微幅低开或回踩区间缩量企稳可补仓。")

    advice = {
        "建议买入区间": f"{buy_low} ~ {buy_high}",
        "建议买入区间_低": buy_low, "建议买入区间_高": buy_high,
        "建议止损位": f"{stop_loss_p} (ATR动态)",
        "第一止盈目标": f"{target1} (~{target_pct:.1f}%)",
        "第二止盈目标": f"{target2}",
        "动态压力位": round(max(high_p * 1.03, close_p * 1.06), 2),
        "动态支撑位": support_p, "ATR": round(atr, 3)
    }
    return advice, {"最佳买入时机": "14:30 尾盘低吸 / 次日早盘踩 MA5", "买点战术详情": timing_tactics}

# ==================== 核心评分 ====================
def evaluate_multi_period_score(df: pd.DataFrame, row_data: dict, enable_weekly: bool, macro_status: dict):
    quality, timing, tags, risk_level = 15, 10, [], "🟢 低"
    close = df['收盘'].values
    high = df['最高'].values
    low = df['最低'].values
    vol = df['成交量'].values
    pct = float(row_data.get('涨跌幅', 0))
    turnover = float(row_data.get('换手率', 1.0))
    amt_wan = float(row_data.get('成交额(万)', 0))

    weekly_pass, weekly_tag = check_weekly_resonance(df)
    if enable_weekly and not weekly_pass:
        return 0, 0, 0, [weekly_tag], {}, {}, {}, {}, False, "🔴 高"
    if "共振" in weekly_tag:
        quality += 8
        tags.append(weekly_tag)

    ma5 = df['收盘'].rolling(5).mean().iloc[-1] if len(df) >= 5 else close[-1]
    ma10 = df['收盘'].rolling(10).mean().iloc[-1] if len(df) >= 10 else close[-1]
    ma20 = df['收盘'].rolling(20).mean().iloc[-1] if len(df) >= 20 else close[-1]
    ma60 = df['收盘'].rolling(60).mean().iloc[-1] if len(df) >= 60 else ma20
    vol_ma5 = df['成交量'].rolling(5).mean().iloc[-1] if len(df) >= 5 else vol[-1]
    vol_ratio = vol[-1] / vol_ma5 if vol_ma5 > 0 else 1.0
    atr = calculate_atr(df, 14)
    bias5 = (close[-1] / ma5 - 1) * 100 if ma5 > 0 else 0

    chip_info = calculate_chip_distribution_decay(df, close[-1])
    profit_chip = chip_info["获利盘比例(%)"]

    if 70 <= profit_chip <= 85:
        quality += 4
        tags.append(f"获利盘健康{profit_chip}%")
    elif 85 < profit_chip <= 95 and bias5 < 5:
        quality += 6
        tags.append(f"获利盘{profit_chip}%且未高乖离")
    elif profit_chip > 95 and bias5 > 6:
        quality -= 8
        timing -= 6
        tags.append("🚨 高获利+高乖离(派发风险)")
        risk_level = "🟡 中"
    elif profit_chip < 55 and close[-1] > ma20:
        quality += 3
        tags.append("底部突破+低获利盘")

    raw_flow = amt_wan * (pct / 100.0) * 0.35
    strength = raw_flow / max(amt_wan, 1) * 100
    if strength > 1.5 and amt_wan > 5000:
        quality += 7
        tags.append(f"资金强度强(+{strength:.1f}%)")
    elif strength > 0.5:
        quality += 3
        tags.append("资金净流入倾向")
    elif strength < -1.0:
        quality -= 4
        tags.append("资金流出压力")

    if close[-1] > ma5 > ma10 > ma20:
        quality += 9
        tags.append("日线多头排列")
    elif close[-1] >= ma20:
        quality += 4
        tags.append("站稳20日线")
    else:
        quality -= 3

    max_60 = np.max(close[-60:-1]) if len(close) >= 60 else close[-1]
    if close[-1] >= max_60 * 0.98 and vol_ratio >= 1.25:
        quality += 6
        tags.append("放量突破平台")
    elif close[-1] >= ma60:
        quality += 3

    if 2.5 <= turnover <= 12:
        quality += 4
        tags.append(f"换手健康({turnover:.1f}%)")
    elif turnover > 18:
        quality -= 3
        tags.append("换手过高")

    quality = max(0, min(40, quality))

    if -1.5 < bias5 < 3.5:
        timing += 12
        tags.append("乖离适中")
    elif 3.5 <= bias5 < 6:
        timing += 5
        tags.append("轻度偏高")
    elif bias5 >= 6:
        timing -= 10
        tags.append("🚨 高位追涨风险")
        risk_level = "🔴 高"
    elif bias5 < -3:
        timing += 6
        tags.append("超跌回踩机会")

    if vol_ratio >= 1.3 and pct > 0:
        timing += 6
        tags.append("量价齐升")
    elif vol_ratio < 0.7 and pct > -1:
        timing += 4
        tags.append("缩量企稳")

    if ma5 * 0.98 <= close[-1] <= ma5 * 1.02:
        timing += 8
        tags.append("贴近MA5买点")
    elif close[-1] > ma5 * 1.04 and bias5 > 4:
        timing -= 5

    if macro_status["status_color"] == "🔴":
        timing -= 8
    elif macro_status["status_color"] == "🟢":
        timing += 4

    timing = max(0, min(40, timing))
    market_score = macro_status.get("market_score", 10)
    total = max(0, min(100, quality + timing + market_score))

    if total < 55 or risk_level == "🔴 高":
        risk_level = "🔴 高"
    elif total < 70 or risk_level == "🟡 中":
        risk_level = "🟡 中"

    advice, timing_dict = generate_buy_timing_strategy(
        close[-1], ma5, ma10, ma20, low[-1], high[-1], atr, macro_status, chip_info, bias5)

    radar = {
        "均线趋势": min(20, int(quality * 0.4)),
        "资金强度": min(20, int(max(0, strength) * 4 + 8)),
        "突破动能": 16 if "突破" in " ".join(tags) else 10,
        "换手活跃": 16 if 2.5 <= turnover <= 12 else 10,
        "位置安全": min(20, int(timing * 0.45))
    }
    return total, quality, timing, tags, advice, radar, timing_dict, chip_info, True, risk_level

# ==================== 对齐实盘评分的历史回测引擎 ====================
def is_limit_up(open_p, prev_close, threshold=1.098):
    return prev_close > 0 and open_p >= prev_close * threshold

def is_limit_down(close_p, prev_close, threshold=0.902):
    return prev_close > 0 and close_p <= prev_close * threshold

def prepare_backtest_features(k_df: pd.DataFrame):
    df = k_df.copy().reset_index(drop=True)
    close = df['收盘']
    df['MA5'] = close.rolling(5).mean()
    df['MA10'] = close.rolling(10).mean()
    df['MA20'] = close.rolling(20).mean()
    df['MA60'] = close.rolling(60).mean()
    df['VOL_MA5'] = df['成交量'].rolling(5).mean()
    df['BIAS5'] = (close / df['MA5'] - 1) * 100
    high = df['最高'].values
    low = df['最低'].values
    c_prev = close.shift(1).fillna(close.iloc[0]).values
    tr = np.maximum(high - low, np.maximum(np.abs(high - c_prev), np.abs(low - c_prev)))
    df['ATR'] = pd.Series(tr).rolling(14).mean().fillna(close * 0.02)
    df['MAX60'] = close.shift(1).rolling(60).max()
    df['PCT'] = close.pct_change() * 100
    return df

def run_realistic_historical_backtest(
    k_df: pd.DataFrame,
    signal_score_threshold: float = 58,
    timing_score_threshold: float = 14,
    hold_days_max: int = 5,
    commission_rate: float = 0.00025,
    stamp_tax_rate: float = 0.001,
    slippage_rate: float = 0.001,
    min_shares: int = 100,
    initial_capital: float = 100000.0
):
    if k_df is None or len(k_df) < 40:
        return pd.DataFrame(), None

    df = prepare_backtest_features(k_df)
    trades, equity_curve = [], [initial_capital]
    position, entry_price, entry_date, entry_idx, capital = 0, 0.0, None, -1, initial_capital
    start_i = 25
    end_i = len(df) - 1

    for i in range(start_i, end_i):
        row = df.iloc[i]
        c = row['收盘']
        ma5, ma10, ma20, ma60 = row['MA5'], row['MA10'], row['MA20'], row['MA60']
        vol_ma5, bias5, max60, atr = row['VOL_MA5'], row['BIAS5'], row['MAX60'], row['ATR']
        vol_ratio = row['成交量'] / vol_ma5 if vol_ma5 > 0 else 1.0
        pct = row['PCT'] if not pd.isna(row['PCT']) else 0.0

        # 与实盘更接近的评分（核心部分）
        quality = 18
        timing = 12

        if c >= ma5 >= ma10:
            quality += 9
        if c >= ma20:
            quality += 5
        if not pd.isna(max60) and c >= max60 * 0.97:
            quality += 6
        if vol_ratio >= 1.2 and pct > 0:
            quality += 4

        if -2.0 <= bias5 <= 4.0:
            timing += 12
        elif 4.0 < bias5 <= 6.5:
            timing += 4
        elif bias5 > 6.5:
            timing -= 9
        elif bias5 < -3:
            timing += 5

        if vol_ratio >= 1.15:
            timing += 5
        if ma5 * 0.985 <= c <= ma5 * 1.025:
            timing += 6

        total_score = quality + timing + 10  # 中性宏观分
        signal = (total_score >= signal_score_threshold) and (timing >= timing_score_threshold)

        # 持仓卖出
        if position > 0:
            curr_open, curr_close = row["开盘"], row["收盘"]
            curr_high, curr_low = row["最高"], row["最低"]
            prev_close = df["收盘"].iloc[i-1]
            days_held = i - entry_idx

            if not is_limit_down(curr_close, prev_close):
                # ATR 动态止损 / 止盈
                stop_price = entry_price - 1.2 * atr
                target_price = entry_price + 1.6 * atr
                sell_signal, sell_reason, sell_price = False, "", curr_close

                if curr_low <= stop_price:
                    sell_signal, sell_reason, sell_price = True, "ATR止损", min(curr_open, stop_price)
                elif curr_high >= target_price:
                    sell_signal, sell_reason, sell_price = True, "ATR止盈", max(curr_open, target_price * 0.998)
                elif days_held >= hold_days_max:
                    sell_signal, sell_reason, sell_price = True, f"持仓满{hold_days_max}日", curr_close

                if sell_signal:
                    sell_price *= (1 - slippage_rate)
                    proceeds = position * sell_price
                    net_proceeds = proceeds - proceeds * commission_rate - proceeds * stamp_tax_rate
                    pnl = net_proceeds - position * entry_price
                    pnl_pct = pnl / (position * entry_price) * 100
                    trades.append({
                        "买入日期": entry_date, "卖出日期": row["日期"],
                        "持有天数": days_held, "买入价": round(entry_price, 2),
                        "卖出价": round(sell_price, 2), "收益率(%)": round(pnl_pct, 2),
                        "盈亏金额": round(pnl, 2), "卖出原因": sell_reason,
                        "触发评分": round(total_score, 1)
                    })
                    capital += net_proceeds
                    equity_curve.append(capital)
                    position, entry_price, entry_date, entry_idx = 0, 0.0, None, -1

        # 开仓（T+1 开盘）
        if position == 0 and signal and (i + 1 < len(df)):
            next_i = i + 1
            next_open = df["开盘"].iloc[next_i]
            if not is_limit_up(next_open, row["收盘"]):
                buy_price_raw = next_open * (1 + slippage_rate)
                max_shares = int((capital * 0.95) / buy_price_raw / min_shares) * min_shares
                if max_shares >= min_shares:
                    cost = max_shares * buy_price_raw
                    total_cost = cost + cost * commission_rate
                    if total_cost <= capital:
                        position = max_shares
                        entry_price = buy_price_raw
                        entry_date = df["日期"].iloc[next_i]
                        entry_idx = next_i
                        capital -= total_cost

    # 期末强制平仓
    if position > 0:
        sell_price = df["收盘"].iloc[-1] * (1 - slippage_rate)
        proceeds = position * sell_price
        net_proceeds = proceeds - proceeds * commission_rate - proceeds * stamp_tax_rate
        pnl = net_proceeds - position * entry_price
        pnl_pct = pnl / (position * entry_price) * 100
        trades.append({
            "买入日期": entry_date, "卖出日期": df["日期"].iloc[-1],
            "持有天数": len(df) - 1 - entry_idx, "买入价": round(entry_price, 2),
            "卖出价": round(sell_price, 2), "收益率(%)": round(pnl_pct, 2),
            "盈亏金额": round(pnl, 2), "卖出原因": "期末强制平仓",
            "触发评分": "-"
        })
        capital += net_proceeds
        equity_curve.append(capital)

    trades_df = pd.DataFrame(trades)
    if trades_df.empty:
        return trades_df, None

    returns = trades_df["收益率(%)"].values
    win_trades = returns[returns > 0]
    loss_trades = returns[returns <= 0]
    win_rate = len(win_trades) / len(returns) * 100
    avg_return = float(np.mean(returns))
    avg_win = float(np.mean(win_trades)) if len(win_trades) > 0 else 0.0
    avg_loss = float(np.mean(loss_trades)) if len(loss_trades) > 0 else 0.0
    profit_factor = abs(avg_win * len(win_trades) / (avg_loss * len(loss_trades))) if len(loss_trades) > 0 and avg_loss != 0 else float("inf")
    expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)
    equity = np.array(equity_curve)
    peak = np.maximum.accumulate(equity)
    max_drawdown = float(((equity - peak) / peak * 100).min())

    summary = {
        "总交易次数": len(trades_df),
        "胜率(%)": round(win_rate, 1),
        "平均收益率(%)": round(avg_return, 2),
        "盈亏比": round(profit_factor, 2) if profit_factor != float("inf") else "∞",
        "期望值(%)": round(expectancy, 2),
        "最大回撤(%)": round(max_drawdown, 2),
        "最终资金": round(capital, 2),
        "总收益率(%)": round((capital / initial_capital - 1) * 100, 2)
    }
    return trades_df, summary

# ==================== 工作线程 ====================
def worker_task(code, name, row_data, exclude_limit, enable_weekly, macro_status):
    k_df = fetch_kline_safe(code, row_data, days=180)
    last_close = float(k_df['收盘'].iloc[-1])
    pct_today = float(k_df['涨跌幅'].iloc[-1]) if len(k_df) > 1 else float(row_data.get('涨跌幅', 0))
    if exclude_limit and pct_today >= 9.2:
        return None

    total, quality, timing, tags, advice, radar, timing_dict, chip_info, passed, risk_level = \
        evaluate_multi_period_score(k_df, row_data, enable_weekly, macro_status)
    if not passed or total < 55:
        return None

    amt_wan = float(row_data.get('成交额(万)', 0))
    strength = (amt_wan * (pct_today / 100.0) * 0.35) / max(amt_wan, 1) * 100

    k_df['MA5'] = k_df['收盘'].rolling(5).mean()
    k_df['MA10'] = k_df['收盘'].rolling(10).mean()
    k_df['MA20'] = k_df['收盘'].rolling(20).mean()
    if len(k_df) >= 60:
        k_df['MA60'] = k_df['收盘'].rolling(60).mean()

    t1_up_proxy = min(85, max(35, 50 + (timing - 20) * 1.2 + (quality - 20) * 0.6))

    return {
        "代码": code, "名称": name,
        "资金强度": f"{'+' if strength > 0 else ''}{strength:.1f}%强度",
        "综合评分": total, "质量分": quality, "时机分": timing, "风险": risk_level,
        "AI评级": "👑 S级短线" if total >= 85 and timing >= 28 else ("🔥 强力关注" if total >= 75 else "⭐ 观察"),
        "最新价": last_close, "涨跌幅(%)": round(pct_today, 2), "成交额(万)": int(amt_wan),
        "量化特征": " | ".join(tags) if tags else "多周期共振良好",
        "T+1上涨代理%": round(t1_up_proxy, 0),
        "advice": advice, "timing": timing_dict, "radar": radar, "chip_info": chip_info,
        "k_df": k_df, "row_data": row_data
    }

# ==================== 图表 ====================
def draw_pro_kline(code, name, k_df, advice):
    recent = k_df.tail(70).copy()
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.72, 0.28])
    fig.add_trace(go.Candlestick(x=recent['日期'], open=recent['开盘'], high=recent['最高'],
                                 low=recent['最低'], close=recent['收盘'],
                                 increasing_line_color='#ef5350', decreasing_line_color='#26a69a', name="K线"), row=1, col=1)
    for ma_col, color, label in [('MA5', '#ff9800', 'MA5'), ('MA10', '#2196f3', 'MA10'),
                                 ('MA20', '#9c27b0', 'MA20'), ('MA60', '#4caf50', 'MA60')]:
        if ma_col in recent.columns:
            fig.add_trace(go.Scatter(x=recent['日期'], y=recent[ma_col], line=dict(color=color, width=1.4), name=label), row=1, col=1)
    b_low = advice.get("建议买入区间_低", recent['收盘'].iloc[-1] * 0.98)
    b_high = advice.get("建议买入区间_高", recent['收盘'].iloc[-1])
    fig.add_hrect(y0=b_low, y1=b_high, fillcolor="rgba(0, 230, 118, 0.15)", line_width=0,
                  annotation_text=f"🎯 买入区间: {b_low}~{b_high}", annotation_position="top left", row=1, col=1)
    fig.add_hline(y=advice["动态压力位"], line_dash="dot", line_color="#ff1744", annotation_text=f"压力: {advice['动态压力位']}", row=1, col=1)
    fig.add_hline(y=advice["动态支撑位"], line_dash="dash", line_color="#00e676", annotation_text=f"支撑: {advice['动态支撑位']}", row=1, col=1)
    vol_colors = ['#ef5350' if c >= o else '#26a69a' for c, o in zip(recent['收盘'], recent['开盘'])]
    fig.add_trace(go.Bar(x=recent['日期'], y=recent['成交量'], marker_color=vol_colors, name="成交量"), row=2, col=1)
    fig.update_layout(title=f"📈 {code} {name} (最新 {recent['收盘'].iloc[-1]} 元 | ATR={advice.get('ATR',0)})",
                      xaxis_rangeslider_visible=False, height=480, margin=dict(l=10, r=10, t=35, b=10),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
    return fig

def draw_min_timeline(code, name, m_df):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.72, 0.28])
    fig.add_trace(go.Scatter(x=m_df['时间'], y=m_df['现价'], line=dict(color='#ffffff', width=1.5), name="分时白线"), row=1, col=1)
    fig.add_trace(go.Scatter(x=m_df['时间'], y=m_df['均价'], line=dict(color='#ffd600', width=1.5), name="均价黄线"), row=1, col=1)
    fig.add_trace(go.Bar(x=m_df['时间'], y=m_df['成交量'], marker_color='#29b6f6', name="分时量"), row=2, col=1)
    fig.update_layout(title=f"⚡ {code} {name} 今日实时分时", xaxis_rangeslider_visible=False,
                      height=450, margin=dict(l=10, r=10, t=35, b=10),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
    return fig

def draw_radar_chart(radar_data: dict):
    cats = list(radar_data.keys()) + [list(radar_data.keys())[0]]
    vals = list(radar_data.values()) + [list(radar_data.values())[0]]
    fig = go.Figure(go.Scatterpolar(r=vals, theta=cats, fill='toself',
                                    fillcolor='rgba(255, 75, 75, 0.35)', line=dict(color='#ff4b4b', width=2)))
    fig.update_layout(polar=dict(radialaxis=dict(visible=True, range=[0, 20])),
                      showlegend=False, height=220, margin=dict(l=25, r=25, t=25, b=25))
    return fig

# ==================== 扫描按钮 ====================
if st.button("🚀 启动全市场深度量化极速扫描", type="primary", use_container_width=True):
    t_start = time.time()
    with st.spinner("正在全景扫描 60 / 00 主板标的池..."):
        pool = get_all_realtime_stocks_tx(board_type, min_price, max_price, min_amount, exclude_limit_up)
    if len(pool) == 0:
        st.error("❌ 未找到符合条件的标的，请调宽【股价区间】或降低成交额门槛。")
        st.stop()

    candidates = pool[(pool['涨跌幅'] >= 0.0) & (pool['涨跌幅'] <= 7.5)].sort_values(
        by=["成交额(万)", "涨跌幅"], ascending=[False, False]).head(deep_sample_size)
    if len(candidates) < min(30, len(pool)):
        candidates = pool.sort_values(by=["成交额(万)", "涨跌幅"], ascending=[False, False]).head(deep_sample_size)

    hit_results, new_kline_cache = [], {}
    progress_bar = st.progress(0, text=f"正在深度分析 {len(candidates)} 只样本...")
    completed = 0
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(worker_task, str(row['代码']).zfill(6), row['名称'], row.to_dict(),
                                   exclude_limit_up, enable_weekly_filter, macro)
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

    hit_results = sorted(hit_results, key=lambda x: (x["综合评分"], x["时机分"], x["涨跌幅(%)"]), reverse=True)[:display_top_n]
    st.session_state['scan_results'] = hit_results
    st.session_state['kline_cache'] = new_kline_cache
    st.session_state['has_scanned'] = True
    elapsed = round(time.time() - t_start, 1)

    if auto_push_on_scan and wechat_key and hit_results:
        ok, msg = send_wechat_notification(wechat_key, f"AI量化共振买点严选 Top {len(hit_results)}", hit_results, macro)
        st.toast(f"📱 微信推送已发出！耗时 {elapsed} 秒" if ok else f"⚠️ {msg}", icon="🎉" if ok else "⚠️")
    else:
        st.toast(f"⚡ 深度扫描完毕！精选锁定 Top {len(hit_results)}，耗时 {elapsed} 秒", icon="🎉")

# ==================== 结果展示 ====================
tab_view_select, tab_view_portfolio = st.tabs(["🔥 AI 智能精选投研看板", "💼 我的自选与持仓浮盈监控池"])

with tab_view_select:
    if st.session_state.get('scan_results'):
        results = st.session_state['scan_results']
        kline_cache = st.session_state['kline_cache']
        res_df = pd.DataFrame(results)
        st.success(f"🎉 投研完成！已在 {deep_sample_size} 只深度样本中严选出 **Top {len(res_df)}** 只。")

        display_cols = ["代码", "名称", "综合评分", "质量分", "时机分", "风险", "T+1上涨代理%",
                        "最新价", "涨跌幅(%)", "资金强度", "AI评级"]
        st.dataframe(res_df[display_cols], use_container_width=True, hide_index=True)

        code_text = "\n".join([r["代码"] for r in results])
        c1, c2, c3 = st.columns(3)
        c1.download_button("📥 导出CSV", data=res_df.to_csv(index=False).encode('utf-8-sig'),
                           file_name=f"共振选股_{datetime.today().strftime('%Y%m%d')}.csv")
        c2.download_button("📌 导出自选(.txt)", data=code_text, file_name=f"自选_{datetime.today().strftime('%Y%m%d')}.txt")
        if c3.button("📲 手动推送微信"):
            if wechat_key:
                ok, msg = send_wechat_notification(wechat_key, f"AI量化买点严选 Top {len(results)}", results, macro)
                st.success("✅ 微信推送成功！") if ok else st.error(f"❌ {msg}")
            else:
                st.warning("👈 请先在左侧输入微信推送 Key")

        st.subheader("📊 个股决策中枢")
        selected_code = st.selectbox(
            "选择要诊断的股票：",
            options=[r["代码"] for r in results],
            format_func=lambda x: (f"[{next(r['综合评分'] for r in results if r['代码']==x)}分 | "
                                   f"质{next(r['质量分'] for r in results if r['代码']==x)}/"
                                   f"时{next(r['时机分'] for r in results if r['代码']==x)}] "
                                   f"{x} - {next(r['名称'] for r in results if r['代码']==x)}")
        )

        if selected_code and selected_code in kline_cache:
            s_name, s_df, s_adv, s_radar, s_timing, s_chip, s_row_data = kline_cache[selected_code]
            target_item = next(r for r in results if r['代码'] == selected_code)

            ca, cb, cc, cd, ce = st.columns(5)
            ca.metric("🎯 建议买入区间", s_adv["建议买入区间"])
            cb.metric("🛡️ 动态止损", s_adv["建议止损位"])
            cc.metric("🚀 第一目标", s_adv["第一止盈目标"])
            cd.metric("👑 衰减获利盘", f"{s_chip['获利盘比例(%)']}%")
            ce.metric("⚠️ 衰减套牢盘", f"{s_chip['套牢盘比例(%)']}%")

            st.info(f"💡 **最佳买入时机**：`{s_timing['最佳买入时机']}`\n\n" +
                    "\n".join([f"- {t}" for t in s_timing['买点战术详情']]))

            if target_item['时机分'] >= 28 and target_item['风险'] == "🟢 低":
                st.success("✅ 当前质量与时机匹配较好，可考虑在买入区间分批。")
            elif target_item['时机分'] < 20:
                st.warning("⏳ 股票质量尚可，但当前位置时机不佳，建议等待回踩 MA5 或分时企稳。")
            else:
                st.info("⚖️ 中性，轻仓试探或继续观察。")

            with st.expander("💼 一键加入【我的持仓监控池】"):
                cp1, cp2, cp3 = st.columns(3)
                buy_in_p = cp1.number_input("买入成本价 (元)", value=float(s_df['收盘'].iloc[-1]), step=0.01)
                buy_in_shares = cp2.number_input("买入股数 (股)", value=1000, step=100)
                if cp3.button("➕ 确认加入持仓监控"):
                    new_item = {
                        "代码": selected_code, "名称": s_name, "买入价": buy_in_p, "持股数": buy_in_shares,
                        "建议止损位": float(s_adv["建议止损位"].split(" ")[0]),
                        "第一止盈目标": float(s_adv["第一止盈目标"].split(" ")[0]),
                        "加入时间": datetime.today().strftime('%Y-%m-%d')
                    }
                    st.session_state['portfolio'] = [p for p in st.session_state['portfolio'] if p['代码'] != selected_code] + [new_item]
                    save_portfolio(st.session_state['portfolio'])
                    st.success(f"✅ 已成功将 [{selected_code}] {s_name} 加入持仓监控池！")

            r1, r2 = st.columns([1, 1.8])
            with r1:
                st.plotly_chart(draw_radar_chart(s_radar), use_container_width=True)
            with r2:
                st.markdown(
                    f"- **周线**：`{check_weekly_resonance(s_df)[1]}`\n"
                    f"- **筹码峰**：{s_chip['筹码峰']} 元 | 距峰 {s_chip['距筹码峰(%)']}%\n"
                    f"- **平均成本**：{s_chip['平均成本']} 元\n"
                    f"- **90%筹码宽度**：{s_chip['筹码90%宽度(%)']}%\n"
                    f"- **ATR(14)**：{s_adv.get('ATR', 0)}"
                )

            tab_kline, tab_min, tab_llm, tab_backtest = st.tabs([
                "📈 专业日K线与买入区间", "⚡ 今日实时分时", "🤖 AI 点评（诚实版）", "⏳ 真实历史回测"
            ])

            with tab_kline:
                st.plotly_chart(draw_pro_kline(selected_code, s_name, s_df, s_adv), use_container_width=True)
            with tab_min:
                min_df = fetch_min_timeline(selected_code)
                if min_df is not None and not min_df.empty:
                    st.plotly_chart(draw_min_timeline(selected_code, s_name, min_df), use_container_width=True)
                else:
                    st.info("⚠️ 当前非交易时间或分时加载中。")
            with tab_llm:
                with st.spinner("AI 正在生成技术点评..."):
                    llm_res = generate_ai_llm_analysis(selected_code, s_name, target_item, llm_api_key, llm_base_url)
                    st.markdown(f"### 📋 [{selected_code}] {s_name}\n{llm_res}")
            with tab_backtest:
                st.markdown("#### 回测参数（可调整）")
                bc1, bc2, bc3, bc4 = st.columns(4)
                bt_score_th = bc1.number_input("综合评分阈值", 40, 90, 58, 1)
                bt_timing_th = bc2.number_input("时机分阈值", 5, 35, 14, 1)
                bt_hold_days = bc3.number_input("最大持有天数", 1, 10, 5, 1)
                bt_capital = bc4.number_input("初始资金(元)", 10000, 1000000, 100000, 10000)

                if st.button("🔄 开始真实历史回测", type="primary"):
                    with st.spinner("正在进行真实历史模拟回测（评分已对齐实盘核心逻辑）..."):
                        bt_df, bt_summary = run_realistic_historical_backtest(
                            k_df=s_df,
                            signal_score_threshold=bt_score_th,
                            timing_score_threshold=bt_timing_th,
                            hold_days_max=bt_hold_days,
                            initial_capital=float(bt_capital)
                        )
                    if bt_df is not None and not bt_df.empty:
                        cols = st.columns(5)
                        cols[0].metric("总交易次数", bt_summary["总交易次数"])
                        cols[1].metric("胜率", f"{bt_summary['胜率(%)']}%")
                        cols[2].metric("平均收益", f"{bt_summary['平均收益率(%)']}%")
                        cols[3].metric("最大回撤", f"{bt_summary['最大回撤(%)']}%")
                        cols[4].metric("盈亏比", bt_summary["盈亏比"])
                        st.markdown(f"""
                        **期望值**: {bt_summary['期望值(%)']}%  
                        **最终资金**: {bt_summary['最终资金']:,.0f} 元  
                        **总收益率**: {bt_summary['总收益率(%)']}%
                        """)
                        st.dataframe(bt_df, use_container_width=True, hide_index=True)
                        st.caption("回测规则：仅用T日数据打分 → T+1开盘买入 → 涨跌停限制 + 滑点 + 佣金 + 印花税 + 100股整数倍 + ATR动态止盈止损。")
                    else:
                        st.warning(
                            "在当前股票的历史数据中未触发符合条件的买点。\n\n"
                            "建议：\n"
                            "1. 把「综合评分阈值」调低至 52～55\n"
                            "2. 把「时机分阈值」调低至 10～12\n"
                            "3. 该股历史走势较为平缓，符合强势共振的交易日天然较少"
                        )
                else:
                    st.info("点击上方按钮开始回测。评分逻辑已尽量对齐实盘，默认阈值更容易产生样本。")

    elif not st.session_state.get('has_scanned'):
        st.info("👈 请确认左侧策略与参数后，点击上方红色的 **“🚀 启动全市场深度量化极速扫描”** 按钮。")

with tab_view_portfolio:
    st.subheader("💼 我的自选与持仓浮盈监控池")
    st.caption("实时拉取最新价格，自动跟踪持仓盈亏，触及止损时红色告警")

    p_list = st.session_state.get('portfolio', [])
    if not p_list:
        st.info("💡 监控池暂无持仓。在精选看板选中股票后，点击【一键加入持仓监控】即可。")
    else:
        p_symbols = [f"sh{p['代码']}" if str(p['代码']).startswith("60") else f"sz{p['代码']}" for p in p_list]
        price_map = {}
        try:
            resp_p = requests.get(f"https://qt.gtimg.cn/q={','.join(p_symbols)}", timeout=2.0)
            for line in resp_p.text.strip().split(";"):
                if "=" in line:
                    code_cur = line.split("=")[0].split("_")[-1][2:]
                    p_cur = float(line.split("=")[1].strip('"').split("~")[3] or 0)
                    price_map[code_cur] = p_cur
        except Exception:
            pass

        p_rows = []
        total_pnl = 0.0
        for p in p_list:
            c_code = p['代码']
            cur_price = price_map.get(c_code, p['买入价'])
            buy_price, shares = p['买入价'], p['持股数']
            profit_pct = round((cur_price - buy_price) / buy_price * 100, 2)
            profit_amt = round((cur_price - buy_price) * shares, 2)
            total_pnl += profit_amt
            status_tag = "持仓中"
            if cur_price <= p['建议止损位']:
                status_tag = "🚨 触及止损位 (建议平仓)"
            elif cur_price >= p['第一止盈目标']:
                status_tag = "🎉 达到止盈位"
            p_rows.append({
                "代码": c_code, "名称": p['名称'], "买入成本": buy_price, "当前最新价": cur_price,
                "浮动盈亏(%)": f"{'+' if profit_pct > 0 else ''}{profit_pct}%",
                "浮动盈亏(元)": f"{'+' if profit_amt > 0 else ''}{profit_amt}",
                "建议止损位": p['建议止损位'], "第一止盈目标": p['第一止盈目标'],
                "风控状态": status_tag, "建仓日期": p['加入时间']
            })

        st.metric("当前持仓总浮动盈亏", f"{'+' if total_pnl >= 0 else ''}{total_pnl:,.2f} 元")
        st.dataframe(pd.DataFrame(p_rows), use_container_width=True, hide_index=True)

        st.write("---")
        del_c1, del_c2 = st.columns([2, 1])
        with del_c1:
            code_to_remove = st.selectbox(
                "选择要移出/平仓的个股：",
                [p['代码'] for p in p_list],
                format_func=lambda x: f"{x} - {next(p['名称'] for p in p_list if p['代码']==x)}"
            )
            if st.button("🗑️ 移出选中个股"):
                st.session_state['portfolio'] = [p for p in st.session_state['portfolio'] if p['代码'] != code_to_remove]
                save_portfolio(st.session_state['portfolio'])
                st.rerun()
        with del_c2:
            st.write("危险操作：")
            if st.button("🚨 一键清空所有持仓"):
                st.session_state['portfolio'] = []
                save_portfolio([])
                st.rerun()
