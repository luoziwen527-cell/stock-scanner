import os
import time
import urllib.request
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# 彻底屏蔽代理拦截，直连国内金融数据
for k in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']:
    os.environ.pop(k, None)
urllib.request.getproxies = lambda: {}

import streamlit as st
import akshare as ak
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime

st.set_page_config(page_title="AI 智能量化投研选股系统", layout="wide", page_icon="🧠")
st.title("🧠 AI 智能多维量化投研选股系统 (60 / 00 主板)")
st.caption("全市场主板覆盖 · AI 动态多因子评分 · 智能保底机制 · 专业量价 K 线")

# 初始化 Session 状态
if 'scan_results' not in st.session_state:
    st.session_state['scan_results'] = []
if 'kline_cache' not in st.session_state:
    st.session_state['kline_cache'] = {}
if 'has_scanned' not in st.session_state:
    st.session_state['has_scanned'] = False

# ----------------- 侧边栏配置 -----------------
with st.sidebar:
    st.header("⚙️ 选股模式与参数")
    engine_mode = st.radio("选择选股引擎", [
        "🧠 AI 多因子综合评分 (推荐，按综合得分排序)",
        "🏆 高胜率共振策略",
        "🔥 短线游资/爆发战法",
        "📈 经典均线与波段"
    ])
    
    st.divider()
    board_type = st.selectbox("市场板块", ["全部主板 (60 + 00)", "仅沪市主板 (60开头)", "仅深市主板 (00开头)"])
    max_price = st.slider("最高股价上限 (元)", min_value=5.0, max_value=100.0, value=20.0, step=1.0)
    
    if "AI 多因子" in engine_mode:
        min_score = st.slider("入选基准评分", min_value=50, max_value=90, value=65, step=5)
    else:
        min_score = 60
        
    st.divider()
    if engine_mode == "🏆 高胜率共振策略":
        strategy_choice = st.selectbox("策略选项", [
            "1. 均线粘合一阳穿多线", "2. 首板次日缩量十字星", "3. 缩量双底红三兵"
        ])
    elif engine_mode == "🔥 短线游资/爆发战法":
        strategy_choice = st.selectbox("战法选项", [
            "1. 底部首板启动", "2. 龙头首阴回踩", "3. 弱转强反包", "4. 尾盘抢筹低吸"
        ])
    elif engine_mode == "📈 经典均线与波段":
        strategy_choice = st.selectbox("策略选项", [
            "1. 均线多头排列", "2. 突破60日平台", "3. 回踩年线支撑", "4. 放量突破"
        ])
    else:
        strategy_choice = "AI_SCORE"

# ----------------- 数据源：单次全局拉取并本地过滤（极速无限流） -----------------
@st.cache_data(ttl=1800)
def get_filtered_stock_pool(b_type: str, price_cap: float):
    # 单次请求拉取全市场实时行情
    try:
        df = ak.stock_zh_a_spot_em()
    except Exception:
        df = ak.stock_zh_a_spot()

    col_map = {
        '代码': '代码', 'code': '代码', 'symbol': '代码', '证券代码': '代码',
        '名称': '名称', 'name': '名称', '证券名称': '名称',
        '最新价': '最新价', 'trade': '最新价', 'price': '最新价',
        '涨跌幅': '涨跌幅', 'changepercent': '涨跌幅'
    }
    df = df.rename(columns=col_map)
    df['代码'] = df['代码'].astype(str).str.extract(r'(\d{6})')[0]
    df = df.dropna(subset=['代码'])

    # 1. 主板过滤 (60 / 00)
    if "仅沪市" in b_type:
        df = df[df['代码'].str.startswith('60')]
    elif "仅深市" in b_type:
        df = df[df['代码'].str.startswith('00')]
    else:
        df = df[df['代码'].str.startswith(('60', '00'))]

    # 2. 剔除 ST / 退市
    if '名称' in df.columns:
        df = df[~df['名称'].str.contains("ST|退")]

    # 3. 价格在本地直接过滤 ≤ 20 元（大幅缩减需要拉 K 线的数量）
    df['最新价'] = pd.to_numeric(df['最新价'], errors='coerce').fillna(0)
    df = df[(df['最新价'] <= price_cap) & (df['最新价'] > 0)]

    return df[['代码', '名称', '最新价', '涨跌幅']].reset_index(drop=True)

