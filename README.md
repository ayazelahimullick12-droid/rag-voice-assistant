# ব্র্যাক ভয়েস সহকারী

## Quick start

```bash
pip install -r requirements.txt
python server.py
# → http://localhost:6001
```

The first time you run it, open the admin panel (button in the top-right,
or go to `http://localhost:6001/admin`) and add your Gemini API key —
login is `admin` / `ilovebracmf`. Everything else works without touching
a config file.

Document upload still runs as a separate app on its own port:
```bash
python upload_server.py
# → http://localhost:6501
```

## What's new in this version

- **Settings button + admin panel** (`/admin`, login-gated) — change the
  voice, retrieval behavior, and interface preferences without editing code.
- **Dark / light theme toggle** in the main UI, saved in the browser.
  Light mode swaps the background to a creamy white and darkens the text/
  border tones enough to stay legible; the magenta accent and orb colors
  are untouched.
- **Add or replace the Gemini API key from the admin panel** — saved to
  `.env`, picked up by the running process immediately.
- **Mobile layout rewritten**: on a phone, the transcript is a small
  "peek" bar showing the latest turn. Tap it to open the full conversation
  as a bottom sheet you can scroll through; tap the ✕ or the dimmed
  background to close it.
- **Removed the spinning ring behind the talk button** — it's a static
  pill now.

## Settings reference

| Setting | What it does | Applies |
|---|---|---|
| Voice preset | One of 8 Gemini voices: Puck, Charon, Kore, Fenrir, Aoede, Leda, Orus, Zephyr | next session |
| Echo guard default | Whether the mic mutes while the assistant speaks, by default | immediately (client-side default) |
| Show transcription | Whether spoken text appears in the transcript at all | next session |
| Show only assistant | Hide the user's own transcript, keep the assistant's | next session |
| Show retrieval logs | Show which documents were searched, in the transcript | next session |
| Minimum match score | How strict the RAG retrieval floor is (0.10–0.30) | next session |
| Passages per question | How many document chunks get sent to Gemini (2/4/6/8) | next session |
| Orb animation speed | Slow / normal / fast | next session (also cached until reload) |
| Transcript font size | Small / normal / large | next session |

**Why "next session" and not instantly:** a Gemini Live session is a single
open WebSocket connection with its configuration fixed at connect time.
Changing the voice or retrieval settings mid-call isn't possible — the
change is read fresh every time someone presses **কথা বলুন**, so it takes
effect on the next conversation, never the current one. The admin panel
says this next to the relevant settings so it doesn't look broken.

## API key handling

The key lives in `.env` (`GEMINI_API_KEY=...`), not in `settings.json` —
it's a secret, not a preference, and is handled by its own endpoint
(`/api/admin/apikey`) with its own validation. Saving a new key:

1. Writes it to `.env` (replacing the old line, not duplicating it)
2. Sets it in the current process's environment
3. Re-creates the embedding client and re-indexes the knowledge base

If you're running the voice server and the upload server as two separate
processes, only the process you saved the key from picks it up
immediately — the other reads `.env` the next time it starts.

## File structure

```
brac_voice_rag/
├── server.py              Voice assistant (port 6001) + admin API routes
├── settings_store.py      settings.json + .env read/write, validation
├── upload_server.py       Document upload (port 6501)
├── document_processor.py  PDF/image/DOCX extraction via Claude
├── rag.py                 Retrieval (shared by both servers)
├── requirements.txt
├── .env                   GEMINI_API_KEY=...  (created on first run if missing)
├── settings.json          Created automatically on first admin save
├── knowledge/             Shared .md documents
└── static/
    ├── index.html         Main voice assistant page
    ├── styles.css         Dark/light theme, mobile layout
    ├── app.js             Mic/playback/WebSocket, theme toggle, orb
    ├── mic-processor.js   AudioWorklet: 16kHz PCM16 capture
    ├── admin.html         Settings panel (behind login)
    ├── admin_login.html   Login form
    ├── admin.css          Shared styling for both admin pages
    └── admin.js           Settings load/save, API key, KB reload
```

## Admin panel routes

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/admin` | GET | — | Serves login page or panel depending on session cookie |
| `/api/admin/login` | POST | — | `{username, password}` → sets session cookie |
| `/api/admin/logout` | POST | cookie | Clears the session |
| `/api/admin/settings` | GET | cookie | Current settings + voice list + masked key + KB info |
| `/api/admin/settings` | POST | cookie | Save a settings patch (validated/clamped server-side) |
| `/api/admin/apikey` | POST | cookie | `{api_key}` → writes `.env`, re-indexes KB |
| `/api/admin/knowledge/reload` | POST | cookie | Re-index `knowledge/` on demand |
| `/api/settings` | GET | — | Public read used by the main page (no secrets) |

Sessions are in-memory (a Python dict), which is fine for a single-process
prototype but won't survive a server restart or work across multiple
worker processes — logging back in takes two seconds, so this wasn't
worth adding a database for.

## Known limits, worth saying in a demo

- Voice/retrieval settings apply to the *next* session, not the current call.
- Admin sessions are in-memory — restarting the server logs everyone out.
- The password is in the source code, not hashed — fine for a prototype
  behind `localhost`, not fine if you expose this on the open internet
  without putting it behind a real auth layer first.
- Light mode is a background/text swap, not a full redesign — the magenta
  accents and orb rendering are identical in both themes.
