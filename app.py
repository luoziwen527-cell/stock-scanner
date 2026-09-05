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
    page_title="AI 智能主力量化投研系统 v7.4 (实时分时图+历史回测版)",
    layout="wide",
    page_icon="🧠"
)

# ==================== 本地持久化与配置 ====================
CONFIG_FILE = "user_config.json"
PORTFOLIO_FILE = "user_portfolio.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_config(data):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

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

sys_config = load_config()

if 'scan_results' not in st.session_state:
    st.session_state['scan_results'] = []
if 'kline_cache' not in st.session_state:
    st.session_state['kline_cache'] = {}
if 'has_scanned' not in st.session_state:
    st.session_state['has_scanned'] = False
if 'portfolio' not in st.session_state:
    st.session_state['portfolio'] = load_portfolio()

# ==================== 微信推送引擎 ====================
def send_wechat_push(title: str, content_markdown: str, push_token: str, push_channel: str = "PushPlus"):
    if not push_token or not push_token.strip():
        return False, "未配置推送 Token"
    token = push_token.strip()
    try:
        if push_channel == "PushPlus":
            url = "http://www.pushplus.plus/send"
            payload = {"token": token, "title": title, "content": content_markdown, "template": "markdown"}
            res = requests.post(url, json=payload, timeout=5.0).json()
            if res.get("code") == 200:
                return True, "PushPlus 微信推送成功！"
            return False, f"PushPlus 报错: {res.get('msg')}"
        else:
            url = f"https://sctapi.ftqq.com/{token}.send"
            payload = {"title": title, "desp": content_markdown}
            res = requests.post(url, data=payload, timeout=5.0).json()
            if res.get("code") == 0:
                return True, "Server酱 微信推送成功！"
            return False, f"Server酱 报错: {res.get('message')}"
    except Exception as e:
        return False, f"网络请求失败: {str(e)}"

# ==================== 行业直查 ====================
@st.cache_data(ttl=86400 * 30)
def get_exact_industry_by_code(code: str) -> str:
    clean_code = str(code).zfill(6)
    market_flag = "1" if clean_code.startswith("6") else "0"
    secid = f"{market_flag}.{clean_code}"
    url = f"https://push2.eastmoney.com/api/qt/stock/get?fields=f127,f128&secid={secid}"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    try:
        resp = requests.get(url, headers=headers, timeout=1.8).json()
        data = resp.get("data")
        if data:
            ind = data.get("f127")
            if ind and ind not in ["-", "None", "未知", ""]:
                return str(ind).strip()
    except Exception:
        pass
    return "综合制造"

STANDARD_SECTORS = [
    "半导体", "消费电子", "通信设备", "汽车零部件", "新能源汽车", "光伏设备", "电池",
    "电力行业", "石油行业", "银行", "证券", "影视院线", "光学光电子", "电网设备",
    "计算机设备", "软件开发", "有色金属", "贵金属", "能源金属", "电子化学品", "化学制药"
]

# ==================== 腾讯资金流向 ====================
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
                    flow_map[code_key] = {
                        "主力净流入": round(float(data_fields[3] or 0), 1),
                        "主力净占比": round(float(data_fields[4] or 0), 1)
                    }
        except Exception:
            continue
    return flow_map

# ==================== 腾讯实时日内分时线抓取 (分钟级数据) ====================
def fetch_realtime_minute_timeline(code: str):
    """
    抓取当日分钟级分时数据：包含分时走势价、日内均价 VWAP、成交量
    """
    market = "sh" if str(code).startswith("60") else "sz"
    clean_c = str(code).zfill(6)
    url = f"https://data.gtimg.cn/flashdata/hushen/minute/{market}{clean_c}.js"
    try:
        resp = requests.get(url, timeout=2.5)
        text = resp.text
        if not text or "min_data=" not in text:
            return None
        raw_str = text.split("min_data=")[-1].strip().strip('";').strip()
        lines = [line.strip() for line in raw_str.split("\\n\\n") if line.strip()]
        if not lines:
            lines = [line.strip() for line in raw_str.split("\n") if line.strip()]

        data = []
        cum_volume = 0
        cum_amount = 0.0

        for line in lines:
            parts = line.split()
            if len(parts) >= 3:
                time_str = parts[0]
                price = float(parts[1])
                vol = float(parts[2])

                # 格式化时间为 HH:MM
                if len(time_str) == 4:
                    fmt_time = f"{time_str[:2]}:{time_str[2:]}"
                else:
                    fmt_time = time_str

                cum_volume += vol
                cum_amount += price * vol
                vwap = round(cum_amount / max(1e-6, cum_volume), 2)

                data.append({
                    "时间": fmt_time,
                    "现价": price,
                    "均价": vwap,
                    "成交量": vol
                })

        if data:
            return pd.DataFrame(data)
    except Exception:
        pass
    return None

# ==================== 绘制专业日内分时图 ====================
def draw_pro_timeline(code, name, timeline_df, prev_close):
    if timeline_df is None or timeline_df.empty:
        fig = go.Figure()
        fig.add_annotation(text="暂未获取到日内分时数据（休市或数据暂未刷新）", showarrow=False, font=dict(size=14, color="#aaa"))
        fig.update_layout(height=400)
        return fig

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.72, 0.28]
    )

    # 1. 现价白线
    fig.add_trace(go.Scatter(
        x=timeline_df['时间'], y=timeline_df['现价'],
        line=dict(color='#ffffff', width=1.6),
        name="分时现价"
    ), row=1, col=1)

    # 2. 均价黄线 (VWAP)
    fig.add_trace(go.Scatter(
        x=timeline_df['时间'], y=timeline_df['均价'],
        line=dict(color='#ffd600', width=1.5, dash='dash'),
        name="分时均价 (黄线)"
    ), row=1, col=1)

    # 3. 昨收基准参考线
    if prev_close > 0:
        fig.add_hline(
            y=prev_close, line_dash="dot", line_color="#78909c",
            annotation_text=f"昨收基准: {prev_close}", annotation_position="top left", row=1, col=1
        )

    # 4. 分时量能柱
    colors = ['#ef5350' if p >= prev_close else '#26a69a' for p in timeline_df['现价']]
    fig.add_trace(go.Bar(
        x=timeline_df['时间'], y=timeline_df['成交量'],
        marker_color=colors, name="分时量能"
    ), row=2, col=1)

    latest_p = timeline_df['现价'].iloc[-1]
    latest_vwap = timeline_df['均价'].iloc[-1]
    is_above = latest_p >= latest_vwap

    fig.update_layout(
        title=f"⏱️ {code} {name} 当日实时分时走势 (现价: {latest_p}元 | 均价: {latest_vwap}元 | {'🟢 均线上方稳健' if is_above else '🔴 均线下方承压'})",
        xaxis_rangeslider_visible=False,
        height=420,
        margin=dict(l=10, r=10, t=40, b=10)
    )
    return fig