# ----------------- AI 智能五维多因子评分 -----------------
def evaluate_smart_score(df: pd.DataFrame) -> tuple[int, list, dict, dict]:
    close = df['收盘'].values
    open_p = df['开盘'].values
    vol = df['成交量'].values
    high = df['最高'].values
    low = df['最低'].values
    
    ma5 = df['收盘'].rolling(5).mean().iloc[-1]
    ma10 = df['收盘'].rolling(10).mean().iloc[-1]
    ma20 = df['收盘'].rolling(20).mean().iloc[-1]
    ma60 = df['收盘'].rolling(60).mean().iloc[-1]
    vol_ma5 = df['成交量'].rolling(5).mean().iloc[-1]
    
    radar = {}
    if close[-1] >= ma5 >= ma10 >= ma20: radar["均线趋势"] = 20
    elif close[-1] >= ma20: radar["均线趋势"] = 15
    elif close[-1] >= ma60: radar["均线趋势"] = 12
    else: radar["均线趋势"] = 8
    
    vol_ratio = vol[-1] / vol_ma5 if vol_ma5 > 0 else 1.0
    if 1.2 <= vol_ratio <= 3.0: radar["资金量能"] = 20
    elif vol_ratio > 3.0: radar["资金量能"] = 16
    elif vol[-1] < vol_ma5 * 0.75: radar["资金量能"] = 15
    else: radar["资金量能"] = 10
    
    max_60 = np.max(close[-60:-1])
    if close[-1] >= max_60 * 0.97: radar["突破动能"] = 20
    elif close[-1] >= ma60: radar["突破动能"] = 14
    else: radar["突破动能"] = 8
    
    if close[-1] >= open_p[-1]:
        body = (close[-1] - open_p[-1]) / (high[-1] - low[-1] + 1e-5)
        radar["K线形态"] = 18 if body >= 0.4 else 14
    else:
        radar["K线形态"] = 10
        
    radar["风控空间"] = 18 if close[-1] >= ma10 else 12
    total_score = sum(radar.values())
    
    tags = []
    if radar["均线趋势"] >= 15: tags.append("均线多头支撑")
    if radar["资金量能"] >= 16: tags.append(f"量能活跃({vol_ratio:.1f}倍)")
    if radar["突破动能"] >= 14: tags.append("站稳中长期均线")
    if close[-1] >= open_p[-1]: tags.append("收阳线")
    
    resistance = round(float(np.max(high[-30:])), 2)
    support = round(float(min(ma20, low[-1])), 2)
    stop_loss = round(support * 0.97, 2)
    take_profit = round(close[-1] * 1.08, 2)
    
    advice = {
        "建议买入区间": f"{round(close[-1]*0.99, 2)} ~ {close[-1]}",
        "建议止损位": f"{stop_loss} (-{round((close[-1]-stop_loss)/close[-1]*100, 1)}%)",
        "第一止盈目标": f"{take_profit} (+8.0%)",
        "动态压力位": resistance,
        "动态支撑位": support
    }
    return total_score, tags, advice, radar

