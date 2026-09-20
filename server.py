"""
BRAC Bengali Voice Assistant — web server with retrieval (RAG), admin
settings, and end-user login/register.

Same audio bridge as always (browser mic -> WebSocket -> Gemini Live ->
browser speaker), plus:
  - a retrieval tool the model calls before answering factual questions
  - a settings.json-backed admin panel (voice, retrieval, interface prefs)
  - an endpoint to add/replace the Gemini API key from the admin panel
  - a JSON-file user database (users.json) gating access to the assistant
    itself behind login/register — separate from the admin panel, which
    keeps its own hardcoded credentials

Runs on port 6001.

IMPORTANT: routes that serve different HTML for the same URL depending on
session state (/, /admin, /login, /register) must NEVER be served via
FileResponse — Starlette's FileResponse sets a Last-Modified header from
the file's mtime but no Cache-Control, and browsers apply heuristic
caching keyed only by URL. That lets a browser silently replay a stale
cached body for a URL whose *correct* content has since changed (e.g. the
admin panel after logout), which is what caused the "stuck reloading"
loop: a cached, stale, already-logged-out admin.html kept 401'ing against
/api/admin/settings and redirecting back to /admin, which the browser
then re-served from its own cache without ever asking the server. Every
such route below goes through _serve_html(), which sends explicit
Cache-Control: no-store.
"""

import asyncio
import hmac
import json
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dotenv import load_dotenv
from google import genai
from google.genai import types

import users_store
from rag import KnowledgeBase
from settings_store import (
    load_settings,
    save_settings,
    read_api_key,
    write_api_key,
    masked_api_key,
    VALID_VOICES,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
KNOWLEDGE_DIR = BASE_DIR / "knowledge"

load_dotenv(BASE_DIR / ".env")

MODEL = "gemini-3.1-flash-live-preview"


def _serve_html(filename: str) -> HTMLResponse:
    """Read a static HTML file fresh and serve it with no-store caching.

    See the module docstring — this is the fix for the stuck-reloading bug.
    Files here are small (a few KB), so reading on every request is cheap.
    """
    content = (STATIC_DIR / filename).read_text(encoding="utf-8")
    return HTMLResponse(
        content,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ---------------------------------------------------------------------------
# admin session (hardcoded credentials — unchanged from before)
# ---------------------------------------------------------------------------
ADMIN_USER = "admin"
ADMIN_PASS = "ilovebracmf"
ADMIN_COOKIE = "brac_admin_session"
_admin_sessions: dict = {}
ADMIN_SESSION_TTL = 60 * 60 * 8  # 8 hours


def _issue_admin_session() -> str:
    token = secrets.token_urlsafe(32)
    _admin_sessions[token] = time.time()
    return token


def _admin_session_valid(token) -> bool:
    if not token or token not in _admin_sessions:
        return False
    if time.time() - _admin_sessions[token] > ADMIN_SESSION_TTL:
        del _admin_sessions[token]
        return False
    return True


# ---------------------------------------------------------------------------
# end-user session (JSON-backed accounts via users_store)
# ---------------------------------------------------------------------------
USER_COOKIE = "brac_user_session"
_user_sessions: dict = {}  # token -> {"username": str, "issued_at": float}
USER_SESSION_TTL = 60 * 60 * 24 * 30  # 30 days — end users shouldn't re-login often


def _issue_user_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    _user_sessions[token] = {"username": username, "issued_at": time.time()}
    return token


def _user_session_username(token):
    """Return the logged-in username for a session token, or None."""
    record = _user_sessions.get(token)
    if not record:
        return None
    if time.time() - record["issued_at"] > USER_SESSION_TTL:
        del _user_sessions[token]
        return None
    return record["username"]


BASE_SYSTEM_INSTRUCTION = (
    "You are a helpful, polite, and friendly voice assistant for BRAC "
    "(ব্র্যাক). Respond exclusively in short, natural Bengali sentences. "
    "By ব্র্যাক, we mean BRAC. Do not use English words or special "
    "characters like * in your response, unless the user speaks in English.\n\n"
    "GROUNDING RULES — these override everything else:\n"
    "1. You have no knowledge of your own. Every factual statement you make must "
    "come from a passage returned by the search_knowledge_base tool in this "
    "conversation.\n"
    "2. Before answering any question about facts, policies, products, amounts, "
    "eligibility, procedures or contacts, you MUST call search_knowledge_base "
    "first. Never answer such a question from memory, even if you are confident.\n"
    "3. Read the returned passages and use only what actually answers the "
    "question. Do not add details, numbers, examples or context that are not in "
    "the passages.\n"
    "4. If the passages do not contain the answer, or the tool returns nothing, "
    "say clearly in Bengali that this information is not in your documents — for "
    "example: 'দুঃখিত, এই বিষয়ে আমার কাছে তথ্য নেই।' Then offer to answer "
    "something else. Never guess and never fill the gap from general knowledge.\n"
    "5. If a question is only partly covered, answer the covered part and say "
    "plainly which part you do not have.\n"
    "6. Greetings, thanks and small talk need no tool call — answer briefly.\n"
    "7. Do not read out file names, scores or the word 'passage'. Just speak the "
    "answer naturally.\n"
)

# --- knowledge base --------------------------------------------------------
_embed_client = genai.Client(api_key=read_api_key()) if read_api_key() else None

print("[rag] indexing knowledge/ ...")
kb = KnowledgeBase(KNOWLEDGE_DIR, client=_embed_client)
print(f"[rag] {kb.status}")

SEARCH_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="search_knowledge_base",
            description=(
                "Search BRAC's approved documents for passages that answer the "
                "user's question. Call this before answering ANY question about "
                "facts, policies, products, procedures, amounts, eligibility or "
                "contact details. Returns passages with a relevance score, or an "
                "empty list if nothing matches."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "query": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "The search phrase, in the same language the user "
                            "spoke. Use the key nouns from the question rather "
                            "than the whole sentence."
                        ),
                    )
                },
                required=["query"],
            ),
        )
    ]
)


