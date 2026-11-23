import sqlite3
from pathlib import Path
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "kr_quant.db"
OUTPUT_DIR = ROOT_DIR / "outputs"


def load_data():
    conn = sqlite3.connect(DB_PATH)
    ohlcv = pd.read_sql("SELECT * FROM daily_ohlcv ORDER BY ticker, date", conn)
    disc = pd.read_sql("SELECT * FROM disclosures ORDER BY rcept_dt", conn)
    buyback = pd.read_sql("SELECT * FROM buyback_details", conn)
    conn.close()
    return ohlcv, disc, buyback


def prepare_market_metrics(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """종목별 수익률, 시장 초과수익률(AR), 평소 대비 거래량 배수 계산"""
    df = ohlcv.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)

    df["ret"] = df.groupby("ticker")["close"].pct_change()
    df["trading_value"] = df["close"] * df["volume"]

    mkt_ret = df.groupby("date")["ret"].transform("mean")
    df["ar"] = df["ret"] - mkt_ret

    prev_5d_vol = (
        df.groupby("ticker")["volume"]
        .transform(lambda s: s.shift(1).rolling(5, min_periods=3).mean())
        .replace(0, np.nan)
    )
    df["vol_ratio"] = df["volume"] / prev_5d_vol

    df["prev_5d_val"] = (
        df.groupby("ticker")["trading_value"]
        .transform(lambda s: s.shift(1).rolling(5, min_periods=3).mean())
        .replace(0, np.nan)
    )
    return df


def run_event_study(ohlcv: pd.DataFrame, disc: pd.DataFrame):
    """공시일(D+0) 전후 요약 지표 및 D-3~D+5 일별 AR 타임라인 추출"""
    events = disc.drop_duplicates(subset=["rcept_dt", "ticker", "keyword"]).copy()
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

        tradable_ret = np.nan
        if d0 + 5 < len(px_df):
            entry_open = px_df.loc[d0 + 1, "open"]
            exit_close = px_df.loc[d0 + 5, "close"]
            if entry_open and entry_open > 0:
                tradable_ret = (exit_close / entry_open) - 1

        results.append({
            "rcept_no": ev["rcept_no"],
            "rcept_dt": ev["rcept_dt"].strftime("%Y-%m-%d"),
            "ticker": ticker,
            "corp_name": ev["corp_name"],
            "keyword": ev["keyword"],
            "pre_car_3d": pre_car,
            "d0_ar": d0_ar,
            "post_car_5d": post_car,
            "tradable_ret_5d": tradable_ret,
            "vol_spike": vol_spike,
            "prev_5d_val": px_df.loc[d0, "prev_5d_val"],
        })

        for rel_day in range(-3, 6):
            target_idx = d0 + rel_day
            if 0 <= target_idx < len(px_df) and pd.notna(px_df.loc[target_idx, "ar"]):
                timeline_rows.append({
                    "keyword": ev["keyword"],
                    "rel_day": rel_day,
                    "ar": px_df.loc[target_idx, "ar"],
                })

    return pd.DataFrame(results), pd.DataFrame(timeline_rows)


def plot_car_timeline_interactive(timeline_df: pd.DataFrame, ev_df: pd.DataFrame, save_path: Path):
    """인터랙티브 차트 1: 키워드별 D-3 ~ D+5 누적 초과수익률(CAR) 궤적"""
    valid_keywords = ev_df["keyword"].value_counts()
    valid_keywords = valid_keywords[valid_keywords >= 5].index.tolist()

    df = timeline_df[timeline_df["keyword"].isin(valid_keywords)]
    pivot_ar = df.groupby(["rel_day", "keyword"])["ar"].mean().unstack()
    pivot_car = (pivot_ar.cumsum() * 100).round(2)

    fig = go.Figure()
    for col in pivot_car.columns:
        cnt = int((ev_df["keyword"] == col).sum())
        fig.add_trace(
            go.Scatter(
                x=pivot_car.index,
                y=pivot_car[col],
                mode="lines+markers",
                name=f"{col} (n={cnt})",
                hovertemplate=(
                    f"<b>{col}</b><br>"
                    "영업일: D%{x:+d}<br>"
                    "누적 초과수익률(CAR): %{y:.2f}%<extra></extra>"
                ),
            )
        )

    fig.add_vline(x=0, line_dash="dash", line_color="crimson", annotation_text="공시일 (D+0)")
    fig.add_hline(y=0, line_dash="dot", line_color="gray")

    fig.update_layout(
        title="공시 키워드별 누적 초과수익률(CAR) 궤적 (D-3 ~ D+5)",
        xaxis_title="공시일 기준 영업일 (D+0 = 공시일)",
        yaxis_title="누적 초과수익률 CAR (%)",
        xaxis=dict(tickmode="linear", tick0=-3, dtick=1),
        hovermode="x unified",
        template="plotly_white",
        legend_title="키워드 (클릭해서 숨기기/보기)",
    )
    # include_plotlyjs="cdn"으로 저장하면 HTML 파일 용량이 매우 가벼워짐
    fig.write_html(save_path, include_plotlyjs="cdn")


