import argparse
import sys
import time
from pathlib import Path
import duckdb
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from scipy import stats

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import analysis.interpreter as interp
from notifiers.telegram_bot import send_document, send_message

LAKE_DIR = ROOT_DIR / "data_lake"
V2_OUTPUT_DIR = ROOT_DIR / "outputs" / "v2"

TX_COST = 0.0025
MIN_DAILY_VAL = 1_000_000_000  # 직전 5일 평균 일거래대금 10억 원 이상
MIN_PERIODS = 60

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


def load_lake_data(markets: tuple[str, ...] = ("KR_KOSPI", "KR_KOSDAQ")):
    """DuckDB로 v2 데이터 레이크에서 국내 전 종목 주가·공시·심리지표 고속 로드"""
    con = duckdb.connect()
    ohlcv_glob = (LAKE_DIR / "ohlcv" / "market=*" / "year=*" / "data.parquet").as_posix()
    disc_path = (LAKE_DIR / "disclosures" / "market=KR" / "data.parquet").as_posix()
    bb_path = (LAKE_DIR / "buyback_details" / "market=KR" / "data.parquet").as_posix()
    sent_path = (LAKE_DIR / "market_sentiment" / "global_sentiment.parquet").as_posix()

    mkt_sql = ", ".join(f"'{m}'" for m in markets)
    ohlcv = con.execute(
        f"""
        SELECT market, date, ticker, open, high, low, close, volume
        FROM read_parquet('{ohlcv_glob}', hive_partitioning=true, union_by_name=true)
        WHERE market IN ({mkt_sql})
        ORDER BY ticker, date
        """
    ).df()
    disc = con.execute(f"SELECT * FROM read_parquet('{disc_path}') ORDER BY rcept_dt").df()
    buyback = con.execute(f"SELECT * FROM read_parquet('{bb_path}')").df()
    sent = con.execute(f"SELECT * FROM read_parquet('{sent_path}') ORDER BY date").df()
    con.close()
    return ohlcv, disc, buyback, sent


