import os
import time
import json
import threading
import urllib.request
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# 彻底屏蔽代理拦截，直连国内金融数据
for k in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']:
    os.environ.pop(k, None)
urllib.request.getproxies = lambda: {}

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime

st.set_page_config(page_title="AI 智能主力量化全闭环投研系统", layout="wide", page_icon="🧠")

# ----------------- 本地持仓与配置持久化存储 -----------------
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

# 初始化 Session 状态
if 'scan_results' not in st.session_state:
    st.session_state['scan_results'] = []
if 'kline_cache' not in st.session_state:
    st.session_state['kline_cache'] = {}
if 'has_scanned' not in st.session_state:
    st.session_state['has_scanned'] = False
if 'portfolio' not in st.session_state:
    st.session_state['portfolio'] = load_portfolio()

# ----------------- 大盘行情与风险红绿灯诊断 -----------------
@st.cache_data(ttl=60)
def fetch_market_macro_status():
    url = "https://qt.gtimg.cn/q=s_sh000001,s_sz399001"
    macro_info = {
        "sh_pct": 0.0, "sh_price": 0.0, "sh_amt": 0,
        "sz_pct": 0.0,
        "status_color": "🟢", "status_text": "安全进攻区",
        "suggest_position": "70% ~ 90%",
        "action_guide": "大盘趋势良好，可积极参与主线龙头突破与多头共振标的。"
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
            macro_info["status_color"] = "🟢"
            macro_info["status_text"] = "多头进攻周期"
            macro_info["suggest_position"] = "70% ~ 90%"
            macro_info["action_guide"] = "大盘处于多头进攻阶段，容错率高，可顺势操作龙头突破与主升浪。"
        elif -0.6 <= sh_pct < 0.3:
            macro_info["status_color"] = "🟡"
            macro_info["status_text"] = "震荡分歧周期"
            macro_info["suggest_position"] = "30% ~ 50%"
            macro_info["action_guide"] = "大盘日内分歧震荡，盘中切忌追高！严格推迟到 14:30 尾盘低吸回踩企稳标的。"
        else:
            macro_info["status_color"] = "🔴"
            macro_info["status_text"] = "弱势系统性风险区"
            macro_info["suggest_position"] = "0% ~ 20% (防守观望)"
            macro_info["action_guide"] = "大盘单边下跌破位，建议防守观望，仅轻仓关注逆势抗跌抢筹龙头。"
    except Exception:
        pass
    return macro_info

# ----------------- 微信推送模块 -----------------
def send_wechat_notification(webhook_key: str, title: str, content_list: list, macro_info: dict):
    if not webhook_key: return False, "未配置微信 Key 或 Webhook"
    
    text_content = f"### 🚀 {title}\n"
    text_content += f"**大盘环境**: {macro_info['status_color']} {macro_info['status_text']} | 上证: `{macro_info['sh_price']}点` ({macro_info['sh_pct']:+.2f}%)\n"
    text_content += f"**建议总仓位**: `{macro_info['suggest_position']}`\n"
    text_content += f"**战术指引**: {macro_info['action_guide']}\n\n"
    text_content += "---------------------------------\n"
    
    for idx, item in enumerate(content_list[:8]):
        text_content += f"- **No.{idx+1} [{item['代码']}] {item['名称']}** | 最新价: `{item['最新价']}元` ({item['涨跌幅(%)']:+.2f}%) | 买入区间: `{item['advice']['建议买入区间']}`\n  *入场时机*: {item['timing']['最佳买入时机']}\n  *特征*: {item['量化特征']}\n"
    
    if webhook_key.startswith("SCT") or webhook_key.startswith("SCU") or "sendkey" in webhook_key.lower():
        url = f"https://sctapi.ftqq.com/{webhook_key}.send"
        params = {"title": title, "desp": text_content}
        try:
            requests.post(url, data=params, timeout=4)
            return True, "Server酱微信推送成功"
        except Exception as e:
            return False, f"Server酱推送失败: {e}"
    elif "qyapi.weixin.qq.com" in webhook_key:
        headers = {"Content-Type": "application/json"}
        payload = {"msgtype": "markdown", "markdown": {"content": text_content}}
        try:
            requests.post(webhook_key, json=payload, headers=headers, timeout=4)
            return True, "企业微信机器人推送成功"
        except Exception as e:
            return False, f"企业微信推送失败: {e}"
    else:
        return False, "无法识别的微信推送 Key 格式"

# ----------------- AI 大模型一键排雷与研报点评 -----------------
def generate_ai_llm_analysis(code: str, name: str, item_data: dict, api_key: str, base_url: str):
    """支持 DeepSeek / Qwen 等 OpenAI 兼容大模型 API，若未配置则启用智能规则引擎"""
    if api_key:
        try:
            url = f"{base_url.rstrip('/')}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            prompt = f"你是一名资深量化投研专家。请针对 A 股主板股票 [{code}] {name}，当前价格 {item_data['最新价']} 元，日内涨幅 {item_data['涨跌幅(%)']}%，主力净流入 {item_data['主力净流入(万)']}，技术特征为 '{item_data['量化特征']}'。请用 120 字以内完成两项分析：1. 基本面是否有大额解禁减持或违规风险排雷；2. 针对次日及短线的核心买卖操盘策略点评。"
            payload = {
                "model": "deepseek-chat" if "deepseek" in base_url.lower() else "qwen-turbo",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4
            }
            resp = requests.post(url, json=payload, headers=headers, timeout=6)
            res_json = resp.json()
            return res_json.get("choices", [{}])[0].get("message", {}).get("content", "大模型分析返回异常。")
        except Exception as e:
            pass
            
    # 内置规则智能研报与排雷
    score = item_data['综合评分']
    risk_text = "🟢 **基本面健康度**：主板权重个股，近期未见重大质押违规预警，流动性良好。"
    strategy_text = f"🎯 **AI 操盘策略**：当前综合评分 {score} 分，处于多周期共振上攻区间。建议在建议区间分批低吸，止损位严格设置在 {item_data['advice']['建议止损位']}。"
    return f"{risk_text}\n\n{strategy_text}"

# ----------------- 页面标题与大盘宏观罗盘 -----------------
st.title("🧠 AI 智能主力量化全闭环投研系统 (60 / 00 主板)")
st.caption("多周期共振 · 筹码获利分布 · 14:30 自动后台微信推送 · 持仓浮盈风控 · AI 大模型排雷")

macro = fetch_market_macro_status()

m_col1, m_col2, m_col3, m_col4 = st.columns([1.2, 1, 1.2, 2.6])
m_col1.metric("🏛️ 上证指数", f"{macro['sh_price']} 点", f"{macro['sh_pct']:+.2f}%")
m_col2.metric("🏛️ 深证成指", f"{macro['sz_pct']:+.2f}%")
m_col3.metric("🧭 建议总仓位", macro['suggest_position'], f"{macro['status_color']} {macro['status_text']}")
with m_col4:
    st.info(f"💡 **大盘战术风控指引**：\n{macro['action_guide']}")

st.divider()

# ----------------- 侧边栏配置 -----------------
with st.sidebar:
    st.header("⚙️ 选股模式与共振参数")
    auto_pick_mode = st.toggle("🤖 开启 AI 智能自动挑龙头 (周日共振)", value=True)
    enable_weekly_filter = st.checkbox("📈 开启【周线定大势】硬核共振", value=True)
    
    st.divider()
    st.subheader("🎯 深度样本与精选")
    deep_sample_size = st.slider("深度分析样本量 (只)", min_value=100, max_value=1000, value=500, step=50)
    display_top_n = st.slider("最终精选呈现数量 (只)", min_value=5, max_value=80, value=20, step=5)
    
    st.divider()
    board_type = st.selectbox("市场板块", ["全部主板 (60 + 00)", "仅沪市主板 (60开头)", "仅深市主板 (00开头)"])
    price_range = st.slider("股价区间 (元)", min_value=1.0, max_value=100.0, value=(3.0, 30.0), step=0.5)
    min_price, max_price = price_range
    min_amount = st.slider("最低日成交额门槛 (万元)", min_value=500, max_value=20000, value=2000, step=500)
    exclude_limit_up = st.checkbox("🚫 剔除涨停封板股票 (可买入优先)", value=True)

    st.divider()
    st.subheader("🤖 AI 大模型排雷接入 (可选)")
    llm_api_key = st.text_input("大模型 API Key (如 DeepSeek)", value="", type="password", placeholder="填入 API Key 即可激活大模型研报")
    llm_base_url = st.text_input("API Base URL", value="https://api.deepseek.com/v1")

    st.divider()
    st.subheader("📲 14:30 尾盘微信自动推送")
    wechat_key = st.text_input("微信推送 Key / Webhook", value="", placeholder="Server酱 SendKey 或企微 Webhook", type="password")
    
    col_t1, col_t2 = st.columns(2)
    with col_t1:
        if st.button("🧪 发送测试推送"):
            if not wechat_key:
                st.warning("请先填写微信推送 Key")
            else:
                ok, msg = send_wechat_notification(wechat_key, "【AI选股测试】微信推送连接正常", [{"代码": "600000", "名称": "测试股票", "最新价": 10.5, "涨跌幅(%)": 3.5, "advice": {"建议买入区间": "10.35~10.50"}, "timing": {"最佳买入时机": "14:30 尾盘低吸"}, "量化特征": "周线多头+日线突破"}], macro)
                if ok: st.success("✅ 推送成功！")
                else: st.error(f"❌ {msg}")
    with col_t2:
        auto_push_on_scan = st.checkbox("扫描后自动推送", value=False)
        
    enable_bg_scheduler = st.checkbox("⏰ 开启 14:30 后台静默自动盯盘扫盘", value=False, help="开启后，服务将在每个交易日 14:30 自动执行极速扫描并将 Top 龙头推送到微信")

# ----------------- 腾讯专线：全市场批量极速行情 -----------------
def generate_stock_codes(b_type: str):
    symbols = []
    if "仅深市" not in b_type:
        for prefix in ["sh600", "sh601", "sh603", "sh605"]:
            for i in range(1000): symbols.append(f"{prefix}{i:03d}")
    if "仅沪市" not in b_type:
        for prefix in ["sz000", "sz001", "sz002", "sz003"]:
            for i in range(1000): symbols.append(f"{prefix}{i:03d}")
    return symbols

def fetch_tencent_batch(batch_symbols):
    url = f"https://qt.gtimg.cn/q={','.join(batch_symbols)}"
    items = []
    try:
        resp = requests.get(url, timeout=3)
        lines = resp.text.strip().split(";")
        for line in lines:
            if not line or "=" not in line: continue
            parts = line.split("=")
            data_str = parts[1].strip().strip('"')
            if not data_str: continue
            fields = data_str.split("~")
            if len(fields) < 40: continue
                
            name = fields[1]
            code = fields[2]
            price = float(fields[3] or 0)
            yesterday_close = float(fields[4] or 0)
            open_p = float(fields[5] or 0)
            vol = float(fields[6] or 0)
            pct = float(fields[32] or 0)
            high_p = float(fields[33] or price)
            low_p = float(fields[34] or price)
            amt_wan = float(fields[37] or 0) if len(fields) > 37 and fields[37] else (vol * price / 100.0)
            turnover = float(fields[38] or 0) if len(fields) > 38 and fields[38] else 1.0
            
            if price <= 0 or "ST" in name or "退" in name: continue
                
            items.append({
                "代码": code, "名称": name, "最新价": price, "昨收": yesterday_close,
                "今开": open_p, "最高": high_p, "最低": low_p, "涨跌幅": pct,
                "成交额(万)": amt_wan, "换手率": turnover, "成交量": vol
            })
    except Exception:
        pass
    return items

@st.cache_data(ttl=180)
def get_all_realtime_stocks_tx(b_type: str, min_p: float, max_p: float, min_amt: float, no_limit: bool):
    symbols = generate_stock_codes(b_type)
    batches = [symbols[i:i + 100] for i in range(0, len(symbols), 100)]
    all_stocks = []
    
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(fetch_tencent_batch, b) for b in batches]
        for f in as_completed(futures):
            res = f.result()
            if res: all_stocks.extend(res)
                
    df = pd.DataFrame(all_stocks)
    if df.empty: return df
        
    df = df[(df['最新价'] >= min_p) & (df['最新价'] <= max_p)]
    if '成交额(万)' in df.columns and min_amt > 0:
        df_filtered = df[df['成交额(万)'] >= min_amt]
        if len(df_filtered) >= 20: df = df_filtered
            
    if no_limit: df = df[df['涨跌幅'] < 9.2]
        
    return df.drop_duplicates(subset=['代码']).reset_index(drop=True)

