import sqlite3

DB_PATH = "kr_quant.db"

def init_db():
    """SQLite DB 및 테이블 생성"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 1. 일별 주가(OHLCV) 테이블
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_ohlcv (
            date TEXT,
            ticker TEXT,
            open INTEGER,
            high INTEGER,
            low INTEGER,
            close INTEGER,
            volume INTEGER,
            PRIMARY KEY (date, ticker)
        )
    """)

    # 2. 공시(Disclosures) 테이블
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS disclosures (
            rcept_no TEXT PRIMARY KEY,
            rcept_dt TEXT,
            ticker TEXT,
            corp_name TEXT,
            report_nm TEXT,
            flr_nm TEXT,
            keyword TEXT,
            dart_url TEXT
        )
    """)

    # 기존 disclosures 테이블에 dart_url 컬럼이 없으면 자동 추가
    cursor.execute("PRAGMA table_info(disclosures)")
    existing_cols = [col[1] for col in cursor.fetchall()]
    if "dart_url" not in existing_cols:
        cursor.execute("ALTER TABLE disclosures ADD COLUMN dart_url TEXT")

    # 3. 자기주식 취득 결정 상세(Buyback Details) 테이블
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS buyback_details (
            rcept_no TEXT PRIMARY KEY,
            ticker TEXT,
            corp_name TEXT,
            bddd TEXT,
            aqpln_stk_ostk INTEGER,
            aqpln_prc_ostk INTEGER,
            aqexpd_bgd TEXT,
            aqexpd_edd TEXT,
            aq_pp TEXT,
            aq_mth TEXT
        )
    """)

    conn.commit()
    conn.close()

def save_ohlcv(records):
    """주가 데이터 Upsert 저장"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.executemany("""
        INSERT OR REPLACE INTO daily_ohlcv (date, ticker, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, records)
    conn.commit()
    conn.close()

def save_disclosures(records):
    """공시 데이터 Upsert 저장"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.executemany("""
        INSERT OR REPLACE INTO disclosures
        (rcept_no, rcept_dt, ticker, corp_name, report_nm, flr_nm, keyword, dart_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, records)
    conn.commit()
    conn.close()

def save_buyback_details(records):
    """자기주식 취득 결정 상세 수치 Upsert 저장"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.executemany("""
        INSERT OR REPLACE INTO buyback_details
        (rcept_no, ticker, corp_name, bddd, aqpln_stk_ostk, aqpln_prc_ostk,
         aqexpd_bgd, aqexpd_edd, aq_pp, aq_mth)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, records)
    conn.commit()
    conn.close()

def get_saved_tickers():
    """DB(daily_ohlcv)에 저장된 종목 코드 리스트 반환"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT ticker FROM daily_ohlcv")
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]