// Movie Studio — SSE chat + inline multimodal rendering + 3D tilt + gallery/export.
const feed = document.getElementById('feed');
const form = document.getElementById('composer');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const gallery = document.getElementById('gallery');
const exportAllBtn = document.getElementById('exportAll');
const activityEl = document.getElementById('activity');
const tabGallery = document.getElementById('tabGallery');
const tabActivity = document.getElementById('tabActivity');
const dot = document.getElementById('dot');
const statusText = document.getElementById('statusText');
const sessionList = document.getElementById('sessionList');
const newSessionBtn = document.getElementById('newSession');
const loginOverlay = document.getElementById('login');
const loginForm = document.getElementById('loginForm');
const ldapInput = document.getElementById('ldapInput');
const userChip = document.getElementById('userChip');
const uploadBtn = document.getElementById('uploadBtn');
const uploadModal = document.getElementById('uploadModal');
const uploadForm = document.getElementById('uploadForm');
const uploadKind = document.getElementById('uploadKind');
const uploadName = document.getElementById('uploadName');
const uploadFile = document.getElementById('uploadFile');
const uploadCancel = document.getElementById('uploadCancel');
const uploadMsg = document.getElementById('uploadMsg');

let USER = localStorage.getItem('ldap') || '';
let CURRENT_PROJECT = null;         // learned from the stream; needed to attach uploads
let SESSION = null;
let SESSIONS = [];                  // persisted sessions for this user (their LDAP)
let busy = false;
const seenAssets = new Set();      // dedup gallery by url
const galleryUrls = [];            // for export-all
const galleryThumbs = {};          // base url -> thumb media el (to refresh on regenerate)

const AUDIO_SVG = '<svg viewBox="0 0 24 24" fill="none"><path d="M9 18V6l10-2v12" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><circle cx="6" cy="18" r="3" stroke="currentColor" stroke-width="1.6"/><circle cx="19" cy="16" r="3" stroke="currentColor" stroke-width="1.6"/></svg>';
const DL_SVG = '<svg viewBox="0 0 24 24" fill="none"><path d="M12 3v12m0 0l-4-4m4 4l4-4M5 21h14" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

// ---- sessions: retained per user; run several in parallel ----
async function loadSessions() {
  try {
    const d = await (await fetch('/sessions?user=' + encodeURIComponent(USER))).json();
    SESSIONS = d.sessions || [];
    if (!SESSIONS.length) await createSession();
    else { renderSessions(); await selectSession(SESSIONS[SESSIONS.length - 1].id); }
    dot.classList.add('on'); statusText.textContent = 'ready';
  } catch { statusText.textContent = 'offline'; }
}

async function createSession() {
  const meta = await (await fetch('/sessions?user=' + encodeURIComponent(USER), { method: 'POST' })).json();
  SESSIONS.push(meta); renderSessions();
  await selectSession(meta.id);
  return meta;
}

async function selectSession(sid) {
  if (busy) return;                       // don't switch mid-turn on this page
  SESSION = sid; renderSessions(); resetView();
  try {
    const d = await (await fetch(`/sessions/${encodeURIComponent(sid)}/history?user=` + encodeURIComponent(USER))).json();
    renderHistory(d.events || []);
  } catch {}
}

function renderSessions() {
  sessionList.innerHTML = '';
  SESSIONS.forEach(s => {
    const chip = el('button', 'session-chip' + (s.id === SESSION ? ' active' : ''));
    chip.textContent = s.title; chip.title = s.title;
    chip.addEventListener('click', () => selectSession(s.id));
    sessionList.append(chip);
  });
}

function resetView() {
  feed.innerHTML = '';
  gallery.innerHTML = '<p class="empty">Generated frames, clips &amp; scores appear here.</p>';
  if (activityEl) activityEl.innerHTML = '<p class="empty">The director\'s steps — skills loaded, tools called, results — stream here live.</p>';
  seenAssets.clear(); galleryUrls.length = 0; exportAllBtn.disabled = true;
}

