import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from evaluation.evaluator import EndToEndEvaluator, LLMJudge, QualityScores


@pytest.fixture(autouse=True)
def no_unused_remote_client(monkeypatch):
    # End-to-end tests inject their Judge; no remote client is used there.
    monkeypatch.setattr("evaluation.evaluator.AsyncAnthropic", lambda **kwargs: None)


class JudgeClient:
    """Only the remote model boundary is replaced; Judge parsing stays real."""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.prompts = []
        self.messages = self

    async def create(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        if self.error:
            raise self.error
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.payload)])


class ConversationService:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    async def chat(self, message, user_id, conv_id=None, *, wait_for_profile=False):
        self.calls.append((message, user_id, conv_id, wait_for_profile))
        if self.failure and message == "broken":
            raise self.failure
        assert wait_for_profile is True
        return {
            "response": "answer: " + message,
            "conv_id": conv_id or "service-conversation",
            "agent_type": "general",
            "intent": "greeting",
            "latency_ms": 12.5,
            "orchestrator_latency_ms": 5.0,
            "tool_traces": [{"name": "policy", "success": True}],
        }


class FixedJudge:
    def __init__(self, scores):
        self.scores = iter(scores)
        self.calls = []

    async def judge(self, question, response, context=None, **kwargs):
        self.calls.append((question, response, context, kwargs))
        value = next(self.scores)
        if isinstance(value, Exception):
            raise value
        return value


def quality(value):
    return QualityScores(value, value, value, value)


def evaluator(tmp_path, scores, *, service=None, baseline=None):
    result = EndToEndEvaluator(
        orchestrator=None, recognizer=None, api_key="offline-test-key",
        chat_service=service or ConversationService(),
        baseline_path=str(baseline or tmp_path / "baseline.json"),
        report_dir=str(tmp_path / "runs"),
    )
    result._judge = FixedJudge(scores)
    return result


def snapshot(**changes):
    values = {
        "dataset": "customer-service-v1", "model": "offline-fake-v1",
        "prompt": "p1", "skills": "s1", "knowledge": "k1",
        "code": "commit1", "judge": "j1", "retrieval": "always-v1",
    }
    values.update(changes)
    return values


@pytest.mark.parametrize("payload", [
    '{"relevance": 0.9}',
    '{"relevance": NaN, "accuracy": 1, "completeness": 1, "helpfulness": 1}',
    '{"relevance": Infinity, "accuracy": 1, "completeness": 1, "helpfulness": 1}',
    '{"relevance": -0.1, "accuracy": 1, "completeness": 1, "helpfulness": 1}',
    '{"relevance": 1.1, "accuracy": 1, "completeness": 1, "helpfulness": 1}',
    '{"relevance": true, "accuracy": 1, "completeness": 1, "helpfulness": 1}',
    '{"relevance": "0.9", "accuracy": 1, "completeness": 1, "helpfulness": 1}',
    "not JSON",
])
def test_judge_rejects_incomplete_nonfinite_out_of_range_or_nonnumeric_scores(payload):
    scores = asyncio.run(LLMJudge(JudgeClient(payload), "offline").judge("Q", "A"))
    assert scores.judge_failed is True
    assert scores.overall is None
    assert scores.error


def test_judge_remote_failure_is_unscored():
    scores = asyncio.run(LLMJudge(JudgeClient(error=TimeoutError("timeout")), "offline").judge("Q", "A"))
    assert scores.judge_failed is True
    assert scores.overall is None


def test_judge_uses_expected_answer_and_evidence_without_breaking_old_calls():
    payload = '{"relevance": 0, "accuracy": 1, "completeness": 0.5, "helpfulness": 1}'
    client = JudgeClient(payload)
    judge = LLMJudge(client, "offline")
    scores = asyncio.run(judge.judge("Q", "A", expected="policy answer", evidence=[{"id": "policy-v1"}]))
    assert scores.overall == 0.625
    assert "policy answer" in client.prompts[0]
    assert "policy-v1" in client.prompts[0]
    assert asyncio.run(judge.judge("Q", "A", "history")).judge_failed is False


