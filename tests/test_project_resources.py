"""Project references and human-reviewed planning, without live credentials or outreach."""
import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from james_monitoring.config import ProjectAgent
from james_monitoring.executor import ExecutorError
from james_monitoring.llm import LLMError
from james_monitoring.llm.fake import FakeLLM
from james_monitoring.project_plans import ProjectPlan
from james_monitoring.project_resources import ProjectResources
from james_monitoring.runtime import ConsoleBus, Event, Runtime
from .test_workforce import app, http


def resource(rt, **fields):
    store = ProjectResources(rt.ws.root, 'site')
    return store.save(dict(title='Approved scope', notes='Only synthetic leads; never send automatically.', **fields), 'Owner')


def drafts(owner='sofia'):
    return {'tasks': [
        dict(title='Find matching leads', owner=owner, priority='P1', description='Use the attached scope.',
             done_means=['Two synthetic leads match the scope'], due='', depends_on=[]),
        dict(title='Validate leads and draft outreach', owner=owner, priority='P2', description='Do not send.',
             done_means=['Human reviews the evidence and message'], due='', depends_on=['D1'])]}


async def generate(rt):
    plan = ProjectPlan(rt, 'site')
    rt.llm.push(drafts())
    return plan, await plan.generate('Keep the plan small.', plan.signature())


def acceptance(data, draft='D1', **over):
    return dict(plan_id=data['id'], draft_id=draft, reviewed=True, **over)


def test_resource_revision_archive_detach_and_restoration(rt):
    store = ProjectResources(rt.ws.root, 'site')
    data = base64.b64encode(b'company,scope\nExample,synthetic').decode()
    r = resource(rt, filename='leads.csv', data=data, url='https://docs.google.com/spreadsheets/d/example')
    assert 'data' not in r and store.get(r['id'])['data'] == data
    assert 'Example,synthetic' in store.context() and 'reference_only' in store.context()
    with pytest.raises(ValueError, match='changed'):
        store.save(dict(resource_id=r['id'], revision=0, notes='stale'), 'Owner')
    archived = store.save(dict(resource_id=r['id'], revision=1, archived=True), 'Owner')
    assert store.context() == ''
    store.save(dict(resource_id=r['id'], revision=archived['revision'], archived=False, remove_file=True), 'Owner')
    assert 'Only synthetic' in store.context() and store.get(r['id'])['data'] == ''
    assert store.get(r['id'])['revision'] == 3


@pytest.mark.parametrize('fields', [
    {'url': 'javascript:alert(1)'}, {'url': 'https://user:pass@example.org/'},
    {'url': 'https://example.org/?api_key=secret'}, {'url': 'https://example.org/#access_token=secret'},
    {'filename': '../scope.txt', 'data': 'YWJj'}, {'filename': 'payload.html', 'data': 'YWJj'},
    {'filename': 'scope.txt', 'data': 'bad%%'}, {'filename': 'scope.txt', 'data': ''},
    {'filename': 'scope.txt', 'data': base64.b64encode(b'x' * 2_000_001).decode()},
    {'kind': 'credential'}, {'include': True, 'url': 'https://exa mple.org/'}])
def test_invalid_resources_fail_before_write(rt, fields):
    with pytest.raises(ValueError):
        resource(rt, **fields)
    assert not ProjectResources(rt.ws.root, 'site').all()


def test_resources_are_bounded_inert_and_scoped(rt):
    resource(rt, filename='brief.pdf', data=base64.b64encode(b'opaque pdf contents').decode())
    store = ProjectResources(rt.ws.root, 'site')
    assert 'not been extracted or read' in store.context()
    assert 'opaque pdf contents' not in store.context()
    excluded = store.save(dict(title='Excluded context', notes='do-not-include', include=False), 'Owner')
    assert 'do-not-include' not in store.context()
    assert len(store.context(10)) < 100 and 'truncated' in store.context(10)
    assert 'Only synthetic' in rt.project_brief(rt.cfg.member('sofia'), 'site')
    assert 'Only synthetic' not in rt.project_brief(rt.cfg.member('marcus'), 'site')
    assert 'Only synthetic' not in rt.project_brief(rt.cfg.member('sofia'), '')
    assert excluded in [store.view(x) for x in store.all()]


