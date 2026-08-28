import os
import urllib.request
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# 屏蔽代理干扰，保障国内金融数据直连
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

try:
    import talib
    HAS_TALIB = True
except ImportError:
    HAS_TALIB = False

st.set_page_config(page_title="AI智能与量化严格选股系统", layout="wide", page_icon="🧠")
st.title("🧠 沪深主板 AI 智能多维选股系统 (60 / 00 主板)")
st.caption("东财直连 · 60日深度K线回溯 · AI五维雷达诊断 · 严格选股与止盈止损规划")

# ----------------- 初始化 Session 状态 -----------------
if 'scan_results' not in st.session_state:
    st.session_state['scan_results'] = []
if 'kline_cache' not in st.session_state:
    st.session_state['kline_cache'] = {}
if 'last_scan_info' not in st.session_state:
    st.session_state['last_scan_info'] = ""

# ----------------- 侧边栏：参数调节区 -----------------
with st.sidebar:
    st.header("⚙️ 选股模式与参数")
    engine_mode = st.radio("选择核心引擎", [
        "🧠 AI 智能多因子评分 (推荐，五维雷达严选)",
        "🔥 短线游资/爆发战法",
        "📈 经典波段/趋势策略",
        "🕯️ 经典K线形态识别"
    ])
    
    st.divider()
    board_type = st.selectbox("市场板块", ["全部主板 (60 + 00)", "仅沪市主板 (60开头)", "仅深市主板 (00开头)"])
    max_price = st.slider("最高股价上限 (元)", min_value=5.0, max_value=100.0, value=20.0, step=1.0)
    
    if "AI 智能多因子" in engine_mode:
        min_score = st.slider("入选评分门槛", min_value=60, max_value=95, value=75, step=5, help="推荐 75~80 分，严选高胜率标的")
    else:
        min_score = 70
        
    st.divider()
    scan_all = st.checkbox("扫描全市场主板 (3200+ 只)", value=True)
    if not scan_all:
        scan_limit = st.slider("自定义扫描数量", min_value=50, max_value=3000, value=300, step=50)

    st.divider()
    if engine_mode == "🔥 短线游资/爆发战法":
        strategy_choice = st.selectbox("选择短线战法", [
            "1. 底部首板启动 (筑底后首个放量涨停)",
            "2. 龙头首阴回踩 (涨停后缩量回踩5日线)",
            "3. 弱转强反包 (大阳线反包昨日高点)",
            "4. 尾盘抢筹低吸 (温和放量多头套利)"
        ])
    elif engine_mode == "📈 经典波段/趋势策略":
        strategy_choice = st.selectbox("选择波段策略", [
            "1. 均线多头 (MA30持续向上)",
            "2. 突破平台 (放量站上60日线)",
            "3. 回踩年线 (突破250日线缩量回踩)",
            "4. 停机坪策略 (涨停后高开平台蓄势)",
            "5. 放量上涨 (成交额活跃突破)",
            "6. 海龟交易法则 (创近60日新高)"
        ])
    elif engine_mode == "🕯️ 经典K线形态识别":
        pattern_choice = st.selectbox("选择K线形态", [
            ("CDLHAMMER", "锤头线 (Hammer - 底部看涨)"),
            ("CDLMORNINGSTAR", "早晨之星 (Morning Star - 底部反转)"),
            ("CDLENGULFING", "吞没形态 (Engulfing - 多头/空头吞噬)"),
            ("CDL3WHITESOLDIERS", "三个白兵 (Three White Soldiers - 强看涨)"),
            ("CDLPIERCING", "刺透形态 (Piercing - 底部看涨)"),
            ("CDL3BLACKCROWS", "三只乌鸦 (Three Black Crows - 顶部看跌)"),
            ("CDLDARKCLOUDCOVER", "乌云盖顶 (Dark Cloud - 顶部看跌)"),
            ("CDLSHOOTINGSTAR", "射击之星 (Shooting Star - 顶部看跌)"),
            ("CDLDOJI", "十字星 (Doji - 趋势转折)")
        ], format_func=lambda x: x[1])
    else:
        strategy_choice = "AI_SCORE"