def prepare_events_fast(ohlcv: pd.DataFrame, disc: pd.DataFrame, sent: pd.DataFrame) -> pd.DataFrame:
    """국내 전 종목(약 300만 행) 대상 고속 이벤트 스터디 패널 생성"""
    df = ohlcv.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["ticker", "date", "market"]).drop_duplicates(subset=["ticker", "date"], keep="last").reset_index(drop=True)

    df["prev_close"] = df.groupby("ticker")["close"].shift(1)
    valid_open = df["open"] > 0
    valid_prev = df["prev_close"] > 0
    df["ret"] = np.where(valid_prev, df["close"] / df["prev_close"] - 1, np.nan)
    df["gap_ret"] = np.where(valid_open & valid_prev, df["open"] / df["prev_close"] - 1, np.nan)
    df["oc_ret"] = np.where(valid_open, df["close"] / df["open"] - 1, np.nan)

    # 시장별(코스피/코스닥) 동일가중 벤치마크 산출
    mkt_df = (
        df.groupby(["market", "date"])
        .agg(mkt_ret=("ret", "mean"), mkt_gap=("gap_ret", "mean"), mkt_oc=("oc_ret", "mean"))
        .reset_index()
    )
    df = df.merge(mkt_df, on=["market", "date"], how="left")
    df["ar"] = df["ret"] - df["mkt_ret"]
    df["excess_gap"] = df["gap_ret"] - df["mkt_gap"]

    # 거래정지일 제외 후 직전 5일 평균 거래대금
    df["trading_value"] = np.where(df["volume"] > 0, df["close"].astype(float) * df["volume"].astype(float), np.nan)
    df["prev_5d_val"] = (
        df.groupby("ticker")["trading_value"]
        .transform(lambda s: s.shift(1).rolling(5, min_periods=3).mean())
    )

    # 심리지표 Expanding Q75 (미래참조 편향 차단)
    sent_df = sent.copy()
    sent_df["date"] = pd.to_datetime(sent_df["date"])
    sent_df = sent_df.sort_values("date").reset_index(drop=True)
    merge_cols = ["date"]
    for col, _, _ in INDICATORS:
        if col in sent_df.columns:
            sent_df[f"{col}_cutoff"] = sent_df[col].expanding(min_periods=MIN_PERIODS).quantile(0.75)
            merge_cols.extend([col, f"{col}_cutoff"])

    # 종목별 NumPy 배열 딕셔너리 구축 (초고속 탐색용)
    price_arrays = {}
    for ticker, g in df.groupby("ticker", sort=False):
        dates_dt = g["date"].to_numpy(dtype="datetime64[ns]")
        price_arrays[ticker] = {
            "dates": dates_dt,
            "dates_ns": dates_dt.astype("int64"),
            "open": g["open"].to_numpy(dtype=np.float64),
            "close": g["close"].to_numpy(dtype=np.float64),
            "ar": g["ar"].to_numpy(dtype=np.float64),
            "excess_gap": g["excess_gap"].to_numpy(dtype=np.float64),
            "mkt_oc": g["mkt_oc"].to_numpy(dtype=np.float64),
            "mkt_ret": g["mkt_ret"].to_numpy(dtype=np.float64),
            "prev_5d_val": g["prev_5d_val"].to_numpy(dtype=np.float64),
            "market": g["market"].iloc[-1],
        }

    events = disc.copy()
    events["event_type"] = events.apply(classify_event_type, axis=1)
    events = events.drop_duplicates(subset=["rcept_dt", "ticker", "event_type"]).copy()
    rcept_dt_ns_arr = pd.to_datetime(events["rcept_dt"]).to_numpy(dtype="datetime64[ns]").astype("int64")

    rows = []
    for rcept_no, ticker, corp_name, etype, ev_dt_ns in zip(
        events["rcept_no"], events["ticker"], events["corp_name"], events["event_type"], rcept_dt_ns_arr
    ):
        arr = price_arrays.get(ticker)
        if arr is None:
            continue
        dates_ns = arr["dates_ns"]
        d0 = int(np.searchsorted(dates_ns, ev_dt_ns, side="left"))
        n_px = len(dates_ns)
        if d0 >= n_px:
            continue

        prev_val = arr["prev_5d_val"][d0]
        if np.isnan(prev_val) or prev_val < MIN_DAILY_VAL:
            continue

        post_car = np.nan
        excess_gap = arr["excess_gap"][d0 + 1] if d0 + 1 < n_px else np.nan
        mn_gross = np.nan
        mn_net = np.nan
        long_net = np.nan

        if d0 + 5 < n_px:
            ar_slice = arr["ar"][d0 + 1 : d0 + 6]
            if not np.all(np.isnan(ar_slice)):
                post_car = float(np.nansum(ar_slice))

            entry_open = arr["open"][d0 + 1]
            exit_close = arr["close"][d0 + 5]
            if entry_open > 0 and not np.isnan(exit_close):
                tradable_ret = (exit_close / entry_open) - 1.0
                long_net = tradable_ret - TX_COST

                m_oc = arr["mkt_oc"][d0 + 1]
                m_cc = arr["mkt_ret"][d0 + 2 : d0 + 6]
                if not np.isnan(m_oc) and not np.any(np.isnan(m_cc)):
                    mkt_hold = (1.0 + m_oc) * float(np.prod(1.0 + m_cc)) - 1.0
                    mn_gross = tradable_ret - mkt_hold
                    mn_net = mn_gross - TX_COST

        rows.append({
            "rcept_no": rcept_no,
            "d0_date": pd.Timestamp(arr["dates"][d0]),
            "market": arr["market"],
            "ticker": ticker,
            "corp_name": corp_name,
            "event_type": etype,
            "post_car_5d": post_car,
            "excess_gap": excess_gap,
            "mn_gross_5d": mn_gross,
            "mn_net_5d": mn_net,
            "long_net_5d": long_net,
            "prev_5d_val": prev_val,
        })

    ev_df = pd.DataFrame(rows)
    if ev_df.empty:
        return ev_df
    ev_df["d0_date"] = pd.to_datetime(ev_df["d0_date"]).astype("datetime64[ns]")
    sent_df["date"] = pd.to_datetime(sent_df["date"]).astype("datetime64[ns]")
    ev_df = ev_df.merge(sent_df[merge_cols], left_on="d0_date", right_on="date", how="left")
    return ev_df


