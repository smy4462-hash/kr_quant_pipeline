import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db.lake_manager import DataLakeManager

load_dotenv(ROOT_DIR / ".env")

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_BUYBACK_URL = "https://opendart.fss.or.kr/api/tsstkAqDecsn.json"

TARGET_KEYWORDS = [
    "자기주식", "소각", "배당",
    "단일판매", "공급계약",
    "유상증자", "전환사채", "신주인수권부사채",
    "영업(잠정)실적", "매출액또는손익구조",
]

CORP_CLS_MAP = {
    "Y": "KR_KOSPI",
    "K": "KR_KOSDAQ",
}


def _match_keyword(report_nm: str) -> str | None:
    text = str(report_nm)
    for kw in TARGET_KEYWORDS:
        if kw in text:
            return kw
    return None


def _parse_int(val) -> int:
    if not val or str(val).strip() in ("-", ""):
        return 0
    return int(str(val).replace(",", "").strip())


def _split_date_chunks(start_date: str, end_date: str, chunk_days: int = 89) -> list[tuple[str, str]]:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    chunks = []
    curr = start_dt
    while curr <= end_dt:
        chunk_end = min(curr + timedelta(days=chunk_days), end_dt)
        chunks.append((curr.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")))
        curr = chunk_end + timedelta(days=1)
    return chunks


def _request_json_with_retry(
    session: requests.Session,
    url: str,
    params: dict,
    max_retries: int = 5,
    timeout: int = 30,
) -> dict:
    """OpenDART 응답 지연 및 일시 네트워크 오류 대비 자동 재시도 요청"""
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            if attempt == max_retries:
                raise
            wait_sec = min(2 * attempt, 10)
            print(f"    [재시도 {attempt}/{max_retries}] 응답 지연/오류 ({type(e).__name__}) -> {wait_sec}초 후 재요청", flush=True)
            time.sleep(wait_sec)
    return {}


def fetch_chunk_disclosures(
    session: requests.Session,
    api_key: str,
    corp_cls: str,
    market_name: str,
    bgn_de: str,
    end_de: str,
) -> tuple[pd.DataFrame, dict[str, str]]:
    """단일 89일 구간 공시 목록 수집"""
    rows = []
    page_no = 1

    while True:
        params = {
            "crtfc_key": api_key,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "corp_cls": corp_cls,
            "page_no": page_no,
            "page_count": 100,
        }
        data = _request_json_with_retry(session, DART_LIST_URL, params=params)
        status = data.get("status")

        if status == "013":  # 조회된 데이터 없음
            break
        if status == "020":
            raise RuntimeError(
                "OpenDART 일일 요청 한도(020)를 초과했습니다. "
                "지금까지 완료된 구간은 저장되었으니 내일 --resume 옵션으로 이어받으세요."
            )
        if status != "000":
            raise RuntimeError(f"OpenDART API 오류 ({status}): {data.get('message')}")

        for row in data.get("list", []):
            row["market"] = market_name
            rows.append(row)

        total_page = int(data.get("total_page", 1))
        if page_no >= total_page:
            break
        page_no += 1
        time.sleep(0.1)

    if not rows:
        return pd.DataFrame(), {}

    df = pd.DataFrame(rows).drop_duplicates(subset=["rcept_no"])
    df["stock_code"] = df["stock_code"].fillna("").astype(str).str.strip()
    df = df[df["stock_code"].str.len() == 6].copy()
    if df.empty:
        return pd.DataFrame(), {}

    df["keyword"] = df["report_nm"].apply(_match_keyword)
    df = df[df["keyword"].notnull()].copy()
    if df.empty:
        return pd.DataFrame(), {}

    df["rcept_dt"] = (
        df["rcept_dt"].str[:4] + "-" + df["rcept_dt"].str[4:6] + "-" + df["rcept_dt"].str[6:8]
    )
    df["dart_url"] = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + df["rcept_no"]

    buyback_df = df[df["report_nm"].str.contains("자기주식취득", na=False)]
    buyback_targets = dict(zip(buyback_df["corp_code"], buyback_df["stock_code"]))

    out = df[[
        "rcept_no", "rcept_dt", "stock_code", "corp_name",
        "report_nm", "flr_nm", "keyword", "dart_url", "market",
    ]].rename(columns={"stock_code": "ticker"}).reset_index(drop=True)

    return out, buyback_targets


def fetch_chunk_buyback_details(
    session: requests.Session,
    api_key: str,
    corp_ticker_map: dict[str, str],
    bgn_de: str,
    end_de: str,
) -> pd.DataFrame:
    """해당 구간 내 자기주식 취득 결정 정형 API 상세 수치 수집"""
    if not corp_ticker_map:
        return pd.DataFrame()

    rows = []
    for corp_code, ticker in corp_ticker_map.items():
        params = {
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": end_de,
        }
        try:
            data = _request_json_with_retry(session, DART_BUYBACK_URL, params=params)
            if data.get("status") == "020":
                raise RuntimeError(
                    "OpenDART 일일 요청 한도(020)를 초과했습니다. "
                    "내일 --resume 옵션으로 이어받으세요."
                )
            if data.get("status") == "000":
                for item in data.get("list", []):
                    rows.append({
                        "rcept_no": item.get("rcept_no"),
                        "ticker": ticker,
                        "corp_name": item.get("corp_name"),
                        "bddd": item.get("bddd"),
                        "aqpln_stk_ostk": _parse_int(item.get("aqpln_stk_ostk")),
                        "aqpln_prc_ostk": _parse_int(item.get("aqpln_prc_ostk")),
                        "aqexpd_bgd": item.get("aqexpd_bgd"),
                        "aqexpd_edd": item.get("aqexpd_edd"),
                        "aq_pp": item.get("aq_pp"),
                        "aq_mth": item.get("aq_mth"),
                    })
        except RuntimeError:
            raise
        except Exception as e:
            print(f"    [경고] 자사주 상세 조회 실패 ({ticker}): {e}", flush=True)

        time.sleep(0.08)

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop_duplicates(subset=["rcept_no"]).reset_index(drop=True)


def upsert_lake_table(lake: DataLakeManager, new_df: pd.DataFrame, table_name: str, sub_path: str, pk: str = "rcept_no") -> int:
    """기존 Parquet 테이블과 병합(Upsert) 후 ZSTD 압축 저장"""
    out_file = lake.lake_dir / table_name / sub_path
    if new_df.empty:
        if out_file.exists():
            return int(lake.con.execute(f"SELECT COUNT(*) FROM read_parquet('{out_file.as_posix()}')").fetchone()[0])
        return 0

    if out_file.exists():
        existing = lake.con.execute(f"SELECT * FROM read_parquet('{out_file.as_posix()}')").df()
        merged = pd.concat([existing, new_df], ignore_index=True)
        merged = merged.drop_duplicates(subset=[pk], keep="last").reset_index(drop=True)
    else:
        merged = new_df.copy()

    if table_name == "disclosures" and "market" in merged.columns:
        merged["market"] = merged["market"].fillna("KR_KOSPI")

    lake.write_table(merged, table_name, sub_path)
    return len(merged)


def main():
    parser = argparse.ArgumentParser(description="v2 국내 전 종목(KOSPI+KOSDAQ) OpenDART 공시 수집기 (재시도·체크포인트 지원)")
    parser.add_argument("--days", type=int, default=14, help="수집 기간(일) — 일간 5일, 테스트 14일, 5년 백필 1825일")
    parser.add_argument("--resume", action="store_true", help="중단된 백필을 완료된 구간 다음부터 이어받기")
    args = parser.parse_args()

    api_key = os.getenv("DART_API_KEY", "").strip()
    if not api_key:
        raise ValueError(".env 파일에 DART_API_KEY가 설정되어 있지 않습니다.")

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=args.days)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")

    lake = DataLakeManager()
    ckpt_file = lake.lake_dir / ".done_dart_chunks.txt"
    done_keys: set[str] = set()

    if args.resume and ckpt_file.exists():
        done_keys = set(ckpt_file.read_text(encoding="utf-8").splitlines())
        print(f"[이어받기 활성화] 기존 완료 구간 {len(done_keys)}개 스킵", flush=True)
    elif not args.resume and ckpt_file.exists():
        ckpt_file.unlink()

    chunks = _split_date_chunks(start_str, end_str)
    session = requests.Session()

    collected_disc = 0
    collected_bb = 0
    total_disc = 0
    total_bb = 0
    t0 = time.perf_counter()

    for corp_cls, market_name in CORP_CLS_MAP.items():
        print(f"\n[{market_name}] OpenDART 공시 수집 시작 ({start_str} ~ {end_str}, {len(chunks)}개 구간)", flush=True)
        for idx, (bgn_de, end_de) in enumerate(chunks, start=1):
            chunk_key = f"{market_name}|{bgn_de}|{end_de}"
            if chunk_key in done_keys:
                continue

            disc_df, bb_targets = fetch_chunk_disclosures(session, api_key, corp_cls, market_name, bgn_de, end_de)
            bb_df = fetch_chunk_buyback_details(session, api_key, bb_targets, bgn_de, end_de)

            total_disc = upsert_lake_table(lake, disc_df, "disclosures", "market=KR/data.parquet")
            total_bb = upsert_lake_table(lake, bb_df, "buyback_details", "market=KR/data.parquet")
            collected_disc += len(disc_df)
            collected_bb += len(bb_df)

            with open(ckpt_file, "a", encoding="utf-8") as f:
                f.write(chunk_key + "\n")

            elapsed = time.perf_counter() - t0
            print(
                f"  - [{market_name}] 구간 {idx}/{len(chunks)} 저장 완료 ({bgn_de}~{end_de}) "
                f"| 공시 +{len(disc_df)}건, 자사주 +{len(bb_df)}건 (경과 {elapsed:.0f}초)",
                flush=True,
            )

    print("\n================ [v2 OpenDART 전 종목 적재 완료] ================")
    print(f"• 이번 수집 공시 건수 : {collected_disc:,}건 (누적 {total_disc:,}건)")
    print(f"• 이번 수집 자사주상세: {collected_bb:,}건 (누적 {total_bb:,}건)")
    print(f"• 전체 data_lake/ 용량: {lake.get_lake_size_mb():.2f} MB")


if __name__ == "__main__":
    main()