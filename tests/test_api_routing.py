"""Offline provider contracts, opt-in fallback, purpose routing and consent invalidation."""
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from james_monitoring.config import ConfigError, model_settings, parse_config, parse_llm
from james_monitoring.llm import LLMError, LLMResult, make_llm
from james_monitoring.llm.fallback import can_fallback
from james_monitoring.project_plans import ProjectPlan
from james_monitoring.runtime import Event, Runtime
from james_monitoring.server import App
from .conftest import make_raw


def test_provider_defaults_and_nested_secret_store(tmp_path):
    cfg = parse_config(make_raw(tmp_path, llm={'provider': 'opencode', 'model': 'p/m', 'fallbacks': [
        {'provider': 'gemini', 'model': 'gemini-test'}, {'provider': 'bedrock', 'model': 'profile'}]},
        model_routes={'planning': {'provider': 'gemini', 'model': 'planner'}}), base_dir=tmp_path)
    assert cfg.llm.fallbacks[0].base_url == 'https://generativelanguage.googleapis.com/v1beta/openai/'
    assert cfg.llm.fallbacks[0].api_key_env == 'GEMINI_API_KEY'
    assert cfg.llm.fallbacks[1].api_key_env == 'AWS_BEARER_TOKEN_BEDROCK'
    assert cfg.llm.fallbacks[0].secret_store is cfg.secret_store
    assert cfg.model_routes['planning'].secret_store is cfg.secret_store
    assert 'secret_store' not in json.dumps(model_settings(cfg.llm))


@pytest.mark.parametrize('provider,env,url,model', [
    ('groq', 'GROQ_API_KEY', 'https://api.groq.com/openai/v1', 'openai/gpt-oss-20b'),
    ('mistral', 'MISTRAL_API_KEY', 'https://api.mistral.ai/v1', 'mistral-small-latest'),
])
def test_free_tier_provider_http_contract_and_settings(tmp_path, monkeypatch, provider, env, url, model):
    import httpx
    import openai
    from james_monitoring import connections
    real_client, requests = openai.OpenAI, []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 0,
            'model': model, 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '{"reply":"ok"}'},
                                      'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(openai, 'OpenAI', lambda **kw: real_client(**kw, http_client=transport))
        monkeypatch.setenv(env, 'synthetic-key')
        cfg = parse_llm({'provider': provider, 'model': model},
                        {'provider': 'anthropic', 'model': 'old', 'api_key_env': 'WRONG_KEY', 'base_url': 'https://wrong.invalid'})
        assert cfg.base_url == url and cfg.api_key_env == env and not cfg.free_only
        result = make_llm(cfg).complete('Return JSON', [{'role': 'user', 'content': 'ping'}])
        assert result.total_tokens == 10 and result.text == '{"reply":"ok"}'
        assert str(requests[0].url) == url + '/chat/completions'
        assert requests[0].headers['authorization'] == 'Bearer synthetic-key'
        body = json.loads(requests[0].content)
        assert body['model'] == model and body['response_format'] == {'type': 'json_object'}
        assert 'provider' not in body  # no misleading account-level free-only price claim
    with pytest.raises(ConfigError):
        parse_llm({'provider': provider, 'model': model, 'free_only': True})
    monkeypatch.setattr(connections, 'check', lambda c: {'ok': False, 'installed': False, 'detail': '', 'fix': ''})
    monkeypatch.setattr(connections, 'opencode_info', lambda: {})
    monkeypatch.setattr(connections, 'ollama_models', lambda: (False, []))
    entry = next(p for p in connections.catalog() if p['id'] == provider)
    assert entry['base_url'] == url and entry['key_env'] == env and model in entry['models']
    app = App(tmp_path, telegram=False)
    try:
        app.setup({'company': 'Test', 'owner': 'Owner', 'provider': 'fake'})
        app.models_assign({'provider': provider, 'model': model, 'who': 'work',
                           'fallbacks': [{'provider': provider, 'model': model}]})
        saved = app.models_routes()['work']
        assert saved['base_url'] == url and saved['api_key_env'] == env
        assert saved['fallbacks'][0]['base_url'] == url
        assert app.rt.cfg.model_routes['work'].fallbacks[0].secret_store is app.rt.cfg.secret_store
        assert app.rt.cfg.llm.provider == 'fake' and env in app.credential_names()
        assert 'synthetic-key' not in (tmp_path / 'config.yaml').read_text()
    finally:
        app.close()