def summarize_keywords(ev_df: pd.DataFrame) -> pd.DataFrame:
    """전 기간 공시 유형별 요약 (interpreter.py 호환 스키마)"""
    rows = []
    for etype in TARGET_TYPES:
        grp = ev_df[ev_df["event_type"] == etype]
        mn = grp["mn_net_5d"].dropna()
        if len(mn) < 5:
            continue
        t_val, p_val = stats.ttest_1samp(mn, 0.0)
        rows.append({
            "공시유형": etype,
            "건수": len(mn),
            "사후_CAR(%)": grp["post_car_5d"].mean() * 100,
            "오버나잇갭_초과(%)": grp["excess_gap"].mean() * 100,
            "시장중립_비용전(%)": grp["mn_gross_5d"].mean() * 100,
            "시장중립_비용후(%)": mn.mean() * 100,
            "롱온리_비용후(%)": grp["long_net_5d"].mean() * 100,
            "t_stat(중립)": t_val,
            "p_value(중립)": p_val,
            "승률_중립(%)": (mn > 0).mean() * 100,
        })

    res = pd.DataFrame(rows).set_index("공시유형").sort_values("건수", ascending=False)
    num_cols = [c for c in res.columns if c not in ("건수", "p_value(중립)")]
    res[num_cols] = res[num_cols].round(2)
    res["p_value(중립)"] = res["p_value(중립)"].round(4)
    return res


def analyze_single_indicator(ev_df: pd.DataFrame, col: str):
    cutoff_col = f"{col}_cutoff"
    if col not in ev_df.columns or cutoff_col not in ev_df.columns:
        return pd.DataFrame(), pd.DataFrame()

    df = ev_df.dropna(subset=[col, cutoff_col, "mn_net_5d"]).copy()
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()

    df["regime"] = np.where(df[col] >= df[cutoff_col], "공포(Fear)", "평온(Calm)")
    rows = []
    for etype in TARGET_TYPES:
        sub = df[df["event_type"] == etype]
        fear = sub[sub["regime"] == "공포(Fear)"]
        calm = sub[sub["regime"] == "평온(Calm)"]
        if len(fear) < 5 or len(calm) < 5:
            continue
        _, p_diff = stats.ttest_ind(fear["mn_net_5d"], calm["mn_net_5d"], equal_var=False)
        rows.append({
            "공시유형": etype,
            "평온_건수": len(calm),
            "평온_중립수익(%)": calm["mn_net_5d"].mean() * 100,
            "공포_건수": len(fear),
            "공포_갭(%)": fear["excess_gap"].mean() * 100,
            "공포_중립수익(%)": fear["mn_net_5d"].mean() * 100,
            "공포_승률(%)": (fear["mn_net_5d"] > 0).mean() * 100,
            "공포-평온_격차(%)": (fear["mn_net_5d"].mean() - calm["mn_net_5d"].mean()) * 100,
            "차이_p_value": p_diff,
        })

    if not rows:
        return pd.DataFrame(), df
    res = pd.DataFrame(rows).set_index("공시유형")
    num_cols = [c for c in res.columns if c not in ("평온_건수", "공포_건수", "차이_p_value")]
    res[num_cols] = res[num_cols].round(2)
    res["차이_p_value"] = res["차이_p_value"].round(4)
    return res, df


def analyze_buyback_indicator(buyback: pd.DataFrame, tagged_df: pd.DataFrame, col: str, label: str) -> pd.DataFrame:
    if buyback.empty or tagged_df.empty:
        return pd.DataFrame()
    bb = buyback.merge(
        tagged_df[["rcept_no", "d0_date", "regime", col, "excess_gap", "mn_net_5d", "prev_5d_val"]],
        on="rcept_no",
        how="inner",
    )
    bb["매입강도"] = (bb["aqpln_prc_ostk"] / bb["prev_5d_val"]).round(2)
    high_bb = bb[bb["매입강도"] >= 3.0].copy()
    if high_bb.empty:
        return pd.DataFrame()

    rows = []
    for reg, g in high_bb.groupby("regime"):
        mn = g["mn_net_5d"].dropna()
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


def get_v2_current_regimes(sent: pd.DataFrame) -> dict:
    """Parquet 심리지표 기반 현재 시장 국면 조회 (interpreter.py 주입용)"""
    regimes = {}
    for col, label in [("vix", "미국 VIX"), ("vkospi", "한국 VKOSPI"), ("pcr_val", "옵션 풋콜비율")]:
        if col not in sent.columns:
            continue
        sub = sent[["date", col]].dropna().reset_index(drop=True)
        if len(sub) < MIN_PERIODS:
            continue
        sub["cutoff"] = sub[col].expanding(min_periods=MIN_PERIODS).quantile(0.75)
        latest = sub.iloc[-1]
        regimes[col] = {
            "label": label,
            "is_fear": bool(latest[col] >= latest["cutoff"]),
            "val": round(float(latest[col]), 2),
            "cutoff": round(float(latest["cutoff"]), 2),
            "date": str(latest["date"]),
        }
    return regimes


