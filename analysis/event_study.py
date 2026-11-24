import sqlite3
from pathlib import Path
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from scipy import stats

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "kr_quant.db"
OUTPUT_DIR = ROOT_DIR / "outputs"

TX_COST = 0.0025               # 왕복 거래비용 0.25% (거래세 + 수수료 + 슬리피지 보수적 반영)
MIN_DAILY_VAL = 1_000_000_000  # 직전 5일 평균 일거래대금 10억 원 미만 저유동성 제외


def load_data():
    conn = sqlite3.connect(DB_PATH)
    ohlcv = pd.read_sql("SELECT * FROM daily_ohlcv ORDER BY ticker, date", conn)
    disc = pd.read_sql("SELECT * FROM disclosures ORDER BY rcept_dt", conn)
    buyback = pd.read_sql("SELECT * FROM buyback_details", conn)
    conn.close()
    return ohlcv, disc, buyback


def classify_event_type(row) -> str:
    """자기주식 공시를 취득/처분/소각으로 세분화해 호재·악재 혼재 노이즈 제거"""
    title = str(row["report_nm"])
    kw = str(row["keyword"])

    if "소각" in title:
        return "소각"
    if kw == "자기주식" or "자기주식" in title:
        if "처분" in title or "해지" in title:
            return "자사주_처분/해지"
        if "취득" in title or "신탁" in title:
            return "자사주_취득/신탁"
        return "자사주_기타"
    return kw


def prepare_market_metrics(ohlcv: pd.DataFrame):
    """종목별 수익률, 갭/장중 수익률, 시장 벤치마크 일별 수익률 산출"""
    df = ohlcv.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)

    df["prev_close"] = df.groupby("ticker")["close"].shift(1)
    df["ret"] = df["close"] / df["prev_close"] - 1

    # 오버나잇 갭 (전일 종가 -> 당일 시가) & 장중 수익률 (당일 시가 -> 당일 종가)
    valid_open = df["open"] > 0
    df["gap_ret"] = np.where(valid_open & (df["prev_close"] > 0), df["open"] / df["prev_close"] - 1, np.nan)
    df["oc_ret"] = np.where(valid_open, df["close"] / df["open"] - 1, np.nan)

    # 코스피200 동일가중 일별 시장 벤치마크
    mkt_df = (
        df.groupby("date")
        .agg(
            mkt_ret=("ret", "mean"),
            mkt_gap=("gap_ret", "mean"),
            mkt_oc=("oc_ret", "mean"),
        )
        .sort_index()
    )

    df = df.merge(mkt_df[["mkt_ret", "mkt_gap"]], on="date", how="left")
    df["ar"] = df["ret"] - df["mkt_ret"]
    df["excess_gap"] = df["gap_ret"] - df["mkt_gap"]

    # 거래정지일(volume == 0)은 NaN 처리해 거래량·거래대금 왜곡 방지
    clean_vol = df["volume"].replace(0, np.nan)
    df["trading_value"] = np.where(df["volume"] > 0, df["close"] * df["volume"], np.nan)

    prev_5d_vol = (
        clean_vol.groupby(df["ticker"])
        .transform(lambda s: s.shift(1).rolling(5, min_periods=3).mean())
    )
    df["vol_ratio"] = clean_vol / prev_5d_vol

    df["prev_5d_val"] = (
        df.groupby("ticker")["trading_value"]
        .transform(lambda s: s.shift(1).rolling(5, min_periods=3).mean())
    )
    return df, mkt_df


