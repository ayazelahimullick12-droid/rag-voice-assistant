/* BRAC Bengali Voice Assistant — browser client
 *
 * One button starts and stops a session. While a session is live:
 *   mic -> AudioWorklet (16 kHz PCM16) -> WebSocket -> Gemini Live
 *   Gemini Live -> WebSocket -> scheduled playback at 24 kHz
 *
 * The orb has two distinct looks: a smooth inward-drawn wave while you
 * speak, and outward radial bars while the assistant speaks.
 *
 * Settings (voice, retrieval, interface prefs) are read from /api/settings
 * on load and applied here; voice/top-K/min-score are applied server-side
 * per session and can't change mid-call.
 */

const SEND_RATE = 16000;
const PLAY_RATE = 24000;

const els = {
  toggle: document.getElementById('toggle'),
  toggleText: document.getElementById('toggleText'),
  state: document.getElementById('stateLine'),
  log: document.getElementById('log'),
  clear: document.getElementById('clearLog'),
  kbValue: document.getElementById('kbValue'),
  echoGuard: document.getElementById('echoGuard'),
  canvas: document.getElementById('orb'),
  themeToggle: document.getElementById('themeToggle'),
  logoutBtn: document.getElementById('logoutBtn'),
  panel: document.getElementById('panel'),
  panelScrim: document.getElementById('panelScrim'),
  panelClose: document.getElementById('panelClose'),
  feedPeek: document.getElementById('feedPeek'),
  peekWho: document.getElementById('peekWho'),
  peekText: document.getElementById('peekText'),
  textForm: document.getElementById('textForm'),
  textInput: document.getElementById('textInput'),
  textSendBtn: document.getElementById('textSendBtn'),
};

const state = {
  mode: 'idle',       // idle | connecting | listening | thinking | speaking | error
  running: false,     // WebSocket connected (mic on or not — text alone can open it)
  micRunning: false,  // microphone actively capturing, on top of a running connection
  ws: null,
  micCtx: null,
  micStream: null,
  micNode: null,
  micAnalyser: null,
  playCtx: null,
  playGain: null,
  playAnalyser: null,
  playHead: 0,
  playLead: 0.2,      // seconds buffered before a reply starts playing (PLAY_LEAD_MIN)
  replyOpen: false,   // a reply's audio has started arriving and isn't complete yet
  lastAudioAt: 0,     // performance.now() of the latest audio chunk
  underruns: 0,       // times the queue ran dry mid-reply (for diagnosis)
  sources: [],
  partial: { user: null, bot: null },
  clockScale: 1,      // set from orb_speed setting
  wakeLock: null,     // screen wake lock held while a session is open
};

/* ------------------------------------------------------------------ *
 * theme
 * ------------------------------------------------------------------ */
// Colors the phone browser's own toolbar/status bar to match the page.
const THEME_COLORS = { dark: '#100309', light: '#FBF6EC' };

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', THEME_COLORS[theme]);
}

function initTheme() {
  const saved = localStorage.getItem('brac-theme');
  applyTheme(saved === 'light' ? 'light' : 'dark');
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme');
  const next = current === 'light' ? 'dark' : 'light';
  applyTheme(next);
  localStorage.setItem('brac-theme', next);
}

initTheme();
els.themeToggle.addEventListener('click', toggleTheme);

els.logoutBtn.addEventListener('click', async () => {
  if (state.running) finishSession();
  try { await fetch('/api/auth/logout', { method: 'POST' }); } catch {}
  window.location.replace('/login');
});

/* ------------------------------------------------------------------ *
 * settings — applied on load, voice/top-K/min-score apply next session
 * ------------------------------------------------------------------ */
const FONT_SIZES = { small: '0.85rem', normal: '0.95rem', large: '1.08rem' };
const CLOCK_SCALES = { slow: 0.6, normal: 1, fast: 1.6 };

function applySettings(settings) {
  if (!settings) return;
  if (typeof settings.echo_guard_default === 'boolean') {
    els.echoGuard.checked = settings.echo_guard_default;
  }
  const size = FONT_SIZES[settings.font_size] || FONT_SIZES.normal;
  document.documentElement.style.setProperty('--transcript-size', size);
  state.clockScale = CLOCK_SCALES[settings.orb_speed] || 1;
}

fetch('/api/settings')
  .then((r) => r.json())
  .then(applySettings)
  .catch(() => {});

