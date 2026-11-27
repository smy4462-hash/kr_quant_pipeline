import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
import duckdb
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from notifiers.telegram_bot import send_document, send_message

LAKE_DIR = ROOT_DIR / "data_lake"
OUTPUT_DIR = ROOT_DIR / "outputs"
MIN_PERIODS = 60


def classify_event_type(row) -> str:
    title = str(row["report_nm"])
    kw = str(row["keyword"])
    if "소각" in title:
        return "소각"
    if kw == "자기주식" or "자기주식" in title:
        if "처분" in title or "해지" in title:
            return "자사주_처분/해지"
        if "취득" in title or "신탁" in title:
            return "자사주_취득/신탁"
        return "자사주_기타"
    return kw


def format_event_name(etype: str) -> str:
    mapping = {
        "자사주_취득/신탁": "자사주 취득 및 신탁",
        "자사주_처분/해지": "자사주 처분 및 해지",
        "영업(잠정)실적": "영업잠정실적",
        "매출액또는손익구조": "매출액 또는 손익구조",
        "단일판매": "단일판매 공급계약",
        "소각": "자사주 소각",
    }
    return mapping.get(etype, etype.replace("_", " "))


def get_current_regimes(con: duckdb.DuckDBPyConnection) -> dict:
    sent_path = (LAKE_DIR / "market_sentiment" / "global_sentiment.parquet").as_posix()
    sent = con.execute(f"SELECT date, vix, vkospi, pcr_val FROM read_parquet('{sent_path}') ORDER BY date").df()
    regimes = {}

    for col, label in [("vix", "미국 VIX"), ("vkospi", "한국 VKOSPI"), ("pcr_val", "옵션 풋콜비율")]:
        sub = sent[["date", col]].dropna().reset_index(drop=True)
        if len(sub) < MIN_PERIODS:
            regimes[col] = {"label": label, "is_fear": False, "val": None, "cutoff": None, "date": "확인불가"}
            continue
        sub["cutoff"] = sub[col].expanding(min_periods=MIN_PERIODS).quantile(0.75)
        latest = sub.iloc[-1]
        regimes[col] = {
            "label": label,
            "is_fear": bool(latest[col] >= latest["cutoff"]),
            "val": round(float(latest[col]), 2),
            "cutoff": round(float(latest["cutoff"]), 2),
            "date": str(latest["date"]),
        }
    return regimes


def get_recent_avg_val_kr(con: duckdb.DuckDBPyConnection) -> dict[str, float]:
    """DuckDB로 국내 전 종목(KR_KOSPI + KR_KOSDAQ) 최근 5거래일 평균 거래대금 산출"""
    ohlcv_glob = (LAKE_DIR / "ohlcv" / "market=*" / "year=*" / "data.parquet").as_posix()
    df = con.execute(
        f"""
        WITH ranked AS (
            SELECT ticker, close, volume,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
            FROM read_parquet('{ohlcv_glob}', hive_partitioning=true, union_by_name=true)
            WHERE market IN ('KR_KOSPI', 'KR_KOSDAQ')
              AND volume > 0
        )
        SELECT ticker, AVG(CAST(close AS DOUBLE) * CAST(volume AS DOUBLE)) AS avg_val
        FROM ranked
        WHERE rn <= 5
        GROUP BY ticker
        """
    ).df()
    if df.empty:
        return {}
    return dict(zip(df["ticker"], df["avg_val"]))


