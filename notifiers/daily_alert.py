import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from notifiers.telegram_bot import send_message

DB_PATH = ROOT_DIR / "kr_quant.db"
MIN_PERIODS = 60
MIN_DAILY_VAL = 1_000_000_000  # 10억 원 유동성 필터


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
    }
    return mapping.get(etype, etype.replace("_", " "))


def get_current_regimes(conn: sqlite3.Connection) -> dict:
    """VIX, VKOSPI, 옵션 풋콜비율 최신 확정치 및 Expanding Q75 공포 여부 판정"""
    sent = pd.read_sql("SELECT date, vix, vkospi, pcr_val FROM market_sentiment ORDER BY date", conn)
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


def get_recent_avg_val(conn: sqlite3.Connection) -> dict[str, float]:
    """종목별 최근 5거래일 평균 거래대금 계산"""
    query = """
        SELECT ticker, close, volume
        FROM (
            SELECT ticker, close, volume,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
            FROM daily_ohlcv
            WHERE volume > 0
        )
        WHERE rn <= 5
    """
    df = pd.read_sql(query, conn)
    if df.empty:
        return {}
    df["val"] = df["close"] * df["volume"]
    return df.groupby("ticker")["val"].mean().to_dict()


def build_daily_alert(days: int = 1) -> tuple[str, bool]:
    conn = sqlite3.connect(DB_PATH)
    regimes = get_current_regimes(conn)
    avg_val_map = get_recent_avg_val(conn)

    max_dt_row = conn.execute("SELECT MAX(rcept_dt) FROM disclosures").fetchone()
    latest_dt_str = max_dt_row[0] if max_dt_row and max_dt_row[0] else datetime.now().strftime("%Y-%m-%d")
    cutoff_dt = (datetime.strptime(latest_dt_str, "%Y-%m-%d") - timedelta(days=max(days - 1, 0))).strftime("%Y-%m-%d")

    disc = pd.read_sql(
        """
        SELECT d.rcept_no, d.rcept_dt, d.ticker, d.corp_name, d.report_nm, d.keyword, d.dart_url,
               b.aqpln_prc_ostk
        FROM disclosures d
        LEFT JOIN buyback_details b ON d.rcept_no = b.rcept_no
        WHERE d.rcept_dt >= ?
        ORDER BY d.rcept_dt DESC, d.ticker
        """,
        conn,
        params=(cutoff_dt,),
    )
    conn.close()

    vix_fear = regimes["vix"]["is_fear"]
    pcr_fear = regimes["pcr_val"]["is_fear"]
    vix_state = "공포" if vix_fear else "평온"
    pcr_state = "공포" if pcr_fear else "평온"

    # 1) 헤더 & 현재 시장 국면 진단
    lines = [
        f"[KR Quant 일간 시그널 브리핑] {latest_dt_str}",
        "",
        "■ 현재 시장 국면 진단",
        f"현재 시장은 미국 VIX {vix_state}, 옵션 풋콜비율 {pcr_state} 국면입니다.",
    ]
    for col in ("vix", "vkospi", "pcr_val"):
        r = regimes[col]
        badge = "🔴 공포" if r["is_fear"] else "🟢 평온"
        val_str = f"현재 {r['val']}, 기준 {r['cutoff']}" if r["val"] is not None else "데이터 없음"
        lines.append(f"• {r['label']}: {badge} | {val_str}")

    if disc.empty:
        lines.extend(["", "조회 기간 내 신규 핵심 공시가 없습니다."])
        return "\n".join(lines), False

    disc["event_type"] = disc.apply(classify_event_type, axis=1)
    disc = disc.drop_duplicates(subset=["rcept_dt", "ticker", "event_type"]).copy()

    long_alerts = []
    strong_avoid_alerts = []
    general_avoid = []
    watch_buybacks = []

    for _, row in disc.iterrows():
        ticker = row["ticker"]
        avg_val = avg_val_map.get(ticker, 0.0)
        if avg_val < MIN_DAILY_VAL:
            continue

        etype = row["event_type"]
        clean_ev = format_event_name(etype)
        intensity = None
        if pd.notna(row["aqpln_prc_ostk"]) and avg_val > 0:
            intensity = round(float(row["aqpln_prc_ostk"]) / avg_val, 1)

        detail_parts = [f"공시일 {row['rcept_dt']}"]
        if intensity is not None:
            detail_parts.append(f"매입강도 {intensity}배")
        detail_line = "  " + " | ".join(detail_parts)
        url_line = f"  원문: {row['dart_url']}" if pd.notna(row["dart_url"]) and row["dart_url"] else ""

        def _format_block(headline: str) -> str:
            block = [headline, detail_line]
            if url_line:
                block.append(url_line)
            return "\n".join(block)

        # 실증 검증 시그널 매칭
        if etype == "자사주_취득/신탁":
            if vix_fear and intensity is not None and intensity >= 3.0:
                long_alerts.append(_format_block(f"• {row['corp_name']} {ticker} | 미국 VIX 공포 국면 고강도 자사주"))
            elif pcr_fear:
                long_alerts.append(_format_block(f"• {row['corp_name']} {ticker} | 옵션 풋콜비율 공포 국면 자사주 취득"))
            else:
                watch_buybacks.append(_format_block(f"• {row['corp_name']} {ticker} | {clean_ev}"))
        elif etype == "자사주_처분/해지":
            if vix_fear:
                strong_avoid_alerts.append(_format_block(f"• {row['corp_name']} {ticker} | 미국 VIX 공포 국면 자사주 처분"))
            else:
                watch_buybacks.append(_format_block(f"• {row['corp_name']} {ticker} | {clean_ev}"))
        elif etype == "소각":
            watch_buybacks.append(_format_block(f"• {row['corp_name']} {ticker} | 자사주 소각"))
        elif etype in ("영업(잠정)실적", "배당", "매출액또는손익구조"):
            general_avoid.append(f"• {row['corp_name']} {ticker} | {clean_ev} | {row['rcept_dt']}")

    has_actionable = bool(long_alerts or strong_avoid_alerts)

    if long_alerts:
        lines.extend([
            "",
            "■ 핵심 기회형 매수 경보",
            "익일 시가 진입 후 5영업일 보유 전략을 검토하시기 바랍니다.",
            "",
        ])
        lines.extend(long_alerts)

    if strong_avoid_alerts:
        lines.extend([
            "",
            "■ 강력 회피 경보",
            "공포 국면 악재 공시로 5영업일간 신규 매수 보류를 권고합니다.",
            "",
        ])
        lines.extend(strong_avoid_alerts)

    if watch_buybacks:
        lines.extend([
            "",
            f"■ 자사주 및 소각 모니터링 | 총 {len(watch_buybacks)}건",
            "현재 평온 국면으로 참고용 모니터링 대상입니다.",
            "",
        ])
        lines.extend(watch_buybacks[:5])
        if len(watch_buybacks) > 5:
            lines.append(f"  외 {len(watch_buybacks) - 5}건이 더 있습니다.")

    if general_avoid:
        lines.extend([
            "",
            f"■ 일반 회피 필터 대상 | 총 {len(general_avoid)}건",
            "실적, 배당, 손익구조 공시 종목으로 5영업일간 신규 매수 보류를 권고합니다.",
            "",
        ])
        lines.extend(general_avoid[:5])
        if len(general_avoid) > 5:
            lines.append(f"  외 {len(general_avoid) - 5}건이 더 있습니다.")

    if not (long_alerts or strong_avoid_alerts or watch_buybacks or general_avoid):
        lines.extend(["", "유동성 기준을 통과한 주요 시그널 공시가 없습니다."])

    lines.extend([
        "",
        "────────────────────",
        f"조회 기간: {cutoff_dt} ~ {latest_dt_str}",
        "기준: 일평균 거래대금 10억 원 이상 코스피200 종목",
    ])
    return "\n".join(lines), has_actionable


def main():
    parser = argparse.ArgumentParser(description="일간 공시·파생국면 실시간 텔레그램 경보")
    parser.add_argument("--days", type=int, default=1, help="스캔할 최근 일수 (기본 1일, 테스트 시 14 권장)")
    parser.add_argument("--dry-run", action="store_true", help="텔레그램 발송 없이 터미널 미리보기만 출력")
    parser.add_argument("--only-signals", action="store_true", help="핵심 기회/강력 회피 시그널이 있을 때만 발송")
    args = parser.parse_args()

    msg, has_actionable = build_daily_alert(days=args.days)
    print(msg)

    if args.dry_run:
        print("\n[Dry-Run] 텔레그램 발송을 건너뛰었습니다.")
        return

    if args.only_signals and not has_actionable:
        print("\n[스킵] 핵심 경보 시그널이 없어 발송하지 않았습니다.")
        return

    send_message(msg)


if __name__ == "__main__":
    main()