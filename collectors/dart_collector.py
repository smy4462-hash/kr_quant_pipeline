import os
import time
import requests
import pandas as pd

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_BUYBACK_URL = "https://opendart.fss.or.kr/api/tsstkAqDecsn.json"

TARGET_KEYWORDS = [
    "자기주식", "소각", "배당",
    "단일판매", "공급계약",
    "유상증자", "전환사채", "신주인수권부사채",
    "영업(잠정)실적", "매출액또는손익구조"
]

def _match_keyword(report_nm):
    for kw in TARGET_KEYWORDS:
        if kw in report_nm:
            return kw
    return None

def _parse_int(val):
    """'1,000,000' 같은 문자열을 정수로 변환 (빈 값이나 '-'는 0 처리)"""
    if not val or str(val).strip() in ("-", ""):
        return 0
    return int(str(val).replace(",", "").strip())

def fetch_disclosures(target_tickers, start_date, end_date):
    """
    코스피 공시 일괄 조회 후 필터링.
    반환값: (공시 레코드 리스트, 자사주취득 상세 조회 대상 {corp_code: ticker})
    """
    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        raise ValueError(".env 파일에 DART_API_KEY가 설정되어 있지 않습니다.")

    bgn_de = start_date.replace("-", "")
    end_de = end_date.replace("-", "")

    all_rows = []
    page_no = 1

    while True:
        params = {
            "crtfc_key": api_key,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "corp_cls": "Y",
            "page_no": page_no,
            "page_count": 100,
        }
        resp = requests.get(DART_LIST_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        status = data.get("status")
        if status == "013":
            break
        if status != "000":
            raise RuntimeError(f"OpenDART API 오류 ({status}): {data.get('message')}")

        all_rows.extend(data.get("list", []))

        total_page = int(data.get("total_page", 1))
        if page_no >= total_page:
            break

        page_no += 1
        time.sleep(0.1)

    if not all_rows:
        return [], {}

    df = pd.DataFrame(all_rows)

    # 1. 코스피200 종목만 필터링
    ticker_set = set(target_tickers)
    df = df[df["stock_code"].isin(ticker_set)].copy()
    if df.empty:
        return [], {}

    # 2. 핵심 키워드 필터링 및 태깅
    df["keyword"] = df["report_nm"].apply(_match_keyword)
    df = df[df["keyword"].notnull()].copy()
    if df.empty:
        return [], {}

    # 3. 날짜 형식 통일 및 DART 원문 링크 생성
    df["rcept_dt"] = (
        df["rcept_dt"].str[:4] + "-" + df["rcept_dt"].str[4:6] + "-" + df["rcept_dt"].str[6:8]
    )
    df["dart_url"] = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + df["rcept_no"]

    # 4. 자기주식취득결정 공시가 있는 기업(corp_code)만 추출
    buyback_df = df[df["report_nm"].str.contains("자기주식취득", na=False)]
    buyback_targets = dict(zip(buyback_df["corp_code"], buyback_df["stock_code"]))

    records = df[[
        "rcept_no", "rcept_dt", "stock_code", "corp_name",
        "report_nm", "flr_nm", "keyword", "dart_url"
    ]].values.tolist()

    return records, buyback_targets

def fetch_buyback_details(corp_ticker_map, start_date, end_date):
    """자기주식 취득 결정 정형 API(tsstkAqDecsn.json)로 상세 수치 조회"""
    api_key = os.getenv("DART_API_KEY")
    bgn_de = start_date.replace("-", "")
    end_de = end_date.replace("-", "")

    detail_records = []
    for corp_code, ticker in corp_ticker_map.items():
        params = {
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": end_de,
        }
        try:
            resp = requests.get(DART_BUYBACK_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") != "000":
                continue

            for item in data.get("list", []):
                detail_records.append([
                    item.get("rcept_no"),
                    ticker,
                    item.get("corp_name"),
                    item.get("bddd"),                            # 이사회결의일
                    _parse_int(item.get("aqpln_stk_ostk")),      # 취득예정 보통주식수
                    _parse_int(item.get("aqpln_prc_ostk")),      # 취득예정 보통주금액(원)
                    item.get("aqexpd_bgd"),                      # 취득예상 시작일
                    item.get("aqexpd_edd"),                      # 취득예상 종료일
                    item.get("aq_pp"),                           # 취득목적
                    item.get("aq_mth"),                          # 취득방법
                ])
        except Exception as e:
            print(f"  [경고] 자사주 상세 조회 실패 ({ticker}): {e}")

        time.sleep(0.1)

    return detail_records