def build_v2_daily_alert(days: int = 1, alert_min_val: float = 5_000_000_000, csv_min_val: float = 1_000_000_000) -> tuple[str, bool, Path | None]:
    con = duckdb.connect()
    regimes = get_current_regimes(con)
    avg_val_map = get_recent_avg_val_kr(con)

    disc_path = (LAKE_DIR / "disclosures" / "market=KR" / "data.parquet").as_posix()
    bb_path = (LAKE_DIR / "buyback_details" / "market=KR" / "data.parquet").as_posix()

    latest_dt_str = con.execute(f"SELECT MAX(rcept_dt) FROM read_parquet('{disc_path}')").fetchone()[0]
    if not latest_dt_str:
        latest_dt_str = datetime.now().strftime("%Y-%m-%d")
    cutoff_dt = (datetime.strptime(latest_dt_str, "%Y-%m-%d") - timedelta(days=max(days - 1, 0))).strftime("%Y-%m-%d")

    disc = con.execute(
        f"""
        SELECT d.rcept_no, d.rcept_dt, d.ticker, d.corp_name, d.report_nm, d.keyword, d.dart_url,
               COALESCE(d.market, 'KR_KOSPI') AS market,
               b.aqpln_prc_ostk
        FROM read_parquet('{disc_path}') d
        LEFT JOIN read_parquet('{bb_path}') b ON d.rcept_no = b.rcept_no
        WHERE d.rcept_dt >= '{cutoff_dt}'
        ORDER BY d.rcept_dt DESC, d.ticker
        """
    ).df()
    con.close()

    vix_fear = regimes["vix"]["is_fear"]
    vix_state = "공포" if vix_fear else "평온"

    lines = [
        f"[KR Quant 전 종목 일간 시그널] {latest_dt_str}",
        "",
        "■ 현재 시장 국면 진단",
        f"메인 국면 필터인 미국 VIX는 현재 {vix_state} 국면입니다.",
    ]
    for col in ("vix", "vkospi", "pcr_val"):
        r = regimes[col]
        badge = "🔴 공포" if r["is_fear"] else "🟢 평온"
        role = "메인" if col == "vix" else "보조"
        val_str = f"현재 {r['val']}, 기준 {r['cutoff']}" if r["val"] is not None else "데이터 없음"
        lines.append(f"• {r['label']} [{role}]: {badge} | {val_str}")

    if disc.empty:
        lines.extend(["", "조회 기간 내 신규 핵심 공시가 없습니다."])
        return "\n".join(lines), False, None

    disc["event_type"] = disc.apply(classify_event_type, axis=1)
    disc = disc.drop_duplicates(subset=["rcept_dt", "ticker", "event_type"]).copy()
    disc["avg_val"] = disc["ticker"].map(avg_val_map).fillna(0.0)
    disc = disc[disc["avg_val"] >= csv_min_val].copy()

    if disc.empty:
        lines.extend(["", "유동성 기준을 통과한 주요 시그널 공시가 없습니다."])
        return "\n".join(lines), False, None

    # 거래대금 내림차순 정렬 (대형주·고유동성 우선)
    disc = disc.sort_values(["avg_val", "rcept_dt"], ascending=[False, False]).reset_index(drop=True)

    long_alerts = []
    strong_avoid_alerts = []
    watch_buybacks = []
    general_avoid = []
    csv_records = []

    for _, row in disc.iterrows():
        avg_val = float(row["avg_val"])
        avg_val_eok = round(avg_val / 100_000_000, 1)
        etype = row["event_type"]
        clean_ev = format_event_name(etype)
        mkt_label = "코스피" if row["market"] == "KR_KOSPI" else "코스닥"

        intensity = None
        if pd.notna(row["aqpln_prc_ostk"]) and avg_val > 0:
            intensity = round(float(row["aqpln_prc_ostk"]) / avg_val, 1)

        # 시그널 분류
        if etype == "자사주_취득/신탁" and vix_fear and intensity is not None and intensity >= 3.0:
            category = "핵심 기회형 매수"
        elif etype == "자사주_처분/해지" and vix_fear:
            category = "강력 회피 경보"
        elif etype in ("자사주_취득/신탁", "자사주_처분/해지", "소각"):
            category = "자사주 및 소각 모니터링"
        elif etype in ("영업(잠정)실적", "배당", "매출액또는손익구조"):
            category = "일반 회피 필터"
        else:
            continue

        csv_records.append({
            "시그널구분": category,
            "시장": mkt_label,
            "공시일": row["rcept_dt"],
            "종목명": row["corp_name"],
            "종목코드": row["ticker"],
            "공시유형": clean_ev,
            "일평균거래대금_억원": avg_val_eok,
            "매입강도_배": intensity if intensity is not None else "",
            "원문링크": row["dart_url"] if pd.notna(row["dart_url"]) else "",
        })

        # 텔레그램 본문 노출은 알림 전용 유동성 기준(기본 50억 원) 적용
        if avg_val < alert_min_val:
            continue

        detail_parts = [mkt_label, f"공시일 {row['rcept_dt']}", f"거래대금 {avg_val_eok:.0f}억"]
        if intensity is not None:
            detail_parts.append(f"매입강도 {intensity}배")
        detail_line = "  " + " | ".join(detail_parts)
        url_line = f"  원문: {row['dart_url']}" if pd.notna(row["dart_url"]) and row["dart_url"] else ""

        def _block(headline: str) -> str:
            parts = [headline, detail_line]
            if url_line:
                parts.append(url_line)
            return "\n".join(parts)

        if category == "핵심 기회형 매수":
            long_alerts.append(_block(f"• {row['corp_name']} {row['ticker']} | 미국 VIX 공포 국면 고강도 자사주"))
        elif category == "강력 회피 경보":
            strong_avoid_alerts.append(_block(f"• {row['corp_name']} {row['ticker']} | 미국 VIX 공포 국면 자사주 처분"))
        elif category == "자사주 및 소각 모니터링":
            watch_buybacks.append(_block(f"• {row['corp_name']} {row['ticker']} | {clean_ev}"))
        elif category == "일반 회피 필터":
            general_avoid.append(
                f"• {row['corp_name']} {row['ticker']} | {clean_ev}\n"
                f"  {mkt_label} | 공시일 {row['rcept_dt']} | 거래대금 {avg_val_eok:.0f}억"
            )

    # 전체 시그널 CSV 저장 (10억 원 이상 전 종목)
    csv_path = None
    if csv_records:
        OUTPUT_DIR.mkdir(exist_ok=True)
        dt_tag = latest_dt_str.replace("-", "")
        csv_path = OUTPUT_DIR / f"daily_signals_{dt_tag}.csv"
        pd.DataFrame(csv_records).to_csv(csv_path, index=False, encoding="utf-8-sig")

    has_actionable = bool(long_alerts or strong_avoid_alerts)

    if long_alerts:
        lines.extend([
            "",
            f"■ 핵심 기회형 매수 경보 | {len(long_alerts)}건",
            "익일 시가 진입 후 20영업일 보유 전략을 검토하시기 바랍니다.",
            "",
        ])
        lines.extend(long_alerts)

    if strong_avoid_alerts:
        lines.extend([
            "",
            f"■ 강력 회피 경보 | {len(strong_avoid_alerts)}건",
            "공포 국면 악재 공시로 5영업일간 신규 매수 보류를 권고합니다.",
            "",
        ])
        lines.extend(strong_avoid_alerts)

    if watch_buybacks:
        lines.extend([
            "",
            f"■ 자사주 및 소각 모니터링 | 거래대금 상위 5건, 총 {len(watch_buybacks)}건",
            "참고용 모니터링 대상입니다.",
            "",
        ])
        lines.extend(watch_buybacks[:5])
        if len(watch_buybacks) > 5:
            lines.append(f"  외 {len(watch_buybacks) - 5}건은 첨부 파일을 확인하시기 바랍니다.")

    if general_avoid:
        lines.extend([
            "",
            f"■ 일반 회피 필터 대상 | 거래대금 상위 5건, 총 {len(general_avoid)}건",
            "실적, 배당, 손익구조 공시 종목으로 5영업일에서 20영업일간 신규 매수 보류를 권고합니다.",
            "",
        ])
        lines.extend(general_avoid[:5])
        if len(general_avoid) > 5:
            lines.append(f"  외 {len(general_avoid) - 5}건은 첨부 파일을 확인하시기 바랍니다.")

    if not (long_alerts or strong_avoid_alerts or watch_buybacks or general_avoid):
        lines.extend(["", "본문 노출 기준을 통과한 주요 시그널 공시가 없습니다."])

    lines.extend([
        "",
        "────────────────────",
        f"조회 기간: {cutoff_dt} ~ {latest_dt_str}",
        f"대상: 국내 전 종목 | 본문 거래대금 {int(alert_min_val / 1e8)}억 이상, 첨부 CSV {int(csv_min_val / 1e8)}억 이상",
    ])
    return "\n".join(lines), has_actionable, csv_path