# ----------------- 数据源：交易所官方主板代码表 -----------------
@st.cache_data(ttl=86400)
def get_main_board_pool(b_type: str):
    stock_list = []
    if "仅深市" not in b_type:
        try:
            df_sh = ak.stock_info_sh_name_code(symbol="主板A股")
            df_sh = df_sh[['证券代码', '证券简称']].rename(columns={'证券代码': '代码', '证券简称': '名称'})
            stock_list.append(df_sh)
        except Exception:
            pass
            
    if "仅沪市" not in b_type:
        try:
            df_sz = ak.stock_info_sz_name_code(symbol="A股列表")
            df_sz = df_sz[['A股代码', 'A股简称']].rename(columns={'A股代码': '代码', 'A股简称': '名称'})
            df_sz['代码'] = df_sz['代码'].astype(str).str.zfill(6)
            df_sz = df_sz[df_sz['代码'].str.startswith('00')]
            stock_list.append(df_sz)
        except Exception:
            pass

    if not stock_list:
        try:
            df_all = ak.stock_zh_a_spot_em()
            col_map = {'代码': '代码', '名称': '名称', 'code': '代码', 'name': '名称', 'symbol': '代码'}
            df_all = df_all.rename(columns=col_map)[['代码', '名称']]
            stock_list.append(df_all)
        except Exception:
            pass

    df = pd.concat(stock_list, ignore_index=True)
    df['代码'] = df['代码'].astype(str).str.zfill(6)
    
    if "仅沪市" in b_type:
        df = df[df['代码'].str.startswith('60')]
    elif "仅深市" in b_type:
        df = df[df['代码'].str.startswith('00')]
    else:
        df = df[df['代码'].str.startswith(('60', '00'))]
        
    df = df[~df['名称'].str.contains("ST|退")]
    return df.drop_duplicates(subset=['代码']).reset_index(drop=True)

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
    ma60 = df['收盘'].rolling(60).mean().iloc[-1] if len(df) >= 60 else ma20
    vol_ma5 = df['成交量'].rolling(5).mean().iloc[-1]
    
    radar = {}
    # 1. 均线趋势
    if close[-1] >= ma5 >= ma10 >= ma20: radar["均线趋势"] = 20
    elif close[-1] >= ma20: radar["均线趋势"] = 15
    elif close[-1] >= ma60: radar["均线趋势"] = 12
    else: radar["均线趋势"] = 8
    
    # 2. 资金量能
    vol_ratio = vol[-1] / vol_ma5 if vol_ma5 > 0 else 1.0
    if 1.3 <= vol_ratio <= 3.2: radar["资金量能"] = 20
    elif vol_ratio > 3.2: radar["资金量能"] = 16
    elif vol[-1] < vol_ma5 * 0.75: radar["资金量能"] = 14
    else: radar["资金量能"] = 10
    
    # 3. 突破动能
    max_60 = np.max(close[-60:-1]) if len(close) >= 60 else close[-1]
    if close[-1] >= max_60 * 0.98: radar["突破动能"] = 20
    elif close[-1] >= ma60: radar["突破动能"] = 14
    else: radar["突破动能"] = 8
    
    # 4. K线形态
    if close[-1] >= open_p[-1]:
        body = (close[-1] - open_p[-1]) / (high[-1] - low[-1] + 1e-5)
        radar["K线形态"] = 18 if body >= 0.4 else 14
    else:
        radar["K线形态"] = 10
        
    # 5. 风控空间
    radar["风控空间"] = 18 if close[-1] >= ma10 else 12
    total_score = sum(radar.values())
    
    tags = []
    if radar["均线趋势"] >= 15: tags.append("均线多头支撑")
    if radar["资金量能"] >= 16: tags.append(f"量能活跃({vol_ratio:.1f}倍)")
    if radar["突破动能"] >= 14: tags.append("站稳中长期均线")
    if close[-1] >= open_p[-1]: tags.append("红盘阳线")
    
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

