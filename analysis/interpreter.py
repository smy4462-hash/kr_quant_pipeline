import sqlite3
from datetime import datetime
from pathlib import Path
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "kr_quant.db"
OUTPUT_DIR = ROOT_DIR / "outputs"
MIN_PERIODS = 60

# ==============================================================================
# 1. 상세 리포트용 핵심 용어 사전
# ==============================================================================
GLOSSARY = {
    "롱온리 수익률": "종목을 단순 매수해 보유했을 때의 절대 수익률입니다. 시장 전체의 등락인 베타가 포함되어 있습니다.",
    "시장중립 수익률": "종목 매수와 시장 지수 헤지를 결합해 시장 방향성 위험을 제거한 순수 알파 수익률입니다.",
    "사후 누적초과수익률": "공시일 종가 기준으로 이후 5영업일간 시장 대비 초과수익률을 누적 합산한 이론 수익률입니다.",
    "오버나잇 갭": "전일 종가 대비 익일 시가의 변동 폭입니다. 장 마감 후 공시가 시가에 선반영되는 정도를 측정합니다.",
    "미국 VIX 및 한국 VKOSPI": "각각 미국 S&P500과 한국 코스피200 옵션 가격에서 역산한 향후 30일 기대변동성 지표입니다.",
    "옵션 풋콜비율": "콜옵션 대비 풋옵션 비율입니다. 수치가 높을수록 하락 방어와 공포 심리가 강함을 의미하며, 저가 외가격 노이즈를 줄이기 위해 거래대금 기준을 사용합니다.",
    "유의확률 및 t-통계량": "관측된 수익률이 우연에 의해 발생했을 확률입니다. 유의확률 0.05 미만이면 통계적으로 유의하며, 0.01 미만이면 매우 유의합니다.",
    "본페로니 다중검정 교정": "다수의 조건을 동시에 검정할 때 우연히 유의하게 나타나는 착시를 막기 위해 유의수준 기준을 검정 횟수로 나누어 엄격하게 적용하는 기법입니다.",
    "리스크 회피 필터": "직접 공매도 대신 악재 공시 발생 종목의 신규 매수를 일정 기간 보류하거나 비중을 축소하는 실전 방어 규칙입니다.",
    "기회형 알파 슬리브": "발생 빈도는 낮지만 승률과 기대수익이 높은 고확신 시그널에 별도 비중을 배정해 운용하는 방식입니다.",
}

METHODOLOGY_MANUAL = """## 1. 5단계 퀀트 결과 판독 매뉴얼

본 리포트의 모든 자동 진단은 아래 5단계 규칙에 따라 수행됩니다.

| 단계 | 핵심 질문 | 수치 판독 기준 |
| :--- | :--- | :--- |
| 1단계. 수익률 순도 분해 | 순수 알파인가, 베타 또는 시가 선반영 착시인가 | 롱온리가 양수이나 시장중립이 음수이면 베타 착시로 판정합니다. 오버나잇 갭이 누적초과수익률의 60% 이상이면 시가 선반영으로 판정합니다. |
| 2단계. 통계 및 분포 건전성 | 우연인가, 소수 이상치에 의한 왜곡인가 | 평균 수익률과 승률의 방향이 일치해야 건전한 분포로 인정합니다. 표본 30건 미만은 소표본 주의 대상으로 분류하며, 본페로니 교정 통과 여부를 확인합니다. |
| 3단계. 국면 상호작용 | 특정 시장 국면에서만 차별화되는가 | 공포 국면과 평온 국면 간 수익률 격차의 유의확률이 0.05 미만일 때만 국면 효과를 인정합니다. |
| 4단계. 편향 및 유동성 통제 | 미래참조 편향이나 저유동성 왜곡이 없는가 | 국면 기준선은 과거 누적 상위 25%인 Expanding Q75만 사용합니다. 거래정지일과 직전 5일 평균 일거래대금 10억 원 미만 종목은 제외합니다. |
| 5단계. 실전 운용 번역 | 포트폴리오에 어떻게 적용할 것인가 | 고빈도 음의 알파는 리스크 회피 필터로, 저빈도 고확신 양의 알파는 기회형 매수 슬리브로 분류합니다. |
"""

