import os
from datetime import datetime, timedelta
from pykrx import stock

def get_kospi200_tickers():
    """최근 7일 중 데이터가 있는 가장 최근 영업일 기준 코스피200 종목 반환"""
    print(f"[체크] KRX_ID 로드 여부: {bool(os.getenv('KRX_ID'))}")

    for i in range(1, 8):
        target_date = (datetime.today() - timedelta(days=i)).strftime("%Y%m%d")
        try:
            tickers = stock.get_index_portfolio_deposit_file("1028", date=target_date)
            if len(tickers) > 0:
                print(f"[안내] {target_date} 기준 코스피200 종목 {len(tickers)}개 조회 성공")
                return tickers
            else:
                print(f"[{target_date}] 조회 결과 0건")
        except Exception as e:
            print(f"[{target_date}] 실패 원인: {type(e).__name__} - {e}")

    raise RuntimeError("코스피200 종목을 불러오지 못했습니다.")

def fetch_ohlcv(ticker, start_date, end_date):
    df = stock.get_market_ohlcv(start_date, end_date, ticker)
    if df.empty:
        return []

    df = df.reset_index()
    df["date"] = df["날짜"].dt.strftime("%Y-%m-%d")
    df["ticker"] = ticker

    return df[["date", "ticker", "시가", "고가", "저가", "종가", "거래량"]].values.tolist()