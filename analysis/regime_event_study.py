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
MIN_PERIODS = 60               # Expanding 분위수 산출 최소 과거 거래일(약 3개월)

TARGET_TYPES = [
    "소각", "자사주_취득/신탁", "자사주_처분/해지",
    "영업(잠정)실적", "배당", "매출액또는손익구조", "단일판매", "유상증자",
]

INDICATORS = [
    ("vix", "미국 VIX", "regime_event_summary.csv"),
    ("vkospi", "한국 VKOSPI", "regime_vkospi_summary.csv"),
    ("pcr_val", "옵션 PCR(거래대금)", "regime_pcr_summary.csv"),
]


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

    # 미래참조 편향(Look-ahead Bias) 제거: 각 지표별 과거 누적 상위 25%(Expanding Q75) 계산
    sent = sent.copy()
    sent["date"] = pd.to_datetime(sent["date"])
    sent = sent.sort_values("date").reset_index(drop=True)

    merge_cols = ["date"]
    for col, _, _ in INDICATORS:
        if col in sent.columns:
            sent[f"{col}_cutoff"] = sent[col].expanding(min_periods=MIN_PERIODS).quantile(0.75)
            merge_cols.extend([col, f"{col}_cutoff"])

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
    ev_df = ev_df.merge(sent[merge_cols], left_on="d0_date", right_on="date", how="left")
    return ev_df


def analyze_single_indicator(ev_df: pd.DataFrame, col: str, label: str):
    cutoff_col = f"{col}_cutoff"
    if col not in ev_df.columns or cutoff_col not in ev_df.columns:
        return pd.DataFrame(), pd.DataFrame()

    df = ev_df.dropna(subset=[col, cutoff_col, "mn_net_ret_5d", "prev_5d_val"]).copy()
    df = df[df["prev_5d_val"] >= MIN_DAILY_VAL].copy()
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()

    df["regime"] = np.where(df[col] >= df[cutoff_col], "공포(Fear)", "평온(Calm)")

    summary_rows = []
    for etype in TARGET_TYPES:
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

    if not summary_rows:
        return pd.DataFrame(), df

    res_df = pd.DataFrame(summary_rows).set_index("공시유형")
    num_cols = [c for c in res_df.columns if c not in ("평온_건수", "공포_건수", "차이_p_value")]
    res_df[num_cols] = res_df[num_cols].round(2)
    res_df["차이_p_value"] = res_df["차이_p_value"].round(4)
    return res_df, df


def analyze_buyback_for_indicator(buyback: pd.DataFrame, tagged_df: pd.DataFrame, col: str, label: str):
    if buyback.empty or tagged_df.empty:
        return pd.DataFrame()

    bb = buyback.merge(
        tagged_df[["rcept_no", "d0_date", "regime", col, "excess_gap", "mn_net_ret_5d", "prev_5d_val"]],
        on="rcept_no",
        how="inner",
    )
    bb["매입강도"] = (bb["aqpln_prc_ostk"] / bb["prev_5d_val"]).round(2)
    high_bb = bb[bb["매입강도"] >= 3.0].copy()
    if high_bb.empty:
        return pd.DataFrame()

    rows = []
    for reg, g in high_bb.groupby("regime"):
        mn = g["mn_net_ret_5d"].dropna()
        t_val, p_val = stats.ttest_1samp(mn, 0.0) if len(mn) >= 2 else (np.nan, np.nan)
        rows.append({
            "지표": label,
            "regime": reg,
            "건수": len(g),
            "평균지표": g[col].mean(),
            "오버나잇갭(%)": g["excess_gap"].mean() * 100,
            "시장중립_비용후(%)": mn.mean() * 100,
            "t_stat(vs 0)": t_val,
            "p_value(vs 0)": p_val,
            "승률(%)": (mn > 0).mean() * 100,
        })

    bb_grp = pd.DataFrame(rows)
    num_cols = [c for c in bb_grp.columns if c not in ("지표", "regime", "건수", "p_value(vs 0)")]
    bb_grp[num_cols] = bb_grp[num_cols].round(2)
    bb_grp["p_value(vs 0)"] = bb_grp["p_value(vs 0)"].round(4)
    return bb_grp


def plot_multi_indicator_comparison(results_map: dict[str, pd.DataFrame], save_path: Path):
    """VIX vs VKOSPI vs PCR(거래대금) 공포 국면 시장중립 수익률 비교 차트"""
    colors = {"미국 VIX": "#e45756", "한국 VKOSPI": "#f58518", "옵션 PCR(거래대금)": "#54a24b"}
    fig = go.Figure()

    for label, res_df in results_map.items():
        if res_df.empty:
            continue
        plot_df = res_df.reset_index()
        fig.add_trace(
            go.Bar(
                name=f"{label} 공포(상위 25%)",
                x=plot_df["공시유형"],
                y=plot_df["공포_중립수익(%)"],
                marker_color=colors.get(label, "#4c78a8"),
            )
        )

    fig.add_hline(y=0, line_dash="dash", line_color="gray")
    fig.update_layout(
        barmode="group",
        title="3대 파생 공포지표(VIX · VKOSPI · 옵션 PCR) 공포 국면 공시 시장중립 수익률 비교",
        xaxis_title="공시 유형",
        yaxis_title="공포 국면 시장중립 실전수익률 (%)",
        template="plotly_white",
    )
    fig.write_html(save_path, include_plotlyjs="cdn")


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    ohlcv, disc, buyback, sent = load_all()
    ev_df = prepare_events(ohlcv, disc, sent)

    results_map = {}
    buyback_frames = []

    for col, label, csv_name in INDICATORS:
        res_df, tagged_df = analyze_single_indicator(ev_df, col, label)
        if res_df.empty:
            print(f"\n[안내] {label}({col}) 데이터가 아직 충분하지 않아 건너뜁니다.")
            continue

        results_map[label] = res_df
        res_df.to_csv(OUTPUT_DIR / csv_name, encoding="utf-8-sig")

        start_str = tagged_df["d0_date"].min().strftime("%Y-%m-%d")
        end_str = tagged_df["d0_date"].max().strftime("%Y-%m-%d")
        print(f"\n================ [{label}] 공포 vs 평온 국면별 성과 ({start_str} ~ {end_str}, n={len(tagged_df):,}) ================")
        print(res_df.to_string())

        bb_grp = analyze_buyback_for_indicator(buyback, tagged_df, col, label)
        if not bb_grp.empty:
            buyback_frames.append(bb_grp)
            if col == "vix":
                bb_grp.set_index("regime").drop(columns=["지표"]).to_csv(
                    OUTPUT_DIR / "regime_buyback_summary.csv", encoding="utf-8-sig"
                )

    if buyback_frames:
        all_bb = pd.concat(buyback_frames, ignore_index=True)
        all_bb.to_csv(OUTPUT_DIR / "regime_buyback_multi.csv", index=False, encoding="utf-8-sig")
        print("\n================ [고강도 자사주(≥3배)] 3대 파생 심리지표 국면별 비교 ================")
        print(all_bb.to_string(index=False))

    if results_map:
        plot_multi_indicator_comparison(results_map, OUTPUT_DIR / "regime_event_comparison.html")
        print("\n[저장 완료] outputs/regime_event_comparison.html 및 지표별 CSV 업데이트 완료")


if __name__ == "__main__":
    main()