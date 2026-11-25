import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
import FinanceDataReader as fdr
import pandas as pd
import requests
import yfinance as yf

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db.lake_manager import DataLakeManager

STD_COLS = ["date", "ticker", "open", "high", "low", "close", "volume"]

# FinanceDataReader 내부 requests 호출이 무한 대기(Hang)에 빠지지 않도록 15초 강제 타임아웃 적용
_ORIG_REQUEST = requests.Session.request


def _timeout_request(self, *args, **kwargs):
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = 15
    return _ORIG_REQUEST(self, *args, **kwargs)


requests.Session.request = _timeout_request


def _get_kr_universe(market: str) -> pd.DataFrame:
    """KR_KOSPI 또는 KR_KOSDAQ 전 종목 리스트 조회"""
    fdr_key = "KOSPI" if market == "KR_KOSPI" else "KOSDAQ"
    listing = fdr.StockListing(fdr_key)

    code_col = "Code" if "Code" in listing.columns else "Symbol"
    name_col = "Name" if "Name" in listing.columns else code_col

    uni = pd.DataFrame({
        "ticker": listing[code_col].astype(str).str.zfill(6),
        "name": listing[name_col].astype(str),
        "market": market,
        "sector": listing["Sector"].astype(str) if "Sector" in listing.columns else "",
    })
    return uni.drop_duplicates(subset=["ticker"]).reset_index(drop=True)


def _fetch_single_kr(ticker: str, start_str: str, end_str: str) -> pd.DataFrame | None:
    """단일 종목 조회 (타임아웃 및 일시 오류 시 1회 재시도)"""
    for attempt in range(2):
        try:
            time.sleep(0.1)  # 네이버 금융 Rate Limit 방지용 미세 딜레이
            df = fdr.DataReader(ticker, start_str, end_str)
            if df is None or df.empty:
                return None
            df = df.reset_index()
            date_col = "Date" if "Date" in df.columns else df.columns[0]
            out = pd.DataFrame({
                "date": pd.to_datetime(df[date_col]).dt.strftime("%Y-%m-%d"),
                "ticker": ticker,
                "open": pd.to_numeric(df["Open"], errors="coerce"),
                "high": pd.to_numeric(df["High"], errors="coerce"),
                "low": pd.to_numeric(df["Low"], errors="coerce"),
                "close": pd.to_numeric(df["Close"], errors="coerce"),
                "volume": pd.to_numeric(df["Volume"], errors="coerce").fillna(0).astype("int64"),
            })
            return out.dropna(subset=["close"])
        except Exception:
            if attempt == 0:
                time.sleep(1.0)
            else:
                return None
    return None


