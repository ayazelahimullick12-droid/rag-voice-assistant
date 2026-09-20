"""
BRAC Document Upload Server — separate from the voice assistant (port 6001).

Accepts file uploads (images, PDFs, DOCX, TXT, MD) and processes them into
markdown for the shared knowledge/ folder.

Runs on port 6501.
"""

import tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from document_processor import DocumentProcessor
from settings_store import read_upload_api_key, write_upload_api_key, masked_upload_api_key, upload_key_is_dedicated

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"
KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="BRAC Document Upload")


_PAGE_HTML = """
<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>নথি আপলোড — ব্র্যাক</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Hind+Siliguri:wght@400;500;600;700&display=swap" rel="stylesheet" />
<script>
  // Shares the 'brac-theme' key with the voice assistant and admin panel,
  // so switching theme anywhere keeps all three apps in sync. Applied
  // before paint to avoid a flash of the wrong theme.
  (function () {
    var t = localStorage.getItem('brac-theme') === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', t);
  })();
</script>
<style>
:root, [data-theme="dark"] {
  --magenta: #E6007E;
  --magenta-lit: #FF4FA8;
  --ink: #FDF6FA;
  --muted: #B08AA0;
  --base: #100309;
  --edge: rgba(230, 0, 126, 0.22);
  --card-bg: rgba(26, 7, 16, 0.8);
  --input-bg: rgba(230, 0, 126, 0.06);
  --item-bg: rgba(230, 0, 126, 0.06);
  --item-border: rgba(230, 0, 126, 0.14);
  color-scheme: dark;
}
[data-theme="light"] {
  --magenta: #E6007E;
  --magenta-lit: #FF4FA8;
  --ink: #2B1420;
  --muted: #8A6A78;
  --base: #FBF6EC;
  --edge: rgba(230, 0, 126, 0.18);
  --card-bg: rgba(255, 255, 255, 0.85);
  --input-bg: rgba(43, 20, 32, 0.03);
  --item-bg: rgba(43, 20, 32, 0.03);
  --item-border: rgba(43, 20, 32, 0.08);
  color-scheme: light;
}

* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  background: var(--base);
  color: var(--ink);
  font-family: "Hind Siliguri", "Noto Sans Bengali", system-ui, sans-serif;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 2rem;
  transition: background-color 0.2s, color 0.2s;
}

.theme-toggle {
  position: fixed;
  top: 1.2rem; right: 1.2rem;
  display: flex;
  align-items: center;
  gap: 0.4rem;
  background: none;
  border: 0;
  cursor: pointer;
  color: var(--muted);
  padding: 0.3rem;
}
.theme-track {
  width: 2.3rem; height: 1.15rem;
  border-radius: 999px;
  border: 1px solid var(--edge);
  background: var(--input-bg);
  position: relative;
  transition: background 0.2s;
}
.theme-thumb {
  position: absolute;
  top: 50%; left: 0.16rem;
  width: 0.83rem; height: 0.83rem;
  border-radius: 50%;
  background: var(--magenta);
  transform: translateY(-50%);
  transition: left 0.2s;
}
[data-theme="light"] .theme-thumb { left: 1.28rem; }

.container {
  max-width: 600px;
  width: 100%;
  background: var(--card-bg);
  border: 1px solid var(--edge);
  border-radius: 12px;
  padding: 2.5rem;
  backdrop-filter: blur(6px);
}

h1 {
  margin: 0 0 0.5rem;
  font-size: 1.6rem;
  font-weight: 600;
  letter-spacing: 0.01em;
  text-align: center;
}

.subtitle {
  text-align: center;
  font-size: 0.9rem;
  color: var(--muted);
  margin-bottom: 2rem;
}

.upload-box {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 140px;
  border: 2px dashed var(--edge);
  border-radius: 10px;
  background: var(--input-bg);
  cursor: pointer;
  transition: all 0.2s;
  padding: 2rem;
  margin-bottom: 1.5rem;
}
.upload-box:hover { border-color: var(--magenta); }
.upload-box.drag { border-color: var(--magenta-lit); }
.upload-box input {
  position: absolute;
  opacity: 0;
  width: 100%;
  height: 100%;
  cursor: pointer;
}

.upload-icon { font-size: 3rem; margin-bottom: 0.5rem; }
.upload-text { font-size: 1.05rem; font-weight: 500; color: var(--ink); margin-bottom: 0.25rem; }
.upload-hint { font-size: 0.85rem; color: var(--muted); }
.formats-list { font-size: 0.8rem; color: var(--muted); margin-top: 0.5rem; opacity: 0.8; }

.status {
  min-height: 1.6em;
  font-size: 0.9rem;
  line-height: 1.6;
  margin-bottom: 1rem;
  padding: 0.8rem 1rem;
  border-radius: 8px;
  display: none;
}
.status.show { display: block; }
.status.working { background: rgba(255, 79, 168, 0.12); color: #FF4FA8; border-left: 3px solid #FF4FA8; }
.status.success { background: rgba(111, 217, 111, 0.14); color: #4CAF50; border-left: 3px solid #4CAF50; }
.status.error { background: rgba(255, 107, 107, 0.14); color: #E24B4A; border-left: 3px solid #E24B4A; }
[data-theme="light"] .status.working { color: #C2247F; }
[data-theme="light"] .status.success { color: #2E7D32; }
[data-theme="light"] .status.error { color: #B42323; }

.progress-bar { width: 100%; height: 3px; background: var(--input-bg); border-radius: 2px; margin-top: 0.5rem; overflow: hidden; display: none; }
.progress-bar.show { display: block; }
.progress-fill { height: 100%; background: var(--magenta); width: 0%; animation: pulse 1.5s ease-in-out infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.6} }

.uploads-list { margin-top: 2rem; padding-top: 1.5rem; border-top: 1px solid var(--edge); }
.uploads-title { font-size: 0.9rem; font-weight: 600; color: var(--muted); margin-bottom: 1rem; text-transform: uppercase; letter-spacing: 0.05em; }
.upload-item { padding: 0.9rem; background: var(--item-bg); border: 1px solid var(--item-border); border-radius: 6px; font-size: 0.85rem; margin-bottom: 0.6rem; }
.upload-item-name { font-weight: 500; color: var(--ink); }
.upload-item-size { color: var(--muted); font-size: 0.75rem; margin-top: 0.2rem; }

.kb-info { margin-top: 2rem; padding: 1rem; background: var(--input-bg); border-radius: 8px; font-size: 0.85rem; line-height: 1.6; color: var(--muted); }
.kb-label { font-weight: 600; color: var(--magenta); display: block; margin-bottom: 0.4rem; }

.key-box { margin-top: 1rem; padding: 1rem; background: var(--input-bg); border-radius: 8px; }
.key-hint { margin: 0 0 0.7rem; font-size: 0.82rem; color: var(--muted); }
.key-row { display: flex; gap: 0.6rem; }
.key-row input {
  flex: 1;
  min-width: 0;
  font: inherit;
  font-size: 0.9rem;
  color: var(--ink);
  background: var(--card-bg);
  border: 1px solid var(--edge);
  border-radius: 6px;
  padding: 0.55rem 0.7rem;
  outline: none;
}
.key-row input:focus { border-color: var(--magenta); }
.key-row button {
  flex: none;
  background: linear-gradient(180deg, var(--magenta-lit), var(--magenta));
  color: #fff;
  border: 0;
  border-radius: 6px;
  padding: 0.55rem 1rem;
  font: inherit;
  font-weight: 600;
  font-size: 0.85rem;
  cursor: pointer;
}
.key-row button:hover { opacity: 0.92; }
.key-row button:disabled { opacity: 0.6; cursor: wait; }
.key-status { min-height: 1.3em; font-size: 0.82rem; margin-top: 0.5rem; }
.key-status.ok { color: #4CAF50; }
.key-status.error { color: #E24B4A; }
[data-theme="light"] .key-status.ok { color: #2E7D32; }
[data-theme="light"] .key-status.error { color: #B42323; }

.link-to-voice { margin-top: 1.5rem; padding-top: 1.5rem; border-top: 1px solid var(--edge); text-align: center; }
.link-to-voice a { color: var(--magenta-lit); text-decoration: none; font-weight: 500; }
.link-to-voice a:hover { color: var(--ink); }
</style>
</head>
<body>

<button class="theme-toggle" id="themeToggle" type="button" aria-label="থিম পরিবর্তন করুন">
  <span class="theme-track"><span class="theme-thumb"></span></span>
</button>

<div class="container">
  <h1>নথি আপলোড</h1>
  <p class="subtitle">ব্র্যাক ভয়েস সহকারীর জন্য নথি যোগ করুন</p>

  <div class="upload-box" id="uploadBox">
    <input type="file" id="uploadFile" accept=".txt,.md,.pdf,.docx,.doc,.png,.jpg,.jpeg" />
    <div class="upload-icon">📄</div>
    <div class="upload-text">ফাইল নির্বাচন করুন বা এখানে ড্র্যাগ করুন</div>
    <div class="upload-hint">একটি ফাইল একবারে</div>
    <div class="formats-list">সমর্থিত: PDF, ছবি, Word, টেক্সট, Markdown</div>
  </div>

  <div class="status" id="status"></div>
  <div class="progress-bar" id="progressBar"><div class="progress-fill"></div></div>

  <div class="uploads-list" id="uploadsList" style="display:none">
    <div class="uploads-title">সম্প্রতি আপলোড হয়েছে</div>
    <div id="uploadsContainer"></div>
  </div>

  <div class="kb-info" id="kbInfo">
    <span class="kb-label">জ্ঞান ভাণ্ডার</span>
    <div id="kbStats">লোড হচ্ছে…</div>
  </div>

  <div class="key-box" id="keyBox">
    <span class="kb-label">নথি প্রক্রিয়াকরণের API চাবি</span>
    <p class="key-hint" id="currentKeyHint">লোড হচ্ছে…</p>
    <div class="key-row">
      <input type="password" id="apiKeyInput" placeholder="AIza..." autocomplete="off" />
      <button type="button" id="saveKeyBtn">সংরক্ষণ করুন</button>
    </div>
    <div class="key-status" id="keyStatus"></div>
  </div>

  <div class="link-to-voice">
    <a href="#" id="voiceLink">← ভয়েস সহকারীতে ফিরুন</a>
  </div>
</div>

<script>
const els = {
  uploadFile: document.getElementById('uploadFile'),
  uploadBox: document.getElementById('uploadBox'),
  status: document.getElementById('status'),
  progressBar: document.getElementById('progressBar'),
  uploadsList: document.getElementById('uploadsList'),
  uploadsContainer: document.getElementById('uploadsContainer'),
  kbStats: document.getElementById('kbStats'),
  themeToggle: document.getElementById('themeToggle'),
  voiceLink: document.getElementById('voiceLink'),
  currentKeyHint: document.getElementById('currentKeyHint'),
  apiKeyInput: document.getElementById('apiKeyInput'),
  saveKeyBtn: document.getElementById('saveKeyBtn'),
  keyStatus: document.getElementById('keyStatus'),
};

// Point back at the voice assistant on whatever host this page is being
// viewed from, on its port (6001), not a hardcoded 'localhost'.
els.voiceLink.href = `${location.protocol}//${location.hostname}:6001`;

function toggleTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('brac-theme', next);
}
els.themeToggle.addEventListener('click', toggleTheme);

const uploads = [];

async function loadKbInfo() {
  try {
    const res = await fetch('/api/kb-info');
    const data = await res.json();
    els.kbStats.innerHTML = `<div><strong>${data.files.length}</strong> ফাইল</div><div><strong>${data.chunks}</strong> অনুচ্ছেদ</div>`;
  } catch (err) {
    els.kbStats.textContent = 'সংযোগ করা যাচ্ছে না';
  }
}

async function uploadFile(file) {
  if (!file) return;
  const formData = new FormData();
  formData.append('file', file);

  els.status.textContent = '📥 প্রক্রিয়া করা হচ্ছে…';
  els.status.className = 'status show working';
  els.progressBar.classList.add('show');
  els.uploadFile.disabled = true;

  try {
    const res = await fetch('/api/upload', { method: 'POST', body: formData });
    let data = {};
    try { data = await res.json(); } catch {}
    if (!res.ok) {
      throw new Error(data.detail || data.error || `সার্ভার সমস্যা (${res.status})`);
    }

    uploads.unshift({ name: data.filename, size: data.size, time: new Date().toLocaleTimeString('bn-BD') });
    renderUploads();
    els.uploadsList.style.display = 'block';
    els.status.innerHTML = `✓ <strong>${data.filename}</strong> যোগ হয়েছে`;
    els.status.className = 'status show success';
    els.uploadFile.value = '';
    await loadKbInfo();
    setTimeout(() => { els.status.classList.remove('show'); }, 3000);
  } catch (err) {
    els.status.textContent = `❌ সমস্যা: ${err.message}`;
    els.status.className = 'status show error';
  } finally {
    els.progressBar.classList.remove('show');
    els.uploadFile.disabled = false;
  }
}

function renderUploads() {
  els.uploadsContainer.innerHTML = uploads.slice(0, 10).map((u) =>
    `<div class="upload-item"><div class="upload-item-name">${u.name}</div><div class="upload-item-size">${u.size} অক্ষর · ${u.time}</div></div>`
  ).join('');
}

els.uploadFile.addEventListener('change', (e) => uploadFile(e.target.files[0]));
els.uploadBox.addEventListener('dragover', (e) => { e.preventDefault(); els.uploadBox.classList.add('drag'); });
els.uploadBox.addEventListener('dragleave', () => els.uploadBox.classList.remove('drag'));
els.uploadBox.addEventListener('drop', (e) => { e.preventDefault(); els.uploadBox.classList.remove('drag'); uploadFile(e.dataTransfer.files[0]); });

loadKbInfo();
setInterval(loadKbInfo, 10000);

async function loadKeyStatus() {
  try {
    const res = await fetch('/api/key');
    const data = await res.json();
    if (!data.masked) {
      els.currentKeyHint.textContent = 'বর্তমান চাবি: সেট করা নেই';
    } else if (data.dedicated) {
      els.currentKeyHint.textContent = `বর্তমান চাবি: ${data.masked} (শুধু নথি প্রক্রিয়াকরণের জন্য আলাদা চাবি)`;
    } else {
      els.currentKeyHint.textContent = `বর্তমান চাবি: ${data.masked} (ভয়েস সহকারীর সাথে ভাগ করা — নিচে আলাদা চাবি দিলে শুধু এখানে ব্যবহৃত হবে)`;
    }
  } catch {
    els.currentKeyHint.textContent = 'চাবির তথ্য লোড করা যায়নি';
  }
}

async function saveKey() {
  const key = els.apiKeyInput.value.trim();
  if (!key) {
    els.keyStatus.textContent = 'চাবি ফাঁকা রাখা যাবে না';
    els.keyStatus.className = 'key-status error';
    return;
  }
  els.saveKeyBtn.disabled = true;
  els.saveKeyBtn.textContent = 'সংরক্ষণ করা হচ্ছে…';
  try {
    const res = await fetch('/api/key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: key }),
    });
    let data = {};
    try { data = await res.json(); } catch {}
    if (!res.ok) throw new Error(data.error || `সার্ভার সমস্যা (${res.status})`);

    els.currentKeyHint.textContent = `বর্তমান চাবি: ${data.masked} (শুধু নথি প্রক্রিয়াকরণের জন্য আলাদা চাবি)`;
    els.apiKeyInput.value = '';
    els.keyStatus.textContent = 'চাবি সংরক্ষিত হয়েছে';
    els.keyStatus.className = 'key-status ok';
    setTimeout(() => { els.keyStatus.textContent = ''; }, 3000);
  } catch (err) {
    els.keyStatus.textContent = err.message;
    els.keyStatus.className = 'key-status error';
  } finally {
    els.saveKeyBtn.disabled = false;
    els.saveKeyBtn.textContent = 'সংরক্ষণ করুন';
  }
}
els.saveKeyBtn.addEventListener('click', saveKey);

loadKeyStatus();
</script>

</body>
</html>
"""