def run_event_study(ohlcv: pd.DataFrame, mkt_df: pd.DataFrame, disc: pd.DataFrame):
    """이벤트별 갭/장중보유/시장중립 수익률 분해 및 타임라인 추출"""
    events = disc.copy()
    events["event_type"] = events.apply(classify_event_type, axis=1)
    events = events.drop_duplicates(subset=["rcept_dt", "ticker", "event_type"]).copy()
    events["rcept_dt"] = pd.to_datetime(events["rcept_dt"])

    grouped_price = {
        ticker: grp.reset_index(drop=True)
        for ticker, grp in ohlcv.groupby("ticker")
    }

    results = []
    timeline_rows = []

    for _, ev in events.iterrows():
        ticker = ev["ticker"]
        if ticker not in grouped_price:
            continue

        px_df = grouped_price[ticker]
        idx_candidates = px_df.index[px_df["date"] >= ev["rcept_dt"]]
        if len(idx_candidates) == 0:
            continue
        d0 = idx_candidates[0]

        pre_car = px_df.loc[d0 - 3 : d0 - 1, "ar"].sum() if d0 >= 3 else np.nan
        d0_ar = px_df.loc[d0, "ar"]
        post_car = px_df.loc[d0 + 1 : d0 + 5, "ar"].sum() if d0 + 5 < len(px_df) else np.nan
        vol_spike = px_df.loc[d0, "vol_ratio"]

        # D+0 종가 -> D+1 시가 오버나잇 갭 (초과수익 기준)
        excess_gap = px_df.loc[d0 + 1, "excess_gap"] if d0 + 1 < len(px_df) else np.nan

        # D+1 시가 -> D+5 종가 실전 보유수익 & 동일 기간 시장중립(헤지) 수익
        tradable_ret = np.nan
        mkt_hold_ret = np.nan
        mn_gross_ret = np.nan
        mn_net_ret = np.nan

        if d0 + 5 < len(px_df):
            entry_open = px_df.loc[d0 + 1, "open"]
            exit_close = px_df.loc[d0 + 5, "close"]
            if entry_open and entry_open > 0:
                tradable_ret = (exit_close / entry_open) - 1

                # 동일 기간 시장 벤치마크 수익: (D+1 시가->종가) * (D+2~D+5 일별 종가수익률)
                d1_date = px_df.loc[d0 + 1, "date"]
                d2_to_d5_dates = px_df.loc[d0 + 2 : d0 + 5, "date"]
                if d1_date in mkt_df.index:
                    m_oc = mkt_df.loc[d1_date, "mkt_oc"]
                    m_cc = mkt_df.reindex(d2_to_d5_dates)["mkt_ret"].dropna()
                    if pd.notna(m_oc) and len(m_cc) == len(d2_to_d5_dates):
                        mkt_hold_ret = (1 + m_oc) * np.prod(1 + m_cc.values) - 1
                        mn_gross_ret = tradable_ret - mkt_hold_ret
                        mn_net_ret = mn_gross_ret - TX_COST

        results.append({
            "rcept_no": ev["rcept_no"],
            "rcept_dt": ev["rcept_dt"].strftime("%Y-%m-%d"),
            "ticker": ticker,
            "corp_name": ev["corp_name"],
            "event_type": ev["event_type"],
            "pre_car_3d": pre_car,
            "d0_ar": d0_ar,
            "post_car_5d": post_car,
            "excess_gap": excess_gap,
            "tradable_ret_5d": tradable_ret,
            "net_long_ret_5d": tradable_ret - TX_COST if pd.notna(tradable_ret) else np.nan,
            "mn_gross_ret_5d": mn_gross_ret,
            "mn_net_ret_5d": mn_net_ret,
            "vol_spike": vol_spike,
            "prev_5d_val": px_df.loc[d0, "prev_5d_val"],
        })

        for rel_day in range(-3, 6):
            target_idx = d0 + rel_day
            if 0 <= target_idx < len(px_df) and pd.notna(px_df.loc[target_idx, "ar"]):
                timeline_rows.append({
                    "event_type": ev["event_type"],
                    "rel_day": rel_day,
                    "ar": px_df.loc[target_idx, "ar"],
                })

    return pd.DataFrame(results), pd.DataFrame(timeline_rows)


def _sig_mark(p_val):
    if pd.isna(p_val):
        return "-"
    if p_val < 0.01:
        return "***"
    if p_val < 0.05:
        return "**"
    if p_val < 0.10:
        return "*"
    return "-"