def test_resource_paths_reject_traversal_symlink_and_cap(rt, tmp_path):
    with pytest.raises(ValueError):
        ProjectResources(rt.ws.root, '../escape')
    store = ProjectResources(rt.ws.root, 'site')
    r = resource(rt)
    store.path(r['id']).unlink()
    store.path(r['id']).symlink_to(tmp_path / 'outside')
    with pytest.raises(ValueError, match='symlink'):
        store.get(r['id'])
    with pytest.raises(ValueError):
        store.get('../escape')
    other = ProjectResources(rt.ws.root, 'bounded')
    for i in range(40):
        other.save({'title': str(i)}, 'Owner')
    with pytest.raises(ValueError, match='40 resources'):
        other.save({'title': 'overflow'}, 'Owner')


async def test_ai_plan_has_no_actions_until_review_then_persists_idempotently(rt):
    resource(rt)
    plan, data = await generate(rt)
    assert len(rt.tasks.all()) == 0
    assert rt._usage_today('sofia') > 0
    assert 'Only synthetic' in rt.llm.calls[-1][1][0]['content']
    assert not rt.asks.all()
    with pytest.raises(ValueError, match='reviewed'):
        plan.accept({'plan_id': data['id'], 'draft_id': 'D1'}, rt.owner_id)
    with pytest.raises(ValueError, match='prerequisite'):
        plan.accept(acceptance(data, 'D2'), rt.owner_id)
    first = plan.accept(acceptance(data), rt.owner_id)
    second = plan.accept(acceptance(data, 'D2'), rt.owner_id)
    assert second.depends_on == [first.id] and len(rt.llm.calls) == 1
    restarted = Runtime(rt.cfg, FakeLLM(), bus=ConsoleBus(quiet=True))
    persisted = ProjectPlan(restarted, 'site')
    assert persisted.read()['tasks'][0]['created_task'] == first.id
    with ThreadPoolExecutor(max_workers=3) as pool:
        ids = list(pool.map(lambda _: persisted.accept(acceptance(data), rt.owner_id).id, range(3)))
    assert ids == [first.id] * 3 and len(restarted.tasks.all()) == 2
    assert not restarted.llm.calls


@pytest.mark.parametrize('reply', ['not JSON', {'tasks': []}, {'tasks': [{'owner': 'unknown'}]},
                                 {'tasks': [dict(drafts()['tasks'][0], depends_on=['D2'])]},
                                 {'actions': [{'type': 'post_group', 'text': 'do not send'}]}])
async def test_invalid_model_reply_never_creates_work(rt, reply):
    rt.llm.push(reply)
    plan = ProjectPlan(rt, 'site')
    with pytest.raises(ValueError, match='invalid task plan'):
        await plan.generate('', plan.signature())
    assert not rt.tasks.all() and not rt.asks.all() and not plan.path.exists()
    assert rt._usage_today('sofia') > 0


@pytest.mark.parametrize('change', ['resource', 'model_endpoint', 'member', 'instructions'])
async def test_changed_context_requires_fresh_review(rt, change):
    plan, data = await generate(rt)
    if change == 'resource':
        resource(rt)
    elif change == 'model_endpoint':
        rt.cfg.llm.base_url = 'https://different.example.org'
    elif change == 'member':
        rt.cfg.member('sofia').projects.clear()
    else:
        rt.cfg.projects['site'].agents['sofia'] = ProjectAgent(instructions='New restricted scope')
    assert plan.read()['stale']
    with pytest.raises(ValueError, match='changed'):
        plan.accept(acceptance(data), rt.owner_id)
    assert not rt.tasks.all()


async def test_planning_stops_before_llm_on_pause_budget_or_changed_consent(rt):
    plan = ProjectPlan(rt, 'site')
    with pytest.raises(ValueError, match='confirm'):
        await plan.generate('', 'old signature')
    rt.set_paused('sofia', True)
    with pytest.raises(ValueError, match='Resume'):
        await plan.generate('', plan.signature())
    rt.set_paused('sofia', False)
    rt._add_usage('sofia', rt.cfg.daily_tokens_per_agent, 'fake')
    with pytest.raises(ValueError, match='budget'):
        await plan.generate('', plan.signature())
    assert not rt.llm.calls


async def test_model_error_is_sanitized_and_preserves_previous_plan(rt, monkeypatch):
    plan, data = await generate(rt)
    def fail(*args):
        raise LLMError('provider-private-secret')
    monkeypatch.setattr(rt.llm, 'complete', fail)
    with pytest.raises(ValueError, match='model failed') as e:
        await plan.generate('', plan.signature())
    assert 'private-secret' not in str(e.value)
    assert plan.read()['id'] == data['id']


