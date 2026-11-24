import sqlite3
from pathlib import Path
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from scipy import stats

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "kr_quant.db"
OUTPUT_DIR = ROOT_DIR / "outputs"

TX_COST = 0.0025               # 왕복 거래비용 0.25%
MIN_DAILY_VAL = 1_000_000_000  # 직전 5일 평균 일거래대금 10억 원 미만 저유동성 제외
VIX_MIN_PERIODS = 60           # Expanding 분위수 산출을 위한 최소 과거 거래일(약 3개월)


def classify_event_type(row) -> str:
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


def load_all():
    conn = sqlite3.connect(DB_PATH)
    ohlcv = pd.read_sql("SELECT * FROM daily_ohlcv ORDER BY ticker, date", conn)
    disc = pd.read_sql("SELECT * FROM disclosures ORDER BY rcept_dt", conn)
    buyback = pd.read_sql("SELECT * FROM buyback_details", conn)
    sent = pd.read_sql("SELECT * FROM market_sentiment ORDER BY date", conn)
    conn.close()
    return ohlcv, disc, buyback, sent


def prepare_events(ohlcv: pd.DataFrame, disc: pd.DataFrame, sent: pd.DataFrame) -> pd.DataFrame:
    df = ohlcv.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)

    df["prev_close"] = df.groupby("ticker")["close"].shift(1)
    df["ret"] = df["close"] / df["prev_close"] - 1
    valid_open = df["open"] > 0
    df["gap_ret"] = np.where(valid_open & (df["prev_close"] > 0), df["open"] / df["prev_close"] - 1, np.nan)
    df["oc_ret"] = np.where(valid_open, df["close"] / df["open"] - 1, np.nan)

    mkt_df = df.groupby("date").agg(
        mkt_ret=("ret", "mean"),
        mkt_gap=("gap_ret", "mean"),
        mkt_oc=("oc_ret", "mean"),
    ).sort_index()

    df = df.merge(mkt_df[["mkt_ret", "mkt_gap"]], on="date", how="left")
    df["ar"] = df["ret"] - df["mkt_ret"]
    df["excess_gap"] = df["gap_ret"] - df["mkt_gap"]

    # 거래정지일(volume == 0) 제외 후 직전 5일 평균 거래대금 산출
    df["trading_value"] = np.where(df["volume"] > 0, df["close"] * df["volume"], np.nan)
    df["prev_5d_val"] = (
        df.groupby("ticker")["trading_value"]
        .transform(lambda s: s.shift(1).rolling(5, min_periods=3).mean())
    )

    # 미래참조 편향(Look-ahead Bias) 제거: t일 시점까지 과거 누적 VIX만으로 상위 25% 컷오프 계산
    sent = sent.copy()
    sent["date"] = pd.to_datetime(sent["date"])
    sent = sent.sort_values("date").reset_index(drop=True)
    sent["vix_cutoff"] = sent["vix"].expanding(min_periods=VIX_MIN_PERIODS).quantile(0.75)

    events = disc.copy()
    events["event_type"] = events.apply(classify_event_type, axis=1)
    events = events.drop_duplicates(subset=["rcept_dt", "ticker", "event_type"]).copy()
    events["rcept_dt"] = pd.to_datetime(events["rcept_dt"])

    grouped_price = {t: g.reset_index(drop=True) for t, g in df.groupby("ticker")}

    rows = []
    for _, ev in events.iterrows():
        ticker = ev["ticker"]
        if ticker not in grouped_price:
            continue
        px_df = grouped_price[ticker]
        idx_cand = px_df.index[px_df["date"] >= ev["rcept_dt"]]
        if len(idx_cand) == 0:
            continue
        d0 = idx_cand[0]
        d0_date = px_df.loc[d0, "date"]

        post_car = px_df.loc[d0 + 1 : d0 + 5, "ar"].sum() if d0 + 5 < len(px_df) else np.nan
        excess_gap = px_df.loc[d0 + 1, "excess_gap"] if d0 + 1 < len(px_df) else np.nan

        mn_net_ret = np.nan
        if d0 + 5 < len(px_df):
            entry_open = px_df.loc[d0 + 1, "open"]
            exit_close = px_df.loc[d0 + 5, "close"]
            if entry_open and entry_open > 0:
                tradable_ret = (exit_close / entry_open) - 1
                d1_date = px_df.loc[d0 + 1, "date"]
                d2_d5 = px_df.loc[d0 + 2 : d0 + 5, "date"]
                if d1_date in mkt_df.index:
                    m_oc = mkt_df.loc[d1_date, "mkt_oc"]
                    m_cc = mkt_df.reindex(d2_d5)["mkt_ret"].dropna()
                    if pd.notna(m_oc) and len(m_cc) == len(d2_d5):
                        mkt_hold = (1 + m_oc) * np.prod(1 + m_cc.values) - 1
                        mn_net_ret = (tradable_ret - mkt_hold) - TX_COST

        rows.append({
            "rcept_no": ev["rcept_no"],
            "d0_date": d0_date,
            "ticker": ticker,
            "corp_name": ev["corp_name"],
            "event_type": ev["event_type"],
            "post_car_5d": post_car,
            "excess_gap": excess_gap,
            "mn_net_ret_5d": mn_net_ret,
            "prev_5d_val": px_df.loc[d0, "prev_5d_val"],
        })

    ev_df = pd.DataFrame(rows)
    ev_df = ev_df.merge(sent[["date", "vix", "vix_cutoff", "kr_hv20", "usd_krw"]], left_on="d0_date", right_on="date", how="left")
    return ev_df