# ----------------- 经典与短线战法引擎 -----------------
def execute_strategy(df: pd.DataFrame, strat_name: str) -> tuple[bool, str]:
    if len(df) < 60: return False, ""
    
    close = df['收盘'].values
    open_p = df['开盘'].values
    vol = df['成交量'].values
    pct_chg = df['涨跌幅'].values
    high = df['最高'].values
    low = df['最低'].values
    amount = df['成交额'].values if '成交额' in df.columns else vol * close * 100
    
    if "底部首板启动" in strat_name:
        is_limit_up = pct_chg[-1] >= 9.5
        no_limit_before = not (pct_chg[-60:-1] >= 9.5).any()
        vol_ma5 = df['成交量'].rolling(5).mean().iloc[-1]
        is_vol = vol[-1] >= vol_ma5 * 1.5
        if is_limit_up and no_limit_before and is_vol:
            return True, "底部近60日首个放量涨停板，建仓异动"

    elif "龙头首阴回踩" in strat_name:
        if len(df) < 10: return False, ""
        had_limit_up = (pct_chg[-4:-1] >= 9.5).any()
        is_yin = (close[-1] < open_p[-1]) or (pct_chg[-1] < 0)
        ma5 = df['收盘'].rolling(5).mean().iloc[-1]
        above_ma5 = low[-1] >= ma5 * 0.98
        shrink_vol = vol[-1] < vol[-2]
        if had_limit_up and is_yin and above_ma5 and shrink_vol:
            return True, "涨停后缩量首阴踩5日线，博弈次日反包"

    elif "弱转强反包" in strat_name:
        yesterday_drop = (pct_chg[-2] < 0) or (close[-2] < open_p[-2])
        today_strong = (close[-1] > high[-2]) and (pct_chg[-1] >= 3.5)
        vol_break = vol[-1] > vol[-2] * 1.1
        if yesterday_drop and today_strong and vol_break:
            return True, "放量阳线反包昨日阴线高点，分歧转一致"

    elif "尾盘抢筹低吸" in strat_name:
        c1 = (2.5 <= pct_chg[-1] <= 6.5) and (close[-1] > open_p[-1])
        ma5 = df['收盘'].rolling(5).mean().iloc[-1]
        ma10 = df['收盘'].rolling(10).mean().iloc[-1]
        ma20 = df['收盘'].rolling(20).mean().iloc[-1]
        c2 = close[-1] > ma5 > ma10 > ma20
        vol_ma5 = df['成交量'].rolling(5).mean().iloc[-1]
        c3 = vol[-1] >= vol_ma5 * 1.3
        turnover = df['换手率'].iloc[-1] if '换手率' in df.columns else 4.0
        c4 = 3.0 <= turnover <= 12.0
        if c1 and c2 and c3 and c4:
            return True, "尾盘温和放量拉升，均线多头排列，适合次日冲高套利"

    elif "均线多头" in strat_name:
        if len(df) < 65: return False, ""
        ma30 = df['收盘'].rolling(30).mean()
        c1 = ma30.iloc[-30] < ma30.iloc[-20] < ma30.iloc[-10] < ma30.iloc[-1]
        c2 = (ma30.iloc[-1] / ma30.iloc[-30]) > 1.15
        return (c1 and c2), "MA30趋势向上且增幅>15%"

    elif "突破平台" in strat_name:
        ma60 = df['收盘'].rolling(60).mean()
        c1 = (close[-1] >= ma60.iloc[-1] > open_p[-1])
        c2 = vol[-1] > df['成交量'].rolling(5).mean().iloc[-1] * 1.3
        return (c1 and c2), "放量站上60日生命线"

    elif "回踩年线" in strat_name:
        if len(df) < 250: return False, ""
        ma250 = df['收盘'].rolling(250).mean().iloc[-1]
        c1 = close[-1] >= ma250
        c2 = df['最低'].iloc[-1] <= ma250 * 1.03
        return (c1 and c2), "回踩250日年线支撑有效"

    elif "停机坪" in strat_name:
        if len(df) < 20: return False, ""
        recent_15 = df.iloc[-15:]
        limit_up = (recent_15['涨跌幅'] >= 9.5).any()
        return limit_up, "近15日内出现涨停蓄势"

    elif "放量上涨" in strat_name:
        vol_ma5 = df['成交量'].rolling(5).mean().iloc[-1]
        c1 = (close[-1] > open_p[-1]) or (df['涨跌幅'].iloc[-1] > 0 and df['涨跌幅'].iloc[-1] < 2)
        c2 = amount[-1] >= 100000000
        c3 = vol[-1] >= (vol_ma5 * 1.5)
        return (c1 and c2 and c3), "放量突破且成交活跃"

    elif "海龟交易" in strat_name:
        max_60 = np.max(close[-60:-1]) if len(close) >= 60 else close[-1]
        return (close[-1] >= max_60), "创近60日收盘新高"

    return False, ""

