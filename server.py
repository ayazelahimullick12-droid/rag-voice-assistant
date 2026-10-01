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
    has its own credentials (ADMIN_USERNAME / ADMIN_PASSWORD)
  - document upload (/upload, admin only), which turns PDFs, images and
    Word files into markdown in knowledge/

Runs on port 6001 locally, or on $PORT when a host (e.g. Render) sets one.
When DATABASE_URL is set, data files are also kept in Postgres so they
survive hosts that wipe the disk on restart — see storage.py.

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
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dotenv import load_dotenv
from google import genai
from google.genai import types

import storage
import users_store
from document_processor import DocumentProcessor
from rag import KnowledgeBase
from settings_store import (
    load_settings,
    save_settings,
    read_api_key,
    write_api_key,
    masked_api_key,
    keys_saved_permanently,
    live_model_choices,
    read_upload_api_key,
    write_upload_api_key,
    masked_upload_api_key,
    upload_key_is_dedicated,
    VALID_VOICES,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
KNOWLEDGE_DIR = BASE_DIR / "knowledge"

# Log lines include Bengali file names. On a console that isn't UTF-8
# (Windows with output redirected) print() would raise mid-request —
# escape what can't be shown instead.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="backslashreplace")

load_dotenv(BASE_DIR / ".env")

# Must run before anything reads users.json, settings.json or knowledge/.
storage.restore()



# Browsers cache /static/*.css and *.js on their own schedule, so after a
# deploy a phone can keep running old CSS/JS against new HTML. Every page
# links its assets as "/static/x.css?v=<hash of all css/js>": the URL
# changes exactly when a file changes, which forces a fresh download.
_ASSET_URL_RE = re.compile(r"/static/[\w.-]+\.(?:css|js)\b")


def _asset_version() -> str:
    digest = hashlib.sha1()
    for path in sorted(STATIC_DIR.glob("*")):
        if path.suffix in (".css", ".js"):
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def _serve_html(filename: str) -> HTMLResponse:
    """Read a static HTML file fresh and serve it with no-store caching.

    See the module docstring — this is the fix for the stuck-reloading bug.
    Files here are small (a few KB), so reading on every request is cheap.
    """
    content = (STATIC_DIR / filename).read_text(encoding="utf-8")
    content = _ASSET_URL_RE.sub(lambda m: f"{m.group(0)}?v={_asset_version()}", content)
    return HTMLResponse(
        content,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ---------------------------------------------------------------------------
# sessions — signed cookies instead of a server-side session table
#
# A token is "<base64 json [kind, username, issued_at]>.<hmac>", signed with
# SESSION_SECRET. Nothing is kept in memory, so a restart (which on Render's
# free tier happens every time the service wakes from sleep) doesn't log
# anyone out. The trade-off: logout only deletes the cookie; it can't revoke
# a copied token before it expires. Admin tokens are also keyed on the admin
# password, so changing ADMIN_PASSWORD logs every admin out.
#
# Without SESSION_SECRET a random one is made per process — fine locally,
# but then every restart logs everyone out.
# ---------------------------------------------------------------------------
SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip() or secrets.token_urlsafe(32)

ADMIN_USER = os.environ.get("ADMIN_USERNAME", "").strip() or "admin"
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASS:
    print("[admin] ADMIN_PASSWORD is not set - the admin panel is locked until you set it")

ADMIN_COOKIE = "brac_admin_session"
ADMIN_SESSION_TTL = 60 * 60 * 8  # 8 hours
USER_COOKIE = "brac_user_session"
USER_SESSION_TTL = 60 * 60 * 24 * 30  # 30 days — end users shouldn't re-login often

# When set, new accounts need this code — share it only with people who
# should be able to use the assistant (and your Gemini quota).
REGISTRATION_CODE = os.environ.get("REGISTRATION_CODE", "").strip()


def _signing_key(kind: str) -> bytes:
    extra = ADMIN_PASS if kind == "admin" else ""
    return hashlib.sha256(f"{SESSION_SECRET}|{kind}|{extra}".encode("utf-8")).digest()


def _make_token(kind: str, username: str) -> str:
    body = json.dumps([kind, username, int(time.time())], separators=(",", ":"))
    payload = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii").rstrip("=")
    sig = hmac.new(_signing_key(kind), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _read_token(token, kind: str, ttl: int):
    """Return the username in a valid, unexpired token of this kind, else None."""
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    expected = hmac.new(_signing_key(kind), payload.encode("ascii", "ignore"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        padded = payload + "=" * (-len(payload) % 4)
        token_kind, username, issued_at = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return None
    if token_kind != kind or time.time() - issued_at > ttl:
        return None
    return username


def _set_session_cookie(resp, request: Request, name: str, token: str, max_age: int) -> None:
    resp.set_cookie(
        name, token,
        max_age=max_age, httponly=True, samesite="lax", path="/",
        secure=request.url.scheme == "https",
    )


def _issue_admin_session() -> str:
    return _make_token("admin", ADMIN_USER)


def _admin_session_valid(token) -> bool:
    return bool(ADMIN_PASS) and _read_token(token, "admin", ADMIN_SESSION_TTL) is not None


def _issue_user_session(username: str) -> str:
    return _make_token("user", username)


def _user_session_username(token):
    """Return the logged-in username for a session token, or None."""
    return _read_token(token, "user", USER_SESSION_TTL)


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
def _make_embed_client(api_key: str) -> genai.Client:
    """Client for embedding calls, with a timeout (milliseconds).

    Without one, a stalled connection would hang startup — and a host's
    health check with it — or a search, forever. When a call fails, rag.py
    falls back to keyword-only search.
    """
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=30_000))


_embed_client = _make_embed_client(read_api_key()) if read_api_key() else None

# --- Gemini Live client, shared by every voice session ---------------------
# Constructing a genai.Client costs well over a second of CPU (TLS setup).
# Doing that for every conversation meant, on a host with a fraction of a
# CPU, many seconds of "connecting…" each time — and it starved the audio
# of anyone already mid-conversation. One client is built per API key and
# reused; each session still gets its own connection from it.
_live_client = None
_live_client_key = ""
_live_client_lock = threading.Lock()


def _get_live_client(api_key: str) -> genai.Client:
    global _live_client, _live_client_key
    with _live_client_lock:
        if _live_client is None or api_key != _live_client_key:
            _live_client = genai.Client(api_key=api_key)
            _live_client_key = api_key
        return _live_client


if read_api_key():
    # Built in the background so the first conversation doesn't pay for it
    # and startup (the host's health check) isn't held up.
    threading.Thread(target=_get_live_client, args=(read_api_key(),), daemon=True).start()

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


@app.get("/api/auth/config")
async def auth_config():
    """What the register page needs to know before showing its form."""
    return JSONResponse({"registration_code_required": bool(REGISTRATION_CODE)})


@app.post("/api/auth/register")
async def auth_register(request: Request):
    body = await request.json()
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    code = str(body.get("code", "")).strip()

    if REGISTRATION_CODE and not hmac.compare_digest(code.encode("utf-8"), REGISTRATION_CODE.encode("utf-8")):
        return JSONResponse({"error": "আমন্ত্রণ কোড সঠিক নয়"}, status_code=403)

    try:
        # PBKDF2 hashing is deliberately slow — keep it off the event loop
        # so it can't stall audio streaming for people mid-conversation.
        await asyncio.to_thread(users_store.create_user, username, password)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    resp = JSONResponse({"success": True})
    _set_session_cookie(resp, request, USER_COOKIE, _issue_user_session(username), USER_SESSION_TTL)
    return resp


@app.post("/api/auth/login")
async def auth_login(request: Request):
    body = await request.json()
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))

    if not await asyncio.to_thread(users_store.verify_user, username, password):
        return JSONResponse({"error": "ভুল ইউজারনেম অথবা পাসওয়ার্ড"}, status_code=401)

    resp = JSONResponse({"success": True})
    _set_session_cookie(resp, request, USER_COOKIE, _issue_user_session(username), USER_SESSION_TTL)
    return resp


@app.post("/api/auth/logout")
async def auth_logout():
    resp = JSONResponse({"success": True})
    resp.delete_cookie(USER_COOKIE, path="/")
    return resp


@app.get("/healthz")
async def healthz():
    """Health check for the host (Render pings this to know the app is up)."""
    return JSONResponse({"ok": True})


@app.get("/api/knowledge")
async def knowledge_info(request: Request):
    # File names can be sensitive, so only signed-in users and admins see them.
    if not (_user_session_username(request.cookies.get(USER_COOKIE)) or _require_admin(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
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

    if not ADMIN_PASS:
        return JSONResponse(
            {"error": "ADMIN_PASSWORD সেট করা নেই — সার্ভারের environment-এ যোগ করুন"},
            status_code=503,
        )

    ok = (
        hmac.compare_digest(username.encode("utf-8"), ADMIN_USER.encode("utf-8"))
        and hmac.compare_digest(password.encode("utf-8"), ADMIN_PASS.encode("utf-8"))
    )
    if not ok:
        return JSONResponse({"error": "ভুল ইউজারনেম অথবা পাসওয়ার্ড"}, status_code=401)

    resp = JSONResponse({"success": True})
    _set_session_cookie(resp, request, ADMIN_COOKIE, _issue_admin_session(), ADMIN_SESSION_TTL)
    return resp


@app.post("/api/admin/logout")
async def admin_logout():
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
    s["_valid_live_models"] = live_model_choices()
    s["_api_key_masked"] = masked_api_key()
    s["_api_key_temporary"] = not keys_saved_permanently()
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

    # Building clients is CPU-heavy — keep it off the event loop so audio
    # for people mid-conversation keeps flowing.
    global _embed_client
    _embed_client = await asyncio.to_thread(_make_embed_client, new_key)
    kb.client = _embed_client
    await asyncio.to_thread(kb.load)
    await asyncio.to_thread(_get_live_client, new_key)

    return JSONResponse({
        "success": True,
        "masked": masked_api_key(),
        "knowledge": kb.info(),
    })


@app.post("/api/admin/knowledge/reload")
async def admin_reload_knowledge(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    await asyncio.to_thread(kb.load)
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
    storage.remove(target)

    await asyncio.to_thread(kb.load)
    print(f"[admin] deleted {safe_name}; {kb.status}")
    return JSONResponse(kb.info())


# ---------------------------------------------------------------------------
# document upload — admin only. Used to be a separate app on port 6501;
# it lives here now because a host gives each app a single public port.
# ---------------------------------------------------------------------------
@app.get("/upload")
async def upload_page(request: Request):
    if _require_admin(request):
        return _serve_html("upload.html")
    return _serve_html("admin_login.html")


@app.post("/api/admin/upload")
async def admin_upload(request: Request, file: UploadFile = File(...)):
    """Turn an uploaded document into markdown in knowledge/, then re-index."""
    if not _require_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    api_key = read_upload_api_key()
    if not api_key:
        return JSONResponse({"error": "GEMINI_API_KEY not set"}, status_code=500)

    processor = DocumentProcessor(api_key=api_key)
    original_name = file.filename or "document"

    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(original_name).suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)

    try:
        markdown, suggested_name = await processor.process_upload(tmp_path, original_name)
        output_path = KNOWLEDGE_DIR / suggested_name
        output_path.write_text(markdown, encoding="utf-8")
        storage.save(output_path)
        print(f"[upload] {original_name} → {suggested_name} ({len(markdown)} chars)")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        print(f"[upload error] {original_name}: {e}")
        return JSONResponse({"error": f"Processing failed: {e}"}, status_code=500)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    await asyncio.to_thread(kb.load)
    return JSONResponse({
        "success": True,
        "filename": suggested_name,
        "size": len(markdown),
        "knowledge": kb.info(),
    })


@app.get("/api/admin/upload-key")
async def admin_get_upload_key(request: Request):
    """Masked view of the key document processing uses — never the full key."""
    if not _require_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({
        "masked": masked_upload_api_key(),
        "dedicated": upload_key_is_dedicated(),
        "temporary": not keys_saved_permanently(),
    })


@app.post("/api/admin/upload-key")
async def admin_set_upload_key(request: Request):
    """Set a document-processing key separate from the voice assistant's."""
    if not _require_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    new_key = str(body.get("api_key", "")).strip()
    if not new_key:
        return JSONResponse({"error": "চাবি ফাঁকা রাখা যাবে না"}, status_code=400)
    try:
        write_upload_api_key(new_key)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"success": True, "masked": masked_upload_api_key(), "dedicated": True})


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
    client = await asyncio.to_thread(_get_live_client, key)
    config = build_live_config(settings)
    top_k = settings.get("rag_top_k", 4)

    try:
        async with client.aio.live.connect(model=settings["live_model"], config=config) as session:
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
    # Behind a host's HTTPS proxy, trust its X-Forwarded-* headers so the
    # app sees https:// requests as https (needed for Secure cookies).
    # Passing the app object (not "server:app") avoids importing this module
    # a second time, which would redo the database restore and indexing.
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "6001")),
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )