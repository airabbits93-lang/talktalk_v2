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

DEFAULT_REPLY = "죄송합니다. 해당 문의는 등록되어 있지 않습니다.\n다른 키워드로 다시 문의해주세요."
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
        "당신은 쇼핑몰 고객센터 FAQ 챗봇입니다. "
        "아래 FAQ를 참고하여 고객 질문에 한국어로 친절하고 간결하게 답변하세요. "
        "답변에 **굵게**, *기울임*, # 제목 등 마크다운 서식을 절대 사용하지 마세요. 일반 텍스트로만 작성하세요. "
        "좀 더 자연스럽게 사람이 친절히 말하듯이 답변하세요. "
        "FAQ에 없는 내용이라면 정확히 이렇게만 답변하세요: "
        "\"죄송합니다. 해당 문의는 등록되어 있지 않습니다. 다른 키워드로 다시 문의해주세요.\"\n\n"
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
        print(f"[webhook] reply sent ({len(reply)} chars)")
        await send_naver_reply(user_key, reply)
        return JSONResponse({"ok": True})

    return JSONResponse({"ok": True})


@app.post("/test-reply")
async def test_reply(request: Request) -> dict[str, str]:
    body = await request.json()
    user_message = body.get("text", "")
    faq = await fetch_faq_from_notion()
    return {"reply": await find_answer_with_ai(user_message, faq)}
