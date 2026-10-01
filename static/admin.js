/* Admin panel — loads current settings from /api/admin/settings, lets the
 * operator change them, and saves via /api/admin/settings. The API key has
 * its own endpoint since it's a secret, not a preference. Theme and
 * language are purely client-side, stored in localStorage. */

const els = {
  voice: document.getElementById('voice'),
  echoGuard: document.getElementById('echo_guard_default'),
  showTranscription: document.getElementById('show_transcription'),
  showOnlyAssistant: document.getElementById('show_only_assistant'),
  showRetrievalLogs: document.getElementById('show_retrieval_logs'),
  minScore: document.getElementById('min_score'),
  minScoreValue: document.getElementById('minScoreValue'),
  ragTopK: document.getElementById('rag_top_k'),
  orbSpeed: document.getElementById('orb_speed'),
  fontSize: document.getElementById('font_size'),
  apiKeyInput: document.getElementById('apiKeyInput'),
  currentKeyHint: document.getElementById('currentKeyHint'),
  saveKeyBtn: document.getElementById('saveKeyBtn'),
  kbSummary: document.getElementById('kbSummary'),
  kbFileList: document.getElementById('kbFileList'),
  reloadKbBtn: document.getElementById('reloadKbBtn'),
  saveAllBtn: document.getElementById('saveAllBtn'),
  saveBanner: document.getElementById('saveBanner'),
  logoutBtn: document.getElementById('logoutBtn'),
  themeToggle: document.getElementById('themeToggle'),
  langBn: document.getElementById('langBn'),
  langEn: document.getElementById('langEn'),
};

/* ------------------------------------------------------------------ *
 * theme — shared 'brac-theme' key with the main voice assistant page
 * ------------------------------------------------------------------ */
function initTheme() {
  const saved = localStorage.getItem('brac-theme');
  document.documentElement.setAttribute('data-theme', saved === 'light' ? 'light' : 'dark');
}
function toggleTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('brac-theme', next);
}
initTheme();
els.themeToggle.addEventListener('click', toggleTheme);

/* ------------------------------------------------------------------ *
 * language — admin-panel-only, stored separately from the main site
 * ------------------------------------------------------------------ */
