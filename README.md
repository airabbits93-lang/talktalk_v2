# Naver TalkTalk FAQ Bot

JSON 파일 기반 네이버 톡톡 FAQ 챗봇.

## 구조

```
backend/
├── main.py            FastAPI 서버 (웹훅 + FAQ CRUD + 관리자 페이지)
├── faq.json           FAQ 데이터 (키워드: 답변)
├── static/
│   └── admin.html     단일 HTML 관리자 페이지
├── requirements.txt
├── .env.example       NAVER 토큰 템플릿
└── venv/              Python 가상환경
```

## 실행

### 1. venv 활성화

PowerShell:
```powershell
.\venv\Scripts\Activate.ps1
```

cmd:
```cmd
venv\Scripts\activate.bat
```

### 2. 패키지 설치 (최초 1회)

```powershell
pip install -r requirements.txt
```

### 3. 환경변수 설정

`.env.example`을 `.env`로 복사 후 토큰 입력:
```
NAVER_TALK_AUTH_TOKEN=...
```

> 토큰은 네이버 톡톡 파트너센터 > 챗봇 API 발급 후 입력.
> 미입력 상태로도 서버는 동작하며, 관리자 페이지/응답 테스트는 정상 작동.

### 4. 서버 실행

```powershell
uvicorn main:app --reload
```

- 관리자 페이지: http://localhost:8000/
- FAQ 조회: GET http://localhost:8000/faq
- FAQ 저장: POST http://localhost:8000/faq
- 응답 테스트: POST http://localhost:8000/test-reply
- 톡톡 웹훅: POST http://localhost:8000/webhook

## 네이버 톡톡 연결

### 1. ngrok 으로 외부 노출

```powershell
ngrok http 8000
```

발급된 주소(예: `https://abc123.ngrok-free.app`) 확인.

### 2. 파트너센터에 웹훅 등록

- 네이버 톡톡 파트너센터 > 챗봇 설정
- Webhook URL: `https://abc123.ngrok-free.app/webhook`
- 인증/토큰 발급 후 `.env`의 `NAVER_TALK_AUTH_TOKEN`에 저장

### 3. 동작 확인

톡톡에서 메시지 전송 시 콘솔에 다음과 같이 로그 출력:
```
[webhook] received: {"event":"send","user":"...","textContent":{"text":"배송"}}
[webhook] user='배송' -> reply='배송은 평균 1~2일 소요됩니다.'
[naver-reply] status=200 body=...
```

## 응답 매칭 로직

`main.py` 의 `find_answer()`:
1. 사용자 메시지가 FAQ 키와 **정확히 일치**하면 그 답변 반환
2. 키워드가 사용자 메시지에 **포함**되면 그 답변 반환 (예: "배송 언제와요?" → "배송")
3. 매칭 실패 시 `DEFAULT_REPLY` 반환

## 주의: 실제 네이버 페이로드 확인

처음 연결 시 `[webhook] received: ...` 로그로 실제 네이버가 보내는 JSON 구조를 확인하세요.
구조가 다르면 `webhook()` 함수의 `body.get("event")`, `textContent.get("text")` 부분을 조정합니다.
