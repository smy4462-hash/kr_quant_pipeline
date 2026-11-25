import sqlite3
from pathlib import Path
import FinanceDataReader as fdr
import numpy as np
import pandas as pd
from scipy import stats

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "kr_quant.db"
OUTPUT_DIR = ROOT_DIR / "outputs"

TX_COST = 0.0025
MIN_DAILY_VAL = 1_000_000_000
MIN_PERIODS = 60
HORIZONS = [3, 5, 10, 20]


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


def load_data():
    conn = sqlite3.connect(DB_PATH)
    ohlcv = pd.read_sql("SELECT * FROM daily_ohlcv ORDER BY ticker, date", conn)
    disc = pd.read_sql("SELECT * FROM disclosures ORDER BY rcept_dt", conn)
    buyback = pd.read_sql("SELECT * FROM buyback_details", conn)
    sent = pd.read_sql("SELECT * FROM market_sentiment ORDER BY date", conn)
    conn.close()

    ohlcv["date"] = pd.to_datetime(ohlcv["date"])
    start_str = ohlcv["date"].min().strftime("%Y-%m-%d")
    end_str = ohlcv["date"].max().strftime("%Y-%m-%d")

    print(f"[벤치마크] KODEX 200(069500) 조회 중 ({start_str} ~ {end_str})...")
    kodex = fdr.DataReader("069500", start_str, end_str)[["Open", "Close"]].copy()
    kodex.index = pd.to_datetime(kodex.index)
    kodex.columns = ["k_open", "k_close"]
    return ohlcv, disc, buyback, sent, kodex


def build_event_panel(ohlcv: pd.DataFrame, disc: pd.DataFrame, buyback: pd.DataFrame, sent: pd.DataFrame, kodex: pd.DataFrame) -> pd.DataFrame:
    df = ohlcv.sort_values(["ticker", "date"]).reset_index(drop=True)
    df["prev_close"] = df.groupby("ticker")["close"].shift(1)
    df["ret"] = df["close"] / df["prev_close"] - 1
    df["oc_ret"] = np.where(df["open"] > 0, df["close"] / df["open"] - 1, np.nan)

    # 기존 동일가중 코스피200 벤치마크
    mkt_ew = df.groupby("date").agg(mkt_ret=("ret", "mean"), mkt_oc=("oc_ret", "mean")).sort_index()

    # 유동성 지표 (직전 5일 평균 거래대금)
    df["trading_value"] = np.where(df["volume"] > 0, df["close"] * df["volume"], np.nan)
    df["prev_5d_val"] = df.groupby("ticker")["trading_value"].transform(lambda s: s.shift(1).rolling(5, min_periods=3).mean())

    # 심리지표 국면 컷오프 (Expanding Q75 vs VKOSPI Rolling 252일 Q75)
    sent = sent.copy()
    sent["date"] = pd.to_datetime(sent["date"])
    sent = sent.sort_values("date").reset_index(drop=True)
    sent["vix_exp"] = sent["vix"].expanding(min_periods=MIN_PERIODS).quantile(0.75)
    sent["pcr_exp"] = sent["pcr_val"].expanding(min_periods=MIN_PERIODS).quantile(0.75)
    sent["vkospi_exp"] = sent["vkospi"].expanding(min_periods=MIN_PERIODS).quantile(0.75)
    sent["vkospi_roll252"] = sent["vkospi"].rolling(window=252, min_periods=MIN_PERIODS).quantile(0.75)

    events = disc.copy()
    events["event_type"] = events.apply(classify_event_type, axis=1)
    events = events.drop_duplicates(subset=["rcept_dt", "ticker", "event_type"]).copy()
    events["rcept_dt"] = pd.to_datetime(events["rcept_dt"])

    bb_map = buyback.set_index("rcept_no")["aqpln_prc_ostk"].to_dict() if not buyback.empty else {}
    grouped_price = {t: g.reset_index(drop=True) for t, g in df.groupby("ticker")}

    rows = []
    for _, ev in events.iterrows():
        ticker = ev["ticker"]
        if ticker not in grouped_price:
            continue
        px = grouped_price[ticker]
        idx_cand = px.index[px["date"] >= ev["rcept_dt"]]
        if len(idx_cand) == 0:
            continue
        d0 = idx_cand[0]
        prev_5d_val = px.loc[d0, "prev_5d_val"]
        if pd.isna(prev_5d_val) or prev_5d_val < MIN_DAILY_VAL:
            continue

        d0_date = px.loc[d0, "date"]
        row = {
            "rcept_no": ev["rcept_no"],
            "d0_date": d0_date,
            "ticker": ticker,
            "event_type": ev["event_type"],
            "buyback_intensity": (bb_map[ev["rcept_no"]] / prev_5d_val) if ev["rcept_no"] in bb_map else np.nan,
        }

        if d0 + 1 < len(px):
            entry_open = px.loc[d0 + 1, "open"]
            d1_date = px.loc[d0 + 1, "date"]
            k_open = kodex.loc[d1_date, "k_open"] if d1_date in kodex.index else np.nan
            ew_oc = mkt_ew.loc[d1_date, "mkt_oc"] if d1_date in mkt_ew.index else np.nan

            for h in HORIZONS:
                row[f"kodex_mn_{h}d"] = np.nan
                row[f"ew_mn_{h}d"] = np.nan
                if d0 + h >= len(px) or not (entry_open and entry_open > 0):
                    continue

                exit_close = px.loc[d0 + h, "close"]
                exit_date = px.loc[d0 + h, "date"]
                stk_ret = (exit_close / entry_open) - 1

                # 1) KODEX 200 시총가중 헤지 (D+1 시가 -> D+H 종가)
                if pd.notna(k_open) and k_open > 0 and exit_date in kodex.index:
                    k_close = kodex.loc[exit_date, "k_close"]
                    if pd.notna(k_close):
                        k_ret = (k_close / k_open) - 1
                        row[f"kodex_mn_{h}d"] = (stk_ret - k_ret) - TX_COST

                # 2) 기존 동일가중(EW) 헤지 (D+5 비교용)
                if h == 5 and pd.notna(ew_oc):
                    d2_dh = px.loc[d0 + 2 : d0 + h, "date"]
                    ew_cc = mkt_ew.reindex(d2_dh)["mkt_ret"].dropna()
                    if len(ew_cc) == len(d2_dh):
                        ew_hold = (1 + ew_oc) * np.prod(1 + ew_cc.values) - 1
                        row[f"ew_mn_{h}d"] = (stk_ret - ew_hold) - TX_COST

        rows.append(row)

    ev_df = pd.DataFrame(rows)
    ev_df = ev_df.merge(
        sent[["date", "vix", "vix_exp", "pcr_val", "pcr_exp", "vkospi", "vkospi_exp", "vkospi_roll252"]],
        left_on="d0_date",
        right_on="date",
        how="left",
    )
    ev_df["vix_fear"] = ev_df["vix"] >= ev_df["vix_exp"]
    ev_df["pcr_fear"] = ev_df["pcr_val"] >= ev_df["pcr_exp"]
    ev_df["vk_exp_fear"] = ev_df["vkospi"] >= ev_df["vkospi_exp"]
    ev_df["vk_roll_fear"] = ev_df["vkospi"] >= ev_df["vkospi_roll252"]
    return ev_df


def _calc_stats(series: pd.Series) -> tuple[int, float, float, float]:
    s = series.dropna()
    if len(s) < 2:
        return len(s), np.nan, np.nan, np.nan
    _, p = stats.ttest_1samp(s, 0.0)
    return len(s), round(s.mean() * 100, 2), round((s > 0).mean() * 100, 1), round(p, 4)


def run_benchmark_comparison(ev_df: pd.DataFrame) -> pd.DataFrame:
    """1. 동일가중(EW) vs KODEX 200(시총가중) 벤치마크 비교 (D+5 기준)"""
    cohorts = [
        ("영업(잠정)실적 (전기간)", ev_df[ev_df["event_type"] == "영업(잠정)실적"]),
        ("배당 (전기간)", ev_df[ev_df["event_type"] == "배당"]),
        ("매출액또는손익구조 (전기간)", ev_df[ev_df["event_type"] == "매출액또는손익구조"]),
        ("소각 (전기간)", ev_df[ev_df["event_type"] == "소각"]),
        ("VIX 공포 + 자사주 처분/해지", ev_df[(ev_df["event_type"] == "자사주_처분/해지") & (ev_df["vix_fear"])]),
        ("PCR 공포 + 자사주 취득/신탁", ev_df[(ev_df["event_type"] == "자사주_취득/신탁") & (ev_df["pcr_fear"])]),
        ("VIX 공포 + 고강도 자사주(≥3배)", ev_df[(ev_df["buyback_intensity"] >= 3.0) & (ev_df["vix_fear"])]),
    ]
    rows = []
    for name, sub in cohorts:
        n_ew, ret_ew, win_ew, p_ew = _calc_stats(sub["ew_mn_5d"])
        n_kd, ret_kd, win_kd, p_kd = _calc_stats(sub["kodex_mn_5d"])
        rows.append({
            "시그널": name,
            "건수": n_kd,
            "동일가중_5일(%)": ret_ew,
            "동일가중_p": p_ew,
            "KODEX200_5일(%)": ret_kd,
            "KODEX200_승률(%)": win_kd,
            "KODEX200_p": p_kd,
            "괴리(KODEX-EW)": round(ret_kd - ret_ew, 2) if pd.notna(ret_kd) and pd.notna(ret_ew) else np.nan,
        })
    return pd.DataFrame(rows)