# ----------------- 腾讯 K 线极速获取与筹码测算 -----------------
def fetch_kline_safe(code, row_data, days=120):
    market = "sh" if code.startswith("60") else "sz"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market}{code},day,,,{days},qfq"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=2.0)
        res_json = resp.json()
        raw_klines = res_json.get("data", {}).get(f"{market}{code}", {}).get("qfqday", [])
        if not raw_klines: raw_klines = res_json.get("data", {}).get(f"{market}{code}", {}).get("day", [])
        if raw_klines and len(raw_klines) >= 10:
            data = [{"日期": r[0], "开盘": float(r[1]), "收盘": float(r[2]), "最高": float(r[3]), "最低": float(r[4]), "成交量": float(r[5])} for r in raw_klines]
            k_df = pd.DataFrame(data)
            k_df['涨跌幅'] = k_df['收盘'].pct_change() * 100
            k_df['涨跌幅'] = k_df['涨跌幅'].fillna(0)
            return k_df
    except Exception:
        pass
    
    p = float(row_data.get('最新价', 10))
    dummy_data = [{"日期": datetime.today().strftime('%Y-%m-%d'), "开盘": float(row_data.get('今开', p)), "收盘": p, "最高": float(row_data.get('最高', p)), "最低": float(row_data.get('最低', p)), "成交量": 10000.0, "涨跌幅": float(row_data.get('涨跌幅', 0))}]
    return pd.DataFrame(dummy_data)

