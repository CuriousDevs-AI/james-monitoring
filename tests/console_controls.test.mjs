// Dependency-free behavior checks for the two new forms; browser layout is checked separately.
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync(new URL('../james_monitoring/templates/console.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
const section = (start, end) => script.slice(script.indexOf(start), script.indexOf(end));

// Only the DOM operations these isolated forms use; not a browser or visual-rendering substitute.
class Element {
  constructor(tag, attrs, children) {
    this.tag = tag; this.attrs = {...attrs}; this.children = children.flat(9).filter(x => x != null && x !== false);
    Object.assign(this, attrs); this.value = attrs.value || ''; this.disabled = !!attrs.disabled;
  }
  put(...children) { this.children = children.flat(9).filter(x => x != null && x !== false); return this; }
  setAttribute(name, value) { this.attrs[name] = value; }
  removeAttribute(name) { delete this.attrs[name]; }
  get textContent() { return this.children.map(c => c instanceof Element ? c.textContent : String(c)).join(''); }
  set textContent(text) { this.children = [text]; }
}
const h = (tag, attrs = {}, ...children) => new Element(tag, attrs, children);
const find = (root, predicate) => predicate(root) ? root : root.children.filter(c => c instanceof Element)
  .map(c => find(c, predicate)).find(Boolean);
const byId = (root, id) => find(root, x => x.attrs.id === id);
function fixture(respond = async () => ({}), confirm = true) {
  const calls = [], toasts = [];
  const context = vm.createContext({h, icon: name => h('i', {}, name), badge: text => h('span', {}, text),
    btn: (label, onclick) => h('button', {onclick}, label), confirm: () => confirm,
    api: async (path, body) => { calls.push({path, body}); return respond(path, body); },
    toast: text => toasts.push(text), render: async () => {}});
  new vm.Script(section('function recoveryForm(', 'async function vApprovals(')
    + section('function secretsCard(', 'async function vSettings(')).runInContext(context);
  return {context, calls, toasts};
}
const event = {preventDefault() {}};
const metadata = {backend: 'env', namespace: '', encrypted_at_rest: false,
  entries: [{name: 'OPENAI_API_KEY', status: 'missing'}]};

test('entire console JavaScript parses', () => { new vm.Script(script); });

test('secret status is manual; saving clears the password and does not claim connection success', async () => {
  const f = fixture(async (path, body) => body ? {restart_required: true} : metadata);
  const card = f.context.secretsCard();
  assert.equal(f.calls.length, 0);
  assert.equal(byId(card, 'secret-value').disabled, true);
  await find(card, x => x.tag === 'button' && x.textContent === 'Check status').onclick();
  assert.equal(f.calls[0].path, '/api/secrets');
  const reference = byId(card, 'secret-reference'), secret = byId(card, 'secret-value');
  assert.equal(secret.type, 'password');
  reference.value = 'OPENAI_API_KEY'; secret.value = 'synthetic-value';
  await find(card, x => x.tag === 'form').onsubmit(event);
  assert.equal(f.calls[1].body.secret, 'synthetic-value');
  assert.equal(secret.value, '');
  assert.match(card.textContent, /Restart James/);
  assert.doesNotMatch(card.textContent, /synthetic-value/);
});

test('failed secret save clears input without echoing sensitive provider errors', async () => {
  const f = fixture(async (path, body) => { if (body) throw Error('synthetic-secret-diagnostic'); return metadata; });
  const card = f.context.secretsCard();
  await find(card, x => x.tag === 'button' && x.textContent === 'Check status').onclick();
  byId(card, 'secret-reference').value = 'OPENAI_API_KEY'; byId(card, 'secret-value').value = 'synthetic-secret';
  await find(card, x => x.tag === 'form').onsubmit(event);
  assert.equal(byId(card, 'secret-value').value, '');
  assert.match(card.textContent, /could not be confirmed/);
  assert.doesNotMatch(card.textContent, /synthetic-secret/);
});

test('duplicate credential submits cannot start a second request while saving', async () => {
  let finish;
  const f = fixture((path, body) => body ? new Promise(resolve => { finish = resolve; }) : metadata);
  const card = f.context.secretsCard();
  await find(card, x => x.tag === 'button' && x.textContent === 'Check status').onclick();
  byId(card, 'secret-reference').value = 'OPENAI_API_KEY'; byId(card, 'secret-value').value = 'synthetic';
  const form = find(card, x => x.tag === 'form'), pending = form.onsubmit(event);
  await form.onsubmit(event);
  assert.equal(f.calls.length, 2); // one GET, one POST
  finish({restart_required: true}); await pending;
});

test('status refresh cannot race a credential save or another refresh', async () => {
  let finish, reads = 0;
  const f = fixture(() => ++reads === 1 ? metadata : new Promise(resolve => { finish = resolve; }));
  const card = f.context.secretsCard();
  const check = find(card, x => x.tag === 'button' && x.textContent === 'Check status');
  await check.onclick();
  byId(card, 'secret-reference').value = 'OPENAI_API_KEY';
  byId(card, 'secret-value').value = 'synthetic';
  const pending = check.onclick();
  await find(card, x => x.tag === 'form').onsubmit(event);
  await check.onclick();
  assert.equal(f.calls.length, 2); // GET + GET, no concurrent write or third read
  finish(metadata); await pending;
  assert.equal(byId(card, 'secret-value').disabled, false);
});

test('action reconciliation requires evidence, outcome and explicit verification; calls only reconcile endpoint', async () => {
  const f = fixture(), form = f.context.recoveryForm('action', {id: 'ASK-001'});
  await form.onsubmit(event);
  assert.equal(f.calls.length, 0);
  byId(form, 'recover-action-ASK-001-evidence').value = 'Verified no effect';
  await form.onsubmit(event);
  assert.equal(f.calls.length, 0);
  byId(form, 'recover-action-ASK-001-outcome').value = 'not_completed';
  await form.onsubmit(event);
  assert.equal(f.calls.length, 0);
  byId(form, 'recover-action-ASK-001-verified').checked = true;
  await form.onsubmit(event);
  assert.equal(f.calls[0].path, '/api/ask/reconcile');
  assert.equal(f.calls[0].body.outcome, 'not_completed');
  assert.equal(f.calls.length, 1);
  assert.match(f.toasts[0], /no action retried/);
});

test('missing explicit verification never acknowledges or executes', async () => {
  const f = fixture(), form = f.context.recoveryForm('schedule', {id: 'abc'});
  byId(form, 'recover-schedule-abc-evidence').value = 'Verified synthetic destination';
  await form.onsubmit(event);
  assert.equal(f.calls.length, 0);
});

test('scheduled acknowledgment records evidence without a retry parameter', async () => {
  const f = fixture(), form = f.context.recoveryForm('schedule', {id: 'abc'});
  const evidence = byId(form, 'recover-schedule-abc-evidence');
  evidence.value = 'Verified synthetic destination'; evidence.oninput();
  assert.equal(form.attrs['data-recovery-dirty'], '1');
  byId(form, 'recover-schedule-abc-verified').checked = true;
  await form.onsubmit(event);
  assert.equal(f.calls[0].path, '/api/schedule/acknowledge');
  assert.equal(f.calls[0].body.evidence, 'Verified synthetic destination');
  assert.equal('retry' in f.calls[0].body, false);
});