def run_horizon_analysis(ev_df: pd.DataFrame) -> pd.DataFrame:
    """2. 다중 보유 시계 검증 (KODEX 200 헤지 기준: D+3, D+5, D+10, D+20)"""
    cohorts = [
        ("영업(잠정)실적 (회피)", ev_df[ev_df["event_type"] == "영업(잠정)실적"]),
        ("배당 (회피)", ev_df[ev_df["event_type"] == "배당"]),
        ("매출액또는손익구조 (회피)", ev_df[ev_df["event_type"] == "매출액또는손익구조"]),
        ("VIX 공포 + 자사주 처분/해지 (강력회피)", ev_df[(ev_df["event_type"] == "자사주_처분/해지") & (ev_df["vix_fear"])]),
        ("PCR 공포 + 자사주 취득/신탁 (매수)", ev_df[(ev_df["event_type"] == "자사주_취득/신탁") & (ev_df["pcr_fear"])]),
        ("VIX 공포 + 고강도 자사주≥3배 (매수)", ev_df[(ev_df["buyback_intensity"] >= 3.0) & (ev_df["vix_fear"])]),
    ]
    rows = []
    for name, sub in cohorts:
        rec = {"시그널": name, "기준건수(5일)": int(sub["kodex_mn_5d"].notna().sum())}
        for h in HORIZONS:
            _, ret, _, p = _calc_stats(sub[f"kodex_mn_{h}d"])
            star = "***" if pd.notna(p) and p < 0.01 else ("**" if pd.notna(p) and p < 0.05 else "")
            rec[f"D+{h}일(%)"] = f"{ret:+.2f}%{star}" if pd.notna(ret) else "N/A"
        rows.append(rec)
    return pd.DataFrame(rows)


def run_vkospi_rolling_comparison(ev_df: pd.DataFrame) -> pd.DataFrame:
    """3. VKOSPI Expanding Q75 vs Rolling 252일 Q75 비교 (KODEX 200 5일 수익률 기준)"""
    sub_all = ev_df.dropna(subset=["vkospi", "vkospi_exp", "vkospi_roll252", "kodex_mn_5d"]).copy()
    targets = [
        ("소각", sub_all[sub_all["event_type"] == "소각"]),
        ("자사주_취득/신탁", sub_all[sub_all["event_type"] == "자사주_취득/신탁"]),
        ("자사주_처분/해지", sub_all[sub_all["event_type"] == "자사주_처분/해지"]),
        ("고강도 자사주(≥3배)", sub_all[sub_all["buyback_intensity"] >= 3.0]),
    ]
    rows = []
    for label, flag_col in [("Expanding Q75", "vk_exp_fear"), ("Rolling 252일 Q75", "vk_roll_fear")]:
        overall_fear_pct = round(sub_all[flag_col].mean() * 100, 1)
        for name, sub in targets:
            fear = sub[sub[flag_col]]["kodex_mn_5d"].dropna()
            calm = sub[~sub[flag_col]]["kodex_mn_5d"].dropna()
            if len(fear) < 5 or len(calm) < 5:
                continue
            _, p_diff = stats.ttest_ind(fear, calm, equal_var=False)
            rows.append({
                "방식": f"{label} (전체공포 {overall_fear_pct}%)",
                "공시유형": name,
                "공포건수": len(fear),
                "평온수익(%)": round(calm.mean() * 100, 2),
                "공포수익(%)": round(fear.mean() * 100, 2),
                "공포승률(%)": round((fear > 0).mean() * 100, 1),
                "격차(%p)": round((fear.mean() - calm.mean()) * 100, 2),
                "차이_p": round(p_diff, 4),
            })
    return pd.DataFrame(rows)


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    ohlcv, disc, buyback, sent, kodex = load_data()
    ev_df = build_event_panel(ohlcv, disc, buyback, sent, kodex)

    bench_df = run_benchmark_comparison(ev_df)
    horizon_df = run_horizon_analysis(ev_df)
    vk_df = run_vkospi_rolling_comparison(ev_df)

    bench_df.to_csv(OUTPUT_DIR / "deep_dive_benchmark.csv", index=False, encoding="utf-8-sig")
    horizon_df.to_csv(OUTPUT_DIR / "deep_dive_horizons.csv", index=False, encoding="utf-8-sig")
    vk_df.to_csv(OUTPUT_DIR / "deep_dive_vkospi_rolling.csv", index=False, encoding="utf-8-sig")

    print("\n================ [1] 벤치마크 비교: 동일가중(EW) vs KODEX 200(시총가중) (5일, 비용 0.25% 차감) ================")
    print(bench_df.to_string(index=False))

    print("\n================ [2] 다중 보유 시계 검증: D+3 / D+5 / D+10 / D+20 (KODEX 200 헤지 기준) ================")
    print(horizon_df.to_string(index=False))

    print("\n================ [3] VKOSPI 체제 변화 검증: Expanding Q75 vs Rolling 252일 Q75 ================")
    print(vk_df.to_string(index=False))


if __name__ == "__main__":
    main()