from datetime import datetime
from pathlib import Path
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT_DIR / "outputs"

# ==============================================================================
# 1. 챗봇·텔레그램 응답용 핵심 용어 사전 (초보자 친화적 정의)
# ==============================================================================
GLOSSARY = {
    "롱온리(Long-Only)": "종목을 단순 매수해 보유했을 때의 절대 수익률 (시장 등락인 베타가 섞여 있음).",
    "시장중립(Market-Neutral)": "종목 매수 + 시장 지수 매도(헤지)로 시장 운(베타)을 제거한 순수 알파 수익률.",
    "사후 CAR": "공시일 종가 기준으로 이후 5영업일간 시장 대비 초과수익률을 누적 합산한 이론 수익률.",
    "오버나잇 갭": "전일 종가 대비 다음 날 시가 점프 폭. 장 마감 후 공시가 시가에 이미 선반영된 구간.",
    "VIX / VKOSPI": "각각 미국 S&P500·한국 코스피200 옵션 가격에서 역산한 향후 30일 기대 변동성(공포지수).",
    "PCR(Put-Call Ratio)": "콜옵션 대비 풋옵션 비율(풋÷콜). 높을수록 하락 방어·공포 심리가 강함을 의미하며, 국내는 저가 OTM 왜곡을 줄인 거래대금 기준을 우선 활용.",
    "p-value / t-stat": "관측된 수익률이 순전히 우연일 확률. p<0.05(|t|≥2.0)면 통계적 유의, p<0.01(|t|≥2.6)면 매우 유의.",
    "본페로니 교정": "여러 조건을 동시 테스트할 때 우연히 유의하게 나오는 착시(다중검정 편향)를 막기 위해 기준(0.05/테스트수)을 엄격히 낮추는 기법.",
    "회피 필터(Avoidance)": "직접 공매도 대신, 악재 공시 발생 종목의 신규 매수를 5일간 보류·비중 축소하는 실전 방어 규칙.",
    "기회형 알파 슬리브": "발생 빈도는 낮지만(연 2~5회) 승률과 기대수익이 높은 고확신 시그널을 별도 비중으로 운용하는 방식.",
}

# ==============================================================================
# 2. 5단계 퀀트 결과 판독 매뉴얼 (상세 리포트 자동 첨부용)
# ==============================================================================
METHODOLOGY_MANUAL = """## 0. 5단계 퀀트 결과 판독 매뉴얼 (해석 방법론)

본 리포트의 모든 자동 진단은 아래 5단계 연결 판독 규칙(If-Then)에 따라 수행됩니다.

| 단계 | 핵심 질문 | 수치 연결 판독 규칙 (If-Then) |
| :--- | :--- | :--- |
| **Step 1. 수익률 순도 분해** | 진짜 알파인가, 시장 베타·시가 선반영 착시인가? | • `롱온리 > 0`인데 `시장중립 < 0` → 상승장 편승(**베타 착시**)<br>• `오버나잇 갭 ÷ CAR ≥ 60%` → 다음 날 시가에 이미 흡수(**EMH 선반영**)<br>• `비용전 > 0`인데 `비용후 ≤ 0` → 거래비용(0.25%)에 잠식<br>• `갭 ≈ 0%`인데 `시장중립`이 크게 이동 → 5일간 천천히 반영(**과소반응/드리프트**) |
| **Step 2. 통계·분포 건전성** | 우연인가, 소수 대박·폭락 1건의 왜곡인가? | • 평균 방향과 승률(양수면 55%+, 음수면 45%-)이 일치해야 건전한 분포<br>• `승률 ≥ 70%` & `평균 > 0`인데 `p ≥ 0.05` → 소수 대형 손실(**좌측 꼬리 이상치**)이 분산을 키운 상태<br>• `n < 30`은 소표본 주의, 다수 조합 동시 검정 시 **본페로니 교정(`0.05/K`)** 통과 여부 확인 |
| **Step 3. 국면 상호작용** | 왜 특정 시장 날씨(공포장)에서만 작동하는가? | • 공포 vs 평온 `차이 p-value < 0.05`일 때만 국면 효과 인정<br>• 공포장에서 호재(+)와 악재(-)가 **정반대로 벌어지면** 행동재무·시그널링 논리 강화 |
| **Step 4. 편향·필터 검증** | 미래를 훔쳐보거나 소형주에 왜곡되지 않았는가? | • 국면 컷오프는 과거 누적(`Expanding Q75`)만 사용해 **Look-ahead Bias 차단**<br>• 거래정지일 제외 및 **일거래대금 10억 원 미만 제외**로 저유동성 매입강도 왜곡 차단 |
| **Step 5. 실전 운용 번역** | 내일 포트폴리오에 어떻게 배치할 것인가? | • 고빈도 + 유의한 음(-)의 알파 → 한국 공매도 제약 고려 시 **리스크 회피(Avoidance) 필터**<br>• 저빈도 + 고확신 양(+)의 알파 → 기존 포트폴리오에 얹는 **기회형(Opportunistic) 알파 슬리브** |
"""