def calc_decomposition_summary(ev_df: pd.DataFrame) -> pd.DataFrame:
    """이벤트 유형별 갭 vs 시장중립 실전수익(비용후) 통계 검증"""
    rows = []
    for etype, grp in ev_df.groupby("event_type"):
        mn_net = grp["mn_net_ret_5d"].dropna()
        gap = grp["excess_gap"].dropna()

        if len(mn_net) >= 2:
            t_mn, p_mn = stats.ttest_1samp(mn_net, 0.0)
        else:
            t_mn, p_mn = np.nan, np.nan

        rows.append({
            "event_type": etype,
            "건수": len(grp),
            "사후_CAR(%)": grp["post_car_5d"].mean() * 100,
            "오버나잇갭_초과(%)": gap.mean() * 100 if len(gap) else np.nan,
            "시장중립_비용전(%)": grp["mn_gross_ret_5d"].mean() * 100,
            "시장중립_비용후(%)": mn_net.mean() * 100 if len(mn_net) else np.nan,
            "t_stat(중립)": t_mn,
            "p_value(중립)": p_mn,
            "유의성": _sig_mark(p_mn),
            "승률_중립(%)": (mn_net > 0).mean() * 100 if len(mn_net) else np.nan,
            "롱온리_비용후(%)": grp["net_long_ret_5d"].mean() * 100,
        })

    summary = pd.DataFrame(rows).set_index("event_type").sort_values("건수", ascending=False)
    num_cols = [c for c in summary.columns if c not in ("건수", "유의성", "p_value(중립)")]
    summary[num_cols] = summary[num_cols].round(2)
    summary["p_value(중립)"] = summary["p_value(중립)"].round(4)
    return summary


def plot_alpha_decomposition(summary: pd.DataFrame, save_path: Path):
    """인터랙티브 차트: 오버나잇 갭 vs 시장중립 보유수익 분해"""
    plot_df = summary[summary["건수"] >= 30].reset_index()
    fig = go.Figure()
    fig.add_trace(go.Bar(name="오버나잇 갭 초과 (D0종→D1시)", x=plot_df["event_type"], y=plot_df["오버나잇갭_초과(%)"], marker_color="#ff7f0e"))
    fig.add_trace(go.Bar(name="시장중립 보유수익 (비용전)", x=plot_df["event_type"], y=plot_df["시장중립_비용전(%)"], marker_color="#1f77b4"))
    fig.add_trace(go.Bar(name="시장중립 실전수익 (비용후 0.25%)", x=plot_df["event_type"], y=plot_df["시장중립_비용후(%)"], marker_color="#2ca02c"))

    fig.add_hline(y=0, line_dash="dash", line_color="gray")
    fig.update_layout(
        barmode="group",
        title="공시 이벤트 알파 분해: 오버나잇 갭 vs 시장중립 실전 보유수익 (n≥30)",
        xaxis_title="공시 유형",
        yaxis_title="평균 초과수익률 (%)",
        template="plotly_white",
    )
    fig.write_html(save_path, include_plotlyjs="cdn")