def plot_buyback_scatter_interactive(bb_df: pd.DataFrame, save_path: Path):
    """인터랙티브 차트 2: 자사주 매입 강도 vs 공시 당일 초과수익률(AR)"""
    plot_df = bb_df.dropna(subset=["매입강도_일거래대금배수", "당일_AR(%)"]).copy()
    if plot_df.empty:
        return

    plot_df["반응구분"] = np.where(plot_df["당일_AR(%)"] >= 0, "상승 (AR ≥ 0)", "하락 (AR < 0)")

    fig = px.scatter(
        plot_df,
        x="매입강도_일거래대금배수",
        y="당일_AR(%)",
        color="반응구분",
        text="corp_name",
        color_discrete_map={"상승 (AR ≥ 0)": "#1f77b4", "하락 (AR < 0)": "#d62728"},
        hover_data={
            "corp_name": True,
            "rcept_dt": True,
            "예정금액_억": ":,.1f",
            "매입강도_일거래대금배수": ":.2f",
            "당일_AR(%)": ":.2f",
            "실전수익률(%)": ":.2f",
            "반응구분": False,
        },
        title="자사주 매입 강도(일거래대금 배수) vs 공시 당일 초과수익률",
        labels={
            "매입강도_일거래대금배수": "매입 강도 (취득예정금액 ÷ 직전 5일 평균 일거래대금)",
            "당일_AR(%)": "공시 당일 초과수익률 AR (%)",
            "corp_name": "기업명",
            "rcept_dt": "공시일",
        },
        template="plotly_white",
    )

    fig.update_traces(textposition="top center", marker=dict(size=12, line=dict(width=1, color="black")))
    fig.add_hline(y=0, line_dash="dash", line_color="gray")
    fig.write_html(save_path, include_plotlyjs="cdn")


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    ohlcv_raw, disc, buyback = load_data()
    print(f"[데이터 로드] 주가 {len(ohlcv_raw):,}건 | 공시 {len(disc):,}건 | 자사주 상세 {len(buyback):,}건")

    ohlcv = prepare_market_metrics(ohlcv_raw)
    ev_df, timeline_df = run_event_study(ohlcv, disc)

    if ev_df.empty:
        print("[안내] 주가 기간과 겹치는 공시 이벤트가 없습니다.")
        return

    # 1. 키워드별 요약 CSV + 인터랙티브 HTML 차트 저장
    summary = (
        ev_df.groupby("keyword")
        .agg(
            건수=("ticker", "count"),
            선반영_CAR_D3_1=("pre_car_3d", lambda s: s.mean() * 100),
            당일_AR_D0=("d0_ar", lambda s: s.mean() * 100),
            사후_CAR_D1_5=("post_car_5d", lambda s: s.mean() * 100),
            실전수익률_D1시가_D5종가=("tradable_ret_5d", lambda s: s.mean() * 100),
            당일거래량배수=("vol_spike", "mean"),
        )
        .round(2)
        .sort_values("건수", ascending=False)
    )
    summary.to_csv(OUTPUT_DIR / "keyword_event_summary.csv", encoding="utf-8-sig")
    plot_car_timeline_interactive(timeline_df, ev_df, OUTPUT_DIR / "car_timeline.html")
    print("[저장 완료] outputs/keyword_event_summary.csv, outputs/car_timeline.html")

    # 2. 자사주 강도 CSV + 인터랙티브 HTML 차트 저장
    if not buyback.empty:
        bb = buyback.merge(
            ev_df[["rcept_no", "rcept_dt", "d0_ar", "post_car_5d", "tradable_ret_5d", "prev_5d_val"]],
            on="rcept_no",
            how="inner",
        )
        if not bb.empty:
            bb["예정금액_억"] = (bb["aqpln_prc_ostk"] / 1e8).round(1)
            bb["매입강도_일거래대금배수"] = (bb["aqpln_prc_ostk"] / bb["prev_5d_val"]).round(2)
            bb["당일_AR(%)"] = (bb["d0_ar"] * 100).round(2)
            bb["실전수익률(%)"] = (bb["tradable_ret_5d"] * 100).round(2)

            cols = ["rcept_dt", "corp_name", "예정금액_억", "매입강도_일거래대금배수", "당일_AR(%)", "실전수익률(%)"]
            bb_out = bb[cols].sort_values("매입강도_일거래대금배수", ascending=False)

            bb_out.to_csv(OUTPUT_DIR / "buyback_intensity.csv", index=False, encoding="utf-8-sig")
            plot_buyback_scatter_interactive(bb_out, OUTPUT_DIR / "buyback_scatter.html")
            print("[저장 완료] outputs/buyback_intensity.csv, outputs/buyback_scatter.html")


if __name__ == "__main__":
    main()