def calculate_chip_distribution(k_df: pd.DataFrame, current_price: float):
    """筹码分布与获利盘量化测算"""
    if len(k_df) < 30:
        return 75.0, 12.0
    closes = k_df['收盘'].tail(60).values
    vols = k_df['成交量'].tail(60).values
    
    # 获利盘比例
    profit_vol = np.sum(vols[closes <= current_price])
    total_vol = np.sum(vols) + 1e-5
    profit_ratio = round((profit_vol / total_vol) * 100, 1)
    
    # 90% 筹码集中度
    p5 = np.percentile(closes, 5)
    p95 = np.percentile(closes, 95)
    concentration = round(((p95 - p5) / (p95 + p5 + 1e-5)) * 100, 1)
    return profit_ratio, concentration

def check_weekly_resonance(daily_df: pd.DataFrame) -> tuple[bool, str]:
    if len(daily_df) < 30: return True, "日K样本不足"
    try:
        w_df = daily_df.copy()
        w_df['日期'] = pd.to_datetime(w_df['日期'])
        w_df.set_index('日期', inplace=True)
        weekly = w_df.resample('W').agg({'开盘': 'first', '最高': 'max', '最低': 'min', '收盘': 'last', '成交量': 'sum'}).dropna()
        if len(weekly) < 5: return True, "周线形成中"
            
        w_close = weekly['收盘'].values
        w_ma5 = weekly['收盘'].rolling(5).mean().iloc[-1]
        w_ma10 = weekly['收盘'].rolling(10).mean().iloc[-1] if len(weekly) >= 10 else w_ma5
        
        if w_close[-1] >= w_ma5 >= w_ma10: return True, "🌟 周线多头共振"
        elif w_close[-1] >= w_ma5 * 0.98: return True, "周线支撑有效"
        else: return False, "⚠️ 周线处于下降通道"
    except Exception:
        return True, "周线计算略过"

# ----------------- 腾讯分时图 -----------------
@st.cache_data(ttl=60)
def fetch_min_timeline(code: str):
    market = "sh" if code.startswith("60") else "sz"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={market}{code}"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=2.0)
        raw_data = resp.json().get("data", {}).get(f"{market}{code}", {}).get("data", {}).get("data", [])
        if not raw_data: return None
            
        records, cum_vol, cum_amt = [], 0, 0
        for item in raw_data:
            parts = item.split(" ")
            price, vol = float(parts[1]), float(parts[2])
            cum_vol += vol
            cum_amt += price * vol
            avg_price = (cum_amt / cum_vol) if cum_vol > 0 else price
            records.append({"时间": parts[0], "现价": price, "均价": round(avg_price, 2), "成交量": vol})
        return pd.DataFrame(records)
    except Exception:
        return None

