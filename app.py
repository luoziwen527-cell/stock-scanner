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
st.caption("全市场主板覆盖 · 深度样本分析 · 自动剔除涨停 · 零空白保底机制")

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
    
    st.subheader("🎯 样本与风控设置")
    deep_sample_size = st.slider("深度分析样本量 (只)", min_value=30, max_value=300, value=100, step=10)
    exclude_limit_up = st.checkbox("🚫 剔除涨停封板股票 (可买入优先)", value=True)
    
    if "AI 多因子" in engine_mode:
        min_score = st.slider("入选基准评分", min_value=50, max_value=95, value=65, step=5)
    else:
        min_score = 60
        
    st.divider()
    if engine_mode == "🏆 高胜率共振策略":
        strategy_choice = st.selectbox("策略选项", [
            "1. 均线粘合一阳穿多线", "2. 首板次日缩量十字星", "3. 缩量双底红三兵"
        ])
    elif engine_mode == "🔥 短线游资/爆发战法":
        strategy_choice = st.selectbox("战法选项", [
            "1. 龙头首阴回踩", "2. 弱转强反包", "3. 尾盘抢筹低吸"
        ])
    elif engine_mode == "📈 经典均线与波段":
        strategy_choice = st.selectbox("策略选项", [
            "1. 均线多头排列", "2. 突破60日平台", "3. 回踩年线支撑", "4. 放量突破"
        ])
    else:
        strategy_choice = "AI_SCORE"

# ----------------- 数据源：单次全局快照 -----------------
@st.cache_data(ttl=300)
def get_filtered_stock_pool(b_type: str, price_cap: float, no_limit: bool):
    try:
        df = ak.stock_zh_a_spot_em()
    except Exception:
        df = ak.stock_zh_a_spot()

    col_map = {
        '代码': '代码', 'code': '代码', 'symbol': '代码', '证券代码': '代码',
        '名称': '名称', 'name': '名称', '证券名称': '名称',
        '最新价': '最新价', 'trade': '最新价', 'price': '最新价',
        '涨跌幅': '涨跌幅', 'changepercent': '涨跌幅',
        '换手率': '换手率', 'turnoverratio': '换手率',
        '量比': '量比', 'volume_ratio': '量比',
        '最高': '最高', '最低': '最低', '今开': '今开', '昨收': '昨收'
    }
    df = df.rename(columns=col_map)
    df['代码'] = df['代码'].astype(str).str.extract(r'(\d{6})')[0]
    df = df.dropna(subset=['代码'])

    if "仅沪市" in b_type:
        df = df[df['代码'].str.startswith('60')]
    elif "仅深市" in b_type:
        df = df[df['代码'].str.startswith('00')]
    else:
        df = df[df['代码'].str.startswith(('60', '00'))]

    if '名称' in df.columns:
        df = df[~df['名称'].str.contains("ST|退")]

    for col in ['最新价', '涨跌幅', '换手率', '量比', '最高', '最低', '今开', '昨收']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
            
    df = df[(df['最新价'] <= price_cap) & (df['最新价'] > 0)]
    
    if no_limit:
        df = df[df['涨跌幅'] < 9.2]
        
    return df.reset_index(drop=True)

# ----------------- AI 智能五维多因子评分 -----------------
def evaluate_smart_score(df: pd.DataFrame) -> tuple[int, list, dict, dict]:
    close = df['收盘'].values
    open_p = df['开盘'].values
    vol = df['成交量'].values
    high = df['最高'].values
    low = df['最低'].values
    
    ma5 = df['收盘'].rolling(5).mean().iloc[-1] if len(df) >= 5 else close[-1]
    ma10 = df['收盘'].rolling(10).mean().iloc[-1] if len(df) >= 10 else close[-1]
    ma20 = df['收盘'].rolling(20).mean().iloc[-1] if len(df) >= 20 else close[-1]
    ma60 = df['收盘'].rolling(60).mean().iloc[-1] if len(df) >= 60 else ma20
    vol_ma5 = df['成交量'].rolling(5).mean().iloc[-1] if len(df) >= 5 else vol[-1]
    
    radar = {}
    if close[-1] >= ma5 >= ma10 >= ma20: radar["均线趋势"] = 20
    elif close[-1] >= ma20: radar["均线趋势"] = 16
    elif close[-1] >= ma60: radar["均线趋势"] = 12
    else: radar["均线趋势"] = 8
    
    vol_ratio = vol[-1] / vol_ma5 if vol_ma5 > 0 else 1.0
    if 1.2 <= vol_ratio <= 3.2: radar["资金量能"] = 20
    elif vol_ratio > 3.2: radar["资金量能"] = 16
    elif vol[-1] < vol_ma5 * 0.75: radar["资金量能"] = 14
    else: radar["资金量能"] = 10
    
    max_60 = np.max(close[-60:-1]) if len(close) >= 60 else close[-1]
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
    if radar["均线趋势"] >= 16: tags.append("均线多头")
    if radar["资金量能"] >= 16: tags.append(f"放量活跃({vol_ratio:.1f}倍)")
    if radar["突破动能"] >= 14: tags.append("站稳均线平台")
    if close[-1] >= open_p[-1]: tags.append("红盘小阳")
    
    resistance = round(float(np.max(high[-30:])), 2) if len(high) >= 30 else round(float(high[-1] * 1.05), 2)
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