@app.get("/")
async def index():
    """Serve the upload interface, with no-store caching (this page reads
    live knowledge-base state on load, so it should never be cached)."""
    return HTMLResponse(_PAGE_HTML, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    })


@app.post("/api/upload")
async def upload_document(file: UploadFile = File(...)):
    """Upload and process a document into markdown."""
    api_key = read_upload_api_key()
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not set")

    processor = DocumentProcessor(api_key=api_key)

    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename).suffix) as tmp:
        contents = await file.read()
        tmp.write(contents)
        tmp_path = Path(tmp.name)

    try:
        markdown, suggested_name = await processor.process_upload(tmp_path, file.filename)
        output_path = KNOWLEDGE_DIR / suggested_name
        output_path.write_text(markdown, encoding='utf-8')
        print(f"[upload] {file.filename} → {suggested_name} ({len(markdown)} chars)")
        return {"success": True, "filename": suggested_name, "size": len(markdown)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"[upload error] {file.filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)}")
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


@app.get("/api/kb-info")
async def kb_info():
    """Read knowledge folder and return info."""
    files = sorted([f.name for f in KNOWLEDGE_DIR.glob("*.md")])
    chunk_count = 0
    for f in files:
        content = (KNOWLEDGE_DIR / f).read_text(encoding='utf-8', errors='ignore')
        headings = sum(1 for l in content.split('\n') if l.startswith('#'))
        chunk_count += max(2, headings)
    return {"files": files, "chunks": chunk_count}


@app.get("/api/key")
async def get_key():
    """
    Masked view of the key document processing is currently using — never
    returns it in full. 'dedicated' tells the UI whether this is an
    upload-specific key or the app is falling back to the shared one.
    """
    return {
        "masked": masked_upload_api_key(),
        "dedicated": upload_key_is_dedicated(),
    }


@app.post("/api/key")
async def set_key(request: Request):
    """
    Set a document-processing key independent of the shared GEMINI_API_KEY
    used by the voice assistant. Writes UPLOAD_GEMINI_API_KEY to the same
    .env file the admin panel (server.py, port 6001) reads from, alongside
    (not replacing) the shared key.
    """
    body = await request.json()
    new_key = str(body.get("api_key", "")).strip()
    if not new_key:
        return JSONResponse({"error": "চাবি ফাঁকা রাখা যাবে না"}, status_code=400)

    try:
        write_upload_api_key(new_key)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    return {"success": True, "masked": masked_upload_api_key(), "dedicated": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("upload_server:app", host="0.0.0.0", port=6501, reload=False)