# ----------------- 具体策略匹配 -----------------
def check_custom_strategy(df: pd.DataFrame, strat_name: str) -> tuple[bool, str]:
    if len(df) < 60: return False, ""
    close = df['收盘'].values
    open_p = df['开盘'].values
    vol = df['成交量'].values
    pct_chg = df['涨跌幅'].values
    high = df['最高'].values
    low = df['最低'].values
    vol_ma5 = df['成交量'].rolling(5).mean().iloc[-1]
    
    if "均线粘合一阳穿多线" in strat_name:
        ma5, ma10, ma20 = df['收盘'].rolling(5).mean().iloc[-1], df['收盘'].rolling(10).mean().iloc[-1], df['收盘'].rolling(20).mean().iloc[-1]
        is_converge = (max([ma5, ma10, ma20]) - min([ma5, ma10, ma20])) / min([ma5, ma10, ma20]) <= 0.045
        return (is_converge and close[-1] >= max([ma5, ma10, ma20]) and pct_chg[-1] >= 1.5), "均线高度粘合后放量起爆"
    elif "首板次日缩量十字星" in strat_name:
        if len(df) < 15: return False, ""
        yesterday_limit = pct_chg[-2] >= 9.0
        amplitude = (high[-1] - low[-1]) / close[-2] * 100
        return (yesterday_limit and amplitude <= 6.0 and vol[-1] <= vol[-2] * 0.85), "首板后缩量十字星蓄势"
    elif "缩量双底红三兵" in strat_name:
        three_yang = all(close[-i] >= open_p[-i] for i in range(1, 4))
        return (three_yang and close[-1] >= df['收盘'].rolling(20).mean().iloc[-1] * 0.98), "地量连续小阳线回踩企稳"
    elif "底部首板启动" in strat_name:
        return (pct_chg[-1] >= 9.0 and vol[-1] >= vol_ma5 * 1.2), "底部放量涨停突破"
    elif "龙头首阴回踩" in strat_name:
        had_limit = (pct_chg[-4:-1] >= 9.0).any()
        return (had_limit and pct_chg[-1] <= 1.0 and low[-1] >= df['收盘'].rolling(5).mean().iloc[-1] * 0.96), "涨停后首阴踩5日线"
    elif "弱转强反包" in strat_name:
        return (close[-1] >= high[-2] * 0.99 and pct_chg[-1] >= 2.0), "阳线反包昨日高点"
    elif "尾盘抢筹低吸" in strat_name:
        return (1.0 <= pct_chg[-1] <= 7.0 and close[-1] >= open_p[-1] and vol[-1] >= vol_ma5 * 1.1), "尾盘温和放量上攻"
    elif "均线多头排列" in strat_name:
        ma30 = df['收盘'].rolling(30).mean()
        return (ma30.iloc[-1] >= ma30.iloc[-10] and close[-1] >= df['收盘'].rolling(5).mean().iloc[-1]), "均线稳步向上发散"
    elif "突破60日平台" in strat_name:
        return (close[-1] >= df['收盘'].rolling(60).mean().iloc[-1] and vol[-1] >= vol_ma5 * 1.1), "放量站上60日生命线"
    elif "回踩年线支撑" in strat_name:
        if len(df) < 250: return False, ""
        ma250 = df['收盘'].rolling(250).mean().iloc[-1]
        return (close[-1] >= ma250 * 0.97 and low[-1] <= ma250 * 1.05), "回踩年线支撑位有效"
    elif "放量突破" in strat_name:
        return (close[-1] > open_p[-1] and vol[-1] >= vol_ma5 * 1.3), "放量收阳突破"
    return False, ""

# ----------------- 单个股票处理任务（带自动重试） -----------------
def fetch_kline_with_retry(code):
    for _ in range(2):
        try:
            k_df = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq")
            if k_df is not None and len(k_df) >= 60:
                return k_df
        except Exception:
            time.sleep(0.2)
    return None

def worker_task(code, name, engine_mode, strat_choice, min_score):
    k_df = fetch_kline_with_retry(code)
    if k_df is None: return None
    
    last_close = float(k_df['收盘'].iloc[-1])
    score, tags, advice, radar = evaluate_smart_score(k_df)
    
    is_hit = False
    reason = ""
    if "AI 多因子" in engine_mode:
        if score >= min_score:
            is_hit = True
            reason = " | ".join(tags) if tags else "多因子综合共振"
    else:
        is_hit, reason = check_custom_strategy(k_df, strat_choice)
        
    k_df['MA5'] = k_df['收盘'].rolling(5).mean()
    k_df['MA10'] = k_df['收盘'].rolling(10).mean()
    k_df['MA20'] = k_df['收盘'].rolling(20).mean()
    k_df['MA60'] = k_df['收盘'].rolling(60).mean()
    
    item = {
        "代码": code,
        "名称": name,
        "综合评分": score,
        "AI评级": "🔥 强力推荐" if score >= 80 else ("⭐ 重点关注" if score >= 70 else "👀 观察标的"),
        "最新价": last_close,
        "涨跌幅(%)": k_df['涨跌幅'].iloc[-1],
        "量化特征": reason if reason else " | ".join(tags),
        "advice": advice,
        "radar": radar,
        "k_df": k_df
    }
    return (item, is_hit)