const I18N = {
  bn: {
    panelTitle: 'অ্যাডমিন প্যানেল',
    backToVoice: '← ভয়েস সহকারী',
    logout: 'লগআউট',
    voiceTitle: 'ভয়েস',
    voicePreset: 'ভয়েস প্রিসেট',
    voiceHint: 'পরিবর্তন পরবর্তী সেশন থেকে কার্যকর হবে — চলমান কথোপকথনে প্রভাব ফেলবে না।',
    behaviorTitle: 'আচরণ',
    echoGuardLabel: 'সহকারী বলার সময় মাইক বন্ধ (ডিফল্ট)',
    showTranscriptionLabel: 'কথোপকথনের লিখিত রূপ দেখান',
    showOnlyAssistantLabel: 'শুধু সহকারীর কথা দেখান (ব্যবহারকারীর নয়)',
    showRetrievalLogsLabel: 'নথি অনুসন্ধানের লগ দেখান',
    retrievalTitle: 'তথ্য অনুসন্ধান (RAG)',
    minScoreLabel: 'ন্যূনতম মিল স্কোর:',
    minScoreHint: 'বেশি হলে কড়াকড়ি মিল দরকার হবে — কম প্রাসঙ্গিক উত্তর কমে যাবে, কিন্তু কিছু বৈধ উত্তরও বাদ পড়তে পারে।',
    topKLabel: 'প্রতি প্রশ্নে কতগুলো অংশ পাঠানো হবে',
    retrievalNextSession: 'এই দুটি পরিবর্তনও পরবর্তী সেশন থেকে কার্যকর হবে।',
    interfaceTitle: 'ইন্টারফেস',
    orbSpeedLabel: 'অর্ব অ্যানিমেশনের গতি',
    speedSlow: 'ধীর', speedNormal: 'স্বাভাবিক', speedFast: 'দ্রুত',
    fontSizeLabel: 'লেখার আকার',
    sizeSmall: 'ছোট', sizeNormal: 'স্বাভাবিক', sizeLarge: 'বড়',
    apiKeyTitle: 'Gemini API চাবি',
    newApiKeyLabel: 'নতুন API চাবি',
    saveBtn: 'সংরক্ষণ করুন',
    apiKeyHint: 'নতুন চাবি সাথে সাথে জ্ঞান ভাণ্ডার পুনরায় ইনডেক্স করবে। এই সার্ভার প্রসেসে এখনই কার্যকর হবে।',
    kbTitle: 'জ্ঞান ভাণ্ডার',
    reloadKbBtn: 'পুনরায় ইনডেক্স করুন',
    uploadDocs: 'নথি আপলোড',
    saveAllBtn: 'সব পরিবর্তন সংরক্ষণ করুন',
    savingBtn: 'সংরক্ষণ করা হচ্ছে…',
    reindexingBtn: 'ইনডেক্স হচ্ছে…',
    currentKeyPrefix: 'বর্তমান চাবি: ',
    noKeySet: 'সেট করা নেই',
    settingsLoadError: 'সেটিংস লোড করা যায়নি',
    saveFailed: 'সংরক্ষণ ব্যর্থ',
    saveAllOk: 'সব পরিবর্তন সংরক্ষিত হয়েছে — পরবর্তী সেশন থেকে কার্যকর হবে',
    keyEmpty: 'চাবি ফাঁকা রাখা যাবে না',
    keySavedOk: 'API চাবি সংরক্ষিত হয়েছে এবং জ্ঞান ভাণ্ডার পুনরায় ইনডেক্স হয়েছে',
    reindexOk: 'জ্ঞান ভাণ্ডার পুনরায় ইনডেক্স হয়েছে',
    reindexFailed: 'পুনরায় ইনডেক্স করা যায়নি',
    noFiles: 'কোনো ফাইল পাওয়া যায়নি',
    filesUnit: 'টি ফাইল',
    chunksUnit: 'টি অংশ',
    semanticMode: 'অর্থভিত্তিক + শব্দভিত্তিক',
    keywordMode: 'শব্দভিত্তিক',
    searchSuffix: ' খোঁজ',
    deleteFile: 'মুছুন',
    confirmDeletePrefix: 'আপনি কি নিশ্চিতভাবে মুছে ফেলতে চান: ',
    confirmDeleteSuffix: ' ? এটি পূর্বাবস্থায় ফেরানো যাবে না।',
    fileDeletedOk: 'ফাইল মুছে ফেলা হয়েছে এবং জ্ঞান ভাণ্ডার হালনাগাদ হয়েছে',
    keyManagedSuffix: ' — হোস্টিং ড্যাশবোর্ডে সেট করা (যেমন Render → Environment); পরিবর্তন সেখান থেকেই করুন',
    keyManagedPlaceholder: 'হোস্টিং ড্যাশবোর্ড থেকে পরিবর্তন করুন',
  },
  en: {
    panelTitle: 'Admin panel',
    backToVoice: '← Voice assistant',
    logout: 'Log out',
    voiceTitle: 'Voice',
    voicePreset: 'Voice preset',
    voiceHint: 'Changes take effect on the next session \u2014 they won\u2019t affect a call in progress.',
    behaviorTitle: 'Behavior',
    echoGuardLabel: 'Mute mic while assistant speaks (default)',
    showTranscriptionLabel: 'Show transcription of the conversation',
    showOnlyAssistantLabel: 'Show only the assistant\u2019s speech (not the user\u2019s)',
    showRetrievalLogsLabel: 'Show document search logs',
    retrievalTitle: 'Retrieval (RAG)',
    minScoreLabel: 'Minimum match score:',
    minScoreHint: 'Higher requires a closer match \u2014 fewer weak answers get through, but some valid ones may be dropped too.',
    topKLabel: 'Passages sent per question',
    retrievalNextSession: 'These two also take effect on the next session.',
    interfaceTitle: 'Interface',
    orbSpeedLabel: 'Orb animation speed',
    speedSlow: 'Slow', speedNormal: 'Normal', speedFast: 'Fast',
    fontSizeLabel: 'Transcript font size',
    sizeSmall: 'Small', sizeNormal: 'Normal', sizeLarge: 'Large',
    apiKeyTitle: 'Gemini API key',
    newApiKeyLabel: 'New API key',
    saveBtn: 'Save',
    apiKeyHint: 'A new key re-indexes the knowledge base right away and takes effect in this server process immediately.',
    kbTitle: 'Knowledge base',
    reloadKbBtn: 'Re-index now',
    uploadDocs: 'Upload documents',
    saveAllBtn: 'Save all changes',
    savingBtn: 'Saving\u2026',
    reindexingBtn: 'Indexing\u2026',
    currentKeyPrefix: 'Current key: ',
    noKeySet: 'not set',
    settingsLoadError: 'Could not load settings',
    saveFailed: 'Save failed',
    saveAllOk: 'All changes saved \u2014 will take effect next session',
    keyEmpty: 'Key cannot be empty',
    keySavedOk: 'API key saved and the knowledge base was re-indexed',
    reindexOk: 'Knowledge base re-indexed',
    reindexFailed: 'Could not re-index',
    noFiles: 'No files found',
    filesUnit: ' files',
    chunksUnit: ' passages',
    semanticMode: 'semantic + keyword',
    keywordMode: 'keyword',
    searchSuffix: ' search',
    deleteFile: 'Delete',
    confirmDeletePrefix: 'Are you sure you want to delete: ',
    confirmDeleteSuffix: '? This cannot be undone.',
    fileDeletedOk: 'File deleted and the knowledge base was updated',
    keyManagedSuffix: ' — set in the hosting dashboard (e.g. Render → Environment); change it there',
    keyManagedPlaceholder: 'Change it in the hosting dashboard',
  },
};