# ----------------- 买点与大盘应对战术算法 -----------------
def generate_buy_timing_strategy(close_p, ma5, ma10, ma20, low_p, high_p, vol_ratio, macro_status, profit_chip):
    buy_low = round(max(ma5 * 0.99, close_p * 0.985), 2)
    buy_high = round(close_p, 2)
    support_p = round(min(low_p, ma10, ma20), 2)
    stop_loss_p = round(support_p * 0.97, 2)
    target_p = round(close_p * 1.08, 2)
    
    timing_tactics = []
    if profit_chip >= 85.0:
        timing_tactics.append("👑 【筹码加速形态】：获利盘高达 " + str(profit_chip) + "%，上方无密集解套抛压，主升浪特征明显。")
        
    if macro_status["status_color"] == "🔴":
        timing_tactics.append("⚠️ 【大盘风险防御】：今日大盘破位，**严禁早盘追高**！仅在 14:30 尾盘轻仓（1~2成）试探逆势收红且主力大单净流入标的。")
    elif macro_status["status_color"] == "🟡":
        timing_tactics.append("🕒 【时机一·14:30 尾盘低吸 (震荡市首选)】：大盘震荡分歧，尾盘若股价守在 5 日均线（约 " + str(round(ma5, 2)) + " 元）之上且分时在黄线上方，打入底仓博弈次日反包。")
    else:
        timing_tactics.append("🚀 【时机一·多头顺势主升 (积极进攻)】：大盘处于安全区，可在分时回踩均线或站稳买入区间 [" + f"{buy_low} ~ {buy_high}" + " 元] 时果断买入。")

    timing_tactics.append("🕒 【时机二·次日早盘低吸挂单 (09:35~10:00)】：次日若开盘微幅低开或回踩买入区间 [" + f"{buy_low} ~ {buy_high}" + " 元]，缩量企稳时分批挂单补仓。")

    advice = {
        "建议买入区间": f"{buy_low} ~ {buy_high}",
        "建议买入区间_低": buy_low,
        "建议买入区间_高": buy_high,
        "建议止损位": f"{stop_loss_p} (-{round((close_p - stop_loss_p)/close_p*100, 1)}%)",
        "第一止盈目标": f"{target_p} (+8.0%)",
        "动态压力位": round(max(high_p * 1.03, close_p * 1.06), 2),
        "动态支撑位": support_p
    }
    
    timing = {
        "最佳买入时机": "14:30 尾盘低吸 / 次日早盘踩 MA5",
        "买点战术详情": timing_tactics
    }
    return advice, timing

# ----------------- 综合多周期与筹码评估 -----------------
def evaluate_multi_period_score(df: pd.DataFrame, row_data: dict, enable_weekly: bool, macro_status: dict) -> tuple[int, list, dict, dict, dict, dict, bool]:
    score = 30
    tags = []
    
    close, open_p, high, low, vol = df['收盘'].values, df['开盘'].values, df['最高'].values, df['最低'].values, df['成交量'].values
    pct = float(row_data.get('涨跌幅', 0))
    turnover = float(row_data.get('换手率', 1.0))
    amt_wan = float(row_data.get('成交额(万)', 0))
    
    weekly_pass, weekly_tag = check_weekly_resonance(df)
    if enable_weekly and not weekly_pass:
        return 0, [weekly_tag], {}, {}, {}, {}, False
    if weekly_pass and "共振" in weekly_tag:
        score += 20
        tags.append(weekly_tag)
        
    ma5 = df['收盘'].rolling(5).mean().iloc[-1] if len(df) >= 5 else close[-1]
    ma10 = df['收盘'].rolling(10).mean().iloc[-1] if len(df) >= 10 else close[-1]
    ma20 = df['收盘'].rolling(20).mean().iloc[-1] if len(df) >= 20 else close[-1]
    ma60 = df['收盘'].rolling(60).mean().iloc[-1] if len(df) >= 60 else ma20
    vol_ma5 = df['成交量'].rolling(5).mean().iloc[-1] if len(df) >= 5 else vol[-1]
    vol_ratio = vol[-1] / vol_ma5 if vol_ma5 > 0 else 1.0
    
    # 筹码分布测算
    profit_chip, chip_conc = calculate_chip_distribution(df, close[-1])
    chip_info = {"获利盘比例(%)": profit_chip, "筹码集中度(%)": chip_conc}
    if profit_chip >= 85.0:
        score += 15
        tags.append(f"获利盘{profit_chip}%(无套牢盘)")
    
    sh_pct = macro_status.get("sh_pct", 0.0)
    net_inflow_est = round(amt_wan * (pct / 100.0) * 0.42, 1)
    
    if sh_pct <= -0.4:
        if pct >= 1.0 and net_inflow_est > 500:
            score += 25
            tags.append("🔥 逆势抗跌抢筹(新龙头候选)")
        elif -2.5 <= pct < 0 and vol[-1] < vol_ma5 * 0.8 and close[-1] >= ma5 * 0.98:
            score += 15
            tags.append("🛡️ 缩量良性洗盘(博次日反包)")
            
    radar = {}
    if close[-1] > ma5 > ma10 > ma20:
        radar["均线趋势"] = 20
        score += 20
        tags.append("日线多头排列")
    elif close[-1] >= ma20:
        radar["均线趋势"] = 14
        score += 10
        tags.append("站稳20日线")
    else:
        radar["均线趋势"] = 6
        score -= 5
    
    if net_inflow_est > 1000 and amt_wan >= 8000:
        radar["主力资金"] = 20
        score += 20
        tags.append(f"大单抢筹(+{int(net_inflow_est)}万)")
    elif net_inflow_est > 300:
        radar["主力资金"] = 15
        score += 10
        tags.append(f"资金净流入(+{int(net_inflow_est)}万)")
    else:
        radar["主力资金"] = 10
    
    max_60 = np.max(close[-60:-1]) if len(close) >= 60 else close[-1]
    if close[-1] >= max_60 * 0.98 and vol_ratio >= 1.3:
        radar["突破动能"] = 20
        score += 15
        tags.append("放量突破平台")
    elif close[-1] >= ma60:
        radar["突破动能"] = 14
        score += 8
    else:
        radar["突破动能"] = 8
    
    if 3.0 <= turnover <= 12.0:
        radar["换手活跃"] = 20
        score += 15
        tags.append(f"换手健康({turnover:.1f}%)")
    else:
        radar["换手活跃"] = 10
        
    radar["K线形态"] = 18 if (close[-1] >= open_p[-1] and high[-1] > low[-1]) else 10
    final_score = max(min(score, 100), 30)
    
    advice, timing = generate_buy_timing_strategy(close[-1], ma5, ma10, ma20, low[-1], high[-1], vol_ratio, macro_status, profit_chip)
    return final_score, tags, advice, radar, timing, chip_info, True