# ----------------- 图表渲染函数 -----------------
def draw_radar_chart(radar_data: dict):
    cats = list(radar_data.keys()) + [list(radar_data.keys())[0]]
    vals = list(radar_data.values()) + [list(radar_data.values())[0]]
    fig = go.Figure(go.Scatterpolar(r=vals, theta=cats, fill='toself', fillcolor='rgba(255, 75, 75, 0.35)', line=dict(color='#ff4b4b', width=2)))
    fig.update_layout(polar=dict(radialaxis=dict(visible=True, range=[0, 20])), showlegend=False, height=250, margin=dict(l=25, r=25, t=25, b=25))
    return fig

def draw_pro_kline(code, name, k_df, advice):
    recent = k_df.tail(70).copy()
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.72, 0.28])
    fig.add_trace(go.Candlestick(x=recent['日期'], open=recent['开盘'], high=recent['最高'], low=recent['最低'], close=recent['收盘'], increasing_line_color='#ef5350', decreasing_line_color='#26a69a', name="K线"), row=1, col=1)
    fig.add_trace(go.Scatter(x=recent['日期'], y=recent['MA5'], line=dict(color='#ff9800', width=1.2), name="MA5"), row=1, col=1)
    fig.add_trace(go.Scatter(x=recent['日期'], y=recent['MA10'], line=dict(color='#2196f3', width=1.2), name="MA10"), row=1, col=1)
    fig.add_trace(go.Scatter(x=recent['日期'], y=recent['MA20'], line=dict(color='#9c27b0', width=1.2), name="MA20"), row=1, col=1)
    fig.add_trace(go.Scatter(x=recent['日期'], y=recent['MA60'], line=dict(color='#4caf50', width=1.5), name="MA60"), row=1, col=1)
    fig.add_hline(y=advice["动态压力位"], line_dash="dot", line_color="#ff1744", annotation_text=f"前高阻力: {advice['动态压力位']}元", row=1, col=1)
    fig.add_hline(y=advice["动态支撑位"], line_dash="dash", line_color="#00e676", annotation_text=f"强支撑位: {advice['动态支撑位']}元", row=1, col=1)
    vol_colors = ['#ef5350' if c >= o else '#26a69a' for c, o in zip(recent['收盘'], recent['开盘'])]
    fig.add_trace(go.Bar(x=recent['日期'], y=recent['成交量'], marker_color=vol_colors, name="成交量"), row=2, col=1)
    fig.update_layout(title=f"📈 {code} {name} 智能量价与支撑阻力图", xaxis_rangeslider_visible=False, height=520, margin=dict(l=10, r=10, t=40, b=10), legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
    return fig

# ----------------- 执行按钮 -----------------
if st.button("🚀 启动智能量化极速扫描", type="primary", use_container_width=True):
    with st.spinner(f"正在秒级获取 60 / 00 主板 ≤ {max_price} 元标的池..."):
        try:
            pool = get_filtered_stock_pool(board_type, max_price)
        except Exception as e:
            st.error(f"获取股票池失败：{e}")
            st.stop()
            
    total_count = len(pool)
    if total_count == 0:
        st.error(f"❌ 未找到符合板块且价格 ≤ {max_price} 元的主板标的。")
        st.stop()

    st.info(f"💡 本地已精准锁定符合价格（≤ {max_price} 元）的主板股票共 **{total_count}** 只，正在高速投研分析中...")

    hit_results = []
    all_scored_results = []
    new_kline_cache = {}
    progress_bar = st.progress(0, text="并发投研中...")
    
    # 采用 8 线程适度并发，避免触发接口限流
    completed = 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(worker_task, str(row['代码']).zfill(6), row['名称'], engine_mode, strategy_choice, min_score)
            for _, row in pool.iterrows()
        ]
        for future in as_completed(futures):
            completed += 1
            res_tuple = future.result()
            if res_tuple:
                item, is_hit = res_tuple
                k_df = item.pop("k_df")
                all_scored_results.append(item)
                new_kline_cache[item["代码"]] = (item["名称"], k_df, item["advice"], item["radar"])
                if is_hit:
                    hit_results.append(item)
                    
            if completed % 15 == 0 or completed == total_count:
                progress_bar.progress(completed / total_count, text=f"扫描进度: {completed}/{total_count} 只 (已成功解析 {len(all_scored_results)} 只)")
                
    progress_bar.empty()
    
    # 保底机制：若选股为 0，展示评分前 20 只
    if not hit_results and all_scored_results:
        all_scored_results = sorted(all_scored_results, key=lambda x: x["综合评分"], reverse=True)
        hit_results = all_scored_results[:20]
        for r in hit_results:
            r["量化特征"] = "💡 [智能保底推荐] 综合评分靠前标的"
            
    hit_results = sorted(hit_results, key=lambda x: x["综合评分"], reverse=True)
    st.session_state['scan_results'] = hit_results
    st.session_state['kline_cache'] = new_kline_cache
    st.session_state['has_scanned'] = True