# ----------------- 线程任务 -----------------
def worker_task(code, name, engine_mode, strat_param, price_cap, min_score):
    try:
        k_df = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq")
        if k_df is None or len(k_df) < 60:
            return None
        
        last_close = k_df['收盘'].iloc[-1]
        if last_close > price_cap or last_close <= 0:
            return None
            
        score, tags, advice, radar = evaluate_smart_score(k_df)
        
        is_hit = False
        hit_reason = ""
        
        if "AI 智能多因子" in engine_mode:
            if score >= min_score and k_df['涨跌幅'].iloc[-1] >= 1.5:
                is_hit = True
                hit_reason = " | ".join(tags)
        elif engine_mode in ["🔥 短线游资/爆发战法", "📈 经典波段/趋势策略"]:
            is_hit, hit_reason = execute_strategy(k_df, strat_param)
        elif engine_mode == "🕯️ 经典K线形态识别" and HAS_TALIB:
            pattern_func = getattr(talib, strat_param[0], None)
            if pattern_func:
                sig = pattern_func(k_df['开盘'].values, k_df['最高'].values, k_df['最低'].values, k_df['收盘'].values)
                if sig[-1] != 0:
                    is_hit = True
                    hit_reason = f"触发形态: {strat_param[1]}"
                    
        if is_hit:
            k_df['MA5'] = k_df['收盘'].rolling(5).mean()
            k_df['MA10'] = k_df['收盘'].rolling(10).mean()
            k_df['MA20'] = k_df['收盘'].rolling(20).mean()
            k_df['MA60'] = k_df['收盘'].rolling(60).mean()
            
            return {
                "代码": code,
                "名称": name,
                "综合评分": score,
                "AI评级": "🔥 强力推荐" if score >= 85 else ("⭐ 重点关注" if score >= 75 else "👀 观察标的"),
                "最新价": last_close,
                "涨跌幅(%)": k_df['涨跌幅'].iloc[-1],
                "触发原因": hit_reason,
                "advice": advice,
                "radar": radar,
                "k_df": k_df
            }
    except Exception:
        return None
    return None

# ----------------- 图表渲染 -----------------
def draw_radar_chart(radar_data: dict):
    cats = list(radar_data.keys()) + [list(radar_data.keys())[0]]
    vals = list(radar_data.values()) + [list(radar_data.values())[0]]
    fig = go.Figure(go.Scatterpolar(r=vals, theta=cats, fill='toself', fillcolor='rgba(255, 75, 75, 0.35)', line=dict(color='#ff4b4b', width=2)))
    fig.update_layout(polar=dict(radialaxis=dict(visible=True, range=[0, 20])), showlegend=False, height=250, margin=dict(l=25, r=25, t=25, b=25))
    return fig

def draw_pro_kline(code, name, k_df, advice):
    recent_df = k_df.tail(75).copy()
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.72, 0.28]
    )
    
    # 主图 K 线与均线
    fig.add_trace(go.Candlestick(
        x=recent_df['日期'],
        open=recent_df['开盘'],
        high=recent_df['最高'],
        low=recent_df['最低'],
        close=recent_df['收盘'],
        increasing_line_color='#ef5350',
        decreasing_line_color='#26a69a',
        name="K线"
    ), row=1, col=1)
    
    fig.add_trace(go.Scatter(x=recent_df['日期'], y=recent_df['MA5'], line=dict(color='#ff9800', width=1.2), name="MA5"), row=1, col=1)
    fig.add_trace(go.Scatter(x=recent_df['日期'], y=recent_df['MA10'], line=dict(color='#2196f3', width=1.2), name="MA10"), row=1, col=1)
    fig.add_trace(go.Scatter(x=recent_df['日期'], y=recent_df['MA20'], line=dict(color='#9c27b0', width=1.2), name="MA20"), row=1, col=1)
    fig.add_trace(go.Scatter(x=recent_df['日期'], y=recent_df['MA60'], line=dict(color='#4caf50', width=1.5), name="MA60"), row=1, col=1)
    
    fig.add_hline(y=advice["动态压力位"], line_dash="dot", line_color="#ff1744", annotation_text=f"前高阻力: {advice['动态压力位']}元", row=1, col=1)
    fig.add_hline(y=advice["动态支撑位"], line_dash="dash", line_color="#00e676", annotation_text=f"强支撑位: {advice['动态支撑位']}元", row=1, col=1)

    # 副图成交量
    vol_colors = ['#ef5350' if c >= o else '#26a69a' for c, o in zip(recent_df['收盘'], recent_df['开盘'])]
    fig.add_trace(go.Bar(
        x=recent_df['日期'],
        y=recent_df['成交量'],
        marker_color=vol_colors,
        name="成交量"
    ), row=2, col=1)
    
    fig.update_layout(
        title=f"📈 {code} {name} 量价与支撑阻力图（最新价: {recent_df['收盘'].iloc[-1]} 元）",
        xaxis_rangeslider_visible=False,
        height=520,
        margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )
    return fig