/* ------------------------------------------------------------------ *
 * transcript panel
 * ------------------------------------------------------------------ */
function clearEmpty() {
  const empty = els.log.querySelector('.empty');
  if (empty) empty.remove();
}

function scrollLog() {
  els.log.scrollTop = els.log.scrollHeight;
}

function updatePeek(who, text) {
  if (!text || !text.trim()) return;
  els.peekWho.textContent = who === 'user' ? 'আপনি বলেছেন' : 'সহকারী বলেছে';
  els.peekText.textContent = text.trim();
  // Box is capped at ~5 lines with overflow hidden; pin the visible
  // window to the bottom so the newest words stay in view as the
  // reply keeps growing, instead of getting stuck showing the start.
  els.peekText.scrollTop = els.peekText.scrollHeight;
}

function appendPartial(role, text) {
  clearEmpty();
  let node = state.partial[role];
  if (!node) {
    node = document.createElement('div');
    node.className = `msg ${role === 'user' ? 'user' : 'bot'} live`;
    const who = document.createElement('span');
    who.className = 'who';
    who.textContent = role === 'user' ? 'আপনি' : 'সহকারী';
    node.appendChild(who);
    node.appendChild(document.createElement('span'));
    els.log.appendChild(node);
    state.partial[role] = node;
  }
  node.lastChild.textContent += text;
  scrollLog();
  updatePeek(role, node.lastChild.textContent);
}

function commit(role) {
  const node = state.partial[role];
  if (node) {
    node.classList.remove('live');
    if (!node.lastChild.textContent.trim()) node.remove();
  }
  state.partial[role] = null;
}

function addNote(text) {
  clearEmpty();
  const n = document.createElement('div');
  n.className = 'note';
  n.textContent = text;
  els.log.appendChild(n);
  scrollLog();
  // On a phone the log is tucked away in the sheet — show it in the bar too.
  els.peekWho.textContent = 'বার্তা';
  els.peekText.textContent = text;
}

