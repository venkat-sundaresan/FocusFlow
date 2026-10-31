// Run with: node --test tests/core.test.js
const test = require('node:test');
const assert = require('node:assert');
const { Motion, Dwell, nearestUnambiguous, distToRect } = require('../extension/core.js');

function run(path, hz = 60, sens = 5) {
  const m = new Motion(sens, 250);
  let t = 0, r = null;
  for (const [x, y] of path) { m.feed(t, x, y); r = m.analyze(t); t += 1 / hz; }
  return { m, r };
}
const range = n => Array.from({ length: n }, (_, i) => i);

test('8 Hz wobble is a tremor', () => {
  const { r } = run(range(90).map(i => [500 + 5 * Math.sin(2 * Math.PI * 8 * i / 60), 400]));
  assert.ok(r.isTremor, `tremor=${r.tremor}`);
});
test('straight movement is intentional, not tremor', () => {
  const { r } = run(range(60).map(i => [100 + 12 * i, 300 + 4 * i]));
  assert.ok(!r.isTremor && r.intentional && !r.settled);
});
test('tremor riding on a deliberate move is not flagged', () => {
  const { r } = run(range(60).map(i => [100 + 10 * i + 4 * Math.sin(i * 1.3), 300]));
  assert.ok(!r.isTremor);
});
test('approach metrics count corrections', () => {
  const { m } = run(range(60).map(i => [500 + 6 * Math.sin(2 * Math.PI * 6 * i / 60), 400]));
  const a = m.takeApproach(1);
  assert.ok(a.reversals > 5 && a.path_px > 50);
  assert.strictEqual(m.takeApproach(1), null);
});

function drive(d, secs, { key = 'a', settled = true, start = 0, dwell = 1, cooldown = 1.5 } = {}) {
  const ev = [];
  for (let t = start; t <= start + secs + 1e-9; t += 0.05) {
    const { event } = d.update(t, key, settled, dwell, cooldown);
    if (event) ev.push([+t.toFixed(2), event]);
  }
  return ev;
}
test('dwell fires once and blocks the repeat', () => {
  const ev = drive(new Dwell(), 6);
  assert.strictEqual(ev.filter(e => e[1] === 'fire').length, 1);
  assert.strictEqual(ev.filter(e => e[1] === 'blocked').length, 1);
  assert.ok(Math.abs(ev.find(e => e[1] === 'fire')[0] - 1.0) < 0.1);
});
test('leaving cancels, moving never fires', () => {
  const d = new Dwell();
  drive(d, 0.7);
  assert.strictEqual(d.update(0.8, null, true, 1, 1.5).event, 'cancel');
  assert.deepStrictEqual(drive(new Dwell(), 5, { settled: false }), []);
});
test('re-arms after leaving and returning', () => {
  const d = new Dwell();
  drive(d, 1.2);
  drive(d, 1.0, { key: null, start: 1.3 });
  assert.strictEqual(drive(d, 3.0, { start: 2.4 }).filter(e => e[1] === 'fire').length, 1);
});

const box = (l, t, r, b) => ({ rect: { left: l, top: t, right: r, bottom: b } });
test('nearest requires a clear winner within radius', () => {
  const a = box(100, 0, 140, 40), b = box(100, 70, 140, 110);
  assert.strictEqual(nearestUnambiguous(120, 55, [a, b], 30).target, null);
  assert.strictEqual(nearestUnambiguous(120, 43, [a, b], 30).target, a);
  assert.strictEqual(nearestUnambiguous(300, 20, [a], 30).target, null);
  assert.strictEqual(distToRect(50, 50, { left: 0, top: 0, right: 100, bottom: 100 }), 0);
});