DERIVATIVE_COMPARISON_GUIDE = """## 4. 3대 파생 심리지표(VIX · VKOSPI · PCR) 비교 해석 가이드

학계 연구(*Bandopadhyaya & Jones 2008; Han, Kutan & Ryu 2015*)가 제시하듯, 변동성 지수와 옵션 거래비율은 서로 다른 차원의 시장 심리를 포착합니다.

- **미국 VIX (글로벌 거시 패닉 전이):** 외국인 자금 이탈과 글로벌 리스크오프(Risk-off)를 반영합니다. 위기 국면의 **악재 공시(`자사주_처분/해지`) 하락 증폭**과 대형주의 **초대형 방어 매입(`고강도 자사주 ≥3배`)** 선별에 가장 강하게 작동합니다.
- **한국 옵션 PCR(거래대금) (국내 실질 헤지·과매도 수급):** 저가 외가격(OTM) 거래량 노이즈를 제거한 거래대금 기준 풋/콜 비율입니다. 국내 참여자들이 실제 자금을 투입해 하락 헤지에 과도하게 쏠린 국지적 패닉 구간에서 **`자사주_취득/신탁` 전반의 역발상 반등 알파**를 날카롭게 포착합니다.
- **한국 VKOSPI (국내 내재변동성 체제):** 국내 30일 기대변동성을 직접 반영합니다. 단, `Expanding Q75` 기준에서 최근 변동성 체제 상승(Regime Shift)으로 공포 비중이 절반 가까이 확대되는 경향이 있으므로, 향후 `Rolling 252일(1년)` 윈도우와 교차 검증하는 것이 권장됩니다.
"""

RESEARCH_CAVEATS = """## 5. 연구의 구조적 한계 및 실전 주의사항 (Caveats)

1. **생존편향(Survivorship Bias):** 현재(최신) 코스피200 구성종목으로 과거 5년을 소급 수집했으므로, 기간 중 퇴출된 부실 종목의 악재 하락 폭이 실제보다 완만하게 측정되었을 수 있습니다.
2. **벤치마크 추적오차:** 시장중립 수익률 계산에 코스피200 동일가중 평균을 사용했습니다. 실전 헤지 수단인 시가총액가중 지수(코스피200 선물 / KODEX 200)와 미세한 추적오차가 존재합니다.
3. **공매도 규제 및 대차비용:** 음(-)의 알파 시그널을 직접 숏 전략으로 구현할 경우 공매도 금지 기간 및 대차수수료를 별도 반영해야 하므로, 실무에서는 **신규 매수 보류·비중 축소(Avoidance) 필터** 적용을 권장합니다.
4. **공시 시각 미구분:** OpenDART 목록 API는 일자(`rcept_dt`)만 제공하므로 장중 공시와 장 마감 후 공시가 혼재되어 있습니다. (단, 진입 시점을 보수적으로 `D+1 시가`로 고정해 미래참조 편향은 차단했습니다.)
"""


