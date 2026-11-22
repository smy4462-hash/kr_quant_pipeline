import sqlite3
from config import DB_PATH

def get_connection():
    return sqlite3.connect(DB_PATH)

def init_db():
    with get_connection() as conn:
        conn.execute("""
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

def save_ohlcv(records):
    with get_connection() as conn:
        conn.executemany("""
            INSERT OR REPLACE INTO daily_ohlcv (date, ticker, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, records)