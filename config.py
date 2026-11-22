from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "kr_quant.db"

DEFAULT_TICKERS = ["005930", "000660", "069500"]  # 삼성전자, SK하이닉스, KODEX 200