// FocusFlow Web Assist: runs in every page/frame. Uses real DOM geometry (getBoundingClientRect,
// elementFromPoint) instead of guessing from screen coordinates. Settings come from the desktop agent.
(() => {
  'use strict';
  if (window.__focusflowLoaded) return;
  window.__focusflowLoaded = true;
  const C = window.FocusFlowCore;
  if (!C) return;

  const INTERACTIVE = [
    'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea', 'summary', 'label[for]',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=menuitem]', '[role=tab]', '[role=radio]',
    '[role=switch]', '[role=option]', '[role=combobox]', '[contenteditable=""]', '[contenteditable=true]',
    '[onclick]', '[tabindex]:not([tabindex="-1"])'
  ].join(',');
  const TICK_MS = 16;
  const GATE_PX = 30;
  const ASSIST_HOLD_S = 0.35;   // hover this long beside a target before snapping to it (aiming is not a miss)
  const COLORS = { ring: '#FFE14D', assist: '#FF4FD8' };

  let cfg = null, cfgAt = 0;
  const motion = new C.Motion(5, 250);
  const dwell = new C.Dwell();
  let last = null;               // {x, y} client coordinates of the pointer
  let buttons = 0;
  let timer = null;
  let gatePos = null, gateOpen = false;
  let settleAnchor = null, settledSince = 0;
  let cur = null, curAssisted = false;   // target we are engaged with: {el, rect}
  let episodeEl = null, missLoggedEl = null;
  let candCache = { at: 0, list: [] };
  let nearCache = { x: -1, y: -1, at: 0, res: null };
  let lastPosSent = 0;
  const ids = new WeakMap();
  let nextId = 1;
  const idOf = el => { if (!ids.has(el)) ids.set(el, nextId++); return ids.get(el); };
  const dpr = () => window.devicePixelRatio || 1;
  const send = (msg) => { try { return chrome.runtime.sendMessage(msg).catch(() => null); } catch (e) { return Promise.resolve(null); } };

  // ---- configuration (from the agent; assist stays off if the agent is not running) -------------
  async function refreshConfig(force) {
    if (!force && Date.now() - cfgAt < 4000) return;
    cfgAt = Date.now();
    const c = await send({ type: 'config' });
    cfg = c || null;
    if (cfg) { motion.sensitivity = cfg.sensitivity; motion.hoverSpeed = cfg.hover_speed; }
  }

  // ---- DOM geometry -------------------------------------------------------------------------------
  function usable(el) {
    if (el.disabled || el.getAttribute('aria-disabled') === 'true' || el.hidden) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.pointerEvents !== 'none' && st.display !== 'none';
  }
  function rectOf(el) {
    const r = el.getBoundingClientRect();
    return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
  }
  function candidates() {
    const now = performance.now();
    if (now - candCache.at < 400) return candCache.list;
    const vw = window.innerWidth, vh = window.innerHeight, out = [];
    for (const el of document.querySelectorAll(INTERACTIVE)) {
      const r = rectOf(el);
      if (r.width < 6 || r.height < 6 || r.right < 0 || r.bottom < 0 || r.left > vw || r.top > vh) continue;
      if (r.width * r.height > 0.35 * vw * vh) continue;     // a container, not a button
      if (!usable(el)) continue;
      out.push({ el, rect: r });
    }
    candCache = { at: now, list: out };
    return out;
  }
  function interactiveAncestor(el) {
    const t = el && el.closest ? el.closest(INTERACTIVE) : null;
    return t && usable(t) ? t : null;
  }
  function underTarget(x, y) {
    const hit = document.elementFromPoint(x, y);
    const el = interactiveAncestor(hit);
    return el ? { el, rect: rectOf(el) } : null;
  }
  // A candidate's rectangle is only a hint (an <a> can span blank space, a modal can cover it).
  // Verify that a point inside it really hits it, and remember that point.
  function verifiedHit(c, x, y) {
    const r = c.rect, inset = 2;
    const px = Math.min(Math.max(x, r.left + inset), r.right - inset);
    const py = Math.min(Math.max(y, r.top + inset), r.bottom - inset);
    const centre = [(r.left + r.right) / 2, (r.top + r.bottom) / 2];
    for (const [qx, qy] of [centre, [px, py]]) {
      const hit = document.elementFromPoint(qx, qy);
      if (hit && (hit === c.el || c.el.contains(hit))) return [qx, qy];
    }
    return null;
  }
  function findNearest(x, y, radiusCss) {
    const now = performance.now();
    if (nearCache.res && nearCache.radius === radiusCss && Math.hypot(nearCache.x - x, nearCache.y - y) < 2 && now - nearCache.at < 250) return nearCache.res;
    const ok = candidates().filter(c => verifiedHit(c, x, y));
    const res = C.nearestUnambiguous(x, y, ok, radiusCss);
    nearCache = { x, y, at: now, res, radius: radiusCss };
    return res;
  }
  function labelOf(el) {
    if (!cfg || !cfg.log_names) return el.tagName.toLowerCase();
    if (el.type === 'password') return 'password field';
    let s = el.getAttribute('aria-label') || el.getAttribute('title') || '';
    if (!s && !(el instanceof HTMLInputElement) && !(el instanceof HTMLTextAreaElement)) s = el.textContent || '';
    if (!s) s = el.getAttribute('placeholder') || el.type || el.tagName.toLowerCase();   // never the typed value
    return s.replace(/\s+/g, ' ').trim().slice(0, 40);
  }
  function hasOwnText(el) {
    for (const n of el.childNodes) if (n.nodeType === 3 && n.nodeValue.trim()) return true;
    return false;
  }

  // ---- reporting -------------------------------------------------------------------------------------
  function report(kind, el, detail) {
    send({ type: 'event', body: { kind, name: el ? labelOf(el) : null,
                                   detail: Object.assign({ host: location.hostname }, detail || {}) } });
  }

  // ---- overlay: progress ring + target outline (shadow DOM, click-through) -----------------------------
  let ov = null;
  function overlay() {
    if (ov) return ov;
    const host = document.createElement('div');
    host.style.cssText = 'all:initial;position:fixed;inset:0;pointer-events:none;z-index:2147483647;';
    const root = host.attachShadow({ mode: 'closed' });
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('style', 'position:fixed;inset:0;width:100%;height:100%;pointer-events:none');
    const mk = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); svg.appendChild(e); return e; };
    ov = {
      host, root, svg,
      rect: mk('rect', { fill: 'none', 'stroke-width': 4, rx: 4 }),
      track: mk('circle', { r: 28, fill: 'none', stroke: '#000', 'stroke-width': 15 }),
      ring: mk('circle', { r: 28, fill: 'none', stroke: '#fff', 'stroke-width': 2 }),
      arc: mk('circle', { r: 28, fill: 'none', 'stroke-width': 9, transform: '' }),
      shown: false
    };
    root.appendChild(svg);
    return ov;
  }
  function draw(target, progress, assisted, x, y) {
    const o = overlay();
    if (!o.shown) { (document.documentElement || document.body).appendChild(o.host); o.shown = true; }
    const color = assisted ? COLORS.assist : COLORS.ring;
    const r = target.rect;
    o.rect.setAttribute('x', r.left - 3); o.rect.setAttribute('y', r.top - 3);
    o.rect.setAttribute('width', r.width + 6); o.rect.setAttribute('height', r.height + 6);
    o.rect.setAttribute('stroke', color); o.rect.setAttribute('stroke-dasharray', assisted ? '8 5' : '');
    for (const c of [o.track, o.ring, o.arc]) { c.setAttribute('cx', x); c.setAttribute('cy', y); }
    const circ = 2 * Math.PI * 28;
    o.arc.setAttribute('stroke', color);
    o.arc.setAttribute('stroke-dasharray', `${circ * progress} ${circ}`);
    o.arc.setAttribute('transform', `rotate(-90 ${x} ${y})`);
  }
  function clearOverlay() { if (ov && ov.shown) { ov.host.remove(); ov.shown = false; } }

  // ---- visible enlargement ---------------------------------------------------------------------------------
  // The button under the pointer is really scaled up on the page (CSS transform), so the help is visible.
  // Nothing grows on a plain hover: only a target missed cfg.adapt_misses (default 3) times in a row is enlarged (the whole element, so its
  // text grows with it); it stays enlarged until the page reloads or "Undo adaptation" is pressed in the app.
  const ENGAGED_SCALE = 1.3, ASSIST_SCALE = 1.4, GROWN_SCALE = 1.25;
  const orig = new Map();            // el -> inline styles we replaced
  let missCount = new WeakMap();
  const grown = new Set();
  let engagedEl = null, engagedScale = 1;
  const PROPS = ['transform', 'transition', 'outline', 'outline-offset', 'position', 'z-index', 'display', 'box-shadow'];
  function paint(el) {
    const visible = cfg && cfg.enabled && cfg.adaptation_enabled && cfg.visual_enlarge;
    const s = visible ? Math.max(el === engagedEl ? engagedScale : 1, grown.has(el) ? GROWN_SCALE : 1) : 1;
    if (s <= 1) {
      const o = orig.get(el);
      if (o) { for (const p of PROPS) el.style.setProperty(p, o[p][0], o[p][1]); orig.delete(el); }
      return;
    }
    if (!orig.has(el)) {
      const o = {};
      for (const p of PROPS) o[p] = [el.style.getPropertyValue(p), el.style.getPropertyPriority(p)];
      o.baseTransform = getComputedStyle(el).transform;
      orig.set(el, o);
    }
    const cs = getComputedStyle(el), saved = orig.get(el).baseTransform;
    const base = saved !== 'none' ? saved : '';
    if (cs.display === 'inline') el.style.setProperty('display', 'inline-block', 'important');
    if (cs.position === 'static') el.style.setProperty('position', 'relative', 'important');
    el.style.setProperty('transition', 'transform 120ms ease-out, box-shadow 120ms', 'important');
    el.style.setProperty('transform', `${base} scale(${s})`.trim(), 'important');
    el.style.setProperty('z-index', '2147483000', 'important');
    const live = el === engagedEl;
    el.style.setProperty('outline', `${live ? 4 : 3}px ${live ? 'solid' : 'dashed'} ${live && curAssisted ? COLORS.assist : COLORS.ring}`, 'important');
    el.style.setProperty('outline-offset', '2px', 'important');
    el.style.setProperty('box-shadow', '0 6px 18px rgba(0,0,0,.45)', 'important');
  }
  function syncEnlarge(el, assisted) {
    const want = el && cfg && cfg.visual_enlarge && grown.has(el) ? el : null;
    if (want === engagedEl) {
      const scale = assisted ? ASSIST_SCALE : ENGAGED_SCALE;
      if (want && engagedScale !== scale) { engagedScale = scale; paint(want); }
      return;
    }
    const prev = engagedEl;
    engagedEl = want; engagedScale = assisted ? ASSIST_SCALE : ENGAGED_SCALE;
    if (prev) paint(prev);
    if (want) paint(want);
  }
  function recordMiss(el) {
    const n = (missCount.get(el) || 0) + 1;
    missCount.set(el, n);
    if (cfg && cfg.adaptation_enabled && n >= (cfg.adapt_misses || 3) && !grown.has(el)) {
      grown.add(el); paint(el);
      report('target_adapted', el, { reason: 'nearby_misses', note: 'enlarged on the page; reversible' });
    }
  }
  function resetMisses(el) { missCount.set(el, 0); }
  let epoch = null;
  let visualEnabled = null;
  function syncEpoch() {                      // "Undo adaptation" in the desktop app clears page enlargements too
    if (!cfg || cfg.adapt_epoch == null) return;
    if ((epoch !== null && cfg.adapt_epoch !== epoch) || !cfg.adaptation_enabled) {
      engagedEl = null; engagedScale = 1;
      missCount = new WeakMap();
      const els = [...grown]; grown.clear();
      for (const el of els) paint(el);
    }
    const visible = !!(cfg.enabled && cfg.adaptation_enabled && cfg.visual_enlarge);
    if (visible !== visualEnabled) { for (const el of grown) paint(el); visualEnabled = visible; }
    epoch = cfg.adapt_epoch;
  }

  // ---- acting ------------------------------------------------------------------------------------------
  function activate(el) {
    const typing = el instanceof HTMLInputElement && !['checkbox', 'radio', 'button', 'submit', 'reset', 'file', 'color', 'range', 'image'].includes(el.type)
      || el instanceof HTMLTextAreaElement || el.isContentEditable;
    try { el.focus({ preventScroll: true }); } catch (e) { /* not focusable */ }
    if (!typing) el.click();
  }

  // ---- main loop ---------------------------------------------------------------------------------------
  function closeGate(pos) { gatePos = pos; gateOpen = false; }

  function tick() {
    if (!last || document.hidden) return;
    const now = performance.now() / 1000, k = dpr();
    const px = last.x * k, py = last.y * k;
    motion.feed(now, px, py);
    if (!gatePos) closeGate([px, py]);
    refreshConfig(false);
    syncEpoch();
    if (!cfg || !cfg.enabled || buttons) { dwell.reset(); cur = null; clearOverlay(); syncEnlarge(null); return; }
    const m = motion.analyze(now);
    if (!gateOpen && Math.hypot(px - gatePos[0], py - gatePos[1]) >= GATE_PX) gateOpen = true;
    const tolCss = (cfg.tolerance_px + 20 * m.tremor) / k;
    const snapCss = cfg.snap_radius / k;

    let target = m.settled || !cur ? underTarget(last.x, last.y) : null;
    if (!target && cur && !curAssisted && cur.el.isConnected) {
      const r = rectOf(cur.el);
      if (C.distToRect(last.x, last.y, r) <= tolCss) target = { el: cur.el, rect: r };   // tremor nudged us off the edge
    }
    if (m.settled) {
      if (!settleAnchor || Math.hypot(px - settleAnchor[0], py - settleAnchor[1]) > Math.max(6, tolCss * k)) { settleAnchor = [px, py]; settledSince = now; }
    } else settleAnchor = null;
    const held = settleAnchor && now - settledSince >= ASSIST_HOLD_S;
    let assisted = false, near = null, nearDist = 0;
    if (!target && held && (cfg.snap_enabled || cfg.near_miss_correct)) {
      const res = findNearest(last.x, last.y, Math.max(cfg.snap_radius, cfg.miss_radius) / k);
      near = res.target; nearDist = res.dist * k;
      if (cfg.snap_enabled && near && nearDist <= cfg.snap_radius) { target = { el: near.el, rect: rectOf(near.el) }; assisted = true; }
    }
    cur = target; curAssisted = assisted;

    const key = target && gateOpen ? (assisted ? 'assist:' : '') + idOf(target.el) : null;
    const dwellS = cfg.dwell_ms / 1000 * (assisted ? cfg.assist_factor : 1);
    const { progress, event } = dwell.update(now, key, m.settled, dwellS, cfg.cooldown_ms / 1000);

    if (event === 'start' && target) {
      if (episodeEl !== target.el) {
        episodeEl = target.el;
        report('interaction_detected', target.el, { tremor: +m.tremor.toFixed(2), assisted });
        report('target_identified', target.el, { ctype: target.el.tagName.toLowerCase(), assisted });
      }
      if (assisted && missLoggedEl !== target.el) {
        missLoggedEl = target.el;
        const r = target.rect, hw = Math.max(r.width / 2, 1), hh = Math.max(r.height / 2, 1);
        report('near_miss', target.el, { kind: 'hover', distance_px: Math.round(nearDist),
          nx: +((last.x - (r.left + r.right) / 2) / hw).toFixed(2), ny: +((last.y - (r.top + r.bottom) / 2) / hh).toFixed(2), corrected: true });
      }
    } else if (event === 'blocked' && target) {
      report('click_prevented', target.el, { reason: 'repeat_dwell_guard' });
    } else if (event === 'fire' && target) {
      const approach = motion.takeApproach(now) || { path_px: 0, reversals: 0, duration_ms: 0 };
      report('dwell_complete', target.el, { dwell_ms: Math.round(dwellS * 1000) });
      activate(target.el);
      report('click_assisted', target.el, Object.assign(approach, { mode: 'assisted', method: assisted ? 'snap_dwell' : 'dwell', tremor: +m.tremor.toFixed(2) }));
      closeGate([px, py]);
    }
    syncEnlarge(target ? target.el : null, assisted);
    if (target && gateOpen) draw(target, progress, assisted, last.x, last.y); else clearOverlay();
  }

  function start() { if (!timer) timer = setInterval(tick, TICK_MS); }
  function stop() { if (timer) { clearInterval(timer); timer = null; } clearOverlay(); dwell.reset(); cur = null; syncEnlarge(null); }

  // ---- events --------------------------------------------------------------------------------------------
  addEventListener('mousemove', e => {
    last = { x: e.clientX, y: e.clientY };
    buttons = e.buttons;
    start();
    const t = performance.now();
    if (t - lastPosSent > 120) {
      lastPosSent = t;
      send({ type: 'pos', body: { cx: e.clientX, cy: e.clientY, iw: window.innerWidth, ih: window.innerHeight, dpr: dpr() } });
    }
  }, { capture: true, passive: true });
  document.addEventListener('mouseleave', () => { last = null; stop(); send({ type: 'leave' }); }, { capture: true });
  addEventListener('blur', () => { stop(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });

  // Real clicks: count them, and fix near-misses that landed in dead space beside a target.
  addEventListener('click', e => {
    if (!e.isTrusted || !cfg || !cfg.enabled) return;
    const k = dpr(), now = performance.now() / 1000;
    const hitEl = interactiveAncestor(e.target);
    if (hitEl) {
      const a = motion.takeApproach(now);
      if (a) report('manual_click', hitEl, Object.assign(a, { mode: 'manual' }));
      resetMisses(hitEl);
      return;
    }
    // Query fresh geometry at click time; hover's cached snap radius may differ.
    candCache.at = -Infinity; nearCache.res = null;
    const res = findNearest(e.clientX, e.clientY, cfg.miss_radius * (cfg.adaptation_enabled ? 2 : 1) / k);
    if (!res.target) return;
    const textual = hasOwnText(e.target) || e.target.isContentEditable;   // never hijack a click aimed at text
    if (textual) return;
    const adapted = cfg.adaptation_enabled && grown.has(res.target.el);
    if (C.distToRect(e.clientX, e.clientY, res.target.rect) > cfg.miss_radius * (adapted ? 2 : 1) / k) return;
    const fix = cfg.near_miss_correct || adapted;
    const r = res.target.rect, hw = Math.max((r.right - r.left) / 2, 1), hh = Math.max((r.bottom - r.top) / 2, 1);
    report('near_miss', res.target.el, { kind: 'click', corrected: fix,
      nx: +((e.clientX - (r.left + r.right) / 2) / hw).toFixed(2), ny: +((e.clientY - (r.top + r.bottom) / 2) / hh).toFixed(2) });
    recordMiss(res.target.el);
    if (fix) {
      const a = motion.takeApproach(now) || { path_px: 0, reversals: 0, duration_ms: 0 };
      e.preventDefault(); e.stopImmediatePropagation();
      activate(res.target.el);
      report('click_assisted', res.target.el, Object.assign(a, { mode: 'assisted', method: 'near_miss_redirect' }));
      closeGate([e.clientX * k, e.clientY * k]);
    }
  }, true);

  addEventListener('scroll', () => { candCache.at = 0; nearCache.res = null; }, { capture: true, passive: true });
  addEventListener('resize', () => { candCache.at = 0; nearCache.res = null; });
  new MutationObserver(() => { candCache.at = 0; nearCache.res = null; })
    .observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style', 'hidden', 'disabled'] });

  refreshConfig(true);
})();