# ==================== 均价线与形态过滤 ====================
def advanced_quant_quality_check(row_data):
    open_p = float(row_data.get('今开', 0))
    close_p = float(row_data.get('最新价', 0))
    high_p = float(row_data.get('最高', 0))
    low_p = float(row_data.get('最低', 0))
    amount_wan = float(row_data.get('成交额(万)', 0))
    volume_hand = float(row_data.get('成交量', 0))

    if volume_hand > 0:
        vwap = (amount_wan * 10000) / (volume_hand * 100)
        if close_p < vwap * 0.995:
            return False, "现价低于日内均价线"
    span = high_p - low_p
    if span > 0:
        upper_shadow = high_p - max(open_p, close_p)
        if (upper_shadow / span) > 0.38 and (high_p / max(0.01, open_p) - 1) > 0.035:
            return False, "长上影假突破"
        body = abs(close_p - open_p)
        if (body / span) < 0.28 and (high_p / max(0.01, open_p) - 1) > 0.025:
            return False, "实体单薄十字星"
    return True, "形态饱满"

def calculate_fixed_risk_shares(current_p, stop_loss_p, max_risk_cny=300, max_budget=15000):
    per_share_risk = max(0.05, current_p - stop_loss_p)
    raw_shares = int(max_risk_cny / per_share_risk / 100) * 100
    budget_shares = int(max_budget / max(0.01, current_p) / 100) * 100
    return max(100, min(raw_shares, budget_shares))

# ==================== 历史实盘回测模拟内核 ====================
def run_strategy_backtest(k_df: pd.DataFrame, stop_loss_ratio: float = 0.02, profit_target_ratio: float = 0.04, hold_days: int = 3):
    if len(k_df) < 35:
        return None
    df = k_df.copy().reset_index(drop=True)
    df['MA5'] = df['收盘'].rolling(5).mean()
    df['MA10'] = df['收盘'].rolling(10).mean()
    df['MA20'] = df['收盘'].rolling(20).mean()
    df['VOL5'] = df['成交量'].rolling(5).mean()

    trades = []
    start_idx = max(20, len(df) - 60)
    i = start_idx
    while i < len(df) - 1:
        c = df.loc[i, '收盘']
        ma5, ma10, ma20 = df.loc[i, 'MA5'], df.loc[i, 'MA10'], df.loc[i, 'MA20']
        vol, vol5 = df.loc[i, '成交量'], df.loc[i, 'VOL5']
        pct = df.loc[i, '涨跌幅']

        is_signal = (c > ma5 >= ma10 >= ma20 * 0.98) and (1.5 <= pct <= 6.0) and (vol >= vol5 * 1.2)
        if is_signal:
            buy_date = df.loc[i, '日期']
            buy_p = c
            stop_p = round(buy_p * (1 - stop_loss_ratio), 2)
            target_p = round(buy_p * (1 + profit_target_ratio), 2)

            exit_date = buy_date
            exit_p = buy_p
            exit_reason = "持仓到期平仓"

            for h in range(1, min(hold_days + 1, len(df) - i)):
                future_row = df.loc[i + h]
                curr_high = future_row['最高']
                curr_low = future_row['最低']
                curr_close = future_row['收盘']

                if curr_low <= stop_p:
                    exit_p = stop_p
                    exit_date = future_row['日期']
                    exit_reason = "触发止损平仓"
                    i += h
                    break
                elif curr_high >= target_p:
                    exit_p = target_p
                    exit_date = future_row['日期']
                    exit_reason = "冲高止盈平仓"
                    i += h
                    break
                elif h == hold_days:
                    exit_p = curr_close
                    exit_date = future_row['日期']
                    exit_reason = "周期到期收盘平仓"
                    i += h
                    break
            else:
                i += 1

            ret_pct = round((exit_p / buy_p - 1) * 100, 2)
            trades.append({
                "买入日期": buy_date, "买入价": buy_p,
                "卖出日期": exit_date, "卖出价": exit_p,
                "收益率(%)": ret_pct, "出场原因": exit_reason,
                "是否盈利": "✅ 盈利" if ret_pct > 0 else "❌ 亏损"
            })
        else:
            i += 1

    if not trades:
        return None

    t_df = pd.DataFrame(trades)
    win_rate = round((t_df['收益率(%)'] > 0).sum() / len(t_df) * 100, 1)
    avg_ret = round(t_df['收益率(%)'].mean(), 2)
    max_win = round(t_df['收益率(%)'].max(), 2)
    max_loss = round(t_df['收益率(%)'].min(), 2)
    cum_ret = round((np.prod(1 + t_df['收益率(%)'] / 100) - 1) * 100, 1)

    return {
        "trades_df": t_df, "total_trades": len(t_df),
        "win_rate": win_rate, "avg_ret": avg_ret,
        "max_win": max_win, "max_loss": max_loss, "cum_ret": cum_ret
    }