@pytest.mark.parametrize('raw', [
    {'provider': 'gemini', 'free_only': True},
    {'provider': 'openrouter', 'model': 'paid', 'free_only': True},
    {'provider': 'openrouter', 'model': 'openrouter/free', 'free_only': True, 'base_url': 'https://other.invalid'},
    {'provider': 'fake', 'fallbacks': [{}]},
    {'provider': 'fake', 'fallbacks': [{'provider': 'fake', 'fallbacks': [{'provider': 'fake'}]}]},
    {'provider': 'fake', 'fallbacks': [{'provider': 'fake'}] * 3},
    {'provider': 'openrouter', 'model': 'openrouter/free', 'free_only': True,
     'fallbacks': [{'provider': 'gemini', 'model': 'paid'}]},
])
def test_invalid_fallback_configuration_rejected(raw):
    with pytest.raises(ConfigError):
        parse_llm(raw)


@pytest.mark.parametrize('error', ['429 rate limit', 'quota exhausted', 'usage limit reached',
                                  '402 insufficient credits', '503 overloaded', 'connection timed out'])
def test_only_availability_errors_qualify(error):
    assert can_fallback(error)


@pytest.mark.parametrize('error', ['401 quota', '403 permission denied', 'invalid model', 'API key missing',
                                  'not logged in', 'content filter', 'safety refusal', '400 quota', 'arbitrary failure'])
def test_security_and_configuration_errors_stop(error):
    assert not can_fallback(error)


def test_fallback_order_no_replay_and_actual_model(monkeypatch):
    import james_monitoring.llm.fallback as f
    calls = []
    class Client:
        def __init__(self, c): self.c = c
        def complete(self, system, messages):
            calls.append((self.c.model, messages))
            if self.c.model == 'primary': raise LLMError('usage limit reached')
            return LLMResult('{"reply":"ok","actions":[]}', 10, 5)
    monkeypatch.setattr(f, 'make_llm', Client)
    llm = make_llm(parse_llm({'provider': 'fake', 'model': 'primary',
                             'fallbacks': [{'provider': 'fake', 'model': 'backup'}]}))
    res = llm.complete('system', [{'role': 'user', 'content': 'hi'}])
    assert [m for m, _ in calls] == ['primary', 'backup'] and calls[0][1] == calls[1][1]
    assert res.model == 'fake/backup' and res.fallback_used and res.total_tokens == 15
    assert not getattr(llm, 'sessions', False)


def test_opencode_rate_limit_does_not_get_misclassified_as_login_failure(tmp_path, monkeypatch):
    from james_monitoring.llm import opencode_llm as oc
    monkeypatch.setenv('JM_OPENCODE_BIN', '/synthetic/opencode')
    monkeypatch.setattr(oc, 'run', lambda *a, **k: SimpleNamespace(stdout=json.dumps(
        {'type': 'error', 'error': {'data': {'message': '429 rate limit exceeded'}}}), stderr=''))
    llm = make_llm(parse_llm({'provider': 'opencode', 'model': 'vendor/model'}))
    llm.sandbox = str(tmp_path)
    with pytest.raises(LLMError) as e:
        llm.complete('system', [{'role': 'user', 'content': 'ping'}])
    assert can_fallback(str(e.value)) and 'Log in' not in str(e.value)


def test_gemini_and_openrouter_request_contract(monkeypatch):
    import openai
    clients, requests = [], []
    def client(**kw):
        clients.append(kw)
        def create(**body):
            requests.append(body)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'), finish_reason='stop')],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3, prompt_tokens_details=None))
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(openai, 'OpenAI', client)
    monkeypatch.setenv('GEMINI_API_KEY', 'test-gemini')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test-router')
    for raw in ({'provider': 'gemini', 'model': 'test'},
                {'provider': 'openrouter', 'model': 'openrouter/free', 'free_only': True}):
        res = make_llm(parse_llm(raw)).complete('JSON please', [{'role': 'user', 'content': 'ping'}])
        assert res.total_tokens == 13
    assert clients[0]['api_key'] == 'test-gemini' and 'googleapis.com' in clients[0]['base_url']
    assert clients[1]['api_key'] == 'test-router'
    assert requests[1]['extra_body']['provider']['max_price'] == {'prompt': 0, 'completion': 0}
    assert requests[1]['extra_body']['models'] == ['openrouter/free']


