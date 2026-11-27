import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv
from huggingface_hub import HfApi

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db.lake_manager import DataLakeManager, LAKE_DIR, SQLITE_PATH

load_dotenv(ROOT_DIR / ".env")

IGNORE_PATTERNS = [
    ".done_*.txt",
    "*.duckdb",
    ".cache/**",
    "**/.cache/**",
]


def _get_hf_config() -> tuple[str, str]:
    token = os.getenv("HF_TOKEN", "").strip()
    repo_id = os.getenv("HF_REPO_ID", "").strip()
    if not token or not repo_id:
        raise ValueError(".env 또는 GitHub Secrets에 HF_TOKEN과 HF_REPO_ID를 설정해 주세요.")
    return token, repo_id


def sync_sentiment_to_lake():
    """kr_quant.db의 최신 market_sentiment 테이블을 data_lake Parquet으로 동기화"""
    if not SQLITE_PATH.exists():
        print("[안내] kr_quant.db가 없어 심리지표 동기화를 건너뜁니다.")
        return

    conn = sqlite3.connect(SQLITE_PATH)
    sent = pd.read_sql("SELECT * FROM market_sentiment ORDER BY date", conn)
    conn.close()

    if sent.empty:
        print("[안내] market_sentiment 데이터가 비어 있습니다.")
        return

    lake = DataLakeManager()
    lake.write_table(sent, "market_sentiment", "global_sentiment.parquet")
    print(f"[심리지표 동기화 완료] {len(sent):,}행 → data_lake/market_sentiment/global_sentiment.parquet")


def pull_lake():
    """Hugging Face Datasets에서 data_lake/ 전체 내려받기"""
    token, repo_id = _get_hf_config()
    LAKE_DIR.mkdir(parents=True, exist_ok=True)
    api = HfApi(token=token)
    print(f"[HF Pull] {repo_id} → {LAKE_DIR} 다운로드 시작...")
    api.snapshot_download(
        repo_id=repo_id,
        repo_type="dataset",
        local_dir=str(LAKE_DIR),
        ignore_patterns=[".gitattributes", "README.md"],
    )
    lake = DataLakeManager()
    print(f"[HF Pull 완료] 현재 data_lake/ 용량: {lake.get_lake_size_mb():.2f} MB")


def push_lake(message: str | None = None):
    """로컬 data_lake/ 변경분만 Hugging Face Datasets로 업로드"""
    token, repo_id = _get_hf_config()
    if not LAKE_DIR.exists():
        raise FileNotFoundError(f"업로드할 폴더가 없습니다: {LAKE_DIR}")

    commit_msg = message or f"chore(lake): sync data_lake ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
    api = HfApi(token=token)
    print(f"[HF Push] {LAKE_DIR} → {repo_id} 업로드 시작...")
    api.upload_folder(
        folder_path=str(LAKE_DIR),
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=commit_msg,
        ignore_patterns=IGNORE_PATTERNS,
    )
    print(f"[HF Push 완료] {commit_msg}")


def main():
    parser = argparse.ArgumentParser(description="v2 data_lake Hugging Face Datasets 동기화 도구")
    parser.add_argument("--pull", action="store_true", help="원격 저장소에서 data_lake 내려받기")
    parser.add_argument("--sync-sentiment", action="store_true", help="SQLite 심리지표를 Parquet으로 반영")
    parser.add_argument("--push", action="store_true", help="data_lake 변경분을 원격 저장소로 업로드")
    parser.add_argument("--message", type=str, default=None, help="업로드 커밋 메시지")
    args = parser.parse_args()

    if args.pull:
        pull_lake()
    if args.sync_sentiment:
        sync_sentiment_to_lake()
    if args.push:
        push_lake(args.message)


if __name__ == "__main__":
    main()