def test_invalid_judgments_never_pass_or_enter_quality_means(tmp_path):
    runner = evaluator(tmp_path, [quality(1), QualityScores(.5, .5, .5, .5, judge_failed=True, error="bad JSON")])
    report = asyncio.run(runner.run(dialog_cases=[{"question": "good"}, {"question": "bad"}]))
    assert report.total == 2
    assert report.passed == 1
    assert report.pass_rate == 0.5
    assert report.invalid == 1
    assert report.valid_count == 1
    assert report.avg_scores["accuracy"] == 1.0
    assert report.results[1].scores == {}
    assert report.results[1].metadata["status"] == "invalid"


@pytest.mark.parametrize("bad_score", [quality(float("nan")), RuntimeError("judge unavailable")])
def test_all_invalid_scores_have_no_average_or_success_recommendation(tmp_path, bad_score):
    report = asyncio.run(evaluator(tmp_path, [bad_score]).run(dialog_cases=[{"question": "Q"}]))
    assert report.avg_scores == {}
    assert report.passed == report.valid_count == 0
    assert report.invalid == report.total == 1
    assert "所有指标均达标，继续保持" not in report.recommendations


def test_dialog_uses_complete_service_and_preserves_multiturn_evidence(tmp_path):
    service = ConversationService()
    runner = evaluator(tmp_path, [quality(1), quality(1)], service=service)
    report = asyncio.run(runner.run(dialog_cases=[{
        "turns": ["hello", "follow-up"], "user_id": "isolated-user",
        "expected": "policy answer", "evidence": [{"id": "policy-v1"}],
    }]))
    assert report.total == 2
    assert service.calls[0][1] == "isolated-user"
    assert service.calls[0][2] == service.calls[1][2]
    assert all(call[3] for call in service.calls)
    assert "answer: hello" in runner._judge.calls[1][2]
    assert runner._judge.calls[0][3] == {"expected": "policy answer", "evidence": [{"id": "policy-v1"}]}
    assert report.results[0].metadata["latency_ms"] == 12.5
    assert report.results[0].metadata["tool_traces"][0]["name"] == "policy"
    assert report.metadata["coverage"] == "chat_service"


@pytest.mark.parametrize("failure", [RuntimeError("provider down"), TimeoutError("timeout")])
def test_dialog_error_is_counted_and_later_cases_continue(tmp_path, failure):
    runner = evaluator(tmp_path, [quality(1)], service=ConversationService(failure))
    report = asyncio.run(runner.run(dialog_cases=[{"question": "broken"}, {"question": "good"}]))
    assert report.total == 2
    assert report.invalid == 1
    assert report.valid_count == 1
    assert report.passed == 1
    assert report.results[0].metadata["status"] == "error"


def test_empty_run_is_not_presented_as_success(tmp_path):
    report = asyncio.run(evaluator(tmp_path, []).run())
    assert report.total == report.passed == report.valid_count == 0
    assert report.pass_rate == 0
    assert "所有指标均达标，继续保持" not in report.recommendations
    assert report.metadata["comparison"]["comparable"] is False


def test_runs_archive_independently_and_never_automatically_replace_baseline(tmp_path):
    runner = evaluator(tmp_path, [quality(1), quality(.7)])
    first = asyncio.run(runner.run(dialog_cases=[{"question": "Q"}], metadata=snapshot()))
    assert not (tmp_path / "baseline.json").exists()
    runner.promote_baseline(first)
    frozen = (tmp_path / "baseline.json").read_bytes()
    second = asyncio.run(runner.run(dialog_cases=[{"question": "Q"}], metadata=snapshot()))
    assert (tmp_path / "baseline.json").read_bytes() == frozen
    assert first.run_id != second.run_id
    assert len(list((tmp_path / "runs").glob("*.json"))) == 2
    saved = json.loads((tmp_path / "runs" / f"{second.run_id}.json").read_text(encoding="utf-8"))
    assert saved == asdict(second)
    assert second.regressions
    assert second.metadata["comparison"]["comparable"] is True