function addCitation(payload) {
  clearEmpty();
  const box = document.createElement('div');
  box.className = 'cite' + (payload.found ? '' : ' none');
  if (!payload.found) {
    box.innerHTML = `নথিতে খোঁজা হয়েছে: <b>${escapeHtml(payload.query)}</b> — কিছু পাওয়া যায়নি`;
  } else {
    const list = payload.sources
      .map((s) => `${escapeHtml(s.source)}${s.section ? ' › ' + escapeHtml(s.section) : ''} (${s.score})`)
      .join('<br>');
    box.innerHTML = `নথিতে খোঁজা হয়েছে: <b>${escapeHtml(payload.query)}</b><br>${list}`;
  }
  els.log.appendChild(box);
  scrollLog();
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

els.clear.addEventListener('click', () => {
  els.log.innerHTML = '<p class="empty">মাইক চালু করে বাংলায় প্রশ্ন করুন। উত্তর শুধু আপনার দেওয়া নথি থেকে আসবে।</p>';
  state.partial = { user: null, bot: null };
  els.peekWho.textContent = 'কথোপকথন';
  els.peekText.textContent = 'মাইক চালু করে বাংলায় প্রশ্ন করুন';
});

/* ------------------------------------------------------------------ *
 * mobile bottom sheet
 * ------------------------------------------------------------------ */
function openPanel() {
  els.panel.classList.add('open');
  els.panelScrim.classList.add('open');
  els.feedPeek.setAttribute('aria-expanded', 'true');
  scrollLog();
}
function closePanel() {
  els.panel.classList.remove('open');
  els.panelScrim.classList.remove('open');
  els.feedPeek.setAttribute('aria-expanded', 'false');
}
els.feedPeek.addEventListener('click', openPanel);
els.panelClose.addEventListener('click', closePanel);
els.panelScrim.addEventListener('click', closePanel);

/* ------------------------------------------------------------------ *
 * knowledge base strip
 * ------------------------------------------------------------------ */
async function loadKb() {
  els.kbValue.textContent = 'লোড হচ্ছে…';
  try {
    const res = await fetch('/api/knowledge');
    const info = await res.json();
    const mode = info.semantic ? 'অর্থভিত্তিক + শব্দভিত্তিক' : 'শব্দভিত্তিক';
    els.kbValue.textContent = `${info.files.length}টি ফাইল · ${info.chunks}টি অংশ · ${mode} খোঁজ`;
    els.kbValue.title = info.files.join('\n');
  } catch {
    els.kbValue.textContent = 'সার্ভারে পৌঁছানো যায়নি';
  }
}
loadKb();

/* ------------------------------------------------------------------ *
 * session state
 * ------------------------------------------------------------------ */
const LABELS = {
  idle: 'শুরু করতে বোতামে চাপ দিন',
  connecting: 'সংযোগ হচ্ছে…',
  listening: 'শুনছি…',
  thinking: 'নথি খোঁজা হচ্ছে…',
  speaking: 'উত্তর দিচ্ছি…',
  error: 'সংযোগে সমস্যা হয়েছে',
};

function setMode(mode) {
  if (state.mode === mode) return;
  state.mode = mode;
  els.state.textContent = LABELS[mode] || '';
}

/* ------------------------------------------------------------------ *
 * audio out
 * ------------------------------------------------------------------ */
const AudioCtx = window.AudioContext || window.webkitAudioContext;

// A reply starts playing this long after its first chunk arrives, so later
// chunks have time to land; it grows (up to the max) if the queue still
// runs dry. Slow or uneven connections otherwise make the voice stutter.
const PLAY_LEAD_MIN = 0.2;
const PLAY_LEAD_MAX = 0.8;
const ECHO_TAIL = 0.35;       // seconds of room echo after the speaker stops
const REPLY_STALL_MS = 3000;  // no audio for this long = the reply is over

function newAudioContext(sampleRate) {
  // Older Safari rejects the sampleRate option; the default rate works too
  // (playback buffers carry their own rate, and the mic worklet resamples).
  try { return new AudioCtx({ sampleRate }); } catch { return new AudioCtx(); }
}

function ensurePlayback() {
  if (state.playCtx) return;
  const ctx = newAudioContext(PLAY_RATE);
  const gain = ctx.createGain();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  analyser.smoothingTimeConstant = 0.72;
  gain.connect(analyser);
  analyser.connect(ctx.destination);
  state.playCtx = ctx;
  state.playGain = gain;
  state.playAnalyser = analyser;
  state.playHead = 0;
}

function unlockPlayback() {
  // Phones (iOS Safari especially) only let audio start from inside a tap.
  // Call this first thing in a click/submit handler — before any await —
  // or the reply would arrive and play silently.
  ensurePlayback();
  if (state.playCtx.state === 'suspended') state.playCtx.resume().catch(() => {});
}

function enqueueAudio(arrayBuffer) {
  ensurePlayback();
  const pcm = new Int16Array(arrayBuffer);
  if (!pcm.length) return;
  const floats = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) floats[i] = pcm[i] / 32768;

  const buf = state.playCtx.createBuffer(1, floats.length, PLAY_RATE);
  buf.copyToChannel(floats, 0);

  const src = state.playCtx.createBufferSource();
  src.buffer = buf;
  src.connect(state.playGain);

  const now = state.playCtx.currentTime;
  if (state.playHead < now + 0.02) {
    // The queue is empty: either a reply is starting, or it ran dry in the
    // middle of one because the next chunk was late. A late chunk shows up
    // within moments of the queue emptying (a longer pause is the model
    // looking something up) — when that happens, buffer more from now on.
    if (state.replyOpen && now - state.playHead < 0.6) {
      state.underruns += 1;
      state.playLead = Math.min(PLAY_LEAD_MAX, state.playLead + 0.15);
    }
    state.playHead = now + state.playLead;
  }
  state.replyOpen = true;
  state.lastAudioAt = performance.now();
  // Mute straight away rather than waiting for the next animation frame
  // (frames stop altogether while the screen is off or the tab is hidden).
  if (state.micNode && els.echoGuard.checked) {
    state.micNode.port.postMessage({ type: 'mute', value: true });
  }
  src.start(state.playHead);
  state.playHead += buf.duration;

  state.sources.push(src);
  src.onended = () => {
    const i = state.sources.indexOf(src);
    if (i >= 0) state.sources.splice(i, 1);
  };
}

function stopPlayback() {
  state.sources.forEach((s) => { try { s.stop(); } catch {} });
  state.sources = [];
  if (state.playCtx) state.playHead = state.playCtx.currentTime;
}

function isSpeaking() {
  return !!state.playCtx && state.playHead > state.playCtx.currentTime + 0.02;
}

function endReply() {
  state.replyOpen = false;
}