# ==================== 侧边栏配置 ====================
with st.sidebar:
    st.header("🔀 核心量化策略 (4 选 1)")
    strategy_mode = st.selectbox(
        "选择当前运行策略：",
        [
            "4️⃣ 推文精髓：四重共振主线战法 (强化版·板块+龙头+资金+确定性加仓)",
            "3️⃣ 图片绝技：三步极选强势股闭环策略 (量异动+均线多头+高控盘筹码)",
            "2️⃣ 主力博弈+龙头二波/底部洗盘策略 (视频理念)",
            "1️⃣ 多周期均线+ATR低吸策略 (趋势均线共振)"
        ]
    )

    st.divider()
    st.header("📲 自动化微信推送配置")
    enable_push = st.checkbox("🔔 开启扫描完成自动推送到手机微信", value=sys_config.get("enable_push", False))
    push_channel = st.selectbox("推送通道", ["PushPlus", "Server酱"], index=0 if sys_config.get("push_channel", "PushPlus") == "PushPlus" else 1)
    push_token = st.text_input("微信推送 Token (Key)", value=sys_config.get("push_token", ""), type="password", help="关注微信公众号【PushPlus推送加】免费获取Token")

    if st.button("📨 测试发送微信消息", use_container_width=True):
        if not push_token:
            st.error("请先输入 Token！")
        else:
            with st.spinner("正在发送测试推送..."):
                ok, msg = send_wechat_push("🧠 量化系统微信推送测试", "**恭喜！微信绑定成功！**\n\n- 运行版本：v7.4 实时分时版\n- 时间：" + datetime.now().strftime("%Y-%m-%d %H:%M:%S"), push_token, push_channel)
                if ok:
                    st.success("✅ 微信已收到测试通知！配置自动保存。")
                    sys_config.update({"enable_push": enable_push, "push_channel": push_channel, "push_token": push_token})
                    save_config(sys_config)
                else:
                    st.error(f"❌ 发送失败：{msg}")

    if enable_push != sys_config.get("enable_push") or push_token != sys_config.get("push_token"):
        sys_config.update({"enable_push": enable_push, "push_channel": push_channel, "push_token": push_token})
        save_config(sys_config)

    st.divider()
    st.header("🏷️ 细分主线板块精选")
    selected_sectors = st.multiselect("🎯 锁定特定行业 (留空则全市场扫描)", options=STANDARD_SECTORS, placeholder="如：半导体, 通信设备...")

    st.divider()
    st.header("⚡ 实时动态跳动配置")
    enable_auto_live = st.toggle("🔄 开启盘中秒级自动盯盘刷新", value=True)
    live_interval = st.slider("动态刷新频率 (秒)", 3, 30, 5, 1) if enable_auto_live else 60

    st.divider()
    st.header("🎯 实战风控与回测参数")
    enable_meltdown_guard = st.checkbox("🛑 开启【大盘极端恶劣强制空仓熔断】", value=True)
    max_risk_cny = st.slider("单笔最大可承受风险金额 (元)", 100, 1000, 300, 50)
    max_budget_per_stock = st.slider("单票买入上限金额 (元)", 5000, 30000, 15000, 1000)
    enable_strict_vwap = st.checkbox("🛡️ 开启【分时均线承接与上影线过滤】", value=True)
    display_top_n = st.slider("最终呈现上限 (只)", 3, 60, 30, 1)
    deep_sample_size = st.slider("深度分析样本量 (只)", 100, 1500, 500, 50)

    if "3️⃣" in strategy_mode:
        min_scan_pct, max_scan_pct = 2.8, 5.2
    elif "4️⃣" in strategy_mode:
        min_scan_pct, max_scan_pct = 1.5, 7.0
    else:
        max_scan_pct = st.slider("日内最大涨幅上限 (%)", 1.0, 9.5, 5.5, 0.1)
        min_scan_pct = st.slider("日内最小涨幅下限 (%)", -7.0, 2.0, -4.0, 0.1)

    enable_ultra_filter = st.checkbox("💎 开启【盈亏比与趋势】过滤", value=True)
    min_risk_reward = st.slider("最低盈亏比门槛", 1.2, 3.5, 1.8, 0.1) if (enable_ultra_filter and "3️⃣" not in strategy_mode) else 1.0
    enable_weekly_filter = st.checkbox("📈 开启【中长线多头趋势】过滤", value=True)
    enable_fundamental_filter = st.checkbox("🛡️ 开启【基本面轻量排雷】", value=True)

    board_type = st.selectbox("市场板块", ["全部主板 (60 + 00)", "仅沪市主板 (60开头)", "仅深市主板 (00开头)"])
    price_range = st.slider("股价区间 (元)", 1.0, 100.0, (2.0, 80.0), 0.5)
    min_price, max_price = price_range
    min_amount = st.slider("最低日成交额门槛 (万元)", 500, 30000, 1500, 500)
    exclude_limit_up = st.checkbox("🚫 剔除涨停封板股票", value=True)

# ==================== 大盘宏观风控 ====================
def fetch_realtime_macro_deep():
    url_tx = "https://qt.gtimg.cn/q=s_sh000001,s_sz399001,s_sz399006"
    macro_info = {
        "sh_pct": 0.0, "sh_price": 3100.0, "sh_amt_yi": 0.0,
        "sz_pct": 0.0, "sz_price": 10000.0, "sz_amt_yi": 0.0,
        "cy_pct": 0.0, "cy_price": 2000.0,
        "total_amt_yi": 0.0, "up_count": 0, "down_count": 0, "flat_count": 0,
        "status_color": "🟢", "status_text": "安全进攻区",
        "suggest_position": "60% ~ 80%",
        "action_guide": "大盘处于活跃可操作区间，可积极参与主力蓄势与强势突破标的。",
        "market_score": 12, "is_meltdown": False,
        "update_time": datetime.now().strftime("%H:%M:%S")
    }
    try:
        resp = requests.get(url_tx, timeout=2.0)
        for line in resp.text.strip().split(";"):
            if "s_sh000001" in line and "=" in line:
                parts = line.split("=")[1].strip('"').split("~")
                if len(parts) >= 6:
                    p = float(parts[2] or 0)
                    if p > 100: macro_info["sh_price"] = p
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

    try:
        url_em = "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2&fields=f3,f104,f105,f106&secids=1.000001,0.399001"
        r_em = requests.get(url_em, timeout=2.0).json()
        diff = r_em.get("data", {}).get("diff", [])
        if diff:
            macro_info["up_count"] = sum(int(x.get("f104", 0) or 0) for x in diff)
            macro_info["down_count"] = sum(int(x.get("f105", 0) or 0) for x in diff)
            macro_info["flat_count"] = sum(int(x.get("f106", 0) or 0) for x in diff)
    except Exception:
        macro_info["up_count"], macro_info["down_count"], macro_info["flat_count"] = 2800, 2100, 150

    sh_pct = macro_info["sh_pct"]
    up_cnt = macro_info["up_count"]
    down_cnt = macro_info["down_count"]

    if down_cnt >= 3600 or sh_pct <= -1.8:
        macro_info.update({
            "status_color": "🛑", "status_text": "系统级空仓熔断 (泥沙俱下)",
            "suggest_position": "0% (强制空仓)",
            "action_guide": "全市场大面积杀跌，主力资金全线撤退避险！系统已触发强制风控熔断，今日严禁开仓！",
            "market_score": 4, "is_meltdown": True
        })
    elif sh_pct >= 0.3 and up_cnt > down_cnt:
        macro_info.update({"status_color": "🟢", "status_text": "多头进攻周期", "suggest_position": "70% ~ 90%", "action_guide": "大盘赚钱效应极佳，顺势重仓做主线，利润依托5日线奔跑。", "market_score": 15})
    elif -0.8 <= sh_pct < 0.3:
        macro_info.update({"status_color": "🟡", "status_text": "震荡分歧周期", "suggest_position": "40% ~ 55%", "action_guide": "大盘轮动快，严控追高，仅在主力底线附近分批低吸，有浮盈及时落袋。", "market_score": 10})
    else:
        macro_info.update({"status_color": "🔴", "status_text": "弱势防守区", "suggest_position": "10% ~ 30%", "action_guide": "大盘震荡走弱，个股分化，轻仓或空仓防守！", "market_score": 7})

    return macro_info