DERIVATIVE_COMPARISON_GUIDE = """## 5. 3대 파생 심리지표 비교 해석 가이드

학계 연구에 따르면 변동성 지수와 옵션 거래비율은 서로 다른 차원의 시장 심리를 포착합니다.

* **미국 VIX — 실전 메인 국면 필터**
  외국인 자금 이탈과 글로벌 리스크오프 심리를 가장 직접적으로 반영합니다. 위기 국면의 자사주 처분 하락 증폭과 고강도 자사주 방어 매입 선별에서 가장 안정적인 판별력을 보입니다.
* **옵션 풋콜비율 — 국내 실질 헤지 수급 보조 지표**
  거래대금 기준 풋옵션과 콜옵션 비율을 통해 국내 참여자의 하락 헤지 과열 여부를 점검합니다. 단, 시가총액가중 KODEX 200 헤지 기준에서는 단독 매수 알파가 약화되므로 보조 지표로 활용합니다.
* **한국 VKOSPI — 국내 내재변동성 보조 지표**
  국내 코스피200 옵션의 30일 기대변동성을 반영합니다. 최근 변동성 체제 변화로 공포 국면 비중이 40% 이상으로 확대되는 경향이 있어 보조 모니터링 지표로 운용합니다.
"""

RESEARCH_CAVEATS = """## 6. 연구의 구조적 한계 및 실전 주의사항

1. **생존편향:** 최신 코스피200 구성종목 기준으로 과거 5년을 소급 수집했으므로, 기간 중 지수에서 퇴출된 종목의 악재 하락 폭이 실제보다 완만하게 측정되었을 가능성이 있습니다.
2. **벤치마크 추적오차:** 본 기본 리포트의 시장중립 수익률은 코스피200 동일가중 평균을 기준으로 산출됩니다. 실전 헤지 수단인 시가총액가중 KODEX 200 ETF 기준의 검증 결과는 `outputs/deep_dive_benchmark.csv`를 함께 참고하시기 바랍니다.
3. **공매도 제약 및 대차비용:** 음의 알파 시그널을 직접 매도 전략으로 실행할 경우 공매도 규제와 대차수수료가 발생하므로, 실전에서는 신규 매수 보류 및 비중 축소 필터로 적용하는 것을 권고합니다.
4. **공시 시각 미구분:** OpenDART 목록 API는 공시 일자만 제공하므로 장중 공시와 장 마감 후 공시가 혼재되어 있습니다. 다만 진입 시점을 보수적으로 익일 시가로 고정하여 미래참조 편향은 차단했습니다.
"""


def format_event_name(etype: str) -> str:
    """내부 분류명을 괄호·언더바 없는 정식 표기로 변환"""
    mapping = {
        "자사주_취득/신탁": "자사주 취득 및 신탁",
        "자사주_처분/해지": "자사주 처분 및 해지",
        "영업(잠정)실적": "영업잠정실적",
        "매출액또는손익구조": "매출액 또는 손익구조",
        "단일판매": "단일판매 공급계약",
        "소각": "자사주 소각",
    }
    return mapping.get(etype, etype.replace("_", " "))


def format_indicator_name(label: str) -> str:
    """파생지표 명칭을 괄호 없는 정식 표기로 변환"""
    mapping = {
        "미국 VIX": "미국 VIX",
        "한국 VKOSPI": "한국 VKOSPI",
        "옵션 PCR(거래대금)": "옵션 풋콜비율",
    }
    return mapping.get(label, label)


def get_current_regimes() -> dict:
    """DB에서 3대 파생 심리지표의 최신 확정치 및 공포/평온 국면 조회"""
    if not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(DB_PATH)
    try:
        sent = pd.read_sql("SELECT date, vix, vkospi, pcr_val FROM market_sentiment ORDER BY date", conn)
    except Exception:
        conn.close()
        return {}
    conn.close()

    regimes = {}
    for col, label in [("vix", "미국 VIX"), ("vkospi", "한국 VKOSPI"), ("pcr_val", "옵션 풋콜비율")]:
        if col not in sent.columns:
            continue
        sub = sent[["date", col]].dropna().reset_index(drop=True)
        if len(sub) < MIN_PERIODS:
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


