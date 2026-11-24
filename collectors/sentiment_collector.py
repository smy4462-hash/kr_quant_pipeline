import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import FinanceDataReader as fdr
import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "kr_quant.db"


def init_sentiment_table():
    """market_sentiment 테이블 생성 (추후 vkospi, pcr 컬럼 확장 가능 구조)"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_sentiment (
            date TEXT PRIMARY KEY,
            vix REAL,
            kr_hv20 REAL,
            usd_krw REAL
        )
    """)
    conn.commit()
    conn.close()


def _fetch_fred_series(symbol: str, start_date: str, end_date: str, col_name: str) -> pd.DataFrame:
    """FRED 시계열 조회 후 (us_date, col_name) DataFrame 반환"""
    df = fdr.DataReader(f"FRED:{symbol}", start_date, end_date)
    if df is None or df.empty:
        return pd.DataFrame(columns=["us_date", col_name])

    df = df.reset_index()
    date_col = df.columns[0]
    val_col = df.columns[1]
    out = df[[date_col, val_col]].rename(columns={date_col: "us_date", val_col: col_name})
    out["us_date"] = pd.to_datetime(out["us_date"])
    out[col_name] = pd.to_numeric(out[col_name], errors="coerce")
    return out.dropna().sort_values("us_date")


def build_and_save_sentiment(days: int = 800):
    """DB의 코스피200 거래일 기준으로 VIX, 국내 20일 실현변동성(HV20), 원/달러 환율 적재"""
    init_sentiment_table()

    conn = sqlite3.connect(DB_PATH)
    ohlcv = pd.read_sql("SELECT date, ticker, close FROM daily_ohlcv ORDER BY ticker, date", conn)
    conn.close()

    if ohlcv.empty:
        raise RuntimeError("daily_ohlcv 테이블이 비어 있습니다. 주가 데이터를 먼저 수집해 주세요.")

    # 1. 국내 코스피200 동일가중 수익률 기반 20일 연율화 실현변동성 (HV20) 산출
    ohlcv["date"] = pd.to_datetime(ohlcv["date"])
    ohlcv["ret"] = ohlcv.groupby("ticker")["close"].pct_change()
    kr_mkt = ohlcv.groupby("date")["ret"].mean().sort_index().to_frame("mkt_ret")
    # 당일 장 마감 전 시점 기준으로 쓰기 위해 전일(.shift(1)) HV20 사용
    kr_mkt["kr_hv20"] = (kr_mkt["mkt_ret"].rolling(20, min_periods=10).std() * np.sqrt(252) * 100).shift(1)
    kr_df = kr_mkt.reset_index()[["date", "kr_hv20"]].sort_values("date")

    # 2. FRED에서 VIX(S&P500 옵션 내재변동성) & 원/달러 환율 수집
    end_dt = datetime.today().strftime("%Y-%m-%d")
    start_dt = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    print(f"=== [심리지표] FRED VIX & 원/달러 환율 수집 중 ({start_dt} ~ {end_dt}) ===")
    vix_df = _fetch_fred_series("VIXCLS", start_dt, end_dt, "vix")
    fx_df = _fetch_fred_series("DEXKOUS", start_dt, end_dt, "usd_krw")

    # 3. 시차(Look-ahead bias) 방지 매칭: 한국 거래일(T)에는 반드시 T-1일 이전 미국 종가만 매칭
    kr_df["match_date"] = kr_df["date"] - pd.Timedelta(days=1)

    merged = pd.merge_asof(kr_df, vix_df, left_on="match_date", right_on="us_date", direction="backward")
    merged = pd.merge_asof(merged.drop(columns=["us_date"]), fx_df, left_on="match_date", right_on="us_date", direction="backward")

    merged["date_str"] = merged["date"].dt.strftime("%Y-%m-%d")
    records = [
        (
            row["date_str"],
            round(float(row["vix"]), 2) if pd.notna(row["vix"]) else None,
            round(float(row["kr_hv20"]), 2) if pd.notna(row["kr_hv20"]) else None,
            round(float(row["usd_krw"]), 2) if pd.notna(row["usd_krw"]) else None,
        )
        for _, row in merged.iterrows()
    ]

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.executemany(
        "INSERT OR REPLACE INTO market_sentiment (date, vix, kr_hv20, usd_krw) VALUES (?, ?, ?, ?)",
        records,
    )
    conn.commit()
    conn.close()

    print(f"[성공] market_sentiment 테이블에 {len(records):,}거래일 적재 완료")
    print(merged[["date_str", "vix", "kr_hv20", "usd_krw"]].tail(5).to_string(index=False))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="시장 심리지표(VIX, HV20, 환율) 수집")
    parser.add_argument("--days", type=int, default=1900, help="FRED 조회 과거 일수 (기본값: 1900일)")
    args = parser.parse_args()
    build_and_save_sentiment(days=args.days)