/* True from a reply's first audio chunk until the reply is complete AND the
 * speaker has really gone quiet. Wider than isSpeaking() on purpose:
 *
 *  - it holds through brief gaps between chunks, and
 *  - it adds the speaker's own output delay plus a little room echo at the
 *    end, since sound keeps coming out after the audio clock says "done".
 *
 * The echo guard mutes the mic for this whole span. Muting only while
 * isSpeaking() let the mic open during every small gap while the speaker
 * was still sounding; Gemini heard its own voice, took it for the user
 * interrupting, cut the reply off and started again — over and over.
 */
function assistantBusy() {
  const ctx = state.playCtx;
  if (!ctx) return false;
  const tail = (ctx.outputLatency || ctx.baseLatency || 0) + ECHO_TAIL;
  if (state.playHead + tail > ctx.currentTime) return true;
  // Still mid-reply, unless audio stopped coming without a turn_complete.
  return state.replyOpen && performance.now() - state.lastAudioAt < REPLY_STALL_MS;
}

/* ------------------------------------------------------------------ *
 * audio in
 * ------------------------------------------------------------------ */
async function startMic() {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
  // Recorded straight away (and the context below too) so that stopMic()
  // releases the microphone if any later step fails — otherwise the
  // browser's "recording" indicator would stay on.
  state.micStream = stream;

  // Ask for a 16 kHz context so the browser does the resampling. Firefox
  // refuses to connect a mic to a context at a different rate than the
  // hardware, so fall back to the default rate there — mic-processor.js
  // resamples to 16 kHz itself whenever the context isn't already at it.
  let ctx = newAudioContext(SEND_RATE);
  state.micCtx = ctx;
  let source;
  try {
    source = ctx.createMediaStreamSource(stream);
  } catch {
    try { ctx.close(); } catch {}
    ctx = new AudioCtx();
    state.micCtx = ctx;
    source = ctx.createMediaStreamSource(stream);
  }
  // Created after the permission prompt, i.e. outside the tap — phones
  // can start such a context suspended, and then no audio would be sent.
  // Not awaited: where a browser holds resume() back, the promise stays
  // pending and would freeze the talk button.
  if (ctx.state === 'suspended') ctx.resume().catch(() => {});
  await ctx.audioWorklet.addModule('/static/mic-processor.js');

  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  analyser.smoothingTimeConstant = 0.6;
  const node = new AudioWorkletNode(ctx, 'mic-processor');

  node.port.onmessage = (event) => {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(event.data);
    }
  };

  source.connect(analyser);
  source.connect(node);
  const silence = ctx.createGain();
  silence.gain.value = 0;
  node.connect(silence);
  silence.connect(ctx.destination);

  state.micNode = node;
  state.micAnalyser = analyser;
}

function stopMic() {
  if (state.micNode) { try { state.micNode.port.close(); } catch {} }
  if (state.micStream) state.micStream.getTracks().forEach((t) => t.stop());
  if (state.micCtx) { try { state.micCtx.close(); } catch {} }
  state.micNode = null;
  state.micStream = null;
  state.micCtx = null;
  state.micAnalyser = null;
}

/* ------------------------------------------------------------------ *
 * session control
 *
 * The WebSocket connection and the microphone are now independent:
 * typing a message opens the connection without ever touching the mic
 * (no permission prompt for a text-only user), while pressing the talk
 * button opens the connection AND starts the mic together — or, if a
 * text-only connection is already open, just adds the mic on top of it.
 * ------------------------------------------------------------------ */
/* Keep the phone's screen on while a conversation is open — when it locks,
 * the browser suspends the page and the session drops. Only available on
 * HTTPS, and not in every browser; it's a nicety, so failures are ignored. */
async function holdWakeLock() {
  if (!('wakeLock' in navigator) || state.wakeLock) return;
  try {
    const lock = await navigator.wakeLock.request('screen');
    // The session may have ended, or another call may have won the race,
    // while the request was pending — don't keep a lock nobody will release.
    if (!state.running || state.wakeLock) { lock.release().catch(() => {}); return; }
    state.wakeLock = lock;
    lock.addEventListener('release', () => {
      if (state.wakeLock === lock) state.wakeLock = null;
    });
  } catch {}
}

function releaseWakeLock() {
  if (!state.wakeLock) return;
  state.wakeLock.release().catch(() => {});
  state.wakeLock = null;
}