let lang = localStorage.getItem('brac-admin-lang') === 'en' ? 'en' : 'bn';

function t(key) {
  return I18N[lang][key] ?? key;
}

function applyStaticI18n() {
  document.documentElement.lang = lang;
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    const key = el.dataset.i18n;
    if (I18N[lang][key]) el.textContent = I18N[lang][key];
  });
  els.langBn.classList.toggle('active', lang === 'bn');
  els.langEn.classList.toggle('active', lang === 'en');
}

function setLang(next) {
  lang = next;
  localStorage.setItem('brac-admin-lang', lang);
  applyStaticI18n();
  refreshDynamicText();
}

els.langBn.addEventListener('click', () => setLang('bn'));
els.langEn.addEventListener('click', () => setLang('en'));
applyStaticI18n();

let lastKbInfo = null;
let lastMaskedKey = '';
let keyManaged = false;

function refreshDynamicText() {
  if (lastKbInfo) renderKb(lastKbInfo);
  els.currentKeyHint.textContent =
    `${t('currentKeyPrefix')}${lastMaskedKey || t('noKeySet')}${keyManaged ? t('keyManagedSuffix') : ''}`;
  // A key set by the host would come back on the next restart, so don't
  // offer a form that looks like it changes it.
  els.apiKeyInput.disabled = keyManaged;
  els.saveKeyBtn.disabled = keyManaged;
  els.apiKeyInput.placeholder = keyManaged ? t('keyManagedPlaceholder') : 'AIza...';
}

/* ------------------------------------------------------------------ *
 * settings load/save
 * ------------------------------------------------------------------ */
function showBanner(text, kind) {
  els.saveBanner.textContent = text;
  els.saveBanner.className = `save-banner show ${kind}`;
  setTimeout(() => { els.saveBanner.classList.remove('show'); }, 3500);
}

function renderKb(info) {
  lastKbInfo = info;
  const mode = info.semantic ? t('semanticMode') : t('keywordMode');
  els.kbSummary.textContent =
    `${info.files.length}${t('filesUnit')} \u00b7 ${info.chunks}${t('chunksUnit')} \u00b7 ${mode}${t('searchSuffix')}`;
  els.kbFileList.innerHTML = '';
  if (!info.files.length) {
    const li = document.createElement('li');
    li.className = 'empty-note';
    li.textContent = t('noFiles');
    els.kbFileList.appendChild(li);
    return;
  }
  info.files.forEach((f) => {
    const li = document.createElement('li');
    li.className = 'file-item';

    const name = document.createElement('span');
    name.className = 'file-name';
    name.textContent = f;

    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'file-delete';
    delBtn.setAttribute('aria-label', t('deleteFile'));
    delBtn.title = t('deleteFile');
    delBtn.textContent = '\u2715';
    delBtn.addEventListener('click', () => deleteKbFile(f));

    li.appendChild(name);
    li.appendChild(delBtn);
    els.kbFileList.appendChild(li);
  });
}

async function deleteKbFile(filename) {
  const ok = window.confirm(`${t('confirmDeletePrefix')}${filename}${t('confirmDeleteSuffix')}`);
  if (!ok) return;

  try {
    const res = await fetch(`/api/admin/knowledge/${encodeURIComponent(filename)}`, {
      method: 'DELETE',
    });
    if (res.status === 401) { window.location.replace('/admin'); return; }
    const info = await res.json();
    if (!res.ok) throw new Error(info.error || t('saveFailed'));
    renderKb(info);
    showBanner(t('fileDeletedOk'), 'ok');
  } catch (err) {
    showBanner(err.message, 'error');
  }
}

