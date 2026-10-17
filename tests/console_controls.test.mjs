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
    this.style = {};
    if (tag === 'textarea') this.value = this.textContent; // HTML ignores a textarea value attribute.
  }
  focus() {}
  addEventListener() {}
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

test('model routing needs consent, retains fallback settings and changes only the selected purpose', async () => {
  const calls = [], saved = {company: {provider: 'claude-code', model: 'sonnet'},
    planning: {provider: 'gemini', model: 'planner', fallbacks: [{provider: 'openrouter', model: 'openrouter/free', free_only: true}]}};
  const context = vm.createContext({h, S: {}, AI_HEALTH: null,
    PROVIDERS: [['claude-code', 'Claude CLI'], ['gemini', 'Gemini'], ['openrouter', 'OpenRouter'], ['bedrock', 'Bedrock']],
    api: async (path, body) => { calls.push({path, body}); return path === '/api/models/routes' ? saved : {}; }});
  new vm.Script(section('function modelRoutingCard(', 'function modelsCard(')).runInContext(context);
  const card = context.modelRoutingCard(); await new Promise(setImmediate);
  const field = label => find(card, x => x.attrs['aria-label'] === label);
  field('Model routing scope').value = 'planning'; field('Model routing scope').onchange();
  assert.equal(field('Primary provider').value, 'gemini');
  assert.equal(field('Fallback 1 model').value, 'openrouter/free');
  const save = find(card, x => x.tag === 'button' && x.textContent === 'Save routing');
  await save.onclick(); assert.equal(calls.filter(x => x.body).length, 0);
  field('Approve model routing').checked = true;
  await save.onclick();
  const posted = calls.find(x => x.body)?.body;
  assert.equal(posted.who, 'planning'); assert.equal(posted.fallbacks[0].free_only, true);
  field('Primary provider').value = 'bedrock'; field('Primary provider').onchange();
  assert.equal(field('Primary model').value, '');
  assert.match(card.textContent, /not IAM\/SSO/);
});

function projectFixture(respond = async () => ({})) {
  const calls = [], p = {id: 'site', name: 'Site', members: ['riya'], lead: 'riya'};
  const context = vm.createContext({h, icon: () => '', badge: text => h('span', {}, text),
    can: () => true, nameOf: x => x, VIEW: 'project', PROJECT: 'site', FILTER: {},
    S: {projects: [p], priorities: ['P0','P1','P2'], members: [{id: 'riya', name: 'Riya'}]},
    projOf: () => p, closeSheet: () => {}, render: () => {}, boot: () => {}, toast: () => {},
    api: async (path, body) => { calls.push({path, body}); return respond(path, body); }});
  context.openSheet = (title, ...kids) => { context.sheet = h('div', {}, ...kids); };
  new vm.Script(section('function newTaskSheet(', 'function changesSheet(')
    + section('function projectResourceSheet(', '// Project settings:')).runInContext(context);
  return {context, calls, p};
}

test('draft fields retain description and checks; creation needs explicit human review', async () => {
  const f = projectFixture(async () => ({id: 'T-001', owner: 'riya'}));
  f.context.newTaskSheet({project: 'site', plan_id: 'plan', draft_id: 'D1', title: 'Draft',
    owner: 'riya', description: 'Reviewed scope', done_means: ['Evidence checked']});
  const sheet = f.context.sheet, field = label => find(sheet, x => x.attrs['aria-label'] === label);
  assert.equal(field('Task description').value, 'Reviewed scope');
  assert.equal(field('Acceptance checks').value, 'Evidence checked');
  const create = find(sheet, x => x.tag === 'button' && x.textContent === 'Create reviewed task');
  await create.onclick(); assert.equal(f.calls.length, 0);
  find(sheet, x => x.type === 'checkbox').checked = true;
  field('Task owner').value = 'riya'; field('Task priority').value = 'P1';
  await create.onclick();
  assert.equal(f.calls[0].path, '/api/project/plan/accept');
  assert.equal(f.calls[0].body.reviewed, true);
  assert.deepEqual(Array.from(f.calls[0].body.task.done_means), ['Evidence checked']);
  assert.equal(f.calls.filter(x => x.path === '/api/tasks').length, 0);
});

test('resource editing retains notes and explains external links do not grant access', () => {
  const f = projectFixture();
  f.context.projectResourceSheet(f.p, {id: 'resource', title: 'Scope', notes: 'Preserve existing context'});
  assert.equal(find(f.context.sheet, x => x.attrs['aria-label'] === 'Resource notes').value, 'Preserve existing context');
  assert.match(f.context.sheet.textContent, /link does not grant access/);
});

test('AI drafting needs consent and sends the reviewed context signature without creating tasks', async () => {
  const f = projectFixture(async () => ({plan: null, planner: 'riya', model: 'fake', signature: 'context-v1'}));
  const body = h('div');
  await f.context.projectTaskDrafts(f.p, body);
  const generate = find(body, x => x.tag === 'button' && x.textContent === 'Draft tasks with AI');
  await generate.onclick(); assert.equal(f.calls.length, 1); // metadata GET only
  find(body, x => x.type === 'checkbox').checked = true;
  await generate.onclick();
  assert.equal(f.calls[1].path, '/api/project/plan/generate');
  assert.equal(f.calls[1].body.consent, true);
  assert.equal(f.calls[1].body.signature, 'context-v1');
  assert.equal(f.calls.filter(x => x.body).length, 1);
});

test('uncertain chat approval footers never show approve or reject buttons', () => {
  const context = vm.createContext({h, ASKS: {a: {execution: {status: 'uncertain'}}}, PULSE: null, can: () => true});
  new vm.Script(section('function askFooter(', '// A conversation:')).runInContext(context);
  const footer = context.askFooter('a');
  assert.match(footer.textContent, /Do not retry/);
  assert.equal(find(footer, x => x.tag === 'button'), undefined);
});

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
