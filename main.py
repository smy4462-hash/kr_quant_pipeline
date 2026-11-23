import time
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from db.database import (
    init_db, save_ohlcv, save_disclosures, save_buyback_details, get_saved_tickers
)
from collectors.dart_collector import fetch_disclosures, fetch_buyback_details

def run_stock_pipeline(tickers, days):
    from collectors.krx_collector import fetch_ohlcv

    end_dt = datetime.today().strftime("%Y%m%d")
    start_dt = (datetime.today() - timedelta(days=days)).strftime("%Y%m%d")

    total = len(tickers)
    print(f"=== [주가] 수집 시작: 총 {total}개 종목 ({start_dt} ~ {end_dt}) ===")

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

        time.sleep(0.3)

    print(f"=== [주가] 수집 완료: {success_cnt}/{total}개 종목 성공 ===")

def run_dart_pipeline(tickers, days):
    end_dt = datetime.today().strftime("%Y-%m-%d")
    start_dt = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    print(f"=== [공시] 수집 시작: 대상 {len(tickers)}개 종목 ({start_dt} ~ {end_dt}) ===")
    records, buyback_targets = fetch_disclosures(tickers, start_dt, end_dt)

    if records:
        save_disclosures(records)
        print(f"[성공] 핵심 공시 {len(records)}건 DB 적재 완료 (원문 링크 포함)")
        for rec in records[:5]:
            print(f"  - [{rec[1]}] {rec[3]}({rec[2]}) | {rec[6]} | {rec[4]}")
            print(f"    링크: {rec[7]}")
    else:
        print("[안내] 해당 기간에 조건과 일치하는 공시가 없습니다.")

    if buyback_targets:
        print(f"\n=== [자사주 상세] {len(buyback_targets)}개 종목 정형 수치 조회 시작 ===")
        details = fetch_buyback_details(buyback_targets, start_dt, end_dt)
        if details:
            save_buyback_details(details)
            print(f"[성공] 자사주 취득 상세 {len(details)}건 DB 적재 완료")
            for d in details[:5]:
                amount_eok = d[5] / 100_000_000  # 원 -> 억원 변환
                print(f"  - {d[2]}({d[1]}) | 결의일: {d[3]} | 예정금액: {amount_eok:,.1f}억 원 | 목적: {d[8]}")
        else:
            print("[안내] 조회된 자사주 취득 결정 상세 내역이 없습니다.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="국내 주식 & 공시 데이터 수집 파이프라인")
    parser.add_argument(
        "--mode",
        choices=["stock", "dart", "all"],
        default="stock",
        help="실행 모드 선택 (stock: 주가, dart: 공시, all: 둘 다)"
    )
    parser.add_argument("--days", type=int, default=30, help="수집할 과거 일수 (기본값: 30)")
    parser.add_argument("--tickers", nargs="+", default=None, help="특정 종목 지정 (미지정 시 코스피200 전체)")
    args = parser.parse_args()

    init_db()

    if args.tickers:
        target_tickers = args.tickers
    elif args.mode == "dart":
        target_tickers = get_saved_tickers()
        if target_tickers:
            print(f"[안내] DB에 저장된 코스피200 종목 {len(target_tickers)}개를 불러왔습니다.")
        else:
            from collectors.krx_collector import get_kospi200_tickers
            target_tickers = get_kospi200_tickers()
    else:
        from collectors.krx_collector import get_kospi200_tickers
        target_tickers = get_kospi200_tickers()

    if args.mode in ("stock", "all"):
        run_stock_pipeline(tickers=target_tickers, days=args.days)
    if args.mode in ("dart", "all"):
        run_dart_pipeline(tickers=target_tickers, days=args.days)