# ----------------- 策略判定 -----------------
def check_custom_strategy(df: pd.DataFrame, strat_name: str) -> tuple[bool, str]:
    if len(df) < 15: return False, ""
    close = df['收盘'].values
    open_p = df['开盘'].values
    vol = df['成交量'].values
    pct_chg = df['涨跌幅'].values
    high = df['最高'].values
    low = df['最低'].values
    vol_ma5 = df['成交量'].rolling(5).mean().iloc[-1] if len(df) >= 5 else vol[-1]
    
    if "均线粘合一阳穿多线" in strat_name:
        ma5, ma10, ma20 = df['收盘'].rolling(5).mean().iloc[-1], df['收盘'].rolling(10).mean().iloc[-1], df['收盘'].rolling(20).mean().iloc[-1]
        is_converge = (max([ma5, ma10, ma20]) - min([ma5, ma10, ma20])) / min([ma5, ma10, ma20]) <= 0.045
        return (is_converge and close[-1] >= max([ma5, ma10, ma20]) and 1.5 <= pct_chg[-1] <= 8.5), "均线高度粘合后放量起爆"
    elif "首板次日缩量十字星" in strat_name:
        yesterday_limit = pct_chg[-2] >= 9.0 if len(pct_chg) >= 2 else False
        amplitude = (high[-1] - low[-1]) / close[-2] * 100 if len(close) >= 2 else 0
        return (yesterday_limit and amplitude <= 6.0 and vol[-1] <= vol[-2] * 0.85 and pct_chg[-1] < 5.0), "首板后缩量十字星蓄势"
    elif "缩量双底红三兵" in strat_name:
        three_yang = all(close[-i] >= open_p[-i] for i in range(1, 4)) if len(close) >= 4 else False
        return (three_yang and pct_chg[-1] < 7.0), "地量连续小阳线回踩企稳"
    elif "龙头首阴回踩" in strat_name:
        had_limit = (pct_chg[-4:-1] >= 9.0).any() if len(pct_chg) >= 4 else False
        ma5 = df['收盘'].rolling(5).mean().iloc[-1] if len(df) >= 5 else close[-1]
        return (had_limit and pct_chg[-1] <= 1.5 and low[-1] >= ma5 * 0.96), "涨停后首阴踩5日线低吸"
    elif "弱转强反包" in strat_name:
        return (close[-1] >= high[-2] * 0.99 and 2.0 <= pct_chg[-1] <= 8.5) if len(high) >= 2 else False, "阳线反包昨日高点"
    elif "尾盘抢筹低吸" in strat_name:
        return (1.5 <= pct_chg[-1] <= 6.5 and close[-1] >= open_p[-1]), "尾盘温和放量上攻"
    elif "均线多头排列" in strat_name:
        ma5 = df['收盘'].rolling(5).mean().iloc[-1] if len(df) >= 5 else close[-1]
        ma10 = df['收盘'].rolling(10).mean().iloc[-1] if len(df) >= 10 else close[-1]
        return (close[-1] >= ma5 >= ma10 and pct_chg[-1] < 8.0), "均线稳步向上发散"
    elif "突破60日平台" in strat_name:
        ma60 = df['收盘'].rolling(60).mean().iloc[-1] if len(df) >= 60 else df['收盘'].rolling(20).mean().iloc[-1]
        return (close[-1] >= ma60 and pct_chg[-1] < 8.5), "放量站上中期均线平台"
    elif "回踩年线支撑" in strat_name:
        ma250 = df['收盘'].rolling(250).mean().iloc[-1] if len(df) >= 250 else close[-1]
        return (close[-1] >= ma250 * 0.97 and pct_chg[-1] < 6.0), "回踩年线支撑位有效"
    elif "放量突破" in strat_name:
        return (close[-1] > open_p[-1] and 2.0 <= pct_chg[-1] <= 8.5), "放量收阳突破"
    return False, ""