def build_live_config(settings: dict) -> types.LiveConnectConfig:
    """Build the Live session config from current settings.json values.

    Called once per new WebSocket session, so a change made in the admin
    panel takes effect the next time someone presses the talk button —
    never mid-call, since an open Live session can't have its config swapped.
    """
    system_instruction = (
        BASE_SYSTEM_INSTRUCTION
        + f"\nTopics currently in your documents: {kb.topic_index()}"
    )
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(parts=[types.Part(text=system_instruction)]),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=settings.get("voice", "Kore")
                )
            ),
            language_code="bn-BD",
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        tools=[SEARCH_TOOL],
    )


app = FastAPI(title="BRAC Bengali Voice Assistant")


# ---------------------------------------------------------------------------
# main app — now gated behind end-user login
# ---------------------------------------------------------------------------
@app.get("/")
async def index(request: Request):
    username = _user_session_username(request.cookies.get(USER_COOKIE))
    if not username:
        return _serve_html("login.html")
    return _serve_html("index.html")


@app.get("/login")
async def login_page(request: Request):
    if _user_session_username(request.cookies.get(USER_COOKIE)):
        return _serve_html("index.html")
    return _serve_html("login.html")


@app.get("/register")
async def register_page(request: Request):
    if _user_session_username(request.cookies.get(USER_COOKIE)):
        return _serve_html("index.html")
    return _serve_html("register.html")


@app.post("/api/auth/register")
async def auth_register(request: Request):
    body = await request.json()
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))

    try:
        users_store.create_user(username, password)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    token = _issue_user_session(username)
    resp = JSONResponse({"success": True})
    resp.set_cookie(
        USER_COOKIE, token,
        max_age=USER_SESSION_TTL, httponly=True, samesite="lax", path="/",
    )
    return resp


@app.post("/api/auth/login")
async def auth_login(request: Request):
    body = await request.json()
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))

    if not users_store.verify_user(username, password):
        return JSONResponse({"error": "ভুল ইউজারনেম অথবা পাসওয়ার্ড"}, status_code=401)

    token = _issue_user_session(username)
    resp = JSONResponse({"success": True})
    resp.set_cookie(
        USER_COOKIE, token,
        max_age=USER_SESSION_TTL, httponly=True, samesite="lax", path="/",
    )
    return resp


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.cookies.get(USER_COOKIE)
    if token in _user_sessions:
        del _user_sessions[token]
    resp = JSONResponse({"success": True})
    resp.delete_cookie(USER_COOKIE, path="/")
    return resp


@app.get("/api/knowledge")
async def knowledge_info():
    return JSONResponse(kb.info())


@app.post("/api/knowledge/reload")
async def knowledge_reload():
    """Re-index knowledge/ after you edit or add a file, without restarting."""
    kb.load()
    print(f"[rag] reloaded: {kb.status}")
    return JSONResponse(kb.info())


