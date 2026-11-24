import argparse
import os
import sqlite3
import time
from pathlib import Path
import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "kr_quant.db"
load_dotenv(ROOT_DIR / ".env")

KRX_VKOSPI_URL = "https://data-dbg.krx.co.kr/svc/apis/idx/drvprod_dd_trd"
KRX_OPTION_URL = "https://data-dbg.krx.co.kr/svc/apis/drv/opt_bydd_trd"


def ensure_deriv_columns():
    """market_sentiment 테이블에 VKOSPI 및 옵션 PCR 컬럼 자동 추가"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(market_sentiment)")
    existing_cols = {col[1] for col in cursor.fetchall()}

    new_cols = {
        "vkospi": "REAL",
        "pcr_vol": "REAL",
        "pcr_val": "REAL",
        "pcr_oi": "REAL",
    }
    for col_name, col_type in new_cols.items():
        if col_name not in existing_cols:
            cursor.execute(f"ALTER TABLE market_sentiment ADD COLUMN {col_name} {col_type}")

    conn.commit()
    conn.close()


def _to_float(val) -> float | None:
    if val is None:
        return None
    s = str(val).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _call_krx_api(url: str, bas_dd: str, api_key: str) -> list[dict]:
    headers = {"AUTH_KEY": api_key}
    params = {"basDd": bas_dd}
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("OutBlock_1", [])


def fetch_vkospi_for_date(bas_dd: str, api_key: str) -> float | None:
    """파생상품지수 시세정보에서 '코스피 200 변동성지수(VKOSPI)' 종가 추출"""
    rows = _call_krx_api(KRX_VKOSPI_URL, bas_dd, api_key)
    for row in rows:
        idx_nm = str(row.get("IDX_NM", "")).replace(" ", "")
        if "코스피200변동성지수" in idx_nm or "VKOSPI" in idx_nm.upper():
            return _to_float(row.get("CLSPRC_IDX"))
    return None


def fetch_pcr_for_date(bas_dd: str, api_key: str) -> tuple[float | None, float | None, float | None]:
    """코스피200 옵션 일별매매정보에서 거래량/거래대금/미결제약정 기준 PCR(Put/Call) 산출"""
    rows = _call_krx_api(KRX_OPTION_URL, bas_dd, api_key)
    if not rows:
        return None, None, None

    totals = {
        "CALL": {"vol": 0.0, "val": 0.0, "oi": 0.0},
        "PUT": {"vol": 0.0, "val": 0.0, "oi": 0.0},
    }

    for row in rows:
        prod_nm = str(row.get("PROD_NM", "")).replace(" ", "")
        # 표준 코스피200 옵션만 집계 (미니·위클리·코스닥150 등 제외)
        if prod_nm != "코스피200옵션":
            continue

        rght = str(row.get("RGHT_TP_NM", "")).strip().upper()
        if rght not in ("CALL", "PUT"):
            continue

        totals[rght]["vol"] += _to_float(row.get("ACC_TRDVOL")) or 0.0
        totals[rght]["val"] += _to_float(row.get("ACC_TRDVAL")) or 0.0
        totals[rght]["oi"] += _to_float(row.get("ACC_OPNINT_QTY")) or 0.0

    pcr_vol = round(totals["PUT"]["vol"] / totals["CALL"]["vol"], 4) if totals["CALL"]["vol"] > 0 else None
    pcr_val = round(totals["PUT"]["val"] / totals["CALL"]["val"], 4) if totals["CALL"]["val"] > 0 else None
    pcr_oi = round(totals["PUT"]["oi"] / totals["CALL"]["oi"], 4) if totals["CALL"]["oi"] > 0 else None
    return pcr_vol, pcr_val, pcr_oi


def get_missing_dates(limit: int | None = None) -> list[str]:
    """market_sentiment에서 vkospi 또는 pcr_vol이 비어 있는 최신 거래일 목록 조회"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    query = """
        SELECT date FROM market_sentiment
        WHERE vkospi IS NULL OR pcr_vol IS NULL
        ORDER BY date DESC
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    cursor.execute(query)
    dates = [r[0] for r in cursor.fetchall()]
    conn.close()
    return sorted(dates)


def save_deriv_row(date_str: str, vkospi, pcr_vol, pcr_val, pcr_oi):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO market_sentiment (date, vkospi, pcr_vol, pcr_val, pcr_oi)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            vkospi = COALESCE(excluded.vkospi, market_sentiment.vkospi),
            pcr_vol = COALESCE(excluded.pcr_vol, market_sentiment.pcr_vol),
            pcr_val = COALESCE(excluded.pcr_val, market_sentiment.pcr_val),
            pcr_oi = COALESCE(excluded.pcr_oi, market_sentiment.pcr_oi)
        """,
        (date_str, vkospi, pcr_vol, pcr_val, pcr_oi),
    )
    conn.commit()
    conn.close()


def collect_krx_derivatives(limit: int | None = None, sleep_sec: float = 0.12):
    api_key = os.getenv("KRX_API_KEY")
    if not api_key:
        raise ValueError(".env 파일에 KRX_API_KEY가 설정되어 있지 않습니다.")

    ensure_deriv_columns()
    target_dates = get_missing_dates(limit=limit)

    if not target_dates:
        print("[안내] 수집할 누락 거래일이 없습니다. (모든 날짜에 VKOSPI & PCR 적재 완료)")
        return

    total = len(target_dates)
    print(f"=== [KRX 파생 심리지표] 총 {total}거래일 수집 시작 ({target_dates[0]} ~ {target_dates[-1]}) ===")

    success = 0
    for idx, date_str in enumerate(target_dates, start=1):
        bas_dd = date_str.replace("-", "")
        try:
            vkospi = fetch_vkospi_for_date(bas_dd, api_key)
            time.sleep(sleep_sec)
            pcr_vol, pcr_val, pcr_oi = fetch_pcr_for_date(bas_dd, api_key)
            time.sleep(sleep_sec)

            if vkospi is not None or pcr_vol is not None:
                save_deriv_row(date_str, vkospi, pcr_vol, pcr_val, pcr_oi)
                success += 1
                if idx <= 5 or idx % 20 == 0 or idx == total:
                    print(
                        f"[{idx}/{total}] {date_str} | VKOSPI: {vkospi} | "
                        f"PCR(거래량): {pcr_vol} | PCR(대금): {pcr_val} | PCR(OI): {pcr_oi}"
                    )
            else:
                print(f"[{idx}/{total}] [스킵] {date_str}: API 응답 데이터 없음")
        except Exception as e:
            print(f"[{idx}/{total}] [에러] {date_str} 수집 실패: {e}")
            time.sleep(1.0)

    print(f"=== [KRX 파생 심리지표] 수집 완료: {success}/{total}거래일 적재 성공 ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KRX Open API 기반 VKOSPI & 코스피200 옵션 PCR 수집")
    parser.add_argument("--limit", type=int, default=None, help="최근 누락 거래일 중 최대 수집 개수 (테스트 시 5 권장)")
    args = parser.parse_args()
    collect_krx_derivatives(limit=args.limit)