async function loadSettings() {
  try {
    const res = await fetch('/api/admin/settings');
    if (res.status === 401) { window.location.replace('/admin'); return; }
    const s = await res.json();

    els.voice.innerHTML = s._valid_voices
      .map((v) => `<option value="${v}">${v}</option>`)
      .join('');
    els.voice.value = s.voice;

    els.echoGuard.checked = s.echo_guard_default;
    els.showTranscription.checked = s.show_transcription;
    els.showOnlyAssistant.checked = s.show_only_assistant;
    els.showRetrievalLogs.checked = s.show_retrieval_logs;

    els.minScore.value = s.min_score;
    els.minScoreValue.textContent = s.min_score.toFixed(2);

    els.ragTopK.value = String(s.rag_top_k);
    els.orbSpeed.value = s.orb_speed;
    els.fontSize.value = s.font_size;

    lastMaskedKey = s._api_key_masked || '';
    keyManaged = !!s._api_key_managed;
    renderKb(s._knowledge);
    refreshDynamicText();
  } catch (err) {
    showBanner(t('settingsLoadError'), 'error');
  }
}

els.minScore.addEventListener('input', () => {
  els.minScoreValue.textContent = parseFloat(els.minScore.value).toFixed(2);
});

async function saveAll() {
  els.saveAllBtn.disabled = true;
  els.saveAllBtn.textContent = t('savingBtn');
  try {
    const patch = {
      voice: els.voice.value,
      echo_guard_default: els.echoGuard.checked,
      show_transcription: els.showTranscription.checked,
      show_only_assistant: els.showOnlyAssistant.checked,
      show_retrieval_logs: els.showRetrievalLogs.checked,
      min_score: parseFloat(els.minScore.value),
      rag_top_k: parseInt(els.ragTopK.value, 10),
      orb_speed: els.orbSpeed.value,
      font_size: els.fontSize.value,
    };
    const res = await fetch('/api/admin/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    if (res.status === 401) { window.location.replace('/admin'); return; }
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || t('saveFailed'));
    showBanner(t('saveAllOk'), 'ok');
  } catch (err) {
    showBanner(err.message, 'error');
  } finally {
    els.saveAllBtn.disabled = false;
    els.saveAllBtn.textContent = t('saveAllBtn');
  }
}
els.saveAllBtn.addEventListener('click', saveAll);

async function saveApiKey() {
  const key = els.apiKeyInput.value.trim();
  if (!key) { showBanner(t('keyEmpty'), 'error'); return; }

  els.saveKeyBtn.disabled = true;
  els.saveKeyBtn.textContent = t('savingBtn');
  try {
    const res = await fetch('/api/admin/apikey', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: key }),
    });
    if (res.status === 401) { window.location.replace('/admin'); return; }
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || t('saveFailed'));

    lastMaskedKey = data.masked;
    els.apiKeyInput.value = '';
    renderKb(data.knowledge);
    refreshDynamicText();
    showBanner(t('keySavedOk'), 'ok');
  } catch (err) {
    showBanner(err.message, 'error');
  } finally {
    els.saveKeyBtn.disabled = keyManaged;
    els.saveKeyBtn.textContent = t('saveBtn');
  }
}
els.saveKeyBtn.addEventListener('click', saveApiKey);

async function reloadKb() {
  els.reloadKbBtn.disabled = true;
  els.reloadKbBtn.textContent = t('reindexingBtn');
  try {
    const res = await fetch('/api/admin/knowledge/reload', { method: 'POST' });
    if (res.status === 401) { window.location.replace('/admin'); return; }
    const info = await res.json();
    renderKb(info);
    showBanner(t('reindexOk'), 'ok');
  } catch (err) {
    showBanner(t('reindexFailed'), 'error');
  } finally {
    els.reloadKbBtn.disabled = false;
    els.reloadKbBtn.textContent = t('reloadKbBtn');
  }
}
els.reloadKbBtn.addEventListener('click', reloadKb);

els.logoutBtn.addEventListener('click', async () => {
  await fetch('/api/admin/logout', { method: 'POST' });
  window.location.href = '/';
});

loadSettings();