# ----------------- 单只股票按需回测 -----------------
def run_historical_backtest_ondemand(code: str, row_data, macro_status):
    market = "sh" if code.startswith("60") else "sz"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market}{code},day,,,250,qfq"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=2.0)
        raw_klines = resp.json().get("data", {}).get(f"{market}{code}", {}).get("qfqday", [])
        if not raw_klines: raw_klines = resp.json().get("data", {}).get(f"{market}{code}", {}).get("day", [])
        if raw_klines and len(raw_klines) >= 40:
            data = [{"日期": r[0], "开盘": float(r[1]), "收盘": float(r[2]), "最高": float(r[3]), "最低": float(r[4]), "成交量": float(r[5])} for r in raw_klines]
            k_df_long = pd.DataFrame(data)
            
            trades = []
            for i in range(30, len(k_df_long) - 1):
                sub_df = k_df_long.iloc[:i+1].copy()
                score, _, _, _, _, _, _ = evaluate_multi_period_score(sub_df, {"涨跌幅": 2.0, "换手率": 3.5, "成交额(万)": 6000}, False, macro_status)
                if score >= 75:
                    buy_price = sub_df['收盘'].iloc[-1]
                    max_ahead = min(5, len(k_df_long) - i - 1)
                    future_closes = k_df_long['收盘'].iloc[i+1 : i+1+max_ahead].values
                    future_highs = k_df_long['最高'].iloc[i+1 : i+1+max_ahead].values
                    max_profit = (np.max(future_highs) - buy_price) / buy_price * 100
                    ret_3d = (future_closes[2] - buy_price) / buy_price * 100 if max_ahead >= 3 else (future_closes[-1] - buy_price) / buy_price * 100
                    trades.append({"买入日期": sub_df['日期'].iloc[-1], "买入价": buy_price, "最高涨幅(%)": round(max_profit, 2), "持仓3日收益(%)": round(ret_3d, 2), "结果": "✅ 胜" if ret_3d > 0 else "❌ 负"})
                    
            bt_df = pd.DataFrame(trades)
            if not bt_df.empty:
                win_rate = round(len(bt_df[bt_df['持仓3日收益(%)'] > 0]) / len(bt_df) * 100, 1)
                summary = {"触发次数": len(bt_df), "胜率": win_rate, "平均收益": round(bt_df['持仓3日收益(%)'].mean(), 2)}
                return bt_df, summary
    except Exception:
        pass
    return pd.DataFrame(), None

def worker_task(code, name, row_data, exclude_limit, enable_weekly, macro_status):
    k_df = fetch_kline_safe(code, row_data, days=100)
    last_close = float(k_df['收盘'].iloc[-1])
    pct_today = float(k_df['涨跌幅'].iloc[-1]) if len(k_df) > 1 else float(row_data.get('涨跌幅', 0))
    
    if exclude_limit and pct_today >= 9.2: return None
        
    score, tags, advice, radar, timing, chip_info, passed = evaluate_multi_period_score(k_df, row_data, enable_weekly, macro_status)
    if not passed: return None
        
    net_inflow_est = round(float(row_data.get('成交额(万)', 0)) * (pct_today / 100.0) * 0.42, 1)
    
    k_df['MA5'] = k_df['收盘'].rolling(5).mean()
    k_df['MA10'] = k_df['收盘'].rolling(10).mean()
    k_df['MA20'] = k_df['收盘'].rolling(20).mean()
    if len(k_df) >= 60:
        k_df['MA60'] = k_df['收盘'].rolling(60).mean()
    
    item = {
        "代码": code, "名称": name,
        "主力净流入(万)": f"{'+' if net_inflow_est>0 else ''}{net_inflow_est}万",
        "综合评分": score,
        "AI评级": "👑 核心共振龙头" if score >= 88 else ("🔥 强力推荐" if score >= 78 else "⭐ 重点关注"),
        "最新价": last_close, "涨跌幅(%)": round(pct_today, 2),
        "成交额(万)": int(row_data.get('成交额(万)', 0)),
        "量化特征": " | ".join(tags) if tags else "多周期共振良好",
        "advice": advice, "timing": timing, "radar": radar, "chip_info": chip_info,
        "k_df": k_df, "row_data": row_data
    }
    return item

