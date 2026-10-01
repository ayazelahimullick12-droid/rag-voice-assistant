# ব্র্যাক ভয়েস সহকারী

## Quick start (your own computer)

```bash
pip install -r requirements.txt
python server.py
# → http://localhost:6001
```

Create a `.env` file next to `server.py` (it's git-ignored) with at least:

```
GEMINI_API_KEY=your-gemini-key
ADMIN_PASSWORD=choose-a-password
```

The admin panel is at `http://localhost:6001/admin` (username `admin`
unless you set `ADMIN_USERNAME`). Without `ADMIN_PASSWORD` the admin panel
stays locked. Document upload is at `/upload` — it's part of the same app
now, behind the admin login.

On `localhost` the microphone works over plain HTTP. From any other device
the browser only allows the microphone over **HTTPS**, which is what the
hosted setup below gives you.

## Deploying online for free (Render)

**Render** runs the app for free (no credit card) and gives it an
`https://…onrender.com` address, with HTTPS and WebSockets included.

### Steps

1. **Push this folder to GitHub.** A private repository is fine (and
   recommended — the knowledge documents are in it).
2. **Create the app.** Sign up at <https://render.com> → **New** →
   **Blueprint** → choose this repository and the branch to deploy. Render
   reads `render.yaml` and asks for two values:
   - `GEMINI_API_KEY` — your Gemini key
   - `ADMIN_PASSWORD` — a new password for the admin panel
3. **Wait for the first deploy** (a few minutes), then open the
   `…onrender.com` address Render shows.
4. **Sign in.** Accounts already in `users.json` work straight away. New
   people register with the invite code: in Render open the service →
   **Environment** → `REGISTRATION_CODE`. It's generated for you; change it
   there to something easier to type if you like.
5. **On a phone**, open the same address in Chrome or Safari and allow the
   microphone when asked. "Add to Home screen" makes it feel like an app.

### What is kept, and what isn't

Render's free tier has no permanent disk. The app's files go back to
what's in the GitHub repo whenever the service **restarts**, which happens:

- after 15 minutes with no traffic (it sleeps, then wakes on the next
  visit — that first visit takes about a minute),
- on every deploy, i.e. every push to GitHub,
- occasionally when Render moves or restarts the service.

| | After a restart |
|---|---|
| Accounts in the repo's `users.json` | kept |
| Documents in the repo's `knowledge/` | kept |
| Settings in the repo's `settings.json` | kept |
| API keys, admin password, invite code (Render → Environment) | kept |
| Being logged in | kept |
| Accounts registered on the hosted app | **lost** |
| Documents uploaded at `/upload` on the hosted app | **lost** |
| Settings changed in the hosted admin panel | **lost** |

So with Render alone, make lasting changes **in the repo**: add documents
to `knowledge/` (or upload them on your own computer, where `/upload`
writes into that folder), register accounts on your own computer so they
land in `users.json`, then commit and push.

### Optional: keep everything across restarts (Neon)

To make accounts, uploads and settings made on the hosted app permanent,
add a free Postgres database:

1. Sign up at <https://neon.com> (no credit card), create a project in the
   Singapore region, and copy its *connection string* (`postgresql://…`).
2. In Render open the service → **Environment** → add `DATABASE_URL` with
   that string, and save. The service restarts and from then on mirrors its
   data files to the database (`storage.py`).

**Don't put the hosted `DATABASE_URL` in your local `.env`** unless you
mean to: on startup the database's copies replace the local `users.json`,
`settings.json` and `knowledge/` files.

### Other things to know

- **Updating:** push to GitHub and Render redeploys automatically.
- **API keys and passwords** are changed in Render → Environment, not in
  the admin panel (the admin panel shows the key but locks the field).

### Environment variables

| Variable | Needed | What it does |
|---|---|---|
| `GEMINI_API_KEY` | yes | Voice sessions and retrieval embeddings |
| `ADMIN_PASSWORD` | yes | Admin panel password; the panel is locked without it |
| `ADMIN_USERNAME` | no | Admin username, default `admin` |
| `DATABASE_URL` | no | Postgres connection string; keeps accounts, settings and uploads made on the host across restarts |
| `SESSION_SECRET` | when hosted | Signs login cookies. Without it a random one is made at each start, so every restart logs everyone out |
| `REGISTRATION_CODE` | when hosted | Invite code required on the register page. If unset, anyone who finds the address can register and use your Gemini quota |
| `UPLOAD_GEMINI_API_KEY` | no | Separate key for document processing |
| `PORT` | no | Set by the host; defaults to 6001 |

## What's new in this version

- **Ready to host**: one server on one port, secrets from the environment,
  optional mirroring of data to Postgres, signed login cookies that survive restarts,
  an invite code for registration, a `/healthz` check, and `render.yaml`.
- **Document upload moved into the main app** (`/upload`, admin only). The
  separate `upload_server.py` on port 6501 is gone, and an upload now
  re-indexes the knowledge base by itself.
- **Mobile**: the talk button and text box sit at the bottom of the
  screen, the orb fills the space above, the conversation bar no longer
  runs off the edge, text fields don't trigger iOS zoom, and the screen
  stays on during a conversation. Audio start-up follows phone browsers'
  rules (iOS only starts audio from a tap; Firefox needs the mic at its
  native rate).
- **Settings button + admin panel** (`/admin`, login-gated) — change the
  voice, retrieval behavior, and interface preferences without editing code.
- **Dark / light theme toggle** in the main UI, saved in the browser.
  Light mode swaps the background to a creamy white and darkens the text/
  border tones enough to stay legible; the magenta accent and orb colors
  are untouched.
- **Mobile transcript**: on a phone, the transcript is a small bar showing
  the latest turn. Tap it ("সব দেখুন") to open the full conversation as a
  sheet you can scroll through; tap the ✕ to close it.

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

**Locally** the key lives in `.env` (`GEMINI_API_KEY=...`), not in
`settings.json` — it's a secret, not a preference. Saving a new key from
the admin panel:

1. Writes it to `.env` (replacing the old line, not duplicating it)
2. Sets it in the current process's environment
3. Re-creates the embedding client and re-indexes the knowledge base

**When hosted** the key comes from the host's environment settings and
there is no `.env`. The admin panel shows the masked key but disables the
field: a key saved from the panel would be lost at the next restart, so it
has to be changed in the host's dashboard instead.

## File structure

```
brac-rag/
├── server.py              Voice assistant, admin API, document upload
├── storage.py             Mirrors data files to Postgres (when DATABASE_URL is set)
├── settings_store.py      settings.json + .env read/write, validation
├── users_store.py         users.json accounts (salted PBKDF2 hashes)
├── document_processor.py  PDF/image/DOCX extraction via Gemini
├── rag.py                 Retrieval (chunking, embeddings, keyword search)
├── requirements.txt
├── render.yaml            Render Blueprint (hosting config)
├── .python-version        Python version the host should use
├── .env                   Local secrets — git-ignored, you create it
├── settings.json          Created automatically on first admin save
├── users.json             Registered accounts
├── knowledge/             .md documents the assistant answers from
└── static/
    ├── index.html         Main voice assistant page
    ├── styles.css         Dark/light theme, mobile layout
    ├── app.js             Mic/playback/WebSocket, theme toggle, orb
    ├── mic-processor.js   AudioWorklet: 16kHz PCM16 capture
    ├── login.html         User login
    ├── register.html      User registration (with invite code when required)
    ├── admin.html         Settings panel (behind admin login)
    ├── admin_login.html   Admin login form (also shown at /upload)
    ├── upload.html        Document upload (behind admin login)
    ├── admin.css          Styling for the admin, login and register pages
    └── admin.js           Settings load/save, API key, KB reload
```

## Routes

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/` | GET | — | Voice assistant, or the login page if not signed in |
| `/login`, `/register` | GET | — | User login / registration pages |
| `/api/auth/config` | GET | — | Whether registration needs an invite code |
| `/api/auth/register` | POST | invite code | `{username, password, code}` → sets session cookie |
| `/api/auth/login` | POST | — | `{username, password}` → sets session cookie |
| `/api/auth/logout` | POST | — | Clears the session cookie |
| `/ws/audio` | WebSocket | user | The live voice session |
| `/api/knowledge` | GET | user or admin | Knowledge base file list and status |
| `/api/settings` | GET | — | Public read used by the main page (no secrets) |
| `/admin` | GET | — | Login page or panel depending on session cookie |
| `/api/admin/login` | POST | — | `{username, password}` → sets admin cookie |
| `/api/admin/logout` | POST | — | Clears the admin cookie |
| `/api/admin/settings` | GET / POST | admin | Read settings / save a patch (validated server-side) |
| `/api/admin/apikey` | POST | admin | `{api_key}` → writes `.env`, re-indexes KB (local only) |
| `/api/admin/knowledge/reload` | POST | admin | Re-index `knowledge/` on demand |
| `/api/admin/knowledge/{file}` | DELETE | admin | Delete a document |
| `/upload` | GET | admin | Document upload page |
| `/api/admin/upload` | POST | admin | Upload a file → markdown in `knowledge/` → re-index |
| `/api/admin/upload-key` | GET / POST | admin | Masked view of / set the document-processing key |
| `/healthz` | GET | — | Health check for the host |

Sessions are signed cookies (HMAC with `SESSION_SECRET`), not a
server-side table, so they survive restarts and need no database lookup.
Users stay signed in for 30 days, admins for 8 hours.

## Known limits, worth saying in a demo

- Voice/retrieval settings apply to the *next* session, not the current call.
- Logging out removes the cookie from that browser; it can't cancel a
  copied cookie before it expires. Changing `SESSION_SECRET` signs
  everyone out, and changing `ADMIN_PASSWORD` signs all admins out.
- There is no limit on login attempts, so choose a long admin password.
- Free hosting sleeps when idle — the first request after a quiet spell
  takes about a minute, and without `DATABASE_URL` anything saved on the
  host since the last deploy is gone.
- Every voice session uses your Gemini quota. Keep the invite code among
  the people who should have access.
- Light mode is a background/text swap, not a full redesign — the magenta
  accents and orb rendering are identical in both themes.