def plot_regime_comparison(results_map: dict[str, pd.DataFrame], save_path: Path):
    colors = {"미국 VIX": "#e45756", "한국 VKOSPI": "#f58518", "옵션 PCR(거래대금)": "#54a24b"}
    fig = go.Figure()
    for label, res_df in results_map.items():
        if res_df.empty:
            continue
        p = res_df.reset_index()
        fig.add_trace(
            go.Bar(name=f"{label} 공포(상위 25%)", x=p["공시유형"], y=p["공포_중립수익(%)"], marker_color=colors.get(label))
        )
    fig.add_hline(y=0, line_dash="dash", line_color="gray")
    fig.update_layout(
        barmode="group",
        title="v2 국내 전 종목 — 3대 파생 심리지표 공포 국면 시장중립 수익률 비교",
        xaxis_title="공시 유형",
        yaxis_title="공포 국면 시장중립 비용후 수익률 (%)",
        template="plotly_white",
    )
    fig.write_html(save_path, include_plotlyjs="cdn")


def main():
    parser = argparse.ArgumentParser(description="v2 국내 전 종목 DuckDB 통합 분석 및 자동 해석 리포트 생성")
    parser.add_argument("--send-telegram", action="store_true", help="생성된 v2 주간 브리핑을 텔레그램으로 발송")
    parser.add_argument("--attach-report", action="store_true", help="상세 해석 리포트(.md) 함께 첨부")
    args = parser.parse_args()

    V2_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    print("[1/4] v2 데이터 레이크(DuckDB)에서 국내 전 종목 데이터 로드 중...")
    ohlcv, disc, buyback, sent = load_lake_data()
    print(f"  - 주가 {len(ohlcv):,}행 ({ohlcv['ticker'].nunique():,}종목) | 공시 {len(disc):,}건 | 자사주상세 {len(buyback):,}건")

    print("[2/4] 전 종목 이벤트 스터디 패널 계산 중...")
    ev_df = prepare_events_fast(ohlcv, disc, sent)
    if ev_df.empty:
        print("[안내] 유효한 이벤트 표본이 없습니다.")
        return

    kw_summary = summarize_keywords(ev_df)
    kw_summary.to_csv(V2_OUTPUT_DIR / "keyword_event_summary.csv", encoding="utf-8-sig")
    print("\n================ [v2 전 종목 전 기간 공시 요약] ================")
    print(kw_summary.to_string())

    print("\n[3/4] 3대 파생 심리지표 국면별 교차 검증 중...")
    results_map = {}
    bb_frames = []
    for col, label, csv_name in INDICATORS:
        res_df, tagged_df = analyze_single_indicator(ev_df, col)
        if res_df.empty:
            continue
        results_map[label] = res_df
        res_df.to_csv(V2_OUTPUT_DIR / csv_name, encoding="utf-8-sig")
        print(f"\n--- [{label}] 공포 vs 평온 국면 (표본 {len(tagged_df):,}건) ---")
        print(res_df.to_string())

        bb_grp = analyze_buyback_indicator(buyback, tagged_df, col, label)
        if not bb_grp.empty:
            bb_frames.append(bb_grp)

    if bb_frames:
        all_bb = pd.concat(bb_frames, ignore_index=True)
        all_bb.to_csv(V2_OUTPUT_DIR / "regime_buyback_multi.csv", index=False, encoding="utf-8-sig")
        print("\n================ [v2 전 종목 고강도 자사주(≥3배) 공포 국면 비교] ================")
        print(all_bb.to_string(index=False))

    if results_map:
        plot_regime_comparison(results_map, V2_OUTPUT_DIR / "regime_event_comparison.html")

    print(f"\n[4/4] 5단계 자동 해석 엔진 실행 (출력 경로: {V2_OUTPUT_DIR})...")
    interp.OUTPUT_DIR = V2_OUTPUT_DIR
    interp.get_current_regimes = lambda: get_v2_current_regimes(sent)
    interp.generate_reports()

    elapsed = time.perf_counter() - t0
    print(f"\n[완료] v2 국내 전 종목 전체 분석 및 리포트 생성 완료 (총 소요 {elapsed:.1f}초)")

    if args.send_telegram:
        brief_path = V2_OUTPUT_DIR / "telegram_brief.txt"
        send_message(brief_path.read_text(encoding="utf-8").strip())
        if args.attach_report:
            send_document(V2_OUTPUT_DIR / "interpretation_report.md", caption="v2 국내 전 종목 5단계 상세 해석 리포트")


if __name__ == "__main__":
    main()