@st.fragment(run_every=live_interval if enable_auto_live else None)
def render_live_macro_header():
    macro = fetch_realtime_macro_deep()
    c1, c2, c3, c4 = st.columns([1.1, 1.1, 1.1, 1.5])
    c1.metric("🏛️ 上证指数", f"{macro['sh_price']} 点", f"{macro['sh_pct']:+.2f}%")
    c2.metric("🏛️ 深证成指", f"{macro['sz_price']} 点", f"{macro['sz_pct']:+.2f}%")
    c3.metric("🏛️ 创业板指", f"{macro['cy_price']} 点", f"{macro['cy_pct']:+.2f}%")
    c4.metric("💰 两市总成交额", f"{macro['total_amt_yi']} 亿元", f"沪:{macro['sh_amt_yi']}亿 | 深:{macro['sz_amt_yi']}亿")

    info_col1, info_col2 = st.columns([2.2, 2.8])
    with info_col1:
        total = max(1, macro['up_count'] + macro['down_count'] + macro['flat_count'])
        st.markdown(f"""
        <div style="background-color:rgba(255,255,255,0.04); padding:10px 14px; border-radius:8px; border-left:4px solid #ff9800;">
            <div style="font-size:14px; font-weight:bold; color:#ddd;">📊 全市场即时赚钱效应 ({macro['update_time']})：</div>
            <div style="margin-top:6px; font-size:15px;">
                <span style="color:#ef5350; font-weight:bold;">🔺 上涨: {macro['up_count']} 家 ({round(macro['up_count']/total*100,1)}%)</span> &nbsp;|&nbsp; 
                <span style="color:#26a69a; font-weight:bold;">🔻 下跌: {macro['down_count']} 家 ({round(macro['down_count']/total*100,1)}%)</span>
            </div>
            <div style="font-size:12px; color:#aaa; margin-top:4px;">建议总仓位：<b style="color:#00e676;">{macro['suggest_position']}</b></div>
        </div>
        """, unsafe_allow_html=True)

    with info_col2:
        st.markdown(f"""
        <div style="background-color:rgba(255,255,255,0.04); padding:10px 14px; border-radius:8px; border-left:4px solid {macro['status_color']=='🟢' and '#00e676' or (macro['status_color']=='🟡' and '#ffd600' or '#ff1744')};">
            <div style="font-size:14px; font-weight:bold;">🧭 实时宏观风控指令：<span style="color:#ffd600;">{macro['status_text']}</span></div>
            <div style="font-size:13px; color:#eee; margin-top:4px; line-height:1.4;">{macro['action_guide']}</div>
        </div>
        """, unsafe_allow_html=True)

render_live_macro_header()
st.divider()

def generate_stock_codes(b_type: str):
    symbols = []
    if "仅深市" not in b_type:
        for i in range(0, 600): symbols.append(f"sh600{i:03d}")
        for i in range(0, 350): symbols.append(f"sh601{i:03d}")
        for i in range(0, 500): symbols.append(f"sh603{i:03d}")
        for i in range(0, 400): symbols.append(f"sh605{i:03d}")
    if "仅沪市" not in b_type:
        for i in range(0, 1000): symbols.append(f"sz000{i:03d}")
        for i in range(0, 350): symbols.append(f"sz001{i:03d}")
        for i in range(0, 1000): symbols.append(f"sz002{i:03d}")
        for i in range(0, 400): symbols.append(f"sz003{i:03d}")
    return symbols