def collect_kr_market(
    market: str,
    start_str: str,
    end_str: str,
    lake: DataLakeManager,
    limit: int | None = None,
    workers: int = 3,
    chunk_size: int = 100,
    resume: bool = False,
):
    """국내 시장(KOSPI / KOSDAQ) 안정화 병렬 수집 (100종목 단위 중간 저장 및 이어받기 지원)"""
    uni = _get_kr_universe(market)
    lake.write_table(uni, "universe", f"market={market}/data.parquet")

    tickers = uni["ticker"].tolist()
    if limit:
        tickers = tickers[:limit]

    ckpt_file = lake.lake_dir / f".done_{market}.txt"
    done_set: set[str] = set()
    if resume and ckpt_file.exists():
        done_set = set(ckpt_file.read_text(encoding="utf-8").splitlines())
        tickers = [t for t in tickers if t not in done_set]
        print(f"[{market}] 이어받기 활성화: 기존 완료 {len(done_set)}종목 스킵, 잔여 {len(tickers)}종목")
    elif not resume and ckpt_file.exists():
        ckpt_file.unlink()

    if not tickers:
        print(f"[{market}] 추가 수집할 잔여 종목이 없습니다.\n")
        return

    print(f"[{market}] 총 {len(tickers)}종목 수집 시작 ({start_str} ~ {end_str}, workers={workers})")
    t0 = time.perf_counter()
    total_saved_rows = 0
    processed = 0

    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i : i + chunk_size]
        frames = []
        chunk_done = []

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_fetch_single_kr, t, start_str, end_str): t for t in chunk}
            for fut in as_completed(futures):
                t = futures[fut]
                processed += 1
                res = fut.result()
                if res is not None and not res.empty:
                    frames.append(res)
                chunk_done.append(t)

                if processed % 20 == 0 or processed == len(tickers):
                    elapsed = time.perf_counter() - t0
                    print(f"  - 진행률: {processed}/{len(tickers)} (누적 경과 {elapsed:.0f}초)", flush=True)

        # 청크 완료 시 즉시 Parquet 중간 저장 및 체크포인트 기록
        if frames:
            chunk_df = pd.concat(frames, ignore_index=True)[STD_COLS]
            lake.write_ohlcv_partition(chunk_df, market=market)
            total_saved_rows += len(chunk_df)

        with open(ckpt_file, "a", encoding="utf-8") as f:
            f.write("\n".join(chunk_done) + "\n")

        print(f"  [체크포인트 저장] 누적 {processed}/{len(tickers)}종목 반영 완료 ({total_saved_rows:,}행)", flush=True)

    elapsed = time.perf_counter() - t0
    print(f"[{market}] 완료: 총 {total_saved_rows:,}행 저장 (소요 {elapsed:.1f}초)\n")


def _get_us_sp500_universe() -> pd.DataFrame:
    """미국 S&P 500 전 종목 리스트 조회 (Yahoo Finance 호환 티커 보정)"""
    listing = fdr.StockListing("S&P500")
    sym_col = "Symbol" if "Symbol" in listing.columns else listing.columns[0]
    name_col = "Name" if "Name" in listing.columns else sym_col

    uni = pd.DataFrame({
        "ticker": listing[sym_col].astype(str).str.strip().str.replace(".", "-", regex=False),
        "name": listing[name_col].astype(str),
        "market": "US_SP500",
        "sector": listing["Sector"].astype(str) if "Sector" in listing.columns else "",
    })
    alias_map = {"BRKB": "BRK-B", "BFB": "BF-B"}
    uni["ticker"] = uni["ticker"].replace(alias_map)
    return uni.drop_duplicates(subset=["ticker"]).reset_index(drop=True)


