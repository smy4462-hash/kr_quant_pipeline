import time
import argparse
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).resolve().parent / ".env")

from db.database import init_db, save_ohlcv
from collectors.krx_collector import fetch_ohlcv, get_kospi200_tickers

def run_pipeline(tickers, days):
    init_db()
    end_dt = datetime.today().strftime("%Y%m%d")
    start_dt = (datetime.today() - timedelta(days=days)).strftime("%Y%m%d")

    total = len(tickers)
    print(f"=== 데이터 수집 시작: 총 {total}개 종목 ({start_dt} ~ {end_dt}) ===")

    success_cnt = 0
    for idx, ticker in enumerate(tickers, start=1):
        try:
            records = fetch_ohlcv(ticker, start_dt, end_dt)
            if records:
                save_ohlcv(records)
                success_cnt += 1
                print(f"[{idx}/{total}] [성공] {ticker}: {len(records)}건 적재")
            else:
                print(f"[{idx}/{total}] [스킵] {ticker}: 데이터 없음")
        except Exception as e:
            print(f"[{idx}/{total}] [에러] {ticker} 수집 실패: {e}")

        time.sleep(0.3)  # KRX 서버 차단(Rate Limit) 방지를 위한 대기

    print(f"=== 수집 완료: {success_cnt}/{total}개 종목 성공 ===")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="국내 주식 OHLCV 데이터 수집 파이프라인")
    parser.add_argument("--days", type=int, default=30, help="수집할 과거 일수 (기본값: 30)")
    parser.add_argument("--tickers", nargs="+", default=None, help="특정 종목만 수집할 때 지정 (미지정 시 코스피200 전체)")
    args = parser.parse_args()

    target_tickers = args.tickers if args.tickers else get_kospi200_tickers()
    run_pipeline(tickers=target_tickers, days=args.days)