def diagnose_event_row(row: pd.Series, total_tests: int) -> dict:
    """Step 1(순도) -> Step 2(통계/분포) -> Step 5(실전 역할) 자동 판독"""
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
        tags.append("베타착시")
        notes.append(f"롱온리({long_net:+.2f}%)와 달리 시장중립({mn_net:+.2f}%)은 음수 → 상승장 편승 착시")
    if car > 0.3 and gap > 0 and (gap / car) >= 0.6:
        tags.append("시가선반영(EMH)")
        notes.append(f"CAR({car:+.2f}%)의 {gap / car * 100:.0f}%가 시가 갭({gap:+.2f}%)에 흡수됨")
    if mn_gross > 0 and mn_net <= 0:
        tags.append("비용잠식")
        notes.append(f"비용전({mn_gross:+.2f}%)은 플러스나 거래비용(0.25%) 차감 후 소멸")
    if abs(gap) < 0.3 and abs(mn_net) >= 0.5:
        tags.append("과소반응/드리프트")
        notes.append(f"시가 갭({gap:+.2f}%)은 미미하나 5일간 시장중립({mn_net:+.2f}%) 지속 이동")

    bonf_cutoff = 0.05 / max(total_tests, 1)
    is_bonf = pd.notna(p_val) and p_val < bonf_cutoff
    is_sig = pd.notna(p_val) and p_val < 0.05

    if n < 30:
        tags.append("소표본주의")
    if is_bonf:
        tags.append("다중검정통과(★★★)")
    elif is_sig:
        tags.append("통계유의(★★)")

    if mn_net > 0 and win_rate < 45:
        tags.append("우측꼬리주의")
    elif mn_net < 0 and win_rate > 55:
        tags.append("좌측꼬리주의")
    elif (mn_net > 0 and win_rate >= 55) or (mn_net < 0 and win_rate <= 45):
        tags.append("분포건전")

    if is_sig and mn_net <= -0.4 and n >= 30:
        action = "리스크 회피(Avoidance) 필터 — 5영업일 신규매수 보류/비중축소"
    elif is_sig and mn_net >= 0.5 and n >= 30:
        action = "메인 롱 이벤트 전략 후보"
    elif is_sig and mn_net >= 0.5 and n < 30:
        action = "소표본 참고 시그널 (추가 표본 축적 필요)"
    else:
        action = "단독 매매 비추천 (알파 미미 또는 통계 비유의)"

    return {
        "tags": tags,
        "summary": " / ".join(notes) if notes else "특이 괴리 없음",
        "action": action,
        "is_sig": is_sig,
        "is_bonf": is_bonf,
    }


def diagnose_regime_row(row: pd.Series, total_tests: int) -> dict:
    """Step 3(국면 상호작용 & 다중검정) 자동 판독"""
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
        tags.append("국면효과_최상위(★★★)")
    elif pd.notna(p_diff) and p_diff < 0.05:
        tags.append("국면효과_유의(★★)")
    elif pd.notna(p_diff) and p_diff < 0.10:
        tags.append("국면효과_경계(★)")

    if abs(fear_gap) < 0.3 and abs(fear_ret) >= 1.0:
        tags.append("공포장_과소반응")

    fear_ratio = fear_n / max(fear_n + calm_n, 1)
    if fear_ratio >= 0.40:
        tags.append("국면비중확대(체제변화주의)")

    if pd.notna(p_diff) and p_diff < 0.05 and fear_ret <= -1.0:
        action = f"공포장 강력 회피 필터 (평온 {calm_ret:+.2f}% → 공포 {fear_ret:+.2f}%, 승률 {fear_win:.1f}%)"
    elif pd.notna(p_diff) and p_diff < 0.05 and fear_ret >= 1.0:
        action = f"공포장 기회형 롱 후보 (평온 {calm_ret:+.2f}% → 공포 {fear_ret:+.2f}%, 승률 {fear_win:.1f}%)"
    else:
        action = "국면별 유의미한 차별화 없음"

    return {
        "tags": tags,
        "diff": diff,
        "p_diff": p_diff,
        "fear_n": fear_n,
        "action": action,
        "is_strong": pd.notna(p_diff) and p_diff < 0.05,
    }