def collect_us_sp500(start_str: str, end_str: str, lake: DataLakeManager, limit: int | None = None, batch_size: int = 100):
    """미국 S&P 500 yfinance 배치 수집 후 Parquet 파티션 저장"""
    uni = _get_us_sp500_universe()
    lake.write_table(uni, "universe", "market=US_SP500/data.parquet")

    tickers = uni["ticker"].tolist()
    if limit:
        tickers = tickers[:limit]

    print(f"[US_SP500] 총 {len(tickers)}종목 배치 수집 시작 ({start_str} ~ {end_str})")
    t0 = time.perf_counter()
    frames = []

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i : i + batch_size]
        raw = yf.download(
            batch,
            start=start_str,
            end=end_str,
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
        if raw is None or raw.empty:
            continue

        if len(batch) == 1:
            t = batch[0]
            sub = raw.reset_index()
            date_col = "Date" if "Date" in sub.columns else sub.columns[0]
            out = pd.DataFrame({
                "date": pd.to_datetime(sub[date_col]).dt.strftime("%Y-%m-%d"),
                "ticker": t,
                "open": pd.to_numeric(sub["Open"], errors="coerce"),
                "high": pd.to_numeric(sub["High"], errors="coerce"),
                "low": pd.to_numeric(sub["Low"], errors="coerce"),
                "close": pd.to_numeric(sub["Close"], errors="coerce"),
                "volume": pd.to_numeric(sub["Volume"], errors="coerce").fillna(0).astype("int64"),
            }).dropna(subset=["close"])
            if not out.empty:
                frames.append(out)
        else:
            for t in batch:
                if t not in raw.columns.get_level_values(0):
                    continue
                sub = raw[t].dropna(subset=["Close"]).reset_index()
                if sub.empty:
                    continue
                date_col = "Date" if "Date" in sub.columns else sub.columns[0]
                out = pd.DataFrame({
                    "date": pd.to_datetime(sub[date_col]).dt.strftime("%Y-%m-%d"),
                    "ticker": t,
                    "open": pd.to_numeric(sub["Open"], errors="coerce"),
                    "high": pd.to_numeric(sub["High"], errors="coerce"),
                    "low": pd.to_numeric(sub["Low"], errors="coerce"),
                    "close": pd.to_numeric(sub["Close"], errors="coerce"),
                    "volume": pd.to_numeric(sub["Volume"], errors="coerce").fillna(0).astype("int64"),
                })
                frames.append(out)

        print(f"  - 배치 진행: {min(i + batch_size, len(tickers))}/{len(tickers)}")

    if not frames:
        print("[US_SP500] 수집된 주가 데이터가 없습니다.")
        return

    all_df = pd.concat(frames, ignore_index=True)[STD_COLS]
    lake.write_ohlcv_partition(all_df, market="US_SP500")
    elapsed = time.perf_counter() - t0
    print(f"[US_SP500] 완료: {len(all_df):,}행 저장 (소요 {elapsed:.1f}초)\n")


def print_lake_summary(lake: DataLakeManager):
    """데이터 레이크 전체 시장별 적재 현황 요약"""
    glob_path = (lake.lake_dir / "ohlcv" / "market=*" / "year=*" / "data.parquet").as_posix()
    summary = lake.con.execute(
        f"""
        SELECT market,
               COUNT(DISTINCT ticker) AS 종목수,
               COUNT(*) AS 총행수,
               MIN(date) AS 시작일,
               MAX(date) AS 종료일
        FROM read_parquet('{glob_path}', hive_partitioning=true, union_by_name=true)
        GROUP BY market
        ORDER BY market
        """
    ).df()
    print("================ [v2 데이터 레이크 시장별 적재 현황] ================")
    print(summary.to_string(index=False))
    print(f"• 전체 data_lake/ 용량: {lake.get_lake_size_mb():.2f} MB")


def main():
    parser = argparse.ArgumentParser(description="v2 멀티 마켓(국내 전종목 · 미국 S&P500) 병렬 수집기")
    parser.add_argument(
        "--market",
        choices=["KR_KOSPI", "KR_KOSDAQ", "US_SP500", "ALL"],
        default="ALL",
        help="수집 대상 시장",
    )
    parser.add_argument("--days", type=int, default=365, help="수집 기간(일) — 5년 백필 시 1825 권장")
    parser.add_argument("--limit", type=int, default=None, help="테스트용 종목 수 제한 (예: 20)")
    parser.add_argument("--workers", type=int, default=3, help="국내 시장 병렬 스레드 수 (기본 3, 차단 방지)")
    parser.add_argument("--resume", action="store_true", help="중단된 국내 시장 수집을 체크포인트부터 이어받기")
    args = parser.parse_args()

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=args.days)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")

    lake = DataLakeManager()

    if args.market in ("KR_KOSPI", "ALL"):
        collect_kr_market("KR_KOSPI", start_str, end_str, lake, limit=args.limit, workers=args.workers, resume=args.resume)
    if args.market in ("KR_KOSDAQ", "ALL"):
        collect_kr_market("KR_KOSDAQ", start_str, end_str, lake, limit=args.limit, workers=args.workers, resume=args.resume)
    if args.market in ("US_SP500", "ALL"):
        collect_us_sp500(start_str, end_str, lake, limit=args.limit)

    print_lake_summary(lake)


if __name__ == "__main__":
    main()