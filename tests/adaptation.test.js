// Execute the real content script against a small DOM fixture; no browser validation is implied.
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const C = require('../extension/core.js');

function fixture() {
  const handlers = {}, reports = [];
  const values = new Map();
  const el = {
    tagName: 'BUTTON', childNodes: [], textContent: 'Save',
    style: { getPropertyValue: p => values.get(p)?.[0] || '',
      getPropertyPriority: p => values.get(p)?.[1] || '',
      setProperty: (p, value, priority) => values.set(p, [value, priority]) },
    getAttribute: () => null, closest() { return this; }, contains: t => t === el,
    getBoundingClientRect: () => ({ left: 100, top: 100, right: 150, bottom: 130, width: 50, height: 30 }),
    focus() {}, clicks: 0, click() { this.clicks++; }
  };
  const cfg = { enabled: true, adaptation_enabled: true, visual_enlarge: true, adapt_misses: 3,
    adapt_epoch: 0, miss_radius: 24, near_miss_correct: false };
  const window = { FocusFlowCore: C, devicePixelRatio: 1, innerWidth: 1000, innerHeight: 800 };
  const context = { window, performance: { now: () => 1000 }, location: { hostname: 'test.local' },
    addEventListener: (name, fn) => { handlers[name] = fn; },
    document: { documentElement: {}, addEventListener() {}, querySelectorAll: () => [el],
      elementFromPoint: (x, y) => x >= 100 && x <= 150 && y >= 100 && y <= 130 ? el : null },
    MutationObserver: class { observe() {} },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    chrome: { runtime: { sendMessage: msg => { reports.push(msg); return Promise.resolve(null); } } },
    getComputedStyle: node => ({ transform: node.style.getPropertyValue('transform') || 'none',
      display: 'inline-block', position: 'static', visibility: 'visible', pointerEvents: 'auto' }) };
  const source = fs.readFileSync(require.resolve('../extension/content.js'), 'utf8')
    .replace('  refreshConfig(true);', '  window.test = { setConfig(c) { cfg = c; }, recordMiss, syncEpoch, syncEnlarge };');
  vm.runInNewContext(source, context);
  window.test.setConfig(cfg);
  window.test.syncEpoch();
  const miss = (x = 95, text = false) => handlers.click({ isTrusted: true, clientX: x, clientY: 115,
    target: { closest: () => null, childNodes: text ? [{ nodeType: 3, nodeValue: 'document text' }] : [] },
    preventDefault() {}, stopImmediatePropagation() {} });
  return { api: window.test, cfg, el, miss, reports, values };
}

test('three real near-miss events enlarge the button and activate its expanded hit area', () => {
  const f = fixture();
  f.miss(); f.miss();
  assert.equal(f.el.style.getPropertyValue('transform'), '');
  f.miss();
  assert.equal(f.el.style.getPropertyValue('transform'), 'scale(1.25)');
  f.miss(65);  // 35 pixels outside: larger hit area, immediate correction still disabled
  assert.equal(f.el.clicks, 1);
  assert.equal(f.reports.filter(r => r.body?.kind === 'target_adapted').length, 1);
});

test('hover repaint never compounds transforms and undo clears all three-miss history', () => {
  const f = fixture();
  for (let i = 0; i < 3; i++) f.miss();
  for (let i = 0; i < 8; i++) { f.api.syncEnlarge(f.el, true); f.api.syncEnlarge(null); }
  assert.equal(f.el.style.getPropertyValue('transform'), 'scale(1.25)');
  f.api.syncEnlarge(f.el, true);
  f.cfg.adapt_epoch++; f.api.syncEpoch();
  assert.equal(f.el.style.getPropertyValue('transform'), '');
  f.miss();
  assert.equal(f.el.style.getPropertyValue('transform'), '');
});

test('text selection does not grow or activate a nearby control', () => {
  const f = fixture();
  f.cfg.near_miss_correct = true;
  for (let i = 0; i < 5; i++) f.miss(95, true);
  assert.equal(f.el.clicks, 0);
  assert.equal(f.el.style.getPropertyValue('transform'), '');
});

test('visual toggle preserves learned hit area; adaptation off restores original styles', () => {
  const f = fixture();
  f.cfg.visual_enlarge = false;
  f.api.syncEpoch();
  for (let i = 0; i < 3; i++) f.miss();
  assert.equal(f.el.style.getPropertyValue('transform'), '');
  f.miss(65); assert.equal(f.el.clicks, 1);
  f.cfg.visual_enlarge = true; f.api.syncEpoch();
  assert.equal(f.el.style.getPropertyValue('transform'), 'scale(1.25)');
  f.cfg.adaptation_enabled = false; f.api.syncEpoch();
  assert.equal(f.el.style.getPropertyValue('transform'), '');
  f.miss(65); assert.equal(f.el.clicks, 1);
});