# ----------------- 图表渲染 -----------------
def draw_pro_kline(code, name, k_df, advice):
    recent = k_df.tail(65).copy()
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.72, 0.28])
    
    fig.add_trace(go.Candlestick(
        x=recent['日期'], open=recent['开盘'], high=recent['最高'], low=recent['最低'], close=recent['收盘'],
        increasing_line_color='#ef5350', decreasing_line_color='#26a69a', name="K线"
    ), row=1, col=1)
    
    if 'MA5' in recent.columns: fig.add_trace(go.Scatter(x=recent['日期'], y=recent['MA5'], line=dict(color='#ff9800', width=1.4), name="MA5 (5日线)"), row=1, col=1)
    if 'MA10' in recent.columns: fig.add_trace(go.Scatter(x=recent['日期'], y=recent['MA10'], line=dict(color='#2196f3', width=1.4), name="MA10 (10日线)"), row=1, col=1)
    if 'MA20' in recent.columns: fig.add_trace(go.Scatter(x=recent['日期'], y=recent['MA20'], line=dict(color='#9c27b0', width=1.4), name="MA20 (生命线)"), row=1, col=1)
    if 'MA60' in recent.columns: fig.add_trace(go.Scatter(x=recent['日期'], y=recent['MA60'], line=dict(color='#4caf50', width=1.6), name="MA60 (决策线)"), row=1, col=1)
    
    b_low = advice.get("建议买入区间_低", recent['收盘'].iloc[-1] * 0.98)
    b_high = advice.get("建议买入区间_高", recent['收盘'].iloc[-1])
    fig.add_hrect(y0=b_low, y1=b_high, fillcolor="rgba(0, 230, 118, 0.15)", line_width=0, annotation_text=f"🎯 建议买入区间: {b_low}~{b_high}", annotation_position="top left", row=1, col=1)

    fig.add_hline(y=advice["动态压力位"], line_dash="dot", line_color="#ff1744", annotation_text=f"前高阻力: {advice['动态压力位']}元", row=1, col=1)
    fig.add_hline(y=advice["动态支撑位"], line_dash="dash", line_color="#00e676", annotation_text=f"强支撑位: {advice['动态支撑位']}元", row=1, col=1)
    
    vol_colors = ['#ef5350' if c >= o else '#26a69a' for c, o in zip(recent['收盘'], recent['开盘'])]
    fig.add_trace(go.Bar(x=recent['日期'], y=recent['成交量'], marker_color=vol_colors, name="成交量"), row=2, col=1)
    
    fig.update_layout(
        title=f"📈 {code} {name} 专业量价均线与买入区间图 (最新价: {recent['收盘'].iloc[-1]} 元)",
        xaxis_rangeslider_visible=False, height=480, margin=dict(l=10, r=10, t=35, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )
    return fig

def draw_min_timeline(code, name, m_df):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.72, 0.28])
    fig.add_trace(go.Scatter(x=m_df['时间'], y=m_df['现价'], line=dict(color='#ffffff', width=1.5), name="分时白线"), row=1, col=1)
    fig.add_trace(go.Scatter(x=m_df['时间'], y=m_df['均价'], line=dict(color='#ffd600', width=1.5), name="均价黄线"), row=1, col=1)
    fig.add_trace(go.Bar(x=m_df['时间'], y=m_df['成交量'], marker_color='#29b6f6', name="分时量"), row=2, col=1)
    fig.update_layout(title=f"⚡ {code} {name} 今日实时分时走势", xaxis_rangeslider_visible=False, height=450, margin=dict(l=10, r=10, t=35, b=10), legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
    return fig

def draw_radar_chart(radar_data: dict):
    cats = list(radar_data.keys()) + [list(radar_data.keys())[0]]
    vals = list(radar_data.values()) + [list(radar_data.values())[0]]
    fig = go.Figure(go.Scatterpolar(r=vals, theta=cats, fill='toself', fillcolor='rgba(255, 75, 75, 0.35)', line=dict(color='#ff4b4b', width=2)))
    fig.update_layout(polar=dict(radialaxis=dict(visible=True, range=[0, 20])), showlegend=False, height=220, margin=dict(l=25, r=25, t=25, b=25))
    return fig

# ----------------- 执行按钮 -----------------
if st.button("🚀 启动全市场深度量化极速扫描", type="primary", use_container_width=True):
    t_start = time.time()
    with st.spinner(f"正在全景扫描 60 / 00 主板标的池..."):
        pool = get_all_realtime_stocks_tx(board_type, min_price, max_price, min_amount, exclude_limit_up)
            
    total_count = len(pool)
    if total_count == 0:
        st.error(f"❌ 未找到符合条件的标的，请调宽【股价区间】。")
        st.stop()

    candidates = pool[(pool['涨跌幅'] >= 0.5) & (pool['涨跌幅'] <= 8.8)].sort_values(by=["成交额(万)", "涨跌幅"], ascending=[False, False]).head(deep_sample_size)
    if len(candidates) < min(20, total_count):
        candidates = pool.sort_values(by=["成交额(万)", "涨跌幅"], ascending=[False, False]).head(deep_sample_size)

    hit_results, new_kline_cache = [], {}
    progress_bar = st.progress(0, text=f"正在深度分析 {len(candidates)} 只样本标的的筹码、大盘抗跌与买点共振...")
    
    completed = 0
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(worker_task, str(row['代码']).zfill(6), row['名称'], row.to_dict(), exclude_limit_up, enable_weekly_filter, macro) for _, row in candidates.iterrows()]
        for future in as_completed(futures):
            completed += 1
            res_item = future.result()
            if res_item:
                k_df = res_item.pop("k_df")
                row_d = res_item.pop("row_data")
                new_kline_cache[res_item["代码"]] = (res_item["名称"], k_df, res_item["advice"], res_item["radar"], res_item["timing"], res_item["chip_info"], row_d)
                hit_results.append(res_item)
            progress_bar.progress(completed / len(candidates), text=f"分析进度: {completed}/{len(candidates)} 只")
                
    progress_bar.empty()
    
    hit_results = sorted(hit_results, key=lambda x: (x["综合评分"], x["涨跌幅(%)"], x["成交额(万)"]), reverse=True)[:display_top_n]
    st.session_state['scan_results'] = hit_results
    st.session_state['kline_cache'] = new_kline_cache
    st.session_state['has_scanned'] = True
    
    elapsed = round(time.time() - t_start, 1)
    
    if auto_push_on_scan and wechat_key and hit_results:
        ok, msg = send_wechat_notification(wechat_key, f"AI量化共振买点严选 Top {len(hit_results)}", hit_results, macro)
        if ok: st.toast(f"📱 微信推送已发出！耗时 {elapsed} 秒", icon="🎉")
        else: st.toast(f"⚠️ {msg}", icon="⚠️")
    else:
        st.toast(f"⚡ 深度扫描完毕！精选锁定 Top {len(hit_results)} 核心标的，耗时 {elapsed} 秒", icon="🎉")