// ---- activity panel: how the director is thinking (skills, tools, results, errors) ----
function escapeHtml(s) { return (s + '').replace(/[&<>"]/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;' }[c])); }

function addActivity(d) {
  if (!activityEl) return;
  if (activityEl.querySelector('.empty')) activityEl.innerHTML = '';
  const icon = { skill:'📚', tool:'🛠', result:'✓', thought:'💭', error:'⚠' }[d.kind] || '•';
  const item = el('div', 'act-item act-' + d.kind);
  let body = '';
  if (d.kind === 'skill' || d.kind === 'tool') {
    const pretty = prettyTool(d.name);
    body = `<b>${escapeHtml(pretty)}</b>` + (pretty !== d.name ? ` <span class="mono">${escapeHtml(d.name)}</span>` : '');
    const keys = d.args ? Object.keys(d.args) : [];
    if (keys.length) body += `<div class="act-args mono">${escapeHtml(keys.map(k => `${k}: ${d.args[k]}`).join('  ·  '))}</div>`;
  } else if (d.kind === 'result') {
    body = `<b>${escapeHtml(prettyTool(d.name))}</b>` + (d.summary ? `<div class="act-args mono">${escapeHtml(d.summary)}</div>` : ' <span class="act-args">done</span>');
  } else if (d.kind === 'thought') {
    body = `<span class="act-thought">${escapeHtml(d.text)}</span>`;
  } else if (d.kind === 'error') {
    body = `<span class="act-err">${escapeHtml(d.text)}</span>`;
  }
  item.innerHTML = `<span class="act-ico">${icon}</span><div class="act-body">${body}</div>`;
  activityEl.append(item); activityEl.scrollTop = activityEl.scrollHeight;
  if (activityEl.hidden && tabActivity) tabActivity.classList.add('has-new');
}

// ---- rail tabs (Gallery | Activity) ----
function showTab(which) {
  const g = which === 'gallery';
  if (gallery) gallery.hidden = !g;
  if (activityEl) activityEl.hidden = g;
  if (tabGallery) tabGallery.classList.toggle('active', g);
  if (tabActivity) { tabActivity.classList.toggle('active', !g); if (!g) tabActivity.classList.remove('has-new'); }
  if (exportAllBtn) exportAllBtn.style.display = g ? '' : 'none';
}
if (tabGallery) tabGallery.addEventListener('click', () => showTab('gallery'));
if (tabActivity) tabActivity.addEventListener('click', () => showTab('activity'));

// ---- uploads: bring your own character / prop (optional) ----
function setProject(pid) { if (pid && pid !== CURRENT_PROJECT) CURRENT_PROJECT = pid; }
function captureProject(url) { const m = (url || '').match(/\/asset\/[^/]+\/([^/]+)\//); if (m) setProject(m[1]); }
function captureProjectFromText(s) { const m = (s || '').match(/project_id[=:]\s*([A-Za-z0-9_-]{4,})/); if (m) setProject(m[1]); }

if (uploadBtn) uploadBtn.addEventListener('click', () => {
  if (!CURRENT_PROJECT) {
    alert('Start your story first — once the director creates the project, you can upload characters/props into it.');
    return;
  }
  uploadMsg.textContent = ''; uploadMsg.className = 'up-msg';
  uploadModal.hidden = false; uploadName.focus();
});
if (uploadCancel) uploadCancel.addEventListener('click', () => { uploadModal.hidden = true; });
if (uploadForm) uploadForm.addEventListener('submit', async e => {
  e.preventDefault();
  const f = uploadFile.files[0], nm = (uploadName.value || '').trim();
  if (!f || !nm) { uploadMsg.textContent = 'Pick an image and enter a name.'; uploadMsg.className = 'up-msg err'; return; }
  uploadMsg.textContent = 'Uploading…'; uploadMsg.className = 'up-msg';
  try {
    const q = new URLSearchParams({ user: USER, project: CURRENT_PROJECT, name: nm, kind: uploadKind.value });
    const r = await fetch('/upload?' + q.toString(), { method: 'POST', headers: { 'Content-Type': f.type || 'image/png' }, body: f });
    const d = await r.json();
    if (!r.ok || d.error) { uploadMsg.textContent = d.error || 'Upload failed'; uploadMsg.className = 'up-msg err'; return; }
    uploadMsg.textContent = `✓ Added ${uploadKind.value} “${nm}”`; uploadMsg.className = 'up-msg ok';
    if (d.asset_url) addToGallery({ kind: 'image', url: d.asset_url, name: nm, src: d.asset_url + '?v=' + Date.now() });
    const note = addAiMsg(); note.thinking.remove();
    note.text = `📎 Uploaded ${uploadKind.value} “${nm}”. Ask me to use it and I'll bring it into your scenes (I won't regenerate it).`;
    note.bubble.textContent = note.text; scrollDown();
    setTimeout(() => { uploadModal.hidden = true; uploadForm.reset(); }, 1000);
  } catch (err) { uploadMsg.textContent = 'Upload error: ' + err; uploadMsg.className = 'up-msg err'; }
});

function renderHistory(events) {
  let ai = null;
  for (const d of events) {
    if (d.type === 'text' && d.role === 'you') { addUserMsg(d.text); ai = null; }
    else if (d.type === 'text') {
      if (!ai) { ai = addAiMsg(); ai.thinking.remove(); }
      ai.text += (ai.text ? '\n' : '') + d.text; ai.bubble.textContent = ai.text;
    } else if (d.type === 'media') {
      if (!ai) { ai = addAiMsg(); ai.thinking.remove(); }
      addMediaCard(ai.media, d); addToGallery(d);
    }
  }
  scrollDown();
}

newSessionBtn.addEventListener('click', () => { if (!busy) createSession(); });

// ---- login gate: LDAP as an identity / workspace key (no auth) ----
function showUser() {
  if (!userChip) return;
  userChip.hidden = !USER;
  userChip.textContent = USER ? '👤 ' + USER + ' · sign out' : '';
}
if (userChip) userChip.addEventListener('click', () => { localStorage.removeItem('ldap'); location.reload(); });
if (loginForm) loginForm.addEventListener('submit', e => {
  e.preventDefault();
  const v = (ldapInput.value || '').trim();
  if (!v) return;
  USER = v; localStorage.setItem('ldap', USER);
  if (loginOverlay) loginOverlay.hidden = true;
  showUser(); loadSessions();
});

function boot() {
  if (USER) { showUser(); loadSessions(); }
  else if (loginOverlay) { loginOverlay.hidden = false; ldapInput && ldapInput.focus(); }
  else { loadSessions(); }            // fallback if the overlay markup is absent
}
boot();

// ---- composer ----
input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 140) + 'px';
});
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});
form.addEventListener('submit', e => { e.preventDefault(); send(); });