def analyze_regimes(ev_df: pd.DataFrame):
    df = ev_df.dropna(subset=["vix", "vix_cutoff", "mn_net_ret_5d", "prev_5d_val"]).copy()
    before_cnt = len(df)
    df = df[df["prev_5d_val"] >= MIN_DAILY_VAL].copy()
    excluded_cnt = before_cnt - len(df)

    df["regime"] = np.where(df["vix"] >= df["vix_cutoff"], "공포(Fear)", "평온(Calm)")

    start_str = df["d0_date"].min().strftime("%Y-%m-%d")
    end_str = df["d0_date"].max().strftime("%Y-%m-%d")
    print(f"\n[검증 설정] 기간: {start_str} ~ {end_str} (Look-ahead Bias 제거: Expanding Q75 적용)")
    print(f"[유동성 필터] 일거래대금 {MIN_DAILY_VAL / 1e8:.0f}억 미만 저유동성 이벤트 {excluded_cnt:,}건 제외 (유효 표본 {len(df):,}건)")

    target_types = ["소각", "자사주_취득/신탁", "자사주_처분/해지", "영업(잠정)실적", "배당", "매출액또는손익구조", "단일판매", "유상증자"]
    summary_rows = []

    for etype in target_types:
        sub = df[df["event_type"] == etype]
        fear = sub[sub["regime"] == "공포(Fear)"]
        calm = sub[sub["regime"] == "평온(Calm)"]

        if len(fear) < 5 or len(calm) < 5:
            continue

        t_diff, p_diff = stats.ttest_ind(fear["mn_net_ret_5d"], calm["mn_net_ret_5d"], equal_var=False)

        summary_rows.append({
            "공시유형": etype,
            "평온_건수": len(calm),
            "평온_중립수익(%)": calm["mn_net_ret_5d"].mean() * 100,
            "공포_건수": len(fear),
            "공포_갭(%)": fear["excess_gap"].mean() * 100,
            "공포_중립수익(%)": fear["mn_net_ret_5d"].mean() * 100,
            "공포_승률(%)": (fear["mn_net_ret_5d"] > 0).mean() * 100,
            "공포-평온_격차(%)": (fear["mn_net_ret_5d"].mean() - calm["mn_net_ret_5d"].mean()) * 100,
            "차이_p_value": p_diff,
        })

    res_df = pd.DataFrame(summary_rows).set_index("공시유형")
    num_cols = [c for c in res_df.columns if c not in ("평온_건수", "공포_건수", "차이_p_value")]
    res_df[num_cols] = res_df[num_cols].round(2)
    res_df["차이_p_value"] = res_df["차이_p_value"].round(4)
    return res_df, df