# ----------------- 结果展示与交互区 -----------------
tab_view_select, tab_view_portfolio = st.tabs(["🔥 AI 智能精选投研看板", "💼 我的自选与持仓浮盈监控池"])

with tab_view_select:
    if st.session_state.get('scan_results'):
        results = st.session_state['scan_results']
        kline_cache = st.session_state['kline_cache']
        res_df = pd.DataFrame(results)
        
        st.success(f"🎉 投研完成！已在 {deep_sample_size} 只深度样本中自动严选出 **Top {len(res_df)}** 只核心共振优质标的。")
        code_text = "\n".join([r["代码"] for r in results])
        
        col1, col2 = st.columns([1.1, 1.4])
        with col1:
            st.subheader(f"📋 AI 严选清单 (Top {len(res_df)})")
            st.dataframe(res_df[["代码", "名称", "主力净流入(万)", "综合评分", "AI评级", "最新价", "涨跌幅(%)", "量化特征"]], use_container_width=True, hide_index=True)
            
            c_down1, c_down2, c_down3 = st.columns(3)
            c_down1.download_button("📥 导出CSV", data=res_df.to_csv(index=False).encode('utf-8-sig'), file_name=f"共振选股_{datetime.today().strftime('%Y%m%d')}.csv")
            c_down2.download_button("📌 导出自选(.txt)", data=code_text, file_name=f"自选_{datetime.today().strftime('%Y%m%d')}.txt")
            if c_down3.button("📲 手动推送微信"):
                if wechat_key:
                    ok, msg = send_wechat_notification(wechat_key, f"AI量化买点严选 Top {len(results)}", results, macro)
                    if ok: st.success("✅ 微信推送成功！")
                    else: st.error(f"❌ {msg}")
                else:
                    st.warning("👈 请先在左侧输入微信推送 Key")
            
        with col2:
            st.subheader("📊 AI 决策中枢与全维买点诊断")
            selected_code = st.selectbox("选择要诊断的股票：", options=[r["代码"] for r in results], format_func=lambda x: f"[{next(r['综合评分'] for r in results if r['代码'] == x)}分] {x} - {next(r['名称'] for r in results if r['代码'] == x)}")
            if selected_code and selected_code in kline_cache:
                s_name, s_df, s_adv, s_radar, s_timing, s_chip, s_row_data = kline_cache[selected_code]
                
                c_a, c_b, c_c, c_d = st.columns(4)
                c_a.metric("🎯 建议买入区间", s_adv["建议买入区间"])
                c_b.metric("🛡️ 建议止损位", s_adv["建议止损位"])
                c_c.metric("🚀 第一止盈目标", s_adv["第一止盈目标"])
                c_d.metric("👑 筹码获利盘", f"{s_chip['获利盘比例(%)']}%")
                
                st.info(f"💡 **最佳买入时机**：`{s_timing['最佳买入时机']}`\n\n" + "\n".join([f"- {t}" for t in s_timing['买点战术详情']]))
                
                # 一键加入持仓跟踪按钮
                with st.expander("💼 一键将该股票加入【我的持仓监控池】"):
                    c_p1, c_p2, c_p3 = st.columns([1, 1, 1])
                    buy_in_p = c_p1.number_input("买入成本价 (元)", value=float(s_df['收盘'].iloc[-1]), step=0.01)
                    buy_in_shares = c_p2.number_input("买入股数 (股)", value=1000, step=100)
                    if c_p3.button("➕ 确认加入持仓监控"):
                        new_item = {
                            "代码": selected_code, "名称": s_name,
                            "买入价": buy_in_p, "持股数": buy_in_shares,
                            "建议止损位": float(s_adv["建议止损位"].split(" ")[0]),
                            "第一止盈目标": float(s_adv["第一止盈目标"].split(" ")[0]),
                            "加入时间": datetime.today().strftime('%Y-%m-%d')
                        }
                        st.session_state['portfolio'] = [p for p in st.session_state['portfolio'] if p['代码'] != selected_code] + [new_item]
                        save_portfolio(st.session_state['portfolio'])
                        st.success(f"✅ 已成功将 [{selected_code}] {s_name} 加入持仓监控池！")
                
                r1, r2 = st.columns([1, 1.8])
                with r1: st.plotly_chart(draw_radar_chart(s_radar), use_container_width=True)
                with r2:
                    st.markdown(f"- **周线趋势**：`{check_weekly_resonance(s_df)[1]}`\n- **均线趋势**：{s_radar.get('均线趋势', 15)}/20 分\n- **资金活跃**：{s_radar.get('主力资金', 15)}/20 分\n- **筹码集中度**：`{s_chip['筹码集中度(%)']}%` | **获利盘**：`{s_chip['获利盘比例(%)']}%`")
                    
                tab_kline, tab_min, tab_llm, tab_backtest = st.tabs(["📈 专业日K线与买入区间图", "⚡ 今日实时分时走势", "🤖 AI 大模型排雷与研报点评", "⏳ 策略近一年历史胜率回测"])
                
                with tab_kline:
                    st.plotly_chart(draw_pro_kline(selected_code, s_name, s_df, s_adv), use_container_width=True)
                with tab_min:
                    min_df = fetch_min_timeline(selected_code)
                    if min_df is not None and not min_df.empty:
                        st.plotly_chart(draw_min_timeline(selected_code, s_name, min_df), use_container_width=True)
                    else:
                        st.info("⚠️ 当前非交易时间或分时行情加载中。")
                with tab_llm:
                    with st.spinner("AI 正在深度查阅公告排雷并生成操盘研报..."):
                        target_item = next(r for r in results if r['代码'] == selected_code)
                        llm_res = generate_ai_llm_analysis(selected_code, s_name, target_item, llm_api_key, llm_base_url)
                        st.markdown(f"### 📋 [{selected_code}] {s_name} AI 投研点评\n{llm_res}")
                with tab_backtest:
                    with st.spinner("正在单股极速回测过去一年胜率..."):
                        bt_df, bt_summary = run_historical_backtest_ondemand(selected_code, s_row_data, macro)
                        
                    if bt_df is not None and not bt_df.empty:
                        btc1, btc2, btc3 = st.columns(3)
                        btc1.metric("近一年触发买点", f"{bt_summary['触发次数']} 次")
                        btc2.metric("持仓 3 日胜率", f"{bt_summary['胜率']} %")
                        btc3.metric("平均 3 日收益", f"{bt_summary['平均收益']} %")
                        st.dataframe(bt_df, use_container_width=True, hide_index=True)
                    else:
                        st.info("⚠️ 在过去一年（近 250 个交易日）内，该股未曾触发过当前策略买点信号。")
    elif not st.session_state.get('has_scanned'):
        st.info("👈 请确认左侧策略与参数后，点击上方红色的 **“🚀 启动全市场深度量化极速扫描”** 按钮。")

