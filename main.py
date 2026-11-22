import argparse
from datetime import datetime, timedelta
from config import DEFAULT_TICKERS
from db.database import init_db, save_ohlcv
from collectors.krx_collector import fetch_ohlcv
from dotenv import load_dotenv
load_dotenv()  # .env 파일의 KRX_ID, KRX_PW를 환경 변수로 불러옴

def run_pipeline(tickers, days):
    init_db()
    end_dt = datetime.today().strftime("%Y%m%d")
    start_dt = (datetime.today() - timedelta(days=days)).strftime("%Y%m%d")

    print(f"=== 데이터 수집 시작 ({start_dt} ~ {end_dt}) ===")
    for ticker in tickers:
        try:
            records = fetch_ohlcv(ticker, start_dt, end_dt)
            if records:
                save_ohlcv(records)
                print(f"[성공] {ticker}: {len(records)}건 적재 완료")
            else:
                print(f"[스킵] {ticker}: 수집된 데이터 없음")
        except Exception as e:
            print(f"[에러] {ticker} 수집 실패: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="국내 주식 OHLCV 데이터 수집 파이프라인")
    parser.add_argument("--days", type=int, default=30, help="수집할 과거 일수 (기본값: 30)")
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS, help="수집할 종목 코드 리스트")
    args = parser.parse_args()

    run_pipeline(tickers=args.tickers, days=args.days)