def plot_regime_comparison(res_df: pd.DataFrame, save_path: Path):
    plot_df = res_df.reset_index()
    fig = go.Figure()
    fig.add_trace(go.Bar(name="평온 국면 (Expanding Q75 미만)", x=plot_df["공시유형"], y=plot_df["평온_중립수익(%)"], marker_color="#4c78a8"))
    fig.add_trace(go.Bar(name="공포 국면 (Expanding Q75 이상)", x=plot_df["공시유형"], y=plot_df["공포_중립수익(%)"], marker_color="#e45756"))

    fig.add_hline(y=0, line_dash="dash", line_color="gray")
    fig.update_layout(
        barmode="group",
        title="VIX 국면별 공시 시장중립 실전수익률 (Look-ahead Bias 제거 · 유동성 필터 적용)",
        xaxis_title="공시 유형",
        yaxis_title="시장중립 실전수익률 (%)",
        template="plotly_white",
    )
    fig.write_html(save_path, include_plotlyjs="cdn")


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    ohlcv, disc, buyback, sent = load_all()
    ev_df = prepare_events(ohlcv, disc, sent)
    res_df, tagged_df = analyze_regimes(ev_df)

    print("\n================ [1] VIX 공포 vs 평온 국면별 공시 시장중립 성과 (편향 제거) ================")
    print(res_df.to_string())

    res_df.to_csv(OUTPUT_DIR / "regime_event_summary.csv", encoding="utf-8-sig")
    plot_regime_comparison(res_df, OUTPUT_DIR / "regime_event_comparison.html")

    # 고강도 자사주(>=3배)의 공포 vs 평온 국면 비교
    if not buyback.empty:
        bb = buyback.merge(
            tagged_df[["rcept_no", "d0_date", "regime", "vix", "vix_cutoff", "excess_gap", "mn_net_ret_5d", "prev_5d_val"]],
            on="rcept_no",
            how="inner",
        )
        bb["매입강도"] = (bb["aqpln_prc_ostk"] / bb["prev_5d_val"]).round(2)
        high_bb = bb[bb["매입강도"] >= 3.0].copy()

        if not high_bb.empty:
            grp_rows = []
            for reg, g in high_bb.groupby("regime"):
                mn = g["mn_net_ret_5d"].dropna()
                t_val, p_val = stats.ttest_1samp(mn, 0.0) if len(mn) >= 2 else (np.nan, np.nan)
                grp_rows.append({
                    "regime": reg,
                    "건수": len(g),
                    "평균_VIX": g["vix"].mean(),
                    "오버나잇갭(%)": g["excess_gap"].mean() * 100,
                    "시장중립_비용후(%)": mn.mean() * 100,
                    "t_stat(vs 0)": t_val,
                    "p_value(vs 0)": p_val,
                    "승률(%)": (mn > 0).mean() * 100,
                })
            bb_grp = pd.DataFrame(grp_rows).set_index("regime")
            num_cols = [c for c in bb_grp.columns if c not in ("건수", "p_value(vs 0)")]
            bb_grp[num_cols] = bb_grp[num_cols].round(2)
            bb_grp["p_value(vs 0)"] = bb_grp["p_value(vs 0)"].round(4)

            print("\n================ [2] 고강도 자사주(≥3배) VIX 국면별 성과 (편향·저유동성 제거) ================")
            print(bb_grp.to_string())
            bb_grp.to_csv(OUTPUT_DIR / "regime_buyback_summary.csv", encoding="utf-8-sig")

            fear_bb = high_bb[high_bb["regime"] == "공포(Fear)"].sort_values("d0_date").copy()
            calm_bb = high_bb[high_bb["regime"] == "평온(Calm)"]
            if len(fear_bb) >= 2 and len(calm_bb) >= 2:
                t_diff, p_diff = stats.ttest_ind(fear_bb["mn_net_ret_5d"], calm_bb["mn_net_ret_5d"], equal_var=False)
                print(f"  >> 공포 vs 평온 수익률 차이 검정 (Welch's t-test): t={t_diff:.2f}, p={p_diff:.4f}")

            if not fear_bb.empty:
                fear_bb["공시일"] = fear_bb["d0_date"].dt.strftime("%Y-%m-%d")
                fear_bb["일거래대금_억"] = (fear_bb["prev_5d_val"] / 1e8).round(1)
                fear_bb["갭(%)"] = (fear_bb["excess_gap"] * 100).round(2)
                fear_bb["시장중립(%)"] = (fear_bb["mn_net_ret_5d"] * 100).round(2)

                print("\n================ [3] 공포 국면 고강도 자사주 개별 내역 (유동성 필터 통과) ================")
                print(fear_bb[["공시일", "corp_name", "일거래대금_억", "vix", "매입강도", "갭(%)", "시장중립(%)"]].to_string(index=False))

    print(f"\n[저장 완료] outputs/regime_event_comparison.html 업데이트 완료")


if __name__ == "__main__":
    main()