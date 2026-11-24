import FinanceDataReader as fdr


def _format_date(date_str: str) -> str:
    """'YYYYMMDD' 형식을 'YYYY-MM-DD'로 변환"""
    if len(date_str) == 8 and "-" not in date_str:
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    return date_str


def get_kospi200_tickers():
    """DB가 비어 있을 때만 사용하는 예비 함수: 코스피 시가총액 상위 200개 종목 반환"""
    df = fdr.StockListing("KOSPI")
    if "Marcap" in df.columns:
        df = df.sort_values("Marcap", ascending=False)
    tickers = df["Code"].head(200).tolist()
    print(f"[안내] 네이버/KRX 상장목록 기준 코스피 시총 상위 {len(tickers)}개 종목 조회 성공")
    return tickers


def fetch_ohlcv(ticker: str, start_date: str, end_date: str):
    """FinanceDataReader(네이버 금융)로 수정주가 OHLCV 수집"""
    start = _format_date(start_date)
    end = _format_date(end_date)

    df = fdr.DataReader(ticker, start, end)
    if df is None or df.empty:
        return []

    df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"]).reset_index()
    date_col = df.columns[0]  # 보통 'Date'
    df["date"] = df[date_col].dt.strftime("%Y-%m-%d")
    df["ticker"] = ticker

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = df[col].astype(int)

    return df[["date", "ticker", "Open", "High", "Low", "Close", "Volume"]].values.tolist()