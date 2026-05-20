import os
import time
from pathlib import Path
from typing import Any

import anthropic
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

NAVER_TALK_AUTH_TOKEN = os.getenv("NAVER_TALK_AUTH_TOKEN", "")
NAVER_TALK_API_URL = os.getenv(
    "NAVER_TALK_API_URL", "https://gw.talk.naver.com/chatbot/v1/event"
)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
NOTION_API_TOKEN = os.getenv("NOTION_API_TOKEN", "")
NOTION_TABLE_BLOCK_ID = os.getenv("NOTION_TABLE_BLOCK_ID", "")

DEFAULT_REPLY = "고객님, 문의해주신 내용 확인했습니다.\n해당 내용은 정확한 안내를 위해 담당자 확인이 필요한 부분입니다.\n확인 후 다시 안내드리겠습니다.\n잠시만 기다려 주세요."
FAQ_CACHE_TTL = 300  # 5분

app = FastAPI(title="Naver TalkTalk FAQ Bot")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_ai_client: anthropic.AsyncAnthropic | None = (
    anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
)

_faq_cache: dict[str, str] = {}
_faq_cache_time: float = 0

GREETING = "안녕하세요 케어플리즈입니다.\n\n"
GREETING_TIMEOUT = 86400  # 1일 이상 비활성이면 인사 다시 붙임
_user_last_seen: dict[str, float] = {}


async def fetch_faq_from_notion() -> dict[str, str]:
    global _faq_cache, _faq_cache_time
    now = time.time()
    if _faq_cache and (now - _faq_cache_time) < FAQ_CACHE_TTL:
        return _faq_cache

    if not NOTION_API_TOKEN or not NOTION_TABLE_BLOCK_ID:
        return {}

    headers = {
        "Authorization": f"Bearer {NOTION_API_TOKEN}",
        "Notion-Version": "2022-06-28",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"https://api.notion.com/v1/blocks/{NOTION_TABLE_BLOCK_ID}/children",
            headers=headers,
        )
        resp.raise_for_status()
        rows = resp.json().get("results", [])

    faq: dict[str, str] = {}
    for i, row in enumerate(rows):
        if i == 0:  # 헤더 행 스킵
            continue
        cells = row.get("table_row", {}).get("cells", [])
        if len(cells) < 2:
            continue
        question = "".join(rt.get("plain_text", "") for rt in cells[0]).strip()
        answer = "".join(rt.get("plain_text", "") for rt in cells[1]).strip()
        if question and answer:
            faq[question] = answer

    _faq_cache = faq
    _faq_cache_time = now
    print(f"[notion] FAQ 로드 완료: {len(faq)}개 항목")
    return faq


def find_answer(user_message: str, faq: dict[str, str]) -> str:
    if not user_message:
        return DEFAULT_REPLY
    if user_message in faq:
        return faq[user_message]
    for keyword, answer in faq.items():
        if keyword in user_message:
            return answer
    return DEFAULT_REPLY