@app.get("/api/settings")
async def get_settings():
    """Public read of the behavior/interface settings the frontend applies."""
    return JSONResponse(load_settings())


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# admin panel — login-gated, separate credentials from end users
# ---------------------------------------------------------------------------
@app.get("/admin")
async def admin_page(request: Request):
    token = request.cookies.get(ADMIN_COOKIE)
    if _admin_session_valid(token):
        return _serve_html("admin.html")
    return _serve_html("admin_login.html")


@app.post("/api/admin/login")
async def admin_login(request: Request):
    body = await request.json()
    username = str(body.get("username", ""))
    password = str(body.get("password", ""))

    ok = hmac.compare_digest(username, ADMIN_USER) and hmac.compare_digest(password, ADMIN_PASS)
    if not ok:
        return JSONResponse({"error": "ভুল ইউজারনেম অথবা পাসওয়ার্ড"}, status_code=401)

    token = _issue_admin_session()
    resp = JSONResponse({"success": True})
    resp.set_cookie(
        ADMIN_COOKIE, token,
        max_age=ADMIN_SESSION_TTL, httponly=True, samesite="lax", path="/",
    )
    return resp


@app.post("/api/admin/logout")
async def admin_logout(request: Request):
    token = request.cookies.get(ADMIN_COOKIE)
    if token in _admin_sessions:
        del _admin_sessions[token]
    resp = JSONResponse({"success": True})
    resp.delete_cookie(ADMIN_COOKIE, path="/")
    return resp


def _require_admin(request: Request) -> bool:
    return _admin_session_valid(request.cookies.get(ADMIN_COOKIE))