function send() {
  const text = input.value.trim();
  if (!text || busy || !SESSION) return;
  addUserMsg(text);
  input.value = ''; input.style.height = 'auto';
  const ai = addAiMsg();
  runTurn(text, ai);
}

// ---- message builders ----
function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
}
function addUserMsg(text) {
  const m = el('div', 'msg user');
  m.append(el('div', 'who', 'you'), el('div', 'bubble'));
  m.querySelector('.bubble').textContent = text;
  feed.append(m); scrollDown();
}
function addAiMsg() {
  const m = el('div', 'msg ai');
  const who = el('div', 'who', 'director');
  const chips = el('div', 'chips');
  const bubble = el('div', 'bubble');
  const thinking = el('span', 'thinking', '<i></i><i></i><i></i>');
  bubble.append(thinking);
  const media = el('div', 'media');
  m.append(who, chips, bubble, media);
  feed.append(m); scrollDown();
  return { m, chips, bubble, thinking, media, text: '' };
}
function scrollDown() { feed.scrollTop = feed.scrollHeight; }

// ---- run one turn over SSE ----
function runTurn(message, ai) {
  setBusy(true);
  const url = `/chat/stream?session=${encodeURIComponent(SESSION)}&message=${encodeURIComponent(message)}&user=${encodeURIComponent(USER)}`;
  const es = new EventSource(url);
  const activeChips = {};

  es.onmessage = ev => {
    let d; try { d = JSON.parse(ev.data); } catch { return; }
    if (d.type === 'session') {
      // server created/normalized the id (e.g. after a restart) — adopt it
      SESSION = d.id;
      if (!SESSIONS.find(s => s.id === d.id)) {
        SESSIONS.push({ id: d.id, title: 'Session ' + (SESSIONS.length + 1), created: Date.now() / 1000 });
      }
      renderSessions();
    } else if (d.type === 'tool') {
      // mark prior chips done, add a new working chip
      Object.values(activeChips).forEach(c => c.classList.add('done'));
      const c = el('span', 'chip', `<span class="spin"></span>${prettyTool(d.name)}`);
      ai.chips.append(c); activeChips[d.name + Math.random()] = c; scrollDown();
    } else if (d.type === 'activity') {
      if (d.summary) captureProjectFromText(d.summary);
      addActivity(d);
    } else if (d.type === 'text') {
      ai.text += (ai.text ? '\n' : '') + d.text;
      ai.bubble.textContent = ai.text; scrollDown();
    } else if (d.type === 'media') {
      ai.thinking.remove();
      captureProject(d.url);
      d.src = d.url + (d.url.includes('?') ? '&' : '?') + 'v=' + Date.now();  // bust cache on regenerate
      addMediaCard(ai.media, d); addToGallery(d); scrollDown();
    } else if (d.type === 'error') {
      ai.text += (ai.text ? '\n' : '') + '⚠ ' + d.text;
      ai.bubble.textContent = ai.text;
    } else if (d.type === 'done') {
      es.close();
      Object.values(activeChips).forEach(c => c.classList.add('done'));
      ai.thinking.remove();
      if (!ai.text && !ai.media.children.length) ai.bubble.textContent = '(no response)';
      setBusy(false); scrollDown();
    }
  };
  es.onerror = () => { es.close(); ai.thinking.remove(); setBusy(false); };
}