@pytest.mark.parametrize("changed", ["dataset", "model", "prompt", "skills", "knowledge", "code", "judge", "retrieval"])
def test_different_fingerprint_does_not_claim_regression_comparison(tmp_path, changed):
    runner = evaluator(tmp_path, [quality(1), quality(.1)])
    first = asyncio.run(runner.run(dialog_cases=[{"question": "Q"}], metadata=snapshot()))
    runner.promote_baseline(first)
    second = asyncio.run(runner.run(dialog_cases=[{"question": "Q"}], metadata=snapshot(**{changed: "changed"})))
    assert second.regressions == []
    assert second.metadata["comparison"]["comparable"] is False
    assert changed in second.metadata["comparison"]["reason"]


def test_legacy_baseline_loads_but_missing_snapshot_is_not_comparable(tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"total": 1, "passed": 1, "avg_scores": {"accuracy": 1}}), encoding="utf-8")
    runner = evaluator(tmp_path, [quality(.1)], baseline=baseline)
    report = asyncio.run(runner.run(dialog_cases=[{"question": "Q"}], metadata=snapshot()))
    assert runner._baseline.total == 1
    assert report.regressions == []
    assert report.metadata["comparison"]["comparable"] is False
    assert "missing" in report.metadata["comparison"]["reason"].lower()


def test_regression_uses_frozen_baseline_instead_of_previous_run(tmp_path):
    runner = evaluator(tmp_path, [quality(1), quality(.8), quality(.79)])
    first = asyncio.run(runner.run(dialog_cases=[{"question": "Q"}], metadata=snapshot()))
    runner.promote_baseline(first)
    asyncio.run(runner.run(dialog_cases=[{"question": "Q"}], metadata=snapshot()))
    third = asyncio.run(runner.run(dialog_cases=[{"question": "Q"}], metadata=snapshot()))
    assert any("1.000" in item and "0.790" in item for item in third.regressions)


def test_caller_mutation_cannot_change_promoted_snapshot(tmp_path):
    runner = evaluator(tmp_path, [quality(1), quality(.5)])
    metadata = {"fingerprints": snapshot()}
    first = asyncio.run(runner.run(dialog_cases=[{"question": "Q"}], metadata=metadata))
    runner.promote_baseline(first)
    metadata["fingerprints"]["model"] = "changed-by-caller"
    first.metadata["fingerprints"]["model"] = "changed-report"
    report = asyncio.run(runner.run(dialog_cases=[{"question": "Q"}], metadata={"fingerprints": snapshot()}))
    assert report.metadata["comparison"]["comparable"] is True
    assert report.regressions


def test_duplicate_archive_id_is_rejected_without_overwrite(tmp_path):
    runner = evaluator(tmp_path, [quality(1)])
    asyncio.run(runner.run(dialog_cases=[{"question": "Q"}], metadata={"run_id": "fixed-run"}))
    saved = (tmp_path / "runs" / "fixed-run.json").read_bytes()
    with pytest.raises(ValueError, match="already archived"):
        asyncio.run(runner.run(metadata={"run_id": "fixed-run"}))
    assert (tmp_path / "runs" / "fixed-run.json").read_bytes() == saved


@pytest.mark.parametrize("run_id", ["../escape", "..\\escape", "/absolute", "C:\\escape", ".", ""])
def test_report_run_id_cannot_escape_archive_directory(tmp_path, run_id):
    runner = evaluator(tmp_path, [])
    with pytest.raises(ValueError):
        asyncio.run(runner.run(metadata={"run_id": run_id}))
    assert not (tmp_path / "escape.json").exists()


def test_legacy_orchestrator_path_is_explicit_about_coverage(tmp_path):
    class Orchestrator:
        async def run(self, request):
            return SimpleNamespace(response="legacy answer", agent_type=SimpleNamespace(value="general"), intent=None)

    runner = EndToEndEvaluator(Orchestrator(), None, "offline", report_dir=str(tmp_path))
    runner._judge = FixedJudge([quality(1)])
    report = asyncio.run(runner.run(dialog_cases=[{"question": "hello"}]))
    assert report.metadata["coverage"] == "legacy_orchestrator"
    assert report.results[0].metadata["coverage"] == "legacy_orchestrator"
    assert report.metadata["comparison"]["comparable"] is False
