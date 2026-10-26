import asyncio
from types import SimpleNamespace

import httpx


def test_legacy_eval_uses_isolated_service_and_cleans_up(monkeypatch, tmp_path):
    async def run():
        from api import main
        from evaluation import evaluator
        namespaces, closed, services = [], [], []
        online = object()
        isolated = object()

        async def factory(*, namespace, knowledge_documents=None):
            namespaces.append(namespace)
            async def close(cleanup=False):
                closed.append(cleanup)
            return SimpleNamespace(service=isolated, orchestrator=object(), recognizer=object(), close=close)

        class Evaluator:
            def __init__(self, **kwargs):
                services.append(kwargs['chat_service'])
            async def run(self, **kwargs):
                return SimpleNamespace(pass_rate=1, total=1, passed=1, avg_scores={}, regressions=[],
                                       recommendations=[], results=[], run_id='test', metadata={}, invalid=0, valid_count=1)

        monkeypatch.setattr(main, '_chat_service', online)
        monkeypatch.setattr(main, '_isolated_runtime', factory, raising=False)
        monkeypatch.setattr(evaluator, 'EndToEndEvaluator', Evaluator)
        monkeypatch.setattr(main, '_anthropic_cfg', lambda: {'api_key': 'test', 'model': 'test'})
        monkeypatch.setenv('EVAL_BASELINE_PATH', str(tmp_path / 'baseline.json'))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            for _ in range(2):
                response = await client.post('/eval/run', json={'intent_cases': [], 'dialog_cases': []})
                assert response.status_code == 200
        assert services == [isolated, isolated]
        assert namespaces[0] != namespaces[1]
        assert all(name.startswith('eval_') and len(name) == 37 for name in namespaces)
        assert closed == [True, True]
    asyncio.run(run())


def test_scenario_validation_history_and_safe_report_lookup(monkeypatch, tmp_path):
    async def run():
        from api import main
        dataset = {
            'id': 'test', 'version': '1', 'status': 'synthetic_pending_review',
            'knowledge': [{'id': 'policy', 'title': '演示政策', 'version': '1', 'content': '测试内容'}],
            'cases': [{'id': 'c1', 'scenario': 'single_intent', 'description': '问候',
                       'turns': [{'message': '你好', 'expected_intent': 'greeting'}],
                       'initial_state': {'messages': []}, 'budgets': {'turn_timeout_s': 5},
                       'assertions': [{'id': 'i1', 'type': 'intent', 'turn': 0, 'expected': 'greeting'}]}],
        }
        monkeypatch.setenv('ECHOMIND_EVAL_REPORT_DIR', str(tmp_path))
        monkeypatch.setattr(main, '_scenario_dataset', lambda: dataset, raising=False)
        async def forbidden_factory(**kwargs):
            raise AssertionError('validation must never construct a live runtime')
        monkeypatch.setattr(main, '_isolated_runtime', forbidden_factory)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            catalog = await client.get('/eval/scenarios')
            assert catalog.status_code == 200
            report = await client.post('/eval/scenarios/run')
            assert report.status_code == 200
            data = report.json()
            assert data['status'] == 'validated' and data['total'] == 0
            run_id = data['run_id']
            assert (await client.get('/eval/runs')).json()['items'][0]['run_id'] == run_id
            assert (await client.get(f'/eval/runs/{run_id}')).json()['run_id'] == run_id
            assert (await client.get('/eval/runs/unknown')).status_code == 404
            assert (await client.get('/eval/runs/CON')).status_code == 422
            assert (await client.post('/eval/scenarios/run', json={'mode': 'invented'})).status_code == 422
            assert (await client.post('/eval/scenarios/run', json={'scenario': 'unknown'})).status_code == 422
            comparison = await client.post('/eval/compare', json={'left_run_id': run_id, 'right_run_id': run_id})
            assert comparison.status_code == 200
            assert comparison.json()['comparable'] is False
    asyncio.run(run())