def test_bedrock_contract_and_secret_safe_errors(monkeypatch):
    from james_monitoring.llm import bedrock_llm as b
    monkeypatch.setenv('AWS_BEARER_TOKEN_BEDROCK', 'test-secret')
    seen = []
    def request(req, timeout):
        seen.append(req)
        return io.BytesIO(json.dumps({'output': {'message': {'content': [{'text': 'pong'}]}},
            'usage': {'inputTokens': 7, 'outputTokens': 3, 'cacheReadInputTokens': 4},
            'stopReason': 'max_tokens'}).encode())
    monkeypatch.setattr(b, 'urlopen', request)
    llm = make_llm(parse_llm({'provider': 'bedrock', 'model': 'arn:aws:bedrock:profile/test', 'region': 'ap-south-1'}))
    result = llm.complete('system', [{'role': 'assistant', 'content': ''}, {'role': 'user', 'content': 'hi'}])
    assert result.text == 'pong' and result.truncated and result.input_tokens == 11
    assert 'bedrock-runtime.ap-south-1.amazonaws.com' in seen[0].full_url
    assert '%2F' in seen[0].full_url and seen[0].get_header('Authorization') == 'Bearer test-secret'
    assert json.loads(seen[0].data)['messages'][0]['role'] == 'user'
    def denied(*a, **kw): raise HTTPError('url', 403, 'test-secret', {}, io.BytesIO(b'test-secret'))
    monkeypatch.setattr(b, 'urlopen', denied)
    with pytest.raises(LLMError) as err: llm.complete('s', [])
    assert 'test-secret' not in str(err.value) and not can_fallback(str(err.value))


async def test_routes_reach_planning_and_task_work_and_preserve_overrides(tmp_path, monkeypatch):
    cfg = parse_config(make_raw(tmp_path, model_routes={
        'planning': {'provider': 'fake', 'model': 'planner'}, 'work': {'provider': 'fake', 'model': 'worker'}}), base_dir=tmp_path)
    rt = Runtime(cfg, make_llm(cfg.llm))
    m = cfg.member('sofia')
    assert cfg.llm_for(m, 'site', 'planning').model == 'planner'
    assert cfg.llm_for(m, 'site', 'work').model == 'worker'
    assert cfg.llm_for(m, 'site').model == 'fake'
    seen = []
    def factory(c):
        class Model:
            def complete(self, system, messages):
                seen.append(c.model)
                return LLMResult('{"reply":"done","actions":[]}', 1, 1)
        return Model()
    monkeypatch.setattr('james_monitoring.runtime.make_llm', factory)
    task = rt.tasks.create(title='Work', owner='sofia', created_by='pankaj', project='site')
    await rt.dispatch('sofia', Event('system', 'do the task', sender='system', task=task.id, project='site'))
    assert seen == ['worker']
    plan = ProjectPlan(rt, 'site')
    signature = plan.signature()
    cfg.model_routes['planning'].fallbacks = [parse_llm({'provider': 'fake', 'model': 'backup'})]
    assert signature != plan.signature()
    cfg.projects['site'].llm = parse_llm({'provider': 'fake', 'model': 'explicit'})
    assert cfg.llm_for(m, 'site', 'planning').model == 'explicit'


def test_settings_roundtrip_routes_and_credentials(tmp_path):
    app = App(tmp_path, telegram=False)
    try:
        app.setup({'company': 'Test', 'owner': 'Owner', 'provider': 'fake'})
        app.rt.cfg.llm.max_tokens = 12000
        app.models_assign({'provider': 'gemini', 'model': 'test', 'who': 'planning',
                           'fallbacks': [{'provider': 'openrouter', 'model': 'openrouter/free', 'free_only': True}]})
        assert app.rt.cfg.llm.provider == 'fake'
        assert app.rt.cfg.model_routes['planning'].max_tokens == 12000
        assert app.models_routes()['planning']['fallbacks'][0]['free_only']
        assert {'GEMINI_API_KEY', 'AWS_BEARER_TOKEN_BEDROCK', 'OPENROUTER_API_KEY'} <= app.credential_names()
        app.models_assign({'who': 'planning', 'reset': True})
        assert 'planning' not in app.models_routes()
    finally:
        app.close()


def test_purpose_session_reset_and_display(rt):
    m = rt.cfg.member('sofia')
    sess = rt.session_for(m, 'p-site', 'site', 'work')
    rt._save_session(m, sess, LLMResult('ok', session_id='synthetic'), 'site', 'work')
    assert rt.sessions()[0]['room'] == 'p-site' and rt.sessions()[0]['current']
    assert rt.reset_sessions('sofia', 'p-site') == 1