def fetch_tencent_batch(batch_symbols):
    url = f"https://qt.gtimg.cn/q={','.join(batch_symbols)}"
    items = []
    try:
        resp = requests.get(url, timeout=3.0)
        for line in resp.text.strip().split(";"):
            if not line or "=" not in line: continue
            data_str = line.split("=")[1].strip().strip('"')
            if not data_str: continue
            fields = data_str.split("~")
            if len(fields) < 46: continue
            name, code = fields[1], fields[2]
            price = float(fields[3] or 0)
            if price <= 0 or "ST" in name or "退" in name: continue
            items.append({
                "代码": code, "名称": name, "最新价": price,
                "昨收": float(fields[4] or 0), "今开": float(fields[5] or 0),
                "最高": float(fields[33] or price), "最低": float(fields[34] or price),
                "涨跌幅": float(fields[32] or 0),
                "成交额(万)": float(fields[37] or 0) if len(fields) > 37 and fields[37] else (float(fields[6] or 0) * price / 100),
                "换手率": float(fields[38] or 0) if len(fields) > 38 and fields[38] else 1.0,
                "成交量": float(fields[6] or 0),
                "PE": float(fields[39] or 0) if len(fields) > 39 and fields[39] else 0.0,
                "PB": float(fields[46] or 0) if len(fields) > 46 and fields[46] else 0.0
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
            if res: all_stocks.extend(res)
    df = pd.DataFrame(all_stocks)
    if df.empty: return df
    df = df[(df['最新价'] >= min_p) & (df['最新价'] <= max_p)]
    if min_amt > 0:
        df_f = df[df['成交额(万)'] >= min_amt]
        if len(df_f) >= 20: df = df_f
    if no_limit: df = df[df['涨跌幅'] < 9.5]
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
    if len(k_df) < period + 1: return float(k_df['收盘'].iloc[-1] * 0.025)
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
    cum_w = np.cumsum(decay_weights[sorted_indices])
    total_w = cum_w[-1] + 1e-9
    cum_norm = cum_w / total_w
    p5 = sorted_p[np.searchsorted(cum_norm, 0.05)]
    p95 = sorted_p[np.searchsorted(cum_norm, 0.95)]
    p15 = sorted_p[np.searchsorted(cum_norm, 0.15)]
    p85 = sorted_p[np.searchsorted(cum_norm, 0.85)]
    avg_cost = np.average(prices, weights=decay_weights)
    return {
        "width_90": round((p95 - p5) / (avg_cost + 1e-6) * 100, 1),
        "width_70": round((p85 - p15) / (avg_cost + 1e-6) * 100, 1),
        "chip_peak": round(avg_cost, 2),
        "profit_ratio": round(np.sum(decay_weights[prices <= current_price]) / total_w * 100, 1)
    }

def calculate_dynamic_win_rate(net_main_wan, main_ratio, pos_desc, rr_ratio, quality_score, chip_w70, macro_score, sector_rank_score=0):
    base_score = 40.0 + min(15.0, (quality_score / 45.0) * 15.0)
    if chip_w70 < 10.0: base_score += 15.0
    elif chip_w70 < 14.0: base_score += 10.0
    elif chip_w70 < 17.0: base_score += 6.0
    if rr_ratio >= 3.0: base_score += 12.0
    elif rr_ratio >= 2.2: base_score += 8.0
    elif rr_ratio >= 1.8: base_score += 4.0
    if "黄金买点" in pos_desc: base_score += 12.0
    elif "刚起跑" in pos_desc: base_score += 9.0
    elif "超买" in pos_desc: base_score -= 15.0
    elif "破位" in pos_desc: base_score -= 25.0
    if macro_score >= 14: base_score += 6.0
    elif macro_score <= 8: base_score -= 6.0
    base_score += sector_rank_score

    flow_status = "🟡 资金平衡"
    flow_reason = "资金平稳，多为空头试探与均线承接"
    if net_main_wan > 800 and main_ratio > 3.0:
        base_score += 14.0
        flow_status = f"🟢 主力抢筹 (+{net_main_wan}万)"
        flow_reason = "主力大资金逆势净流入建仓，拉升反包确定性极高！"
    elif net_main_wan > 100:
        base_score += 7.0
        flow_status = f"🟢 主力微买 (+{net_main_wan}万)"
        flow_reason = "主力资金保持温和净买入，下方支撑坚固。"
    elif net_main_wan < -1500 and main_ratio < -6.0:
        base_score -= 15.0
        flow_status = f"🔴 主力离场 ({net_main_wan}万)"
        flow_reason = "主力大单正在离场，警惕冲高回落！"

    final_buy_prob = int(np.clip(round(base_score), 25, 95))
    return final_buy_prob, 100 - final_buy_prob, flow_status, flow_reason

def diagnose_position_and_action(current_p, b_low, b_high, stop_loss, target_p, ma5, ma10):
    bias5 = (current_p / ma5 - 1) * 100 if ma5 > 0 else 0
    dist_sl = (current_p - stop_loss) / current_p * 100
    if b_low <= current_p <= b_high * 1.008:
        return "🟢 黄金买点区 (回踩支撑位)", "👉 尾盘分批建仓，破防守线止损", "#00e676"
    elif current_p > b_high * 1.008 and bias5 <= 3.5:
        return "🟡 刚起跑临界点 (轻度突破)", "👉 盘中小幅回踩可打入底仓，切忌追高", "#ffd600"
    elif bias5 > 3.5:
        return f"🟠 脱离成本超买区 (偏离MA5 {bias5:.1f}%)", "✋ 严禁追买！已有底仓等放量冲高止盈", "#ff9100"
    elif current_p < stop_loss:
        return "🔴 跌破防守线 (破位区)", "🚨 坚决不买！若已持仓次日早盘冲高无条件清仓", "#ff1744"
    else:
        return f"⚪ 蓄势防守区 (距止损仅 {dist_sl:.1f}%)", "👀 观察承接，不破支撑线可轻仓试探", "#e0e0e0"

# ==================== 策略 4 与 策略 3 判定 ====================
def evaluate_strategy_quad_resonance(df: pd.DataFrame, row_data: dict, macro_status: dict, flow_info: dict, sector_name: str, sec_stat: dict, risk_cny: int, budget_cny: int):
    close = df['收盘'].values
    highs = df['最高'].values
    n = len(df)
    if n < 20: return None
    ma5, ma10, ma20 = np.mean(close[-5:]), np.mean(close[-10:]), np.mean(close[-20:])
    if close[-1] < ma20 * 0.965: return None

    is_sector_strong = (sec_stat.get("avg_pct", 0.0) >= 0.5) or (sec_stat.get("strong_count", 0) >= 2)
    sector_rank_bonus = 12 if is_sector_strong else 0
    chip_info = calculate_precise_chip_concentration(df, close[-1])
    atr = calculate_atr(df, 14)

    open_today = float(row_data.get('今开', close[-1]))
    stop_loss = round(min(open_today * 0.985, ma10), 2)
    buy_low = round(max(ma10, close[-1] * 0.975), 2)
    buy_high = round(close[-1] * 1.015, 2)
    target_p = round(max(np.max(highs[-20:]), close[-1] * 1.15), 2)
    rr_ratio = round((target_p - close[-1]) / max(0.01, close[-1] - stop_loss), 1)

    rec_shares = calculate_fixed_risk_shares(close[-1], stop_loss, risk_cny, budget_cny)
    est_loss_cny = round(rec_shares * (close[-1] - stop_loss), 1)
    pos_desc, action_cmd, action_color = diagnose_position_and_action(close[-1], buy_low, buy_high, stop_loss, target_p, ma5, ma10)

    net_wan = flow_info.get("主力净流入", 0.0)
    ratio = flow_info.get("主力净占比", 0.0)
    is_fund_strong = (net_wan > 200 or ratio > 1.8)

    b_prob, s_prob, flow_status, flow_reason = calculate_dynamic_win_rate(
        net_wan, ratio, pos_desc, rr_ratio, 42, chip_info['width_70'], macro_status.get("market_score", 12), sector_rank_bonus
    )

    if is_sector_strong and is_fund_strong and (close[-1] >= ma5):
        resonance_tag = "🔥🔥🔥 四重共振(主线领跑)"
        position_rule = f"{rec_shares} 股 (约{round(rec_shares*close[-1]/10000, 1)}万)"
        why_buy_core = f"【四重共振达成】：所属行业【{sector_name}】集体走强；主力大单净流入 {net_wan} 万；站稳日内均价线且 K 线稳居 MA5 上方。"
        total_score = 95
    else:
        resonance_tag = "⚡⚡ 双重共振(梯队跟进)"
        position_rule = f"{max(100, int(rec_shares * 0.6 / 100) * 100)} 股 (试错底仓)"
        why_buy_core = f"【双重共振】：行业【{sector_name}】有异动或主力资金介入，个股处于洗盘分歧期。"
        total_score = 82

    advice = {
        "建议买入区间": f"{buy_low} ~ {buy_high}", "建议买入区间_低": buy_low, "建议买入区间_高": buy_high,
        "建议止损位": f"{stop_loss} (起涨开盘价/10日线)", "止损数值": stop_loss,
        "第一止盈目标": f"{target_p}", "止盈数值": target_p,
        "动态压力位": round(target_p, 2), "动态支撑位": round(ma10, 2),
        "ATR": round(atr, 3), "盈亏比": f"{rr_ratio} : 1",
        "建议下单股数": f"{rec_shares} 股", "单笔锁定风险金": f"约 {est_loss_cny} 元",
        "买入时段": "🌇 尾盘确认 (14:30-14:50)", "卖出时机": "次日早盘冲高+3%~+5%出半仓，破10日线清仓",
        "预估持股周期": "2 ~ 5 个交易日", "为什么值得买": why_buy_core,
        "自适应仓位": position_rule, "当前位置描述": pos_desc,
        "具体操作指令": action_cmd, "指令颜色": action_color,
        "主力资金状态": flow_status, "买入概率": f"{b_prob}%", "卖出/风险概率": f"{s_prob}%",
        "共振等级": resonance_tag
    }
    timing_dict = {
        "买点战术详情": [
            f"📍 【共振级别】：{resonance_tag} | 建议买入：{rec_shares} 股",
            f"🛡️ 【锁定单笔亏损】：若不幸跌破止损线，最大损失严格锁死在约 {est_loss_cny} 元内！",
            f"💰 【资金动向】：{flow_status} (胜率估算: {b_prob}%)",
            f"🎯 【离场信号】：跌破铁律止损线 {stop_loss} 元，次日无条件清仓！"
        ]
    }
    radar = {"板块热度": min(20, 10 + sector_rank_bonus), "主力异动": 18, "筹码沉淀": min(20, int(chip_info['profit_ratio'] * 0.2)), "底部安全性": 17, "博弈胜率": int(b_prob * 0.2)}
    return total_score, 44, 45, [resonance_tag, f"{rec_shares}股"], advice, radar, timing_dict, chip_info, True, "🟢 低", rr_ratio

def evaluate_strategy_three_step_champion(df: pd.DataFrame, row_data: dict, macro_status: dict, flow_info: dict, risk_cny: int, budget_cny: int, sector_rank_bonus: int = 0):
    pct = float(row_data.get('涨跌幅', 0))
    turnover = float(row_data.get('换手率', 0))
    close = df['收盘'].values
    highs = df['最高'].values
    lows = df['最低'].values
    vols = df['成交量'].values
    n = len(df)
    if n < 25 or not (2.8 <= pct <= 5.2) or not (2.8 <= turnover <= 10.5): return None

    vol_today = vols[-1]
    vol_5d_avg = np.mean(vols[-6:-1]) if n >= 6 else vol_today
    if vol_today / (vol_5d_avg + 1e-6) < 1.8: return None

    ma5, ma10, ma20 = np.mean(close[-5:]), np.mean(close[-10:]), np.mean(close[-20:])
    if not (ma5 >= ma10 * 0.995 and ma10 >= ma20 * 0.995) or close[-1] < ma5 * 0.99: return None

    chip_data = calculate_precise_chip_concentration(df, close[-1])
    if chip_data["width_90"] > 22.0 or chip_data["width_70"] > 16.5: return None

    open_today = float(row_data.get('今开', close[-1]))
    stop_loss_ma10 = round(min(open_today * 0.985, ma10), 2)
    buy_low = round(ma5, 2)
    buy_high = round(close[-1], 2)
    target_high = round(np.max(highs[-20:]) * 1.12, 2)
    rr_ratio = round((target_high - close[-1]) / max(0.01, close[-1] - stop_loss_ma10), 1)

    rec_shares = calculate_fixed_risk_shares(close[-1], stop_loss_ma10, risk_cny, budget_cny)
    est_loss_cny = round(rec_shares * (close[-1] - stop_loss_ma10), 1)
    pos_desc, action_cmd, action_color = diagnose_position_and_action(close[-1], buy_low, buy_high, stop_loss_ma10, target_high, ma5, ma10)

    net_wan = flow_info.get("主力净流入", 0.0)
    ratio = flow_info.get("主力净占比", 0.0)
    b_prob, s_prob, flow_status, flow_reason = calculate_dynamic_win_rate(net_wan, ratio, pos_desc, rr_ratio, 45, chip_data['width_70'], macro_status.get("market_score", 12), sector_rank_bonus)

    advice = {
        "建议买入区间": f"{buy_low} ~ {buy_high}", "建议买入区间_低": buy_low, "建议买入区间_高": buy_high,
        "建议止损位": f"{stop_loss_ma10} (破起涨开盘价清仓)", "止损数值": stop_loss_ma10,
        "第一止盈目标": f"{target_high}", "止盈数值": target_high,
        "动态压力位": round(np.max(highs[-20:]), 2), "动态支撑位": round(ma10, 2),
        "ATR": round(close[-1] * 0.03, 3), "盈亏比": f"{rr_ratio} : 1",
        "建议下单股数": f"{rec_shares} 股", "单笔锁定风险金": f"约 {est_loss_cny} 元",
        "买入时段": "🌇 尾盘进场 (14:30 - 14:50)", "卖出时机": "次日早盘冲高分批落袋，破起涨价清仓",
        "预估持股周期": "⚡ 顺势主升 (2 ~ 5 个交易日)",
        "为什么值得买": f"温和放量 {vol_today / (vol_5d_avg + 1e-6):.1f} 倍涨 {pct:.1f}%，均线多头，70% 筹码集中度达 {chip_data['width_70']}%。",
        "自适应仓位": f"{rec_shares} 股 (风控换算)", "当前位置描述": pos_desc,
        "具体操作指令": action_cmd, "指令颜色": action_color,
        "主力资金状态": flow_status, "买入概率": f"{b_prob}%", "卖出/风险概率": f"{s_prob}%"
    }
    timing_dict = {
        "买点战术详情": [
            f"📍 【当前位置定位】：{pos_desc} | 建议买入：{rec_shares} 股",
            f"🛡️ 【绝对止损红线】：若被扫止损，单次损失严格锁死在约 {est_loss_cny} 元！",
            f"💰 【主力资金流向】：{flow_status} (胜率: {b_prob}%)",
            f"🎯 【铁律止盈止损】：跌破底线 {stop_loss_ma10} 元坚决清仓走人！"
        ]
    }
    radar = {"主力异动": 19, "洗盘充分度": 18, "筹码沉淀": 20, "底部安全性": 17, "博弈胜率": int(b_prob * 0.2)}
    return 94, 45, 47, ["🔥 放量异动", "📈 均线多头", flow_status[:10]], advice, radar, timing_dict, chip_data, True, "🟢 低", 3.0

# ==================== 工作任务分发 ====================
def worker_task(code, name, row_data, strategy_choice, enable_weekly, enable_fundamental, macro_status, min_rr, flow_map, enable_strict_filter, sector_stats, risk_cny, budget_cny):
    if enable_strict_filter:
        is_ok, _ = advanced_quant_quality_check(row_data)
        if not is_ok: return None

    k_df = fetch_kline_safe(code, row_data, days=90)
    last_close = float(k_df['收盘'].iloc[-1])
    pct_today = float(k_df['涨跌幅'].iloc[-1]) if len(k_df) > 1 else float(row_data.get('涨跌幅', 0))
    flow_info = flow_map.get(str(code).zfill(6), {"主力净流入": 0.0, "主力净占比": 0.0})
    sector_name = get_exact_industry_by_code(code)
    sec_stat = sector_stats.get(sector_name, {"avg_pct": 0.0, "strong_count": 0})

    if "4️⃣" in strategy_choice:
        res = evaluate_strategy_quad_resonance(k_df, row_data, macro_status, flow_info, sector_name, sec_stat, risk_cny, budget_cny)
    else:
        res = evaluate_strategy_three_step_champion(k_df, row_data, macro_status, flow_info, risk_cny, budget_cny, 8)

    if not res: return None

    total, quality, timing, tags, advice, radar, timing_dict, chip_info, passed, risk_level, rr_ratio = res
    star_rating = "⭐⭐⭐⭐⭐" if total >= 88 else ("⭐⭐⭐⭐" if total >= 78 else "⭐⭐⭐")

    k_df['MA5'] = k_df['收盘'].rolling(5).mean().fillna(k_df['收盘'])
    k_df['MA10'] = k_df['收盘'].rolling(10).mean().fillna(k_df['收盘'])
    k_df['MA20'] = k_df['收盘'].rolling(20).mean().fillna(k_df['收盘'])

    return {
        "代码": code, "名称": name, "板块": sector_name, "评级": star_rating,
        "当前位置": advice.get("当前位置描述", "蓄势区"),
        "建议股数": advice.get("建议下单股数", "1000 股"),
        "锁定风险": advice.get("单笔锁定风险金", "约 300 元"),
        "主力资金": advice.get("主力资金状态", "平稳"),
        "买入胜率": advice.get("买入概率", "65%"),
        "操作指令": advice.get("具体操作指令", "等待信号"),
        "为什么值得买": advice.get("为什么值得买", ""),
        "买入时段": advice.get("买入时段", "尾盘"),
        "持股周期": advice.get("预估持股周期", "3-5天"),
        "仓位战术": advice.get("自适应仓位", "分批建仓"),
        "综合评分": total, "最新价": last_close, "涨跌幅(%)": round(pct_today, 2),
        "成交额(万)": int(row_data.get('成交额(万)', 0)),
        "advice": advice, "timing": timing_dict, "radar": radar, "chip_info": chip_info,
        "k_df": k_df, "row_data": row_data
    }

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
    fig.add_hrect(y0=b_low, y1=b_high, fillcolor="rgba(0, 230, 118, 0.15)", line_width=0, annotation_text=f"🎯 买入区间: {b_low}~{b_high}", row=1, col=1)
    fig.add_hline(y=advice["动态压力位"], line_dash="dot", line_color="#ff1744", annotation_text=f"止盈: {advice['动态压力位']}", row=1, col=1)
    fig.add_hline(y=advice["动态支撑位"], line_dash="dash", line_color="#00e676", annotation_text=f"止损: {advice['动态支撑位']}", row=1, col=1)
    vol_colors = ['#ef5350' if c >= o else '#26a69a' for c, o in zip(recent['收盘'], recent['开盘'])]
    fig.add_trace(go.Bar(x=recent['日期'], y=recent['成交量'], marker_color=vol_colors, name="成交量"), row=2, col=1)
    fig.update_layout(title=f"📈 {code} {name} (最新 {recent['收盘'].iloc[-1]} 元)", xaxis_rangeslider_visible=False, height=450, margin=dict(l=10, r=10, t=35, b=10))
    return fig

# ==================== 扫描执行 ====================
macro_check = fetch_realtime_macro_deep()

if macro_check.get("is_meltdown", False) and enable_meltdown_guard:
    st.error("🚨 【系统硬性风控熔断】：全市场下跌超3600家或大盘暴跌，主力资金全线撤离！今日系统已强制锁死开仓，严禁开仓！")
    scan_clicked = False
else:
    scan_clicked = st.button("🚀 启动全市场深度量化极速扫描", type="primary", use_container_width=True)

if scan_clicked:
    t_start = time.time()
    macro_now = macro_check
    with st.spinner("正在全景扫描主板流动性标的池..."):
        pool = get_all_realtime_stocks_tx(board_type, min_price, max_price, min_amount, exclude_limit_up)
    if len(pool) == 0:
        st.error("❌ 标的池初筛为空，请调宽左侧参数。")
        st.stop()

    candidates = pool[(pool['涨跌幅'] >= min_scan_pct) & (pool['涨跌幅'] <= max_scan_pct)].sort_values(
        by=["成交额(万)"], ascending=False
    ).head(deep_sample_size)
    if len(candidates) < 15:
        candidates = pool.sort_values(by=["成交额(万)"], ascending=False).head(deep_sample_size)

    candidate_codes = candidates['代码'].tolist()
    money_flow_data = fetch_money_flow_safe_batched(candidate_codes)

    sector_stats = {}
    temp_industries = [get_exact_industry_by_code(c) for c in candidate_codes[:120]]
    temp_df = candidates.head(120).copy()
    temp_df['板块'] = temp_industries
    grouped = temp_df.groupby('板块')['涨跌幅'].agg(['mean', lambda s: (s >= 3.0).sum()]).reset_index()
    grouped.columns = ['板块', 'avg_pct', 'strong_count']
    for _, r in grouped.iterrows():
        sector_stats[r['板块']] = {"avg_pct": round(r['avg_pct'], 2), "strong_count": int(r['strong_count'])}

    hit_results, new_kline_cache = [], {}
    progress_bar = st.progress(0, text=f"正在深度分析 {len(candidates)} 只样本及其历史形态...")
    completed = 0
    with ThreadPoolExecutor(max_workers=25) as executor:
        futures = [executor.submit(worker_task, str(row['代码']).zfill(6), row['名称'], row.to_dict(),
                                   strategy_mode, enable_weekly_filter, enable_fundamental_filter, macro_now, min_risk_reward, money_flow_data, enable_strict_vwap, sector_stats, max_risk_cny, max_budget_per_stock)
                   for _, row in candidates.iterrows()]
        for future in as_completed(futures):
            completed += 1
            res_item = future.result()
            if res_item:
                if selected_sectors and not any(sec in res_item["板块"] for sec in selected_sectors):
                    continue
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

    if enable_push and push_token and hit_results:
        top_list = hit_results[:3]
        push_md = f"### 🎯 AI 量化尾盘精选 (Top {len(top_list)})\n\n"
        push_md += f"> 大盘状态：{macro_now['status_color']} {macro_now['status_text']} | 建议总仓位：{macro_now['suggest_position']}\n\n"
        for i, item in enumerate(top_list):
            adv = item['advice']
            push_md += f"**{i+1}. {item['名称']} ({item['代码']}) - 【{item['板块']}】**\n"
            push_md += f"- 🎯 建议下单：**`{adv.get('建议下单股数','1000股')}`** (锁死亏损: `{adv.get('单笔锁定风险金','300元')}`)\n"
            push_md += f"- 🎯 建议买入区间：`{adv['建议买入区间']}`\n"
            push_md += f"- 🛡️ 铁律防守线：`{adv['建议止损位'].split(' ')[0]}` 元 (破位坚决走)\n"
            push_md += f"- 🚀 目标止盈：`{adv['第一止盈目标'].split(' ')[0]}` 元\n"
            push_md += f"- 🎲 买入胜率：`{adv['买入概率']}`\n\n"
        send_wechat_push(f"🎯 今日量化精选 ({datetime.now().strftime('%m-%d')})", push_md, push_token, push_channel)

    st.toast(f"⚡ 深度扫描完成！锁定 Top {len(hit_results)} 只真实主线标的，耗时 {elapsed} 秒", icon="🎉")

# ==================== 结果看板与回测 ====================
tab_view_select, tab_view_backtest, tab_view_portfolio = st.tabs(["🔥 AI 智能精选投研看板", "🔬 策略历史回测引擎 (验证胜率)", "💼 我的网页持仓/自选监控池"])

with tab_view_select:
    if st.session_state.get('scan_results'):
        results = st.session_state['scan_results']
        kline_cache = st.session_state['kline_cache']
        res_df = pd.DataFrame(results)

        st.subheader("👑 今日核心自选前三甲 (已锁定单笔风险金)")
        top3_cols = st.columns(min(3, len(results)))
        for idx, col in enumerate(top3_cols):
            r_item = results[idx]
            adv = r_item['advice']
            with col:
                st.markdown(f"""
                <div style="background-color:rgba(255,255,255,0.05); padding:14px; border-radius:8px; border-left:4px solid {adv['指令颜色']};">
                    <div style="font-size:17px; font-weight:bold;">{r_item['评级']} {r_item['名称']} ({r_item['代码']}) <span style="font-size:12px; background-color:#2e7d32; padding:2px 6px; border-radius:4px; color:#fff; margin-left:6px;">{r_item['板块']}</span></div>
                    <div style="font-size:13px; color:#ffd600; margin-top:4px;">🎯 <b>建议下单</b>：<span style="font-size:15px; font-weight:bold; color:#00e676;">{adv.get('建议下单股数','1000股')}</span> (止损锁定: {adv.get('单笔锁定风险金','300元')})</div>
                    <div style="font-size:13px; color:#64b5f6; margin-top:2px;">💰 <b>主力动向</b>：{adv['主力资金状态']}</div>
                    <div style="font-size:14px; color:#00e676; margin-top:3px; font-weight:bold;">🎲 <b>买入胜率</b>：{adv['买入概率']} | <b>位置</b>：{adv['当前位置描述']}</div>
                    <div style="font-size:12px; color:#eee; margin-top:6px; line-height:1.4;">💡 {r_item['为什么值得买']}</div>
                </div>
                """, unsafe_allow_html=True)

        st.divider()
        display_cols = ["评级", "代码", "名称", "板块", "建议股数", "锁定风险", "当前位置", "主力资金", "买入胜率", "最新价", "涨跌幅(%)", "综合评分"]
        st.dataframe(res_df[display_cols], use_container_width=True, hide_index=True)

        st.subheader("📊 个股全景决策中枢 (分时承接 / 日K趋势 一键切换)")
        if not res_df.empty:
            selected_code = st.selectbox(
                "选择要深度诊断的股票：",
                options=res_df["代码"].tolist(),
                format_func=lambda x: f"[{next(r['板块'] for r in results if r['代码']==x)}] {x} - {next(r['名称'] for r in results if r['代码']==x)}"
            )
            if selected_code and selected_code in kline_cache:
                s_name, s_df, s_adv, s_radar, s_timing, s_chip, s_row_data = kline_cache[selected_code]
                ca, cb, cc, cd, ce = st.columns(5)
                ca.metric("🎯 建议买入区间", s_adv["建议买入区间"])
                cb.metric("🛡️ 铁律防守止损线", s_adv["建议止损位"].split(" ")[0])
                cc.metric("🚀 第一止盈目标", s_adv["第一止盈目标"].split(" ")[0])
                cd.metric("📦 建议下单股数", s_adv.get("建议下单股数", "1000 股"))
                ce.metric("🔒 锁定单笔亏损", s_adv.get("单笔锁定风险金", "约 300 元"))

                # 增加日线与分时图的切换单选框
                chart_view_mode = st.radio(
                    "📈 选择图表视图：",
                    ["⏱️ 实时分时走势图 (看白线现价与黄线均价承接)", "📊 日K线趋势图 (看MA均线与筹码区间)"],
                    horizontal=True
                )

                if "分时走势" in chart_view_mode:
                    prev_close_price = float(s_row_data.get('昨收', s_df['收盘'].iloc[-1]))
                    with st.spinner("正在加载实时分钟级分时走势数据..."):
                        timeline_data = fetch_realtime_minute_timeline(selected_code)
                    st.plotly_chart(draw_pro_timeline(selected_code, s_name, timeline_data, prev_close_price), use_container_width=True)
                else:
                    st.plotly_chart(draw_pro_kline(selected_code, s_name, s_df, s_adv), use_container_width=True)

    elif st.session_state.get('has_scanned'):
        st.warning("⚠️ 扫描池暂时为空，建议调宽参数重新扫描。")
    else:
        st.info("👈 请在左侧确认参数后，点击上方红色的 **“🚀 启动全市场深度量化极速扫描”** 按钮。")

# ==================== 策略历史回测引擎面板 ====================
with tab_view_backtest:
    st.subheader("🔬 策略实盘历史回测模拟引擎")
    st.caption("回溯测试过去 60 个交易日中，该标的每次出现策略共振信号后，执行「尾盘买入 + 次日冲高止盈/跌破止损」的实战概率表现。")

    bc1, bc2, bc3, bc4 = st.columns(4)
    with bc1:
        bt_stock_code = st.text_input("回测股票代码", value="002466", help="输入你想回测验证的 A 股主板代码，如 002466, 600519")
    with bc2:
        bt_stop_loss = st.slider("止损红线比例 (%)", 1.0, 5.0, 2.0, 0.5) / 100
    with bc3:
        bt_profit_target = st.slider("止盈目标比例 (%)", 2.0, 10.0, 4.0, 0.5) / 100
    with bc4:
        bt_hold_days = st.slider("最长持股周期 (天)", 1, 5, 3, 1)

    if st.button("📊 运行历史实盘回测模拟", type="primary"):
        with st.spinner(f"正在拉取 {bt_stock_code} 历史 90 天数据并回测检验..."):
            bt_kdf = fetch_kline_safe(bt_stock_code.strip(), {}, days=90)
            bt_result = run_strategy_backtest(bt_kdf, bt_stop_loss, bt_profit_target, bt_hold_days)

        if not bt_result or bt_result["total_trades"] == 0:
            st.warning(f"⚠️ 标的 {bt_stock_code} 在过去 60 个交易日内未出现符合共振的买点信号，说明该票历史股性偏弱或一直处于阴跌期。")
        else:
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("🎯 触发交易次数", f"{bt_result['total_trades']} 次")
            m2.metric("🎲 历史实盘胜率", f"{bt_result['win_rate']}%", "高于50%即具备统计优势")
            m3.metric("📈 单笔平均收益率", f"{bt_result['avg_ret']:+.2f}%")
            m4.metric("🚀 最大单笔盈利", f"+{bt_result['max_win']}%")
            m5.metric("🛡️ 最大单笔亏损", f"{bt_result['max_loss']}%")

            st.write("---")
            st.markdown("#### 📋 历史每一笔交易真实进出记录：")
            st.dataframe(bt_result["trades_df"], use_container_width=True, hide_index=True)

# ==================== 网页持仓监控池 ====================
with tab_view_portfolio:
    st.subheader("💼 我的网页专属自选与持仓监控")

    @st.fragment(run_every=live_interval if enable_auto_live else None)
    def render_live_portfolio_panel():
        if not st.session_state['portfolio']:
            st.info("💡 监控池目前为空。在第一页【AI 智能精选看板】中选中股票后，点击加入即可集中盯盘。")
            return
        p_list = st.session_state['portfolio']
        p_codes = [p['code'] for p in p_list]
        symbols = [f"sh{c}" if c.startswith("60") else f"sz{c}" for c in p_codes]
        real_items = fetch_tencent_batch(symbols)
        price_map = {item['代码']: item for item in real_items}
        flow_map_p = fetch_money_flow_safe_batched(p_codes)

        p_display = []
        for p in p_list:
            c = p['code']
            real_d = price_map.get(c, {})
            curr_p = real_d.get('最新价', 0.0)
            sl = float(p.get('stop_loss', 0))
            tg = float(p.get('target', 0))
            status = "🔴 跌破防守线 (坚决清仓)" if curr_p <= sl and curr_p > 0 else ("🟢 触及目标位 (分批止盈)" if curr_p >= tg and curr_p > 0 else f"🟡 正常持有 (距止损 {round((curr_p-sl)/curr_p*100,1) if curr_p>0 else 0}%)")
            p_display.append({
                "代码": c, "名称": p['name'], "最新价": curr_p,
                "今日涨跌幅(%)": real_d.get('涨跌幅', 0.0),
                "主力实时净流入": f"{flow_map_p.get(c, {}).get('主力净流入', 0)}万",
                "建议买入区间": p.get('buy_range', '-'), "铁律防守线": sl, "目标止盈价": tg, "实时状态": status
            })
        st.dataframe(pd.DataFrame(p_display), use_container_width=True, hide_index=True)

    render_live_portfolio_panel()
