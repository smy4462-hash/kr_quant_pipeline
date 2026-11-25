import argparse
import os
from pathlib import Path
import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT_DIR / "outputs"
load_dotenv(ROOT_DIR / ".env")

MAX_MSG_LEN = 4000


def _get_credentials() -> tuple[str, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise ValueError("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 설정되지 않았습니다.")
    return token, chat_id


def send_message(text: str):
    """텔레그램 텍스트 메시지 전송 (4,000자 초과 시 자동 분할)"""
    token, chat_id = _get_credentials()
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    chunks = [text[i : i + MAX_MSG_LEN] for i in range(0, len(text), MAX_MSG_LEN)]
    for idx, chunk in enumerate(chunks, start=1):
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            timeout=15,
        )
        resp.raise_for_status()
        print(f"[텔레그램] 메시지 전송 성공 ({idx}/{len(chunks)})")


def send_document(file_path: Path, caption: str = ""):
    """상세 리포트(.md 등) 파일 첨부 전송"""
    if not file_path.exists():
        print(f"[스킵] 첨부 파일 없음: {file_path}")
        return

    token, chat_id = _get_credentials()
    url = f"https://api.telegram.org/bot{token}/sendDocument"

    with open(file_path, "rb") as f:
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "caption": caption},
            files={"document": (file_path.name, f)},
            timeout=30,
        )
    resp.raise_for_status()
    print(f"[텔레그램] 파일 첨부 전송 성공: {file_path.name}")


def main():
    parser = argparse.ArgumentParser(description="KR Quant 텔레그램 자동 브리핑 발송")
    parser.add_argument(
        "--file",
        type=str,
        default=str(OUTPUT_DIR / "telegram_brief.txt"),
        help="전송할 텍스트 파일 경로",
    )
    parser.add_argument(
        "--attach-report",
        action="store_true",
        help="상세 해석 리포트(interpretation_report.md) 파일 함께 첨부",
    )
    args = parser.parse_args()

    brief_path = Path(args.file)
    if not brief_path.exists():
        raise FileNotFoundError(f"브리핑 파일이 없습니다: {brief_path} (먼저 analysis/interpreter.py를 실행하세요)")

    text = brief_path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("전송할 브리핑 내용이 비어 있습니다.")

    send_message(text)

    if args.attach_report:
        report_path = OUTPUT_DIR / "interpretation_report.md"
        send_document(report_path, caption="📄 5단계 상세 해석 리포트 & 용어 사전")


if __name__ == "__main__":
    main()