def diagnose_event_row(row: pd.Series, total_tests: int) -> dict:
    """1단계(순도) -> 2단계(통계/분포) -> 5단계(실전 역할) 자동 판독"""
    n = int(row["건수"])
    car = row["사후_CAR(%)"]
    gap = row["오버나잇갭_초과(%)"]
    mn_gross = row["시장중립_비용전(%)"]
    mn_net = row["시장중립_비용후(%)"]
    long_net = row["롱온리_비용후(%)"]
    p_val = row["p_value(중립)"]
    win_rate = row["승률_중립(%)"]

    tags, notes = [], []

    if long_net > 0 and mn_net < 0:
        tags.append("베타 착시")
        notes.append(f"롱온리 수익률은 {long_net:+.2f}%이나 시장중립 수익률은 {mn_net:+.2f}%로 상승장 편승 착시가 확인되었습니다.")
    if car > 0.3 and gap > 0 and (gap / car) >= 0.6:
        tags.append("시가 선반영")
        notes.append(f"사후 누적초과수익률 {car:+.2f}% 중 {gap / car * 100:.0f}%가 익일 시가 갭 {gap:+.2f}%에 이미 흡수되었습니다.")
    if mn_gross > 0 and mn_net <= 0:
        tags.append("비용 잠식")
        notes.append(f"비용 차감 전 수익률은 {mn_gross:+.2f}%이나 왕복 거래비용 0.25% 반영 후 알파가 소멸했습니다.")
    if abs(gap) < 0.3 and abs(mn_net) >= 0.5:
        tags.append("과소반응 드리프트")
        notes.append(f"시가 갭은 {gap:+.2f}%로 미미하나 5영업일간 시장중립 수익률 {mn_net:+.2f}%로 추세가 지속되었습니다.")

    bonf_cutoff = 0.05 / max(total_tests, 1)
    is_bonf = pd.notna(p_val) and p_val < bonf_cutoff
    is_sig = pd.notna(p_val) and p_val < 0.05

    if n < 30:
        tags.append("소표본 주의")
    if is_bonf:
        tags.append("다중검정 통과")
    elif is_sig:
        tags.append("통계적 유의")

    if mn_net > 0 and win_rate < 45:
        tags.append("우측 꼬리 주의")
    elif mn_net < 0 and win_rate > 55:
        tags.append("좌측 꼬리 주의")
    elif (mn_net > 0 and win_rate >= 55) or (mn_net < 0 and win_rate <= 45):
        tags.append("분포 건전")

    if is_sig and mn_net <= -0.4 and n >= 30:
        action = "리스크 회피 필터 적용 권고"
        md_action = "리스크 회피 필터 적용 권고 — 5영업일간 신규 매수 보류 및 비중 축소"
    elif is_sig and mn_net >= 0.5 and n >= 30:
        action = "메인 매수 전략 후보"
        md_action = "메인 매수 전략 후보"
    elif is_sig and mn_net >= 0.5 and n < 30:
        action = "소표본 참고 시그널"
        md_action = "소표본 참고 시그널 — 추가 표본 축적 필요"
    else:
        action = "단독 매매 비추천"
        md_action = "단독 매매 비추천 — 알파 미미 또는 통계적 비유의"

    return {
        "tags": tags,
        "summary": " ".join(notes) if notes else "특이한 수익률 괴리나 착시 현상이 관측되지 않았습니다.",
        "action": action,
        "md_action": md_action,
        "is_sig": is_sig,
        "is_bonf": is_bonf,
    }


def diagnose_regime_row(row: pd.Series, total_tests: int) -> dict:
    """3단계(국면 상호작용 및 다중검정) 자동 판독"""
    calm_ret = row["평온_중립수익(%)"]
    fear_ret = row["공포_중립수익(%)"]
    fear_gap = row["공포_갭(%)"]
    fear_n = int(row["공포_건수"])
    calm_n = int(row["평온_건수"])
    fear_win = row["공포_승률(%)"]
    diff = row["공포-평온_격차(%)"]
    p_diff = row["차이_p_value"]

    bonf_cutoff = 0.05 / max(total_tests, 1)
    tags = []

    if pd.notna(p_diff) and p_diff < bonf_cutoff:
        tags.append("최상위 국면 유의")
    elif pd.notna(p_diff) and p_diff < 0.05:
        tags.append("국면 유의")
    elif pd.notna(p_diff) and p_diff < 0.10:
        tags.append("경계 수준")

    if abs(fear_gap) < 0.3 and abs(fear_ret) >= 1.0:
        tags.append("공포장 과소반응")

    fear_ratio = fear_n / max(fear_n + calm_n, 1)
    if fear_ratio >= 0.40:
        tags.append("국면 비중 확대 주의")

    if pd.notna(p_diff) and p_diff < 0.05 and fear_ret <= -1.0:
        tg_role = "강력 회피 권고"
        md_action = "공포 국면 강력 회피 필터 적용을 권고합니다."
    elif pd.notna(p_diff) and p_diff < 0.05 and fear_ret >= 1.0:
        tg_role = "기회형 매수 후보"
        md_action = "공포 국면 기회형 매수 후보로 검토할 수 있습니다."
    else:
        tg_role = "유의미한 차별화 없음"
        md_action = "공포 국면과 평온 국면 간 통계적으로 유의미한 차별화가 없습니다."

    return {
        "tags": tags,
        "diff": diff,
        "p_diff": p_diff,
        "fear_n": fear_n,
        "calm_ret": calm_ret,
        "fear_ret": fear_ret,
        "fear_win": fear_win,
        "tg_role": tg_role,
        "action": md_action,
        "is_strong": pd.notna(p_diff) and p_diff < 0.05,
    }