async def test_task_dm_gets_project_resource_context(rt):
    resource(rt)
    task = rt.tasks.create(title='Scoped task', owner='sofia', created_by=rt.owner_id, project='site')
    await rt.dispatch('sofia', Event('dm', 'Give the next step', sender=rt.owner_id, task=task.id))
    assert 'Only synthetic' in rt.llm.calls[-1][0]


def test_project_http_permissions_consent_and_audit(app, http):
    viewer = http('/api/users', {'action': 'add', 'name': 'Reader', 'role': 'viewer'})[1]['key']
    admin = http('/api/users', {'action': 'add', 'name': 'Planner', 'role': 'admin'})[1]['key']
    app.clients_save({'name': 'Example', 'projects': ['site']})
    client = http('/api/users', {'action': 'add', 'name': 'Client', 'role': 'client', 'client': 'example'})[1]['key']
    assert http('/api/project/resources?id=site', key=client)[0] == 403
    secret = 'sensitive-context-not-for-audit'
    body = dict(id='site', title='Scope', notes=secret, filename='scope.txt', data=base64.b64encode(b'attachment').decode())
    assert http('/api/project/resources', body, key=viewer)[0] == 403
    st, saved = http('/api/project/resources', body, key=admin)
    assert st == 200
    st, listing = http('/api/project/resources?id=site', key=viewer)
    assert st == 200 and 'data' not in listing['resources'][0]
    st, downloaded = http('/api/project/resources?id=site&resource_id=' + saved['id'], key=viewer)
    assert st == 200 and base64.b64decode(downloaded['data']) == b'attachment'
    assert http('/api/project/plan?id=site', key=viewer)[0] == 403
    st, meta = http('/api/project/plan?id=site', key=admin)
    assert st == 200 and meta['planner'] == 'riya'
    assert http('/api/project/plan/generate', {'id': 'site'}, key=admin)[0] == 400
    app.rt.llm.push(drafts('riya'))
    st, data = http('/api/project/plan/generate', dict(id='site', consent=True, signature=meta['signature']), key=admin)
    assert st == 200 and not app.rt.tasks.all()
    st, task = http('/api/project/plan/accept', dict(id='site', **acceptance(data)), key=admin)
    assert st == 200 and task['title'] == 'Find matching leads'
    assert http('/api/project/plan/accept', dict(id='site', **acceptance(data)), key=admin)[1]['id'] == task['id']
    assert secret not in json.dumps(http('/api/audit')[1])
    assert body['data'] not in json.dumps(http('/api/audit')[1])
    assert app.rt.tasks.get(task['id']).project == 'site'


@pytest.mark.parametrize('error', [ExecutorError('private-provider-error'), TimeoutError('private-provider-error'),
                                  asyncio.CancelledError()])
async def test_coding_interruption_holds_task_across_restart_and_reconcile(rt, monkeypatch, error):
    task = rt.tasks.create(title='Code', owner='sofia', created_by=rt.owner_id, project='site')
    async def interrupted(**kwargs):
        raise error
    monkeypatch.setattr(rt.executor, 'run', interrupted)
    rt._coding.add(task.id)
    if isinstance(error, ExecutorError):
        await rt._run_code(rt.cfg.member('sofia'), task.id, 'site', 'synthetic')
    else:
        with pytest.raises(type(error)):
            await rt._run_code(rt.cfg.member('sofia'), task.id, 'site', 'synthetic')
    assert task.id not in rt._coding
    assert rt.tasks.get(task.id).status == 'blocked'
    asks = rt.asks.unresolved_executions()
    assert len(asks) == 1 and asks[0].kind == 'code_recovery'
    assert 'private-provider' not in asks[0].path.read_text()
    await rt._record_code_interruption(rt.cfg.member('sofia'), task.id, 'site', error)
    assert len(rt.asks.unresolved_executions()) == 1
    restarted = Runtime(rt.cfg, FakeLLM(), bus=ConsoleBus(quiet=True))
    assert restarted.next_work(restarted.cfg.member('sofia')) is None
    await restarted.reconcile_ask(asks[0].id, 'not_completed', rt.owner_id, 'Inspected synthetic worktree: no changes')
    assert not restarted.llm.calls and not restarted.asks.unresolved_executions()