# ----------------- 执行按钮 -----------------
if st.button("🚀 启动全市场主板智能扫描", type="primary", use_container_width=True):
    with st.spinner("正在加载 60 / 00 主板标的列表..."):
        try:
            pool = get_main_board_pool(board_type)
            if not scan_all:
                pool = pool.head(scan_limit)
        except Exception as e:
            st.error(f"获取股票池失败：{e}")
            st.stop()
            
    total_count = len(pool)
    st.session_state['last_scan_info'] = f"在 {total_count} 只主板低价股中完成扫描"
    
    new_results = []
    new_kline_cache = {}
    progress_bar = st.progress(0, text="12 线程并发深度扫描中...")
    
    strat_param = strategy_choice if engine_mode != "🕯️ 经典K线形态识别" else pattern_choice
    
    completed = 0
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [
            executor.submit(worker_task, str(row['代码']).zfill(6), row['名称'], engine_mode, strat_param, max_price, min_score)
            for _, row in pool.iterrows()
        ]
        
        for future in as_completed(futures):
            completed += 1
            res = future.result()
            if res:
                k_df = res.pop("k_df")
                new_results.append(res)
                new_kline_cache[res["代码"]] = (res["名称"], k_df, res["advice"], res["radar"])
                
            if completed % 25 == 0 or completed == total_count:
                progress_bar.progress(completed / total_count, text=f"扫描进度: {completed}/{total_count} 只")
                
    progress_bar.empty()
    new_results = sorted(new_results, key=lambda x: (x["综合评分"], x["涨跌幅(%)"]), reverse=True)
    st.session_state['scan_results'] = new_results
    st.session_state['kline_cache'] = new_kline_cache

# ----------------- 持久化交互展示区 -----------------
if st.session_state['scan_results']:
    results = st.session_state['scan_results']
    kline_cache = st.session_state['kline_cache']
    res_df = pd.DataFrame(results)
    
    st.success(f"🎉 投研完成！共精选命中 **{len(res_df)}** 只优质标的（{st.session_state['last_scan_info']}）。")
    
    col1, col2 = st.columns([1.15, 1.35])
    with col1:
        st.subheader("📋 命中评级清单 (高分优先)")
        st.dataframe(res_df[["代码", "名称", "综合评分", "AI评级", "最新价", "涨跌幅(%)", "触发原因"]], use_container_width=True, hide_index=True)
        csv = res_df.to_csv(index=False).encode('utf-8-sig')
        st.download_button("📥 导出清单为 CSV", data=csv, file_name=f"选股结果_{datetime.today().strftime('%Y%m%d')}.csv", mime="text/csv")
        
    with col2:
        st.subheader("📊 AI 决策中枢与五维诊断")
        selected_code = st.selectbox(
            "选择要诊断的股票：", 
            options=[r["代码"] for r in results], 
            format_func=lambda x: f"[{next(r['综合评分'] for r in results if r['代码'] == x)}分] {x} - {next(r['名称'] for r in results if r['代码'] == x)}"
        )
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
elif st.session_state['last_scan_info']:
    st.warning(f"⚠️ 在价格 ≤ {max_price} 元的主板股票中未发现符合该特征的高分标的，可尝试调高价格上限或切换其他模式。")
