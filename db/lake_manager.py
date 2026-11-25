import sqlite3
import time
from pathlib import Path
import duckdb
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
SQLITE_PATH = ROOT_DIR / "kr_quant.db"
LAKE_DIR = ROOT_DIR / "data_lake"


class DataLakeManager:
    """DuckDB + Hive Partitioned Parquet 기반 v2 대용량 퀀트 데이터 레이크 매니저"""

    def __init__(self, lake_dir: Path = LAKE_DIR):
        self.lake_dir = lake_dir
        self.lake_dir.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect()

    def write_ohlcv_partition(self, df: pd.DataFrame, market: str = "KR_KOSPI200"):
        """주가 데이터를 market / year 단위로 ZSTD 압축 Parquet 파티션 저장 (기존 파티션과 Upsert 병합)"""
        if df.empty:
            return

        work = df.copy()
        work["date"] = pd.to_datetime(work["date"]).dt.strftime("%Y-%m-%d")
        work["year"] = work["date"].str.slice(0, 4)

        for year, grp in work.groupby("year"):
            part_dir = self.lake_dir / "ohlcv" / f"market={market}" / f"year={year}"
            part_dir.mkdir(parents=True, exist_ok=True)
            out_file = part_dir / "data.parquet"

            save_df = grp.drop(columns=["year"]).copy()

            # 기존 파티션 파일이 있으면 중복 제거 후 최신 값으로 Upsert 병합
            if out_file.exists():
                existing = self.con.execute(f"SELECT * FROM read_parquet('{out_file.as_posix()}')").df()
                save_df = pd.concat([existing, save_df], ignore_index=True)
                save_df = save_df.drop_duplicates(subset=["date", "ticker"], keep="last")

            save_df = save_df.sort_values(["ticker", "date"]).reset_index(drop=True)
            self.con.register("tmp_ohlcv", save_df)
            self.con.execute(
                f"COPY tmp_ohlcv TO '{out_file.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            self.con.unregister("tmp_ohlcv")

    def write_table(self, df: pd.DataFrame, table_name: str, sub_path: str = "market=KR/data.parquet"):
        """공시·자사주·심리지표 등 메타/이벤트 테이블 Parquet 저장"""
        if df.empty:
            return
        out_file = self.lake_dir / table_name / sub_path
        out_file.parent.mkdir(parents=True, exist_ok=True)
        self.con.register("tmp_tbl", df)
        self.con.execute(
            f"COPY tmp_tbl TO '{out_file.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        self.con.unregister("tmp_tbl")

    def query_ohlcv(self, market: str | None = None, start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
        """Hive 파티셔닝을 활용한 초고속 주가 조회"""
        glob_path = (self.lake_dir / "ohlcv" / "market=*" / "year=*" / "data.parquet").as_posix()
        clauses = []
        if market:
            clauses.append(f"market = '{market}'")
        if start_date:
            clauses.append(f"date >= '{start_date}'")
        if end_date:
            clauses.append(f"date <= '{end_date}'")

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT *
            FROM read_parquet('{glob_path}', hive_partitioning=true)
            {where_sql}
            ORDER BY market, ticker, date
        """
        return self.con.execute(sql).df()

    def get_lake_size_mb(self) -> float:
        total_bytes = sum(f.stat().st_size for f in self.lake_dir.rglob("*.parquet"))
        return total_bytes / (1024 * 1024)


def migrate_sqlite_to_lake():
    """v1 SQLite(kr_quant.db) 전체 테이블을 v2 Parquet 데이터 레이크로 이관 및 벤치마크"""
    if not SQLITE_PATH.exists():
        raise FileNotFoundError(f"SQLite DB 파일이 없습니다: {SQLITE_PATH}")

    print(f"[1/3] v1 SQLite({SQLITE_PATH.name})에서 데이터 읽는 중...")
    sq_conn = sqlite3.connect(SQLITE_PATH)
    ohlcv = pd.read_sql("SELECT * FROM daily_ohlcv", sq_conn)
    disc = pd.read_sql("SELECT * FROM disclosures", sq_conn)
    buyback = pd.read_sql("SELECT * FROM buyback_details", sq_conn)
    sent = pd.read_sql("SELECT * FROM market_sentiment", sq_conn)
    sq_conn.close()

    print(f"[2/3] v2 Parquet 데이터 레이크({LAKE_DIR.name}/)로 파티션 압축 저장 중...")
    lake = DataLakeManager()
    lake.write_ohlcv_partition(ohlcv, market="KR_KOSPI200")
    lake.write_table(disc, "disclosures", "market=KR/data.parquet")
    lake.write_table(buyback, "buyback_details", "market=KR/data.parquet")
    lake.write_table(sent, "market_sentiment", "global_sentiment.parquet")

    # 용량 비교
    sqlite_mb = SQLITE_PATH.stat().st_size / (1024 * 1024)
    lake_mb = lake.get_lake_size_mb()
    reduction = (1 - lake_mb / sqlite_mb) * 100 if sqlite_mb > 0 else 0

    print("\n================ [저장소 용량 압축 비교] ================")
    print(f"• v1 SQLite (kr_quant.db)   : {sqlite_mb:6.2f} MB")
    print(f"• v2 Parquet (data_lake/)   : {lake_mb:6.2f} MB  (용량 {reduction:.1f}% 절감)")

    # 조회 및 집계 속도 벤치마크 (종목별 연평균 거래대금·종가 집계)
    print("\n================ [집계 속도 벤치마크] ================")
    t0 = time.perf_counter()
    sq_conn = sqlite3.connect(SQLITE_PATH)
    _ = pd.read_sql(
        """
        SELECT ticker, SUBSTR(date, 1, 4) AS yr, COUNT(*) AS days, AVG(close * volume) AS avg_val
        FROM daily_ohlcv
        WHERE volume > 0
        GROUP BY ticker, yr
        """,
        sq_conn,
    )
    sq_conn.close()
    t_sqlite = time.perf_counter() - t0

    t1 = time.perf_counter()
    glob_path = (LAKE_DIR / "ohlcv" / "market=*" / "year=*" / "data.parquet").as_posix()
    res_duck = lake.con.execute(
        f"""
        SELECT market, ticker, year, COUNT(*) AS days, AVG(close * volume) AS avg_val
        FROM read_parquet('{glob_path}', hive_partitioning=true)
        WHERE volume > 0
        GROUP BY market, ticker, year
        """
    ).df()
    t_duck = time.perf_counter() - t1

    speedup = t_sqlite / t_duck if t_duck > 0 else 0
    print(f"• v1 SQLite 집계 소요 시간  : {t_sqlite:.4f}초")
    print(f"• v2 DuckDB 집계 소요 시간  : {t_duck:.4f}초  (약 {speedup:.1f}배 빠름, 결과 {len(res_duck):,}행)")
    print("[3/3] v2 데이터 레이크 초기 구축 완료!")


if __name__ == "__main__":
    migrate_sqlite_to_lake()