// The browser drops the lock whenever the tab is hidden; take it again
// when the user comes back to a conversation that's still open.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && state.running) holdWakeLock();
});

function openSocket() {
  // Resolves once the server's "ready" message arrives (the Live session
  // is actually up), not just once the raw socket opens — sending
  // anything before that would be lost.
  return new Promise((resolve, reject) => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/audio`);
    ws.binaryType = 'arraybuffer';
    state.ws = ws;
    let settled = false;

    ws.onopen = () => { state.running = true; holdWakeLock(); };

    ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        enqueueAudio(event.data);
        return;
      }
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }

      switch (msg.type) {
        case 'ready':
          if (msg.settings) applySettings(msg.settings);
          if (!settled) { settled = true; resolve(); }
          break;
        case 'input_transcript':
          appendPartial('user', msg.text);
          break;
        case 'output_transcript':
          commit('user');
          appendPartial('bot', msg.text);
          break;
        case 'retrieval':
          commit('user');
          setMode('thinking');
          addCitation(msg);
          break;
        case 'interrupted':
          stopPlayback();
          endReply();
          commit('bot');
          break;
        case 'turn_complete':
          endReply();
          commit('user');
          commit('bot');
          break;
        case 'error':
          setMode('error');
          // Gemini's own failures arrive as "1011 … Internal error" — say
          // what that means, and keep the original text for diagnosis.
          addNote(/1011|internal error/i.test(msg.message || '')
            ? `Gemini এই মুহূর্তে সাড়া দিচ্ছে না। একটু পরে আবার চেষ্টা করুন। (${msg.message})`
            : `সার্ভার থেকে বার্তা: ${msg.message}`);
          if (!settled) { settled = true; reject(new Error(msg.message)); }
          break;
      }
    };

    ws.onerror = () => {
      setMode('error');
      if (!settled) { settled = true; reject(new Error('connection error')); }
    };

    ws.onclose = () => {
      if (state.running) addNote('সংযোগ বন্ধ হয়েছে। আবার শুরু করতে বোতামে চাপ দিন।');
      finishSession();
      if (!settled) { settled = true; reject(new Error('closed before ready')); }
    };
  });
}

async function startSession() {
  unlockPlayback();   // must run inside the tap, before the awaits below
  els.toggle.disabled = true;
  setMode('connecting');

  if (!state.running) {
    try {
      await openSocket();
    } catch (err) {
      setMode('error');
      addNote('সংযোগ করা যায়নি। আবার চেষ্টা করুন।');
      els.toggle.disabled = false;
      return;
    }
  }

  try {
    await startMic();
    state.micRunning = true;
  } catch (err) {
    setMode('error');
    addNote('মাইক চালু করা যায়নি। ব্রাউজারে মাইক্রোফোনের অনুমতি দিন, তারপর আবার চেষ্টা করুন।');
    els.toggle.disabled = false;
    stopMic();
    return;
  }

  els.toggle.disabled = false;
  els.toggle.setAttribute('aria-pressed', 'true');
  els.toggleText.textContent = 'থামুন';
  setMode('listening');
}

async function addMic() {
  // A text-only connection is already open — just switch the mic on
  // top of it, without reopening the socket.
  unlockPlayback();
  els.toggle.disabled = true;
  try {
    await startMic();
    state.micRunning = true;
    els.toggle.setAttribute('aria-pressed', 'true');
    els.toggleText.textContent = 'থামুন';
    setMode('listening');
  } catch (err) {
    stopMic();
    addNote('মাইক চালু করা যায়নি। ব্রাউজারে মাইক্রোফোনের অনুমতি দিন, তারপর আবার চেষ্টা করুন।');
  } finally {
    els.toggle.disabled = false;
  }
}

function finishSession() {
  state.running = false;
  state.micRunning = false;
  releaseWakeLock();
  stopMic();
  stopPlayback();
  endReply();
  if (state.ws) { try { state.ws.close(); } catch {} }
  state.ws = null;
  commit('user');
  commit('bot');
  els.toggle.disabled = false;
  els.toggle.setAttribute('aria-pressed', 'false');
  els.toggleText.textContent = 'কথা বলুন';
  if (state.mode !== 'error') setMode('idle');
}

els.toggle.addEventListener('click', () => {
  if (state.running && state.micRunning) {
    finishSession();
  } else if (state.running && !state.micRunning) {
    addMic();
  } else {
    setMode('idle');
    startSession();
  }
});

/* ------------------------------------------------------------------ *
 * text input — an alternative to speaking, over the same connection
 * ------------------------------------------------------------------ */
async function sendTextMessage() {
  const text = els.textInput.value.trim();
  if (!text) return;
  unlockPlayback();   // must run inside the tap/Enter, before the await below
  els.textInput.value = '';
  els.textSendBtn.disabled = true;

  if (!state.running) {
    setMode('connecting');
    try {
      await openSocket();
    } catch (err) {
      addNote('সংযোগ করা যায়নি। আবার চেষ্টা করুন।');
      els.textSendBtn.disabled = false;
      return;
    }
  }

  // We already have the exact text, so show it immediately as a
  // finished message rather than routing it through the live-transcript
  // partial/commit machinery (that's only for audio, where words arrive
  // one at a time from Gemini's own transcription).
  clearEmpty();
  const node = document.createElement('div');
  node.className = 'msg user';
  const who = document.createElement('span');
  who.className = 'who';
  who.textContent = 'আপনি';
  node.appendChild(who);
  node.appendChild(document.createTextNode(text));
  els.log.appendChild(node);
  scrollLog();
  updatePeek('user', text);

  state.ws.send(JSON.stringify({ type: 'text_input', text }));
  setMode('thinking');
  els.textSendBtn.disabled = false;
}

els.textForm.addEventListener('submit', (e) => {
  e.preventDefault();
  sendTextMessage();
});

window.addEventListener('beforeunload', () => { if (state.running) finishSession(); });

/* ------------------------------------------------------------------ *
 * orb
 * ------------------------------------------------------------------ */
const cv = els.canvas;
const ctx2d = cv.getContext('2d');
const SIZE = 720;
const CX = SIZE / 2;
const CY = SIZE / 2;
const R = 210;

function fitCanvas() {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  cv.width = SIZE * dpr;
  cv.height = SIZE * dpr;
  ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
}
fitCanvas();
window.addEventListener('resize', fitCanvas);

const timeBuf = new Uint8Array(512);
const freqBuf = new Uint8Array(256);

function readLevel(analyser) {
  if (!analyser) return 0;
  analyser.getByteTimeDomainData(timeBuf);
  let sum = 0;
  for (let i = 0; i < timeBuf.length; i++) {
    const v = (timeBuf[i] - 128) / 128;
    sum += v * v;
  }
  return Math.min(1, Math.sqrt(sum / timeBuf.length) * 3.2);
}

function readSpectrum(analyser, out) {
  if (!analyser) { out.fill(0); return; }
  analyser.getByteFrequencyData(freqBuf);
  const n = out.length;
  const step = Math.floor(freqBuf.length * 0.7 / n);
  for (let i = 0; i < n; i++) {
    let m = 0;
    for (let j = 0; j < step; j++) m = Math.max(m, freqBuf[i * step + j] || 0);
    out[i] = m / 255;
  }
}

const BANDS = 48;
const smoothIn = new Float32Array(BANDS);
const smoothOut = new Float32Array(BANDS);
const bandsIn = new Float32Array(BANDS);
const bandsOut = new Float32Array(BANDS);

const particles = Array.from({ length: 42 }, () => ({
  a: Math.random() * Math.PI * 2,
  d: 1 + Math.random(),
  v: 0.0016 + Math.random() * 0.0034,
  s: 0.7 + Math.random() * 1.5,
}));

let ripples = [];
let lastRipple = 0;
let smoothLevel = 0;

function drawGlow(level, hotter) {
  const g = ctx2d.createRadialGradient(CX, CY, R * 0.1, CX, CY, R * (1.55 + level * 0.3));
  const alpha = 0.16 + level * 0.24;
  g.addColorStop(0, `rgba(${hotter ? '255,79,168' : '230,0,126'},${alpha})`);
  g.addColorStop(1, 'rgba(230,0,126,0)');
  ctx2d.fillStyle = g;
  ctx2d.fillRect(0, 0, SIZE, SIZE);
}

function drawCore(radius, level, hotter) {
  const g = ctx2d.createRadialGradient(CX, CY - radius * 0.25, radius * 0.1, CX, CY, radius);
  g.addColorStop(0, `rgba(255,${hotter ? 190 : 150},${hotter ? 220 : 200},${0.75 + level * 0.2})`);
  g.addColorStop(0.45, 'rgba(230,0,126,0.85)');
  g.addColorStop(1, 'rgba(120,0,66,0.15)');
  ctx2d.beginPath();
  ctx2d.arc(CX, CY, radius, 0, Math.PI * 2);
  ctx2d.fillStyle = g;
  ctx2d.fill();
}

function drawDashedRing(radius, rotation, dashLen, gapLen, alpha, width) {
  ctx2d.save();
  ctx2d.translate(CX, CY);
  ctx2d.rotate(rotation);
  ctx2d.beginPath();
  ctx2d.arc(0, 0, radius, 0, Math.PI * 2);
  ctx2d.setLineDash([dashLen, gapLen]);
  ctx2d.strokeStyle = `rgba(255,140,200,${alpha})`;
  ctx2d.lineWidth = width;
  ctx2d.stroke();
  ctx2d.setLineDash([]);
  ctx2d.restore();
}

function renderIdle(t) {
  const breathe = 0.5 + 0.5 * Math.sin(t * 0.0012);
  drawGlow(0.06 + breathe * 0.05, false);
  drawDashedRing(R * 0.92, t * 0.00012, 3, 22, 0.24, 1.2);
  drawDashedRing(R * 1.06, -t * 0.00008, 1.5, 34, 0.16, 1);
  drawCore(R * (0.30 + breathe * 0.015), 0.05, false);
}

function renderScan(t) {
  drawGlow(0.12, false);
  drawDashedRing(R * 0.92, t * 0.0006, 4, 16, 0.3, 1.3);
  ctx2d.save();
  ctx2d.translate(CX, CY);
  ctx2d.rotate((t * 0.0026) % (Math.PI * 2));
  const grad = ctx2d.createLinearGradient(-R, 0, R, 0);
  grad.addColorStop(0, 'rgba(255,79,168,0)');
  grad.addColorStop(1, 'rgba(255,79,168,0.95)');
  ctx2d.beginPath();
  ctx2d.arc(0, 0, R * 0.92, -0.9, 0);
  ctx2d.strokeStyle = grad;
  ctx2d.lineWidth = 3;
  ctx2d.lineCap = 'round';
  ctx2d.stroke();
  ctx2d.restore();
  const pulse = 0.5 + 0.5 * Math.sin(t * 0.006);
  drawCore(R * (0.28 + pulse * 0.03), 0.15, false);
}

function renderListening(t, level) {
  readSpectrum(state.micAnalyser, bandsIn);
  for (let i = 0; i < BANDS; i++) smoothIn[i] += (bandsIn[i] - smoothIn[i]) * 0.25;

  drawGlow(level, false);
  drawDashedRing(R * 1.12, t * 0.00018, 2, 30, 0.2, 1);

  ctx2d.save();
  for (const p of particles) {
    p.d -= p.v * (0.6 + level * 3.2);
    if (p.d < 0.42) { p.d = 1.25 + Math.random() * 0.35; p.a = Math.random() * Math.PI * 2; }
    p.a += 0.0009;
    const rr = R * p.d;
    const x = CX + Math.cos(p.a) * rr;
    const y = CY + Math.sin(p.a) * rr;
    ctx2d.beginPath();
    ctx2d.arc(x, y, p.s * (0.6 + level), 0, Math.PI * 2);
    ctx2d.fillStyle = `rgba(255,160,210,${0.14 + level * 0.5})`;
    ctx2d.fill();
  }
  ctx2d.restore();

  const base = R * 0.74;
  ctx2d.beginPath();
  for (let i = 0; i <= BANDS; i++) {
    const idx = i % BANDS;
    const ang = (i / BANDS) * Math.PI * 2 - Math.PI / 2;
    const wobble = Math.sin(ang * 3 + t * 0.0018) * 6 + Math.sin(ang * 5 - t * 0.0011) * 4;
    const rr = base + wobble + smoothIn[idx] * 62 * (0.35 + level * 1.6);
    const x = CX + Math.cos(ang) * rr;
    const y = CY + Math.sin(ang) * rr;
    if (i === 0) ctx2d.moveTo(x, y); else ctx2d.lineTo(x, y);
  }
  ctx2d.closePath();
  ctx2d.strokeStyle = `rgba(255,110,185,${0.55 + level * 0.4})`;
  ctx2d.lineWidth = 2;
  ctx2d.stroke();
  ctx2d.fillStyle = `rgba(230,0,126,${0.10 + level * 0.16})`;
  ctx2d.fill();

  drawCore(R * (0.30 + level * 0.10), level, false);
}

function renderSpeaking(t, level) {
  readSpectrum(state.playAnalyser, bandsOut);
  for (let i = 0; i < BANDS; i++) smoothOut[i] += (bandsOut[i] - smoothOut[i]) * 0.35;

  drawGlow(level * 1.1, true);

  if (t - lastRipple > 260 && level > 0.12) {
    ripples.push({ born: t, power: level });
    lastRipple = t;
  }
  ripples = ripples.filter((r) => t - r.born < 1500);
  for (const r of ripples) {
    const age = (t - r.born) / 1500;
    const rr = R * (0.5 + age * 0.85);
    ctx2d.beginPath();
    ctx2d.arc(CX, CY, rr, 0, Math.PI * 2);
    ctx2d.strokeStyle = `rgba(255,79,168,${(1 - age) * 0.42 * (0.4 + r.power)})`;
    ctx2d.lineWidth = 2 - age * 1.4;
    ctx2d.stroke();
  }

  const inner = R * 0.56;
  ctx2d.save();
  ctx2d.translate(CX, CY);
  ctx2d.rotate(-Math.PI / 2 + Math.sin(t * 0.0004) * 0.3);
  for (let i = 0; i < BANDS; i++) {
    const ang = (i / BANDS) * Math.PI * 2;
    const h = 8 + smoothOut[i] * 96;
    const x1 = Math.cos(ang) * inner;
    const y1 = Math.sin(ang) * inner;
    const x2 = Math.cos(ang) * (inner + h);
    const y2 = Math.sin(ang) * (inner + h);
    const g = ctx2d.createLinearGradient(x1, y1, x2, y2);
    g.addColorStop(0, 'rgba(230,0,126,0.95)');
    g.addColorStop(1, 'rgba(255,220,240,0.9)');
    ctx2d.beginPath();
    ctx2d.moveTo(x1, y1);
    ctx2d.lineTo(x2, y2);
    ctx2d.strokeStyle = g;
    ctx2d.lineWidth = 4.4;
    ctx2d.lineCap = 'round';
    ctx2d.stroke();
  }
  ctx2d.restore();

  ctx2d.beginPath();
  for (let i = 0; i <= 72; i++) {
    const ang = (i / 72) * Math.PI * 2;
    const rr = R * 0.40 * (1 + 0.10 * Math.sin(ang * 3 + t * 0.004) * (0.4 + level)
                             + 0.07 * Math.sin(ang * 5 - t * 0.003));
    const x = CX + Math.cos(ang) * rr;
    const y = CY + Math.sin(ang) * rr;
    if (i === 0) ctx2d.moveTo(x, y); else ctx2d.lineTo(x, y);
  }
  ctx2d.closePath();
  const cg = ctx2d.createRadialGradient(CX, CY - R * 0.1, R * 0.05, CX, CY, R * 0.46);
  cg.addColorStop(0, 'rgba(255,235,246,0.95)');
  cg.addColorStop(0.5, 'rgba(255,79,168,0.9)');
  cg.addColorStop(1, 'rgba(160,0,90,0.35)');
  ctx2d.fillStyle = cg;
  ctx2d.fill();
}

function frame(rawT) {
  ctx2d.clearRect(0, 0, SIZE, SIZE);

  const t = rawT * state.clockScale;

  // "busy" (not just "sound is queued") drives the label and the echo
  // guard, so neither flickers during a short gap inside a reply.
  const speaking = assistantBusy();
  const raw = speaking ? readLevel(state.playAnalyser) : readLevel(state.micAnalyser);
  smoothLevel += (raw - smoothLevel) * 0.2;

  if (state.running) {
    if (speaking) {
      setMode('speaking');
      renderSpeaking(t, smoothLevel);
    } else if (state.mode === 'thinking' || state.mode === 'connecting') {
      renderScan(t);
    } else {
      setMode('listening');
      renderListening(t, smoothLevel);
    }
  } else if (state.mode === 'connecting') {
    renderScan(t);
  } else {
    renderIdle(t);
  }

  if (state.micNode) {
    state.micNode.port.postMessage({
      type: 'mute',
      value: els.echoGuard.checked && speaking,
    });
  }

  requestAnimationFrame(frame);
}

requestAnimationFrame(frame);