@app.get("/api/admin/settings")
async def admin_get_settings(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    s = load_settings()
    s["_valid_voices"] = VALID_VOICES
    s["_api_key_masked"] = masked_api_key()
    s["_knowledge"] = kb.info()
    return JSONResponse(s)


@app.post("/api/admin/settings")
async def admin_save_settings(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    patch = await request.json()
    patch.pop("api_key", None)
    saved = save_settings(patch)
    return JSONResponse({"success": True, "settings": saved})


@app.post("/api/admin/apikey")
async def admin_set_api_key(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    new_key = str(body.get("api_key", "")).strip()
    if not new_key:
        return JSONResponse({"error": "চাবি ফাঁকা রাখা যাবে না"}, status_code=400)

    try:
        write_api_key(new_key)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    global _embed_client
    _embed_client = genai.Client(api_key=new_key)
    kb.client = _embed_client
    kb.load()

    return JSONResponse({
        "success": True,
        "masked": masked_api_key(),
        "knowledge": kb.info(),
    })


@app.post("/api/admin/knowledge/reload")
async def admin_reload_knowledge(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    kb.load()
    return JSONResponse(kb.info())


@app.delete("/api/admin/knowledge/{filename}")
async def admin_delete_knowledge_file(filename: str, request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Strip any directory components the client might send, then confirm
    # the resolved path is still strictly inside knowledge/ before deleting
    # — defense against a filename like '../../server.py'.
    safe_name = Path(filename).name
    target = (KNOWLEDGE_DIR / safe_name).resolve()
    if target.parent != KNOWLEDGE_DIR.resolve():
        return JSONResponse({"error": "অবৈধ ফাইলের নাম"}, status_code=400)

    if not target.exists() or target.suffix.lower() not in {".md", ".txt", ".markdown"}:
        return JSONResponse({"error": "ফাইল পাওয়া যায়নি"}, status_code=404)

    try:
        target.unlink()
    except OSError as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    kb.load()
    print(f"[admin] deleted {safe_name}; {kb.status}")
    return JSONResponse(kb.info())


# ---------------------------------------------------------------------------
# voice session — requires a logged-in end user
# ---------------------------------------------------------------------------
@app.websocket("/ws/audio")
async def audio_bridge(ws: WebSocket):
    await ws.accept()

    username = _user_session_username(ws.cookies.get(USER_COOKIE))
    if not username:
        await ws.send_text(json.dumps({
            "type": "error",
            "message": "অনুগ্রহ করে প্রথমে লগইন করুন।",
        }))
        await ws.close()
        return

    key = read_api_key()
    if not key:
        await ws.send_text(json.dumps({
            "type": "error",
            "message": "GEMINI_API_KEY is not set. Add one from the admin panel.",
        }))
        await ws.close()
        return

    settings = load_settings()
    client = genai.Client(api_key=key)
    config = build_live_config(settings)
    top_k = settings.get("rag_top_k", 4)

    try:
        async with client.aio.live.connect(model=MODEL, config=config) as session:
            await ws.send_text(json.dumps({
                "type": "ready",
                "knowledge": kb.info(),
                "settings": settings,
            }))

            async def browser_to_gemini():
                """
                Forward mic audio frames AND typed text messages from the
                browser to Gemini.

                A binary frame is a mic audio chunk (unchanged). A text
                frame is a JSON control message — currently only
                {"type": "text_input", "text": "..."} — sent as a discrete
                turn via send_client_content, exactly as if the same words
                had been spoken. Gemini answers in whichever modality the
                session was configured for (audio here), so a typed
                question still gets a spoken reply, transcribed like any
                other turn.
                """
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))

                    if message.get("bytes") is not None:
                        await session.send_realtime_input(
                            audio=types.Blob(data=message["bytes"], mime_type="audio/pcm;rate=16000")
                        )
                        continue

                    text_frame = message.get("text")
                    if text_frame is None:
                        continue
                    try:
                        payload = json.loads(text_frame)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("type") == "text_input":
                        typed = str(payload.get("text", "")).strip()
                        if typed:
                            await session.send_client_content(
                                turns=types.Content(role="user", parts=[types.Part(text=typed)]),
                                turn_complete=True,
                            )

            async def handle_tool_call(tool_call):
                """Run retrieval for each requested search and reply to Gemini."""
                replies = []
                for fc in tool_call.function_calls:
                    args = dict(fc.args or {})
                    query = str(args.get("query", "")).strip()

                    if fc.name != "search_knowledge_base":
                        replies.append(types.FunctionResponse(
                            id=fc.id, name=fc.name,
                            response={"error": "unknown tool"},
                        ))
                        continue

                    hits = await asyncio.to_thread(kb.search, query, top_k)
                    payload = {
                        "found": bool(hits),
                        "passages": [
                            {
                                "source": h["source"],
                                "section": h["section"],
                                "content": h["text"],
                                "relevance": h["score"],
                            }
                            for h in hits
                        ],
                        "instruction": (
                            "Answer only from these passages. If they do not "
                            "contain the answer, tell the user you do not have "
                            "that information."
                        ),
                    }
                    replies.append(types.FunctionResponse(
                        id=fc.id, name=fc.name, response=payload
                    ))

                    if settings.get("show_retrieval_logs", True):
                        await ws.send_text(json.dumps({
                            "type": "retrieval",
                            "query": query,
                            "found": bool(hits),
                            "sources": [
                                {
                                    "source": h["source"],
                                    "section": h["section"],
                                    "score": h["score"],
                                    "preview": h["text"][:180],
                                }
                                for h in hits
                            ],
                        }, ensure_ascii=False))

                if replies:
                    await session.send_tool_response(function_responses=replies)

            async def gemini_to_browser():
                """Forward Gemini's audio, transcripts and tool calls."""
                while True:
                    turn = session.receive()
                    async for response in turn:
                        if getattr(response, "tool_call", None):
                            await handle_tool_call(response.tool_call)
                            continue

                        server_content = response.server_content
                        if server_content is None:
                            continue

                        if server_content.model_turn:
                            for part in server_content.model_turn.parts:
                                if part.inline_data and part.inline_data.data:
                                    await ws.send_bytes(part.inline_data.data)

                        if settings.get("show_transcription", True):
                            it = server_content.input_transcription
                            if it and it.text and not settings.get("show_only_assistant", False):
                                await ws.send_text(json.dumps({
                                    "type": "input_transcript", "text": it.text,
                                }, ensure_ascii=False))

                            ot = server_content.output_transcription
                            if ot and ot.text:
                                await ws.send_text(json.dumps({
                                    "type": "output_transcript", "text": ot.text,
                                }, ensure_ascii=False))

                        if server_content.interrupted:
                            await ws.send_text(json.dumps({"type": "interrupted"}))

                        if server_content.turn_complete:
                            await ws.send_text(json.dumps({"type": "turn_complete"}))

            async with asyncio.TaskGroup() as tg:
                tg.create_task(browser_to_gemini())
                tg.create_task(gemini_to_browser())

    except WebSocketDisconnect:
        pass
    except ExceptionGroup as eg:
        for exc in eg.exceptions:
            if isinstance(exc, WebSocketDisconnect):
                continue
            try:
                await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
            except Exception:
                pass
    except Exception as exc:
        try:
            await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=6001, reload=False)