def generate_reports():
    kw_path = OUTPUT_DIR / "keyword_event_summary.csv"
    if not kw_path.exists():
        raise FileNotFoundError("먼저 analysis/event_study.py 를 실행해 주세요.")

    kw_df = pd.read_csv(kw_path, index_col=0, encoding="utf-8-sig")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    date_only = datetime.now().strftime("%Y-%m-%d")

    regimes = get_current_regimes()

    # ==========================================================================
    # 상세 리포트(interpretation_report.md) — 결론 우선 · 2단 계층 · 격식체
    # ==========================================================================
    md_lines = [
        f"# KR Quant 이벤트 스터디 자동 해석 리포트",
        "",
        f"생성 일시: {now_str}  ",
        "검증 기준: 과거 누적 상위 25% Expanding Q75 적용 · 일평균 거래대금 10억 원 이상 · 왕복 거래비용 0.25% 차감 · 익일 시가 진입 후 5영업일 종가 청산 시장중립 수익률",
        "",
    ]

    if regimes:
        vix_state = "공포" if regimes.get("vix", {}).get("is_fear") else "평온"
        pcr_state = "공포" if regimes.get("pcr_val", {}).get("is_fear") else "평온"
        md_lines.extend([
            "## 현재 시장 국면 진단",
            "",
            f"현재 시장은 메인 국면 필터인 미국 VIX 기준 **{vix_state}**, 보조 지표인 옵션 풋콜비율 기준 **{pcr_state}** 국면입니다.",
            "",
            "| 심리지표 | 역할 | 현재 국면 | 현재 수치 | 공포 기준선 | 기준 일자 |",
            "| :--- | :---: | :---: | :---: | :---: | :---: |",
        ])
        for col in ("vix", "vkospi", "pcr_val"):
            if col in regimes:
                r = regimes[col]
                role = "메인" if col == "vix" else "보조"
                state = "공포" if r["is_fear"] else "평온"
                md_lines.append(f"| {r['label']} | {role} | **{state}** | {r['val']} | {r['cutoff']} | {r['date']} |")
        md_lines.append("")

    md_lines.extend([
        METHODOLOGY_MANUAL,
        "## 2. 전 기간 공시 유형별 자동 진단",
        "",
        "각 공시 유형의 핵심 결론을 먼저 제시하고, 세부 수치와 진단 근거를 하위 항목으로 정리했습니다.",
        "",
    ])

    # 텔레그램 주간 브리핑(telegram_brief.txt)
    tg_lines = [f"[KR Quant 주간 리포트] {date_only}", ""]

    if regimes:
        vix_state = "공포" if regimes.get("vix", {}).get("is_fear") else "평온"
        pcr_state = "공포" if regimes.get("pcr_val", {}).get("is_fear") else "평온"
        tg_lines.append("■ 현재 시장 국면 진단")
        tg_lines.append(f"현재 시장은 미국 VIX {vix_state}, 옵션 풋콜비율 {pcr_state} 국면입니다.")
        for col in ("vix", "vkospi", "pcr_val"):
            if col in regimes:
                r = regimes[col]
                badge = "🔴 공포" if r["is_fear"] else "🟢 평온"
                tg_lines.append(f"• {r['label']}: {badge} | 현재 {r['val']}, 기준 {r['cutoff']}")
        tg_lines.append("")

    tg_lines.extend([
        "■ 1. 전 기간 핵심 시그널",
        "아래 공시 발생 시 5영업일간 신규 매수 보류를 권고합니다.",
        "",
    ])

    total_kw_tests = len(kw_df)
    has_base_signal = False
    for etype, row in kw_df.iterrows():
        diag = diagnose_event_row(row, total_tests=total_kw_tests)
        clean_name = format_event_name(etype)
        tag_str = ", ".join(diag["tags"]) if diag["tags"] else "특이사항 없음"

        md_lines.extend([
            f"### {clean_name} — {diag['action']}",
            f"* **핵심 수치:** 표본 {int(row['건수']):,}건 | 시장중립 비용후 {row['시장중립_비용후(%)']:+.2f}% | 승률 {row['승률_중립(%)']:.1f}% | 유의확률 {row['p_value(중립)']:.4f}",
            f"* **판독 태그:** {tag_str}",
            f"* **진단 근거:** {diag['summary']}",
            f"* **실전 역할:** {diag['md_action']}",
            "",
        ])

        if diag["is_bonf"] or (diag["is_sig"] and abs(row["시장중립_비용후(%)"]) >= 0.5):
            has_base_signal = True
            tg_lines.append(f"• {clean_name} | {diag['action']}")
            tg_lines.append(
                f"  시장중립 수익률 {row['시장중립_비용후(%)']:+.2f}% | "
                f"승률 {row['승률_중립(%)']:.0f}% | 유의확률 {row['p_value(중립)']:.4f}"
            )

    if not has_base_signal:
        tg_lines.append("• 유의미한 전 기간 시그널이 없습니다.")

    # 3) 3대 파생 심리지표 국면별 교차 진단
    regime_sources = [
        ("미국 VIX", OUTPUT_DIR / "regime_event_summary.csv"),
        ("한국 VKOSPI", OUTPUT_DIR / "regime_vkospi_summary.csv"),
        ("옵션 PCR(거래대금)", OUTPUT_DIR / "regime_pcr_summary.csv"),
    ]

    md_lines.extend([
        "## 3. 3대 파생 심리지표 국면별 교차 진단",
        "",
        "공포 국면과 평온 국면 간 수익률 격차를 Welch t-검정으로 비교한 결과입니다.",
        "",
    ])

    tg_lines.extend([
        "",
        "■ 2. 공포 국면 진입 시 작동하는 조건부 시그널",
        "과거 5년 검증 결과, 공포 국면에서만 유의미한 알파가 확인된 조건입니다.",
        "",
    ])

    found_any_regime = False
    for label, csv_path in regime_sources:
        if not csv_path.exists():
            continue
        reg_df = pd.read_csv(csv_path, index_col=0, encoding="utf-8-sig")
        if reg_df.empty:
            continue

        clean_ind = format_indicator_name(label)
        md_lines.extend([f"### {clean_ind} 기준 국면 진단", ""])
        total_reg_tests = len(reg_df) * 2

        for etype, row in reg_df.iterrows():
            diag = diagnose_regime_row(row, total_tests=total_reg_tests)
            clean_ev = format_event_name(etype)
            tag_str = ", ".join(diag["tags"]) if diag["tags"] else "국면 차이 없음"

            md_lines.extend([
                f"* **{clean_ev} — {diag['tg_role']}**",
                f"  * 수익률 비교: 평온 {diag['calm_ret']:+.2f}% → 공포 {diag['fear_ret']:+.2f}% | 격차 {diag['diff']:+.2f}%p | 공포 승률 {diag['fear_win']:.1f}%",
                f"  * 통계 검정: 공포 표본 {diag['fear_n']}건 | 차이 유의확률 {diag['p_diff']:.4f} | 태그: {tag_str}",
                f"  * 판정 요약: {diag['action']}",
            ])

            if diag["is_strong"]:
                found_any_regime = True
                tg_lines.append(f"• {clean_ev} | {clean_ind} 공포 시 {diag['tg_role']}")
                tg_lines.append(
                    f"  평온 {row['평온_중립수익(%)']:+.2f}% → 공포 {row['공포_중립수익(%)']:+.2f}% | "
                    f"공포 승률 {row['공포_승률(%)']:.0f}% | 유의확률 {diag['p_diff']:.4f}"
                )
        md_lines.append("")

    # 4) 고강도 자사주(3배 이상) 3대 파생 지표 비교
    bb_multi_path = OUTPUT_DIR / "regime_buyback_multi.csv"
    if bb_multi_path.exists():
        bb_df = pd.read_csv(bb_multi_path, encoding="utf-8-sig")
        fear_rows = bb_df[bb_df["regime"] == "공포(Fear)"]
        if not fear_rows.empty:
            md_lines.extend([
                "## 4. 고강도 자사주 3배 이상 3대 지표 공포 국면 비교",
                "",
                "일평균 거래대금 대비 자사주 매입 예정금액이 3배 이상인 고강도 공시의 공포 국면 성과입니다.",
                "",
            ])
            for _, r in fear_rows.iterrows():
                n_bb = int(r["건수"])
                ret_bb = r["시장중립_비용후(%)"]
                p_bb = r["p_value(vs 0)"]
                win_bb = r["승률(%)"]
                p_str = f"{p_bb:.4f}" if pd.notna(p_bb) else "산출 불가"
                clean_ind = format_indicator_name(str(r["지표"]))

                extra_notes = []
                if n_bb < 30:
                    extra_notes.append("표본 30건 미만 소표본 주의")
                if win_bb >= 70.0 and ret_bb > 0 and (pd.isna(p_bb) or p_bb >= 0.05):
                    extra_notes.append("고승률 대비 낮은 t-통계량으로 좌측 꼬리 손실 이상치 왜곡이 의심되므로 손절 및 비중 통제가 필요합니다.")

                verdict = "기회형 매수 후보" if (pd.notna(p_bb) and p_bb < 0.05 and ret_bb > 0) else "모니터링 및 리스크 통제 대상"
                md_lines.extend([
                    f"* **{clean_ind} 공포 국면 — {verdict}**",
                    f"  * 핵심 수치: 표본 {n_bb}건 | 시장중립 비용후 {ret_bb:+.2f}% | 승률 {win_bb:.1f}% | 유의확률 {p_str}",
                ])
                if extra_notes:
                    md_lines.append(f"  * 주의 사항: {' / '.join(extra_notes)}")

                if pd.notna(p_bb) and p_bb < 0.05 and ret_bb > 0:
                    found_any_regime = True
                    tg_lines.append(f"• 고강도 자사주 3배 이상 | {clean_ind} 공포 시 기회형 매수 후보")
                    tg_lines.append(
                        f"  공포 수익률 {ret_bb:+.2f}% | 승률 {win_bb:.0f}% | 유의확률 {p_str} | 표본 {n_bb}건"
                    )
                elif win_bb >= 70.0 and ret_bb >= 1.5 and (pd.isna(p_bb) or p_bb >= 0.05):
                    found_any_regime = True
                    tg_lines.append(f"• 고강도 자사주 3배 이상 | {clean_ind} 공포 시 이상치 주의 모니터링")
                    tg_lines.append(
                        f"  공포 수익률 {ret_bb:+.2f}% | 승률 {win_bb:.0f}% | 표본 {n_bb}건, 손절 통제 필요"
                    )
            md_lines.append("")

    if not found_any_regime:
        tg_lines.append("• 현재 유의미한 국면 차별화 시그널이 없습니다.")

    # 5) 상세 리포트 부록 및 텔레그램 푸터
    md_lines.extend([
        DERIVATIVE_COMPARISON_GUIDE,
        RESEARCH_CAVEATS,
        "## 7. 핵심 용어 사전",
        "",
    ])
    for term, desc in GLOSSARY.items():
        md_lines.append(f"* **{term}:** {desc}")

    tg_lines.extend([
        "",
        "────────────────────",
        "검증 기준: 시장중립 5영업일, 왕복 거래비용 0.25% 차감",
        "상세 해석 및 매뉴얼: outputs/interpretation_report.md",
    ])

    md_path = OUTPUT_DIR / "interpretation_report.md"
    tg_path = OUTPUT_DIR / "telegram_brief.txt"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    tg_path.write_text("\n".join(tg_lines), encoding="utf-8")

    print(f"[저장 완료] {md_path.name}, {tg_path.name}\n")
    print("================ [텔레그램 발송 미리보기] ================")
    print("\n".join(tg_lines))


if __name__ == "__main__":
    generate_reports()