def plot_car_timeline_interactive(timeline_df: pd.DataFrame, ev_df: pd.DataFrame, save_path: Path):
    """인터랙티브 차트: 세분화된 공시 유형별 CAR 궤적"""
    valid_types = ev_df["event_type"].value_counts()
    valid_types = valid_types[valid_types >= 30].index.tolist()

    df = timeline_df[timeline_df["event_type"].isin(valid_types)]
    pivot_ar = df.groupby(["rel_day", "event_type"])["ar"].mean().unstack()
    pivot_car = (pivot_ar.cumsum() * 100).round(2)

    fig = go.Figure()
    for col in pivot_car.columns:
        cnt = int((ev_df["event_type"] == col).sum())
        fig.add_trace(
            go.Scatter(
                x=pivot_car.index,
                y=pivot_car[col],
                mode="lines+markers",
                name=f"{col} (n={cnt})",
                hovertemplate=f"<b>{col}</b><br>영업일: D%{{x:+d}}<br>CAR: %{{y:.2f}}%<extra></extra>",
            )
        )

    fig.add_vline(x=0, line_dash="dash", line_color="crimson", annotation_text="공시일 (D+0)")
    fig.add_hline(y=0, line_dash="dot", line_color="gray")
    fig.update_layout(
        title="세분화된 공시 유형별 누적 초과수익률(CAR) 궤적 (D-3 ~ D+5)",
        xaxis_title="공시일 기준 영업일 (D+0 = 공시일)",
        yaxis_title="누적 초과수익률 CAR (%)",
        xaxis=dict(tickmode="linear", tick0=-3, dtick=1),
        hovermode="x unified",
        template="plotly_white",
    )
    fig.write_html(save_path, include_plotlyjs="cdn")


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    ohlcv_raw, disc, buyback = load_data()
    print(f"[데이터 로드] 주가 {len(ohlcv_raw):,}건 | 공시 {len(disc):,}건 | 자사주 상세 {len(buyback):,}건")

    ohlcv, mkt_df = prepare_market_metrics(ohlcv_raw)
    ev_df, timeline_df = run_event_study(ohlcv, mkt_df, disc)

    # 1. 세분화 이벤트별 갭 & 시장중립 수익 분해
    summary = calc_decomposition_summary(ev_df)
    print("\n================ [1] 공시 세분화 & 알파 분해 (오버나잇 갭 vs 시장중립 실전수익) ================")
    print(summary.to_string())
    print("(* p<0.10, ** p<0.05, *** p<0.01 | 시장중립 실전수익 = D+1시가~D+5종가 초과수익 - 거래비용 0.25%)")

    summary.to_csv(OUTPUT_DIR / "keyword_event_summary.csv", encoding="utf-8-sig")
    plot_alpha_decomposition(summary, OUTPUT_DIR / "alpha_decomposition.html")
    plot_car_timeline_interactive(timeline_df, ev_df, OUTPUT_DIR / "car_timeline.html")

    # 2. 자사주 매입 강도별 갭 & 시장중립 성과 비교
    if not buyback.empty:
        bb = buyback.merge(
            ev_df[["rcept_no", "rcept_dt", "d0_ar", "post_car_5d", "excess_gap", "mn_gross_ret_5d", "mn_net_ret_5d", "net_long_ret_5d", "prev_5d_val"]],
            on="rcept_no",
            how="inner",
        )
        if not bb.empty:
            bb["예정금액_억"] = (bb["aqpln_prc_ostk"] / 1e8).round(1)
            bb["매입강도"] = (bb["aqpln_prc_ostk"] / bb["prev_5d_val"]).round(2)
            bb_valid = bb.dropna(subset=["매입강도", "prev_5d_val"]).copy()
            bb_valid = bb_valid[bb_valid["prev_5d_val"] >= MIN_DAILY_VAL].copy()
            bb_valid["강도그룹"] = np.where(bb_valid["매입강도"] >= 3.0, "고강도 (≥3배)", "저강도 (<3배)")

            grp_rows = []
            for gname, g in bb_valid.groupby("강도그룹"):
                mn = g["mn_net_ret_5d"].dropna()
                t_val, p_val = stats.ttest_1samp(mn, 0.0) if len(mn) >= 2 else (np.nan, np.nan)
                grp_rows.append({
                    "강도그룹": gname,
                    "건수": len(g),
                    "사후_CAR(%)": g["post_car_5d"].mean() * 100,
                    "오버나잇갭(%)": g["excess_gap"].mean() * 100,
                    "시장중립_비용전(%)": g["mn_gross_ret_5d"].mean() * 100,
                    "시장중립_비용후(%)": mn.mean() * 100 if len(mn) else np.nan,
                    "t_stat": t_val,
                    "p_value": p_val,
                    "승률_중립(%)": (mn > 0).mean() * 100 if len(mn) else np.nan,
                })
            grp_df = pd.DataFrame(grp_rows).set_index("강도그룹")
            num_cols = [c for c in grp_df.columns if c not in ("건수", "p_value")]
            grp_df[num_cols] = grp_df[num_cols].round(2)
            grp_df["p_value"] = grp_df["p_value"].round(4)

            print("\n================ [2] 자사주 매입 강도별 갭 & 시장중립 성과 비교 ================")
            print(grp_df.to_string())

            bb.to_csv(OUTPUT_DIR / "buyback_intensity.csv", index=False, encoding="utf-8-sig")

    print(f"\n[저장 완료] outputs/alpha_decomposition.html, car_timeline.html 업데이트 완료")


if __name__ == "__main__":
    main()