def main():
    parser = argparse.ArgumentParser(description="v2 국내 전 종목 일간 시그널 텔레그램 알림")
    parser.add_argument("--days", type=int, default=1, help="스캔할 최근 일수 (기본 1일, 테스트 시 14 권장)")
    parser.add_argument("--min-val", type=float, default=50.0, help="본문 노출 최소 일거래대금(억 원, 기본 50억)")
    parser.add_argument("--dry-run", action="store_true", help="텔레그램 발송 없이 미리보기만 출력")
    parser.add_argument("--only-signals", action="store_true", help="핵심 기회/강력 회피 시그널이 있을 때만 발송")
    args = parser.parse_args()

    msg, has_actionable, csv_path = build_v2_daily_alert(
        days=args.days,
        alert_min_val=args.min_val * 100_000_000,
    )
    print(msg)
    if csv_path:
        print(f"\n[CSV 생성 완료] {csv_path}")

    if args.dry_run:
        print("[Dry-Run] 텔레그램 발송을 건너뛰었습니다.")
        return

    if args.only_signals and not has_actionable:
        print("[스킵] 핵심 경보 시그널이 없어 발송하지 않았습니다.")
        return

    send_message(msg)
    if csv_path and csv_path.exists():
        send_document(csv_path, caption="전 종목 일간 공시 시그널 전체 목록 (일거래대금 10억 원 이상)")


if __name__ == "__main__":
    main()