# ----------------- 结果展示与交互区 -----------------
if st.session_state['scan_results']:
    results = st.session_state['scan_results']
    kline_cache = st.session_state['kline_cache']
    res_df = pd.DataFrame(results)
    
    st.success(f"🎉 投研完成！共精选呈现 **{len(res_df)}** 只优质标的。")
    
    col1, col2 = st.columns([1.15, 1.35])
    with col1:
        st.subheader("📋 命中评级清单 (高分优先)")
        st.dataframe(res_df[["代码", "名称", "综合评分", "AI评级", "最新价", "涨跌幅(%)", "量化特征"]], use_container_width=True, hide_index=True)
        st.download_button("📥 导出清单为 CSV", data=res_df.to_csv(index=False).encode('utf-8-sig'), file_name=f"选股结果_{datetime.today().strftime('%Y%m%d')}.csv")
        
    with col2:
        st.subheader("📊 AI 决策中枢与五维诊断")
        selected_code = st.selectbox("选择要诊断的股票：", options=[r["代码"] for r in results], format_func=lambda x: f"[{next(r['综合评分'] for r in results if r['代码'] == x)}分] {x} - {next(r['名称'] for r in results if r['代码'] == x)}")
        if selected_code and selected_code in kline_cache:
            s_name, s_df, s_adv, s_radar = kline_cache[selected_code]
            
            c_a, c_b, c_c = st.columns(3)
            c_a.metric("🎯 建议买入区间", s_adv["建议买入区间"])
            c_b.metric("🛡️ 建议止损位", s_adv["建议止损位"])
            c_c.metric("🚀 第一止盈目标", s_adv["第一止盈目标"])
            
            r1, r2 = st.columns([1, 1.8])
            with r1:
                st.plotly_chart(draw_radar_chart(s_radar), use_container_width=True)
            with r2:
                st.markdown(f"- **趋势多头**：{s_radar['均线趋势']}/20 分\n- **量能资金**：{s_radar['资金量能']}/20 分\n- **突破动能**：{s_radar['突破动能']}/20 分\n- **动态压力**：`{s_adv['动态压力位']} 元`\n- **动态支撑**：`{s_adv['动态支撑位']} 元`")
                
            st.plotly_chart(draw_pro_kline(selected_code, s_name, s_df, s_adv), use_container_width=True)
elif not st.session_state['has_scanned']:
    st.info("👈 请确认左侧策略与参数后，点击上方红色的 **“🚀 启动智能量化极速扫描”** 按钮开始选股。")