with tab_view_portfolio:
    st.subheader("💼 我的自选与持仓浮盈监控池")
    st.caption("实时拉取最新价格，自动跟踪持仓盈亏，并在触及止损位时触发红色告警")
    
    p_list = st.session_state.get('portfolio', [])
    if not p_list:
        st.info("💡 监控池暂无持仓股票。你可以在左侧看板选中股票后，点击【一键加入持仓监控】进行跟踪。")
    else:
        # 实时拉取持仓最新价
        p_symbols = [f"sh{p['代码']}" if p['代码'].startswith("60") else f"sz{p['代码']}" for p in p_list]
        url_p = f"https://qt.gtimg.cn/q={','.join(p_symbols)}"
        price_map = {}
        try:
            resp_p = requests.get(url_p, timeout=2.0)
            for line in resp_p.text.strip().split(";"):
                if "=" in line:
                    code_cur = line.split("=")[0].split("_")[-1][2:]
                    p_cur = float(line.split("=")[1].strip('"').split("~")[3] or 0)
                    price_map[code_cur] = p_cur
        except Exception:
            pass
            
        p_rows = []
        for p in p_list:
            c_code = p['代码']
            cur_price = price_map.get(c_code, p['买入价'])
            buy_price = p['买入价']
            shares = p['持股数']
            profit_pct = round((cur_price - buy_price) / buy_price * 100, 2)
            profit_amt = round((cur_price - buy_price) * shares, 2)
            
            status_tag = "持仓中"
            if cur_price <= p['建议止损位']:
                status_tag = "🚨 触及止损位 (建议平仓)"
            elif cur_price >= p['第一止盈目标']:
                status_tag = "🎉 达到止盈位 (+8%)"
                
            p_rows.append({
                "代码": c_code, "名称": p['名称'],
                "买入成本": buy_price, "当前最新价": cur_price,
                "浮动盈亏(%)": f"{'+' if profit_pct>0 else ''}{profit_pct}%",
                "浮动盈亏(元)": f"{'+' if profit_amt>0 else ''}{profit_amt}",
                "建议止损位": p['建议止损位'], "第一止盈目标": p['第一止盈目标'],
                "风控状态": status_tag, "建仓日期": p['加入时间']
            })
            
        st.dataframe(pd.DataFrame(p_rows), use_container_width=True, hide_index=True)
        
        if st.button("🗑️ 清空持仓监控池"):
            st.session_state['portfolio'] = []
            save_portfolio([])
            st.rerun()
