from pykrx import stock

def fetch_ohlcv(ticker, start_date, end_date):
    df = stock.get_market_ohlcv(start_date, end_date, ticker)
    if df.empty:
        return []

    df = df.reset_index()
    df["date"] = df["날짜"].dt.strftime("%Y-%m-%d")
    df["ticker"] = ticker

    return df[["date", "ticker", "시가", "고가", "저가", "종가", "거래량"]].values.tolist()