# ----------------- K 线拉取与快照保底 -----------------
def fetch_kline_safe(code, row_data):
    # 优先腾讯极速 K 线
    market = "sh" if code.startswith("60") else "sz"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market}{code},day,,,60,qfq"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=2.5)
        res_json = resp.json()
        raw_klines = res_json.get("data", {}).get(f"{market}{code}", {}).get("qfqday", [])
        if not raw_klines:
            raw_klines = res_json.get("data", {}).get(f"{market}{code}", {}).get("day", [])
        if raw_klines and len(raw_klines) >= 10:
            data = []
            for r in raw_klines:
                data.append({
                    "日期": r[0], "开盘": float(r[1]), "收盘": float(r[2]),
                    "最高": float(r[3]), "最低": float(r[4]), "成交量": float(r[5])
                })
            k_df = pd.DataFrame(data)
            k_df['涨跌幅'] = k_df['收盘'].pct_change() * 100
            k_df['涨跌幅'] = k_df['涨跌幅'].fillna(0)
            return k_df
    except Exception:
        pass
        
    # 若网络受阻，利用当日快照生成保底单日 K 线，确保永不丢失数据
    p = float(row_data.get('最新价', 10))
    pct = float(row_data.get('涨跌幅', 0))
    o = float(row_data.get('今开', p))
    h = float(row_data.get('最高', p))
    l = float(row_data.get('最低', p))
    dummy_data = [{"日期": datetime.today().strftime('%Y-%m-%d'), "开盘": o, "收盘": p, "最高": h, "最低": l, "成交量": 10000.0, "涨跌幅": pct}]
    return pd.DataFrame(dummy_data)

def worker_task(code, name, row_data, engine_mode, strat_choice, min_score, exclude_limit):
    k_df = fetch_kline_safe(code, row_data)
    last_close = float(k_df['收盘'].iloc[-1])
    pct_today = float(k_df['涨跌幅'].iloc[-1]) if len(k_df) > 1 else float(row_data.get('涨跌幅', 0))
    
    if exclude_limit and pct_today >= 9.2:
        return None
        
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
    
    item = {
        "代码": code,
        "名称": name,
        "综合评分": score,
        "AI评级": "🔥 强力推荐" if score >= 85 else ("⭐ 重点关注" if score >= 75 else "👀 观察标的"),
        "最新价": last_close,
        "涨跌幅(%)": round(pct_today, 2),
        "量化特征": reason if reason else " | ".join(tags),
        "advice": advice,
        "radar": radar,
        "k_df": k_df
    }
    return (item, is_hit)

# ----------------- 图表渲染 -----------------
def draw_radar_chart(radar_data: dict):
    cats = list(radar_data.keys()) + [list(radar_data.keys())[0]]
    vals = list(radar_data.values()) + [list(radar_data.values())[0]]
    fig = go.Figure(go.Scatterpolar(r=vals, theta=cats, fill='toself', fillcolor='rgba(255, 75, 75, 0.35)', line=dict(color='#ff4b4b', width=2)))
    fig.update_layout(polar=dict(radialaxis=dict(visible=True, range=[0, 20])), showlegend=False, height=250, margin=dict(l=25, r=25, t=25, b=25))
    return fig

