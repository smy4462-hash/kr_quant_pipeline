from datetime import datetime
from pathlib import Path
import numpy as np
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
    "p-value": "관측된 수익률이 순전히 우연일 확률. 0.05 미만이면 통계적 유의, 0.01 미만이면 매우 유의.",
    "본페로니 교정": "여러 조건을 동시 테스트할 때 우연히 유의하게 나오는 착시를 막기 위해 기준(0.05/테스트수)을 엄격히 낮추는 기법.",
    "회피 필터(Avoidance)": "직접 공매도 대신, 악재 공시 발생 종목의 신규 매수를 5일간 보류·비중 축소하는 실전 방어 규칙.",
    "기회형 알파 슬리브": "발생 빈도는 낮지만(연 2~5회) 승률과 기대수익이 높은 고확신 시그널을 별도 비중으로 운용하는 방식.",
}


# ==============================================================================
# 2. 5단계 퀀트 해석 규칙 엔진 (Rule-Based Auto-Interpreter)
# ==============================================================================
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

    tags = []
    notes = []

    # [Step 1] 수익률 순도 분해
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

    # [Step 2] 통계적 신뢰도 & 분포 건전성
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

    # [Step 5] 실전 운용 역할 부여
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


# ==============================================================================
# 3. 리포트 생성기 (Markdown 상세 리포트 + Telegram 요약 브리핑)
# ==============================================================================
def generate_reports():
    kw_path = OUTPUT_DIR / "keyword_event_summary.csv"
    reg_path = OUTPUT_DIR / "regime_event_summary.csv"
    bb_path = OUTPUT_DIR / "regime_buyback_summary.csv"

    if not kw_path.exists() or not reg_path.exists():
        raise FileNotFoundError("먼저 analysis/event_study.py 와 analysis/regime_event_study.py 를 실행해 주세요.")

    kw_df = pd.read_csv(kw_path, index_col=0, encoding="utf-8-sig")
    reg_df = pd.read_csv(reg_path, index_col=0, encoding="utf-8-sig")
    bb_df = pd.read_csv(bb_path, index_col=0, encoding="utf-8-sig") if bb_path.exists() else pd.DataFrame()

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    md_lines = [
        f"# 퀀트 이벤트 스터디 자동 해석 리포트 ({now_str})",
        "",
        "> 본 리포트는 5단계 퀀트 해석 프레임워크(순도 분해 → 통계·분포 → 국면 대칭성 → 필터 건전성 → 실전 역할)에 따라 자동 생성되었습니다.",
        "",
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

    # 2) VIX 국면별 해석 (Step 3)
    md_lines.extend(["", "## 2. VIX 공포 vs 평온 국면별 자동 진단 (Step 3)"])
    tg_lines.extend(["", "2️⃣ VIX 공포 국면(Expanding Q75) 특이 시그널"])

    total_reg_tests = len(reg_df) * 2
    found_regime_signal = False
    for etype, row in reg_df.iterrows():
        diag = diagnose_regime_row(row, total_tests=total_reg_tests)
        tag_str = " ".join(f"`{t}`" for t in diag["tags"]) if diag["tags"] else "`국면차이없음`"
        md_lines.append(
            f"- **{etype}** (공포 n={diag['fear_n']}, 격차 {diag['diff']:+.2f}%p, p={diag['p_diff']:.4f}) | {tag_str}\n"
            f"  - 판정: {diag['action']}"
        )
        if diag["is_strong"]:
            found_regime_signal = True
            tg_lines.append(
                f"⚡ {etype}: 평온 {row['평온_중립수익(%)']:+.2f}% → 공포 {row['공포_중립수익(%)']:+.2f}% "
                f"(p={diag['p_diff']:.4f}, 공포 승률 {row['공포_승률(%)']:.0f}%)"
            )

    # 3) 고강도 자사주 국면별 해석
    if not bb_df.empty and "공포(Fear)" in bb_df.index:
        fear_bb = bb_df.loc["공포(Fear)"]
        calm_bb = bb_df.loc["평온(Calm)"] if "평온(Calm)" in bb_df.index else None
        n_bb = int(fear_bb["건수"])
        ret_bb = fear_bb["시장중립_비용후(%)"]
        p_bb = fear_bb["p_value(vs 0)"]
        win_bb = fear_bb["승률(%)"]
        sample_warn = " (단, 소표본 n<30 주의)" if n_bb < 30 else ""

        md_lines.extend([
            "",
            "## 3. 고강도 자사주(≥3배 · 유동성 필터 통과) 국면 진단",
            f"- **공포 국면:** n={n_bb}, 시장중립 {ret_bb:+.2f}% (p={p_bb:.4f}, 승률 {win_bb:.1f}%){sample_warn}",
        ])
        if calm_bb is not None:
            md_lines.append(
                f"- **평온 국면:** n={int(calm_bb['건수'])}, 시장중립 {calm_bb['시장중립_비용후(%)']:+.2f}% (승률 {calm_bb['승률(%)']:.1f}%)"
            )

        if pd.notna(p_bb) and p_bb < 0.05 and ret_bb > 0:
            found_regime_signal = True
            md_lines.append("- **실전 역할:** 기회형(Opportunistic) 알파 슬리브 후보")
            tg_lines.append(
                f"🔥 고강도 자사주(≥3배): 공포장 {ret_bb:+.2f}% (승률 {win_bb:.0f}%, p={p_bb:.4f}, n={n_bb}) → 기회형 롱 슬리브"
            )

    if not found_regime_signal:
        tg_lines.append("• 현재 유의미한 국면 차별화 시그널 없음")

    # 4) 챗봇 참고용 용어 사전 첨부 (Markdown 리포트 하단)
    md_lines.extend(["", "## 4. 핵심 용어 사전 (챗봇·리포트 참고용)"])
    for term, desc in GLOSSARY.items():
        md_lines.append(f"- **{term}:** {desc}")

    tg_lines.append("━━━━━━━━━━━━━━━━━━━━")
    tg_lines.append("※ 모든 수치는 Look-ahead Bias 제거 및 거래비용 0.25% 차감 기준")

    md_path = OUTPUT_DIR / "interpretation_report.md"
    tg_path = OUTPUT_DIR / "telegram_brief.txt"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    tg_path.write_text("\n".join(tg_lines), encoding="utf-8")

    print(f"[저장 완료] {md_path.name}, {tg_path.name}\n")
    print("================ [텔레그램 발송 미리보기] ================")
    print("\n".join(tg_lines))


if __name__ == "__main__":
    generate_reports()