def generate_reports():
    kw_path = OUTPUT_DIR / "keyword_event_summary.csv"
    if not kw_path.exists():
        raise FileNotFoundError("먼저 analysis/event_study.py 를 실행해 주세요.")

    kw_df = pd.read_csv(kw_path, index_col=0, encoding="utf-8-sig")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    md_lines = [
        f"# 퀀트 이벤트 스터디 자동 해석 리포트 ({now_str})",
        "",
        "> **검증 기준:** Look-ahead Bias 제거(`Expanding Q75`) · 유동성 필터(일거래대금 10억 원 이상) · 왕복 거래비용 `0.25%` 차감 · 시장중립 초과수익률(`D+1 시가 → D+5 종가`)",
        "",
        METHODOLOGY_MANUAL,
        "## 1. 전 기간 공시 유형별 자동 진단 (Step 1 · 2 · 5)",
    ]
    tg_lines = [
        f"📊 [KR Quant 자동 리포트] {now_str}",
        "━━━━━━━━━━━━━━━━━━━━",
        "1️⃣ 전 기간 핵심 시그널 (시장중립·비용0.25% 차감)",
    ]

    # 1) 전 기간 공시 해석
    total_kw_tests = len(kw_df)
    for etype, row in kw_df.iterrows():
        diag = diagnose_event_row(row, total_tests=total_kw_tests)
        tag_str = " ".join(f"`{t}`" for t in diag["tags"]) if diag["tags"] else "`특이사항없음`"
        md_lines.append(
            f"- **{etype}** (n={int(row['건수'])}, 시장중립 {row['시장중립_비용후(%)']:+.2f}%, "
            f"p={row['p_value(중립)']:.4f}, 승률 {row['승률_중립(%)']:.1f}%) | {tag_str}\n"
            f"  - 진단: {diag['summary']}\n"
            f"  - 실전 역할: **{diag['action']}**"
        )
        if diag["is_bonf"] or (diag["is_sig"] and abs(row["시장중립_비용후(%)"]) >= 0.5):
            icon = "🔻" if row["시장중립_비용후(%)"] < 0 else "🔺"
            tg_lines.append(
                f"{icon} {etype}: {row['시장중립_비용후(%)']:+.2f}% "
                f"(승률 {row['승률_중립(%)']:.0f}%, p={row['p_value(중립)']:.4f}) → {diag['action'].split('—')[0].strip()}"
            )

    # 2) 3대 파생 심리지표(VIX · VKOSPI · PCR) 국면별 교차 진단
    regime_sources = [
        ("미국 VIX", OUTPUT_DIR / "regime_event_summary.csv"),
        ("한국 VKOSPI", OUTPUT_DIR / "regime_vkospi_summary.csv"),
        ("옵션 PCR(거래대금)", OUTPUT_DIR / "regime_pcr_summary.csv"),
    ]

    md_lines.extend(["", "## 2. 3대 파생 심리지표(VIX · VKOSPI · PCR) 국면별 자동 진단 (Step 3)"])
    tg_lines.extend(["", "2️⃣ 파생 공포 국면(Expanding Q75) 특이 시그널"])

    found_any_regime = False
    for label, csv_path in regime_sources:
        if not csv_path.exists():
            continue
        reg_df = pd.read_csv(csv_path, index_col=0, encoding="utf-8-sig")
        if reg_df.empty:
            continue

        md_lines.append(f"\n### [{label}] 공포 vs 평온 국면 진단")
        total_reg_tests = len(reg_df) * 2
        for etype, row in reg_df.iterrows():
            diag = diagnose_regime_row(row, total_tests=total_reg_tests)
            tag_str = " ".join(f"`{t}`" for t in diag["tags"]) if diag["tags"] else "`국면차이없음`"
            md_lines.append(
                f"- **{etype}** (공포 n={diag['fear_n']}, 격차 {diag['diff']:+.2f}%p, p={diag['p_diff']:.4f}) | {tag_str}\n"
                f"  - 판정: {diag['action']}"
            )
            if diag["is_strong"]:
                found_any_regime = True
                tg_lines.append(
                    f"⚡ [{label}] {etype}: 평온 {row['평온_중립수익(%)']:+.2f}% → 공포 {row['공포_중립수익(%)']:+.2f}% "
                    f"(p={diag['p_diff']:.4f}, 공포 승률 {row['공포_승률(%)']:.0f}%)"
                )

    # 3) 고강도 자사주(>=3배) 3대 파생 지표 비교 (이상치 왜곡 자동 감지 포함)
    bb_multi_path = OUTPUT_DIR / "regime_buyback_multi.csv"
    if bb_multi_path.exists():
        bb_df = pd.read_csv(bb_multi_path, encoding="utf-8-sig")
        fear_rows = bb_df[bb_df["regime"] == "공포(Fear)"]
        if not fear_rows.empty:
            md_lines.extend(["", "## 3. 고강도 자사주(≥3배 · 유동성 필터) 3대 지표 공포 국면 비교"])
            for _, r in fear_rows.iterrows():
                n_bb = int(r["건수"])
                ret_bb = r["시장중립_비용후(%)"]
                p_bb = r["p_value(vs 0)"]
                win_bb = r["승률(%)"]
                p_str = f"{p_bb:.4f}" if pd.notna(p_bb) else "N/A"

                extra_notes = []
                if n_bb < 30:
                    extra_notes.append("소표본(n<30) 주의")
                if win_bb >= 70.0 and ret_bb > 0 and (pd.isna(p_bb) or p_bb >= 0.05):
                    extra_notes.append("고승률·저t-stat(좌측 꼬리 손실 이상치 왜곡 의심 → 손절·사이징 통제 필요)")

                note_str = f" | ⚠️ {' / '.join(extra_notes)}" if extra_notes else ""
                md_lines.append(
                    f"- **{r['지표']} 공포 국면:** n={n_bb}, 시장중립 {ret_bb:+.2f}% (p={p_str}, 승률 {win_bb:.1f}%){note_str}"
                )

                if pd.notna(p_bb) and p_bb < 0.05 and ret_bb > 0:
                    found_any_regime = True
                    tg_lines.append(
                        f"🔥 [{r['지표']}] 고강도 자사주: 공포장 {ret_bb:+.2f}% (승률 {win_bb:.0f}%, p={p_str}, n={n_bb})"
                    )
                elif win_bb >= 70.0 and ret_bb >= 1.5 and (pd.isna(p_bb) or p_bb >= 0.05):
                    found_any_regime = True
                    tg_lines.append(
                        f"👀 [{r['지표']}] 고강도 자사주: 공포장 {ret_bb:+.2f}% (승률 {win_bb:.0f}%, n={n_bb}, 이상치 분산 주의)"
                    )

    if not found_any_regime:
        tg_lines.append("• 현재 유의미한 국면 차별화 시그널 없음")

    # 4) 3대 파생지표 해설, 연구 한계, 용어 사전 첨부
    md_lines.extend(["", DERIVATIVE_COMPARISON_GUIDE, RESEARCH_CAVEATS, "## 6. 핵심 용어 사전 (챗봇·초보자 참고용)"])
    for term, desc in GLOSSARY.items():
        md_lines.append(f"- **{term}:** {desc}")

    tg_lines.append("━━━━━━━━━━━━━━━━━━━━")
    tg_lines.append("※ 상세 5단계 해석법·주의사항: outputs/interpretation_report.md 참고")

    md_path = OUTPUT_DIR / "interpretation_report.md"
    tg_path = OUTPUT_DIR / "telegram_brief.txt"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    tg_path.write_text("\n".join(tg_lines), encoding="utf-8")

    print(f"[저장 완료] {md_path.name}, {tg_path.name}\n")
    print("================ [텔레그램 발송 미리보기] ================")
    print("\n".join(tg_lines))


if __name__ == "__main__":
    generate_reports()