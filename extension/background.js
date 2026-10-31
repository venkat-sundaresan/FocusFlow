// Service worker: the only part that talks to the FocusFlow desktop agent on 127.0.0.1.
// Content scripts message us; web pages can never reach the agent or the pairing token.
const PORTS = Array.from({ length: 10 }, (_, i) => 47321 + i);
const CONFIG_TTL_MS = 2000;
const MAX_QUEUE = 200;

let base = null;           // e.g. http://127.0.0.1:47321
let token = null;
let configCache = { at: 0, value: null };
const queue = [];          // events waiting for the agent (kept in memory only)

async function timedFetch(url, opts = {}, ms = 800) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try { return await fetch(url, { ...opts, signal: ctl.signal }); } finally { clearTimeout(timer); }
}

async function findBase() {
  if (base) {
    try { const r = await timedFetch(base + '/api/ping', {}, 500); if (r.ok) return base; } catch (e) { /* fall through */ }
    base = null;
  }
  for (const p of PORTS) {
    try {
      const r = await timedFetch(`http://127.0.0.1:${p}/api/ping`, {}, 400);
      const j = await r.json();
      if (j && j.app === 'focusflow') { base = `http://127.0.0.1:${p}`; return base; }
    } catch (e) { /* try next port */ }
  }
  return null;
}

async function ensureToken(force) {
  if (!force && token) return token;
  if (!force) {
    const saved = await chrome.storage.local.get('token');
    if (saved.token) { token = saved.token; return token; }
  }
  if (!base) return null;
  try {
    const r = await timedFetch(base + '/api/pair', { method: 'POST' });   // agent only answers if auto-pairing is enabled
    if (r.ok) { token = (await r.json()).token; await chrome.storage.local.set({ token }); return token; }
  } catch (e) { /* ignore */ }
  return null;
}

async function api(path, body, retry = true) {
  if (!(await findBase())) return null;
  if (!(await ensureToken(false))) return null;
  const opts = { headers: { 'X-FocusFlow-Token': token } };
  if (body !== undefined) {
    opts.method = 'POST';
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  try {
    const r = await timedFetch(base + path, opts);
    if (r.status === 401 && retry) { token = null; await chrome.storage.local.remove('token'); await ensureToken(true); return api(path, body, false); }
    return r.ok ? await r.json() : null;
  } catch (e) { base = null; return null; }
}

let flushing = false;
async function flush() {
  if (flushing) return;       // single flight: a second flusher would resend the same head event
  flushing = true;
  try {
    while (queue.length) {
      const ev = queue[0];
      const ok = await api('/api/ext/event', ev);
      if (!ok) return;        // agent not reachable: keep the rest for later
      queue.shift();
    }
  } finally { flushing = false; }
}

async function getConfig() {
  if (Date.now() - configCache.at < CONFIG_TTL_MS) return configCache.value;
  const cfg = await api('/api/ext/config');
  configCache = { at: Date.now(), value: cfg };
  if (cfg) flush();
  return cfg;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    switch (msg.type) {
      case 'config': sendResponse(await getConfig()); break;
      case 'pos': sendResponse(await api('/api/ext/pos', msg.body)); break;
      case 'leave': sendResponse(await api('/api/ext/leave', {})); break;
      case 'event':
        queue.push(msg.body);
        if (queue.length > MAX_QUEUE) queue.shift();
        await flush();
        sendResponse({ queued: queue.length });
        break;
      case 'status': {
        const b = await findBase();
        const t = b ? await ensureToken(false) : null;
        const cfg = t ? await api('/api/ext/config') : null;
        sendResponse({ agent: !!b, paired: !!cfg, port: b ? Number(b.split(':').pop()) : null });
        break;
      }
      case 'set-token':
        token = String(msg.token || '').trim() || null;
        if (token) await chrome.storage.local.set({ token }); else await chrome.storage.local.remove('token');
        configCache = { at: 0, value: null };
        sendResponse({ ok: true });
        break;
      default: sendResponse(null);
    }
  })();
  return true;   // async response
});