function prettyTool(name) {
  return ({
    generate_style_ref: 'defining the look', add_character: 'casting a character',
    establish_scene: 'building the set', generate_microshot: 'storyboarding (3 frames)',
    generate_shot: 'rendering a keyframe', start_scene_video: 'animating the scene',
    get_scene_video: 'finishing the clip', start_shot_video: 'animating a shot',
    generate_music: 'composing the score', plan_scene: 'planning shots',
    create_project: 'creating the project', list_project_assets: 'gathering assets'
  })[name] || name;
}

// ---- media cards (inline, 3D tilt) ----
function addMediaCard(container, d) {
  const card = el('div', 'card');
  const src = d.src || d.url;
  let inner = '';
  if (d.kind === 'image') inner = `<img src="${src}" alt="${d.name}" loading="lazy">`;
  else if (d.kind === 'video') inner = `<video src="${src}" controls preload="metadata" playsinline></video>`;
  else if (d.kind === 'audio') inner = `<audio src="${src}" controls preload="metadata"></audio>`;
  card.innerHTML = inner +
    `<div class="cap"><span class="tool">${d.tool ? prettyTool(d.tool) : d.kind}</span>
       <a class="dl" href="${d.url}?download=1" download="${d.name}">${DL_SVG}Download</a></div>`;
  container.append(card);
  attachTilt(card);
}

function attachTilt(card) {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  card.addEventListener('pointermove', e => {
    const r = card.getBoundingClientRect();
    const px = (e.clientX - r.left) / r.width - .5;
    const py = (e.clientY - r.top) / r.height - .5;
    card.style.transform = `perspective(900px) rotateY(${px * 8}deg) rotateX(${-py * 8}deg) translateY(-2px)`;
  });
  card.addEventListener('pointerleave', () => { card.style.transform = ''; });
}

// ---- gallery + export ----
function addToGallery(d) {
  const src = d.src || d.url;
  if (seenAssets.has(d.url)) {                     // regenerated → refresh the existing thumb in place
    const m = galleryThumbs[d.url]; if (m) m.src = src;
    return;
  }
  seenAssets.add(d.url); galleryUrls.push(d);
  if (gallery.querySelector('.empty')) gallery.innerHTML = '';
  const t = el('div', 'thumb' + (d.kind === 'audio' ? ' audio' : ''));
  if (d.kind === 'image') t.innerHTML = `<img src="${src}" alt="${d.name}">`;
  else if (d.kind === 'video') t.innerHTML = `<video src="${src}" muted preload="metadata"></video><span class="badge">clip</span>`;
  else t.innerHTML = AUDIO_SVG + `<span class="badge">score</span>`;
  t.title = d.name;
  t.addEventListener('click', () => window.open(src, '_blank'));
  const mediaEl = t.querySelector('img,video'); if (mediaEl) galleryThumbs[d.url] = mediaEl;
  gallery.append(t);
  exportAllBtn.disabled = false;
}
exportAllBtn.addEventListener('click', () => {
  galleryUrls.forEach((d, i) => setTimeout(() => {
    const a = document.createElement('a');
    a.href = d.url + '?download=1'; a.download = d.name;
    document.body.append(a); a.click(); a.remove();
  }, i * 400));
});

function setBusy(v) {
  busy = v; sendBtn.disabled = v;
  dot.classList.toggle('busy', v); dot.classList.toggle('on', !v);
  statusText.textContent = v ? 'directing…' : 'ready';
}