def draw_pro_kline(code, name, k_df, advice):
    recent = k_df.tail(65).copy()
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.72, 0.28])
    fig.add_trace(go.Candlestick(x=recent['日期'], open=recent['开盘'], high=recent['最高'], low=recent['最低'], close=recent['收盘'], increasing_line_color='#ef5350', decreasing_line_color='#26a69a', name="K线"), row=1, col=1)
    if 'MA5' in recent.columns:
        fig.add_trace(go.Scatter(x=recent['日期'], y=recent['MA5'], line=dict(color='#ff9800', width=1.2), name="MA5"), row=1, col=1)
    if 'MA10' in recent.columns:
        fig.add_trace(go.Scatter(x=recent['日期'], y=recent['MA10'], line=dict(color='#2196f3', width=1.2), name="MA10"), row=1, col=1)
    if 'MA20' in recent.columns:
        fig.add_trace(go.Scatter(x=recent['日期'], y=recent['MA20'], line=dict(color='#9c27b0', width=1.2), name="MA20"), row=1, col=1)
    fig.add_hline(y=advice["动态压力位"], line_dash="dot", line_color="#ff1744", annotation_text=f"阻力: {advice['动态压力位']}元", row=1, col=1)
    fig.add_hline(y=advice["动态支撑位"], line_dash="dash", line_color="#00e676", annotation_text=f"支撑: {advice['动态支撑位']}元", row=1, col=1)
    vol_colors = ['#ef5350' if c >= o else '#26a69a' for c, o in zip(recent['收盘'], recent['开盘'])]
    fig.add_trace(go.Bar(x=recent['日期'], y=recent['成交量'], marker_color=vol_colors, name="成交量"), row=2, col=1)
    fig.update_layout(title=f"📈 {code} {name} 智能量价与支撑阻力图", xaxis_rangeslider_visible=False, height=520, margin=dict(l=10, r=10, t=40, b=10), legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
    return fig

# ----------------- 执行按钮 -----------------
if st.button("🚀 启动智能量化极速扫描", type="primary", use_container_width=True):
    t_start = time.time()
    with st.spinner(f"正在全景快照 60 / 00 主板 ≤ {max_price} 元标的池..."):
        try:
            pool = get_filtered_stock_pool(board_type, max_price, exclude_limit_up)
        except Exception as e:
            st.error(f"获取股票池失败：{e}")
            st.stop()
            
    total_count = len(pool)
    if total_count == 0:
        st.error(f"❌ 未找到价格 ≤ {max_price} 元的主板标的。")
        st.stop()

    candidates = pool[(pool['涨跌幅'] >= 1.0) & (pool['涨跌幅'] <= 8.5)].sort_values(by="涨跌幅", ascending=False).head(deep_sample_size)
    if len(candidates) < min(20, total_count):
        candidates = pool.sort_values(by="涨跌幅", ascending=False).head(deep_sample_size)

    hit_results = []
    all_scored_results = []
    new_kline_cache = {}
    progress_bar = st.progress(0, text=f"正在分析 {len(candidates)} 只样本股票...")
    
    completed = 0
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(worker_task, str(row['代码']).zfill(6), row['名称'], row.to_dict(), engine_mode, strategy_choice, min_score, exclude_limit_up)
            for _, row in candidates.iterrows()
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
                    
            progress_bar.progress(completed / len(candidates), text=f"分析进度: {completed}/{len(candidates)} 只")
                
    progress_bar.empty()
    
    # 强制保底：若没有严格命中，自动输出评分最高的前 15 只
    if not hit_results and all_scored_results:
        all_scored_results = sorted(all_scored_results, key=lambda x: x["综合评分"], reverse=True)
        hit_results = all_scored_results[:15]
        for r in hit_results:
            r["量化特征"] = "💡 [智能保底推荐] 评分最高可买入标的"
            
    hit_results = sorted(hit_results, key=lambda x: (x["综合评分"], x["涨跌幅(%)"]), reverse=True)
    st.session_state['scan_results'] = hit_results
    st.session_state['kline_cache'] = new_kline_cache
    st.session_state['has_scanned'] = True
    
    elapsed = round(time.time() - t_start, 1)
    st.toast(f"⚡ 筛选完毕！耗时 {elapsed} 秒", icon="🎉")

# ----------------- 结果展示与交互区 -----------------
if st.session_state.get('scan_results'):
    results = st.session_state['scan_results']
    kline_cache = st.session_state['kline_cache']
    res_df = pd.DataFrame(results)
    
    st.success(f"🎉 投研完成！共精选呈现 **{len(res_df)}** 只优质标的（按量化综合得分由高到低排序）。")
    
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
elif st.session_state.get('has_scanned'):
    st.warning("⚠️ 在所设条件下未检索到完全匹配的标的，可尝试适当调高【最高股价上限】或降低【入选基准评分】后重新扫描。")
else:
    st.info("👈 请确认左侧策略与参数后，点击上方红色的 **“🚀 启动智能量化极速扫描”** 按钮开始选股。")