async def find_answer_with_ai(user_message: str, faq: dict[str, str]) -> str:
    if not _ai_client or not user_message:
        return find_answer(user_message, faq)

    faq_text = "\n".join(f"Q: {k}\nA: {v}" for k, v in faq.items())
    system_prompt = (
        "당신은 케어플리즈 고객센터의 친절하고 전문적인 CX(고객 경험) 상담사입니다. "
        "정중하고 따뜻한 어조를 유지하며, 아래 FAQ를 참고하여 고객 질문에 명확하고 직관적인 정보를 제공하세요.\n\n"

        "# 말투 및 스타일 지침\n"
        "1. 호칭: 고객을 부를 때 반드시 '고객님'으로 호칭한다.\n"
        "2. 첫 문장: 답변의 첫 문장은 '고객님,'으로 시작하며 문의한 핵심 내용을 짧게 언급한다. "
        "   (예: '고객님, 상품 등록 방법을 문의해 주셨군요.') "
        "   단, '안녕하세요' 등 인사말은 절대 포함하지 않는다. 인사는 이미 별도로 전달되기 때문이다.\n"
        "3. 말투: 항상 해요체(~요, ~죠, ~가요)를 사용한다. 다나까체(~입니다, ~습니까)는 지양한다.\n"
        "4. 줄바꿈: 문단은 2~3줄씩 짧게 끊고, 문단 사이에 빈 줄을 넣어 모바일에서 읽기 쉽게 구성한다.\n"
        "5. 강조: 버튼이나 메뉴명은 대괄호([])로 감싸 시각적으로 강조한다. (예: [상품 등록] 버튼)\n"
        "6. 이모지: 링크나 중요 안내 앞에는 👉를 배치하고, 답변 마지막 줄에는 따뜻한 마무리와 함께 😊를 붙인다.\n"
        "7. 추가 팁: 고객이 놓칠 수 있는 유용한 정보는 문단 앞에 '참고로, '를 붙여 친절하게 덧붙인다.\n"
        "8. 마크다운 서식(**굵게**, *기울임*, # 제목 등)은 절대 사용하지 않는다. 일반 텍스트와 이모지만 사용한다.\n\n"

        "# FAQ에 없는 질문\n"
        "FAQ에 없는 내용이라면 정확히 이렇게만 답변하세요: "
        "\"고객님, 문의해주신 내용 확인했습니다.\n해당 내용은 정확한 안내를 위해 담당자 확인이 필요한 부분입니다.\n확인 후 다시 안내드리겠습니다.\n잠시만 기다려 주세요.\"\n\n"

        f"=== FAQ ===\n{faq_text}"
    )

    try:
        resp = await _ai_client.messages.create(
            model="claude-opus-4-7",
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )
        answer = resp.content[0].text.strip()
        cached = getattr(resp.usage, "cache_read_input_tokens", 0)
        print(f"[ai] input_tokens={resp.usage.input_tokens} cached={cached}")
        return answer
    except Exception as e:
        print(f"[ai-error] {e!r} - fallback to keyword match")
        return find_answer(user_message, faq)


@app.get("/")
def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/faq")
async def get_faq() -> dict[str, str]:
    return await fetch_faq_from_notion()


@app.post("/faq/refresh")
async def refresh_faq() -> dict[str, Any]:
    global _faq_cache_time
    _faq_cache_time = 0  # 캐시 만료 강제
    faq = await fetch_faq_from_notion()
    return {"success": True, "count": len(faq)}


async def send_naver_reply(user_key: str, text: str) -> None:
    if not NAVER_TALK_AUTH_TOKEN:
        print("[warn] NAVER_TALK_AUTH_TOKEN not set; skipping outbound call")
        return
    payload = {
        "event": "send",
        "user": user_key,
        "textContent": {"text": text},
    }
    headers = {
        "Authorization": NAVER_TALK_AUTH_TOKEN,
        "Content-Type": "application/json;charset=UTF-8",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(NAVER_TALK_API_URL, headers=headers, json=payload)
        print(f"[naver-reply] status={resp.status_code} body={resp.text}")


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    body = await request.json()
    print(f"[webhook] received event={body.get('event')} user={body.get('user', '')[:8]}...")

    event = body.get("event")
    user_key = body.get("user", "")

    if event == "send":
        text_content = body.get("textContent") or {}
        user_message = text_content.get("text", "").strip()
        faq = await fetch_faq_from_notion()
        reply = await find_answer_with_ai(user_message, faq)

        now = time.time()
        last_seen = _user_last_seen.get(user_key, 0)
        if now - last_seen > GREETING_TIMEOUT:
            reply = GREETING + reply
        _user_last_seen[user_key] = now

        print(f"[webhook] reply sent ({len(reply)} chars)")
        await send_naver_reply(user_key, reply)
        return JSONResponse({"ok": True})

    return JSONResponse({"ok": True})


@app.post("/test-reply")
async def test_reply(request: Request) -> dict[str, str]:
    body = await request.json()
    user_message = body.get("text", "")
    faq = await fetch_faq_from_notion()
    reply = await find_answer_with_ai(user_message, faq)
    # 테스트는 항상 첫 메시지로 간주해 인사 붙임
    return {"reply": GREETING + reply}
