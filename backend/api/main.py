"""
mas-agent 智能客服系统 — FastAPI 入口

启动时打印项目名称与版本。
所有核心组件在 lifespan 中初始化，通过环境变量配置。
"""
import asyncio
import logging
import os
import pathlib
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Literal


_ROOT = str(pathlib.Path(__file__).parent.parent.resolve())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from services.storage import identity_text

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BANNER = """
    mas-agent v2.0
    智能客服系统
"""

# ── 全局组件（lifespan 中初始化）─────────────────────────────────────────────
_orchestrator = None
_memory       = None
_tool_manager = None
_monitor      = None
_evaluator    = None
_skill_manager = None
_chat_service = None
_runtime = None

def _anthropic_cfg() -> Dict[str, Any]:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("未设置 ANTHROPIC_API_KEY")
    cfg: Dict[str, Any] = {
        "api_key":  key,
        "model":    os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022").strip(),
    }
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if base_url:
        cfg["base_url"] = base_url
    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator, _memory, _tool_manager, _monitor, _skill_manager, _chat_service, _runtime
    from monitor.performance_monitor import PerformanceMonitor
    from observability.telemetry import get_telemetry
    from services.runtime import create_runtime, runtime_config_from_env

    print(BANNER, flush=True)
    if os.getenv('ECHOMIND_EVALUATION_ONLY', 'false').lower() == 'true':
        logger.info('Evaluation-only mode: live conversation components are disabled')
        yield
        return
    _anthropic_cfg()  # Validate the live provider configuration without logging it.
    telemetry = get_telemetry()
    _runtime = await create_runtime(runtime_config_from_env(), telemetry=telemetry)
    _orchestrator, _memory = _runtime.orchestrator, _runtime.memory
    _tool_manager, _skill_manager = _runtime.tool_manager, _runtime.skills
    _chat_service = _runtime.service
    try:
        _monitor = PerformanceMonitor(
            orchestrator=_orchestrator, tool_manager=_tool_manager,
            interval_s=float(os.getenv("MONITOR_INTERVAL", "10")),
            webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
            prometheus_port=int(os.getenv("PROMETHEUS_PORT", "0")) or None,
        )
        await _monitor.start()
        logger.info("mas-agent ready")
        yield
    finally:
        try:
            if _monitor is not None:
                await _monitor.stop()
        finally:
            try:
                await _runtime.close()
            finally:
                telemetry.shutdown()
                _chat_service = None


async def _isolated_runtime(*, namespace, knowledge_documents=None):
    from observability.telemetry import get_telemetry
    from services.runtime import create_runtime, runtime_config_from_env
    return await create_runtime(
        runtime_config_from_env(), namespace=namespace,
        knowledge_documents=knowledge_documents or [], telemetry=get_telemetry(),
    )


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="mas-agent · 智能客服",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 请求/响应模型 ─────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message:     str
    user_id:     str = "anonymous"
    conv_id:     Optional[str] = None


class ChatResponse(BaseModel):
    conv_id:     str
    request_id:  str = ""
    response:    str
    intent:      str
    intent_group: str = "other"
    agent_type:  str
    agent_types: List[str] = Field(default_factory=list)
    primary_agent: str = ""
    supporting_agents: List[str] = Field(default_factory=list)
    tools_used: List[str] = Field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.0
    escalated:   bool
    latency_ms:  float
    knowledge_used: bool = False
    entities: Dict[str, List[str]] = Field(default_factory=dict)
    intent_confidence: float = 0.0
    intent_source_scores: Dict[str, float] = Field(default_factory=dict)
    intent_source_categories: Dict[str, str] = Field(default_factory=dict)
    intent_decision_mechanism: str = ""
    orchestrator_latency_ms: float = 0.0
    stage_latency_ms: Dict[str, float] = Field(default_factory=dict)
    tool_traces: List[Dict[str, Any]] = Field(default_factory=list)
    profile_status: str = "scheduled"
    usage: Dict[str, int] = Field(default_factory=dict)
    usage_scope: str = "through_response"
    memory_evidence_ids: List[str] = Field(default_factory=list)
    knowledge_required: bool = False
    request_understanding: Dict[str, Any] = Field(default_factory=dict)
    voted_intent: str = ""
    intent_resolution: str = "legacy_vote"


class ToolTraceResponse(BaseModel):
    request_id: str
    found: bool
    trace: Dict[str, Any] = Field(default_factory=dict)


class RecentToolTracesResponse(BaseModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)


# ── 路由 ──────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    return {"status": "ok", "agents": _orchestrator.get_stats()}


@app.get("/skills", tags=["Skills"])
async def skills_summary():
    """查看当前已加载的 Skills，便于确认热加载结果和排查解析错误。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    return _skill_manager.summary()


@app.post("/skills/reload", tags=["Skills"])
async def reload_skills():
    """运行时重新扫描 Skill 目录，不需要重启服务。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    _skill_manager.reload()
    if _orchestrator is not None:
        _orchestrator.set_skill_manager(_skill_manager)
    return _skill_manager.summary()


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """
    主对话接口。完整流程：
      记忆读取 → 意图识别 → Agent 路由 → 执行 → 记忆写入
    """
    try:
        # Do not echo invalid Unicode through Pydantic's default error payload.
        user_id = identity_text(req.user_id)
        conv_id = None if req.conv_id is None else identity_text(req.conv_id)
        if _chat_service is None:
            raise HTTPException(503, "服务未就绪")
        return await _chat_service.chat(req.message, user_id, conv_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


async def _build_knowledge_context(message: str, intent=None, top_k: int = 3) -> tuple[str, bool]:
    """
    为 /chat 主链路构建 RAG 知识上下文。

    这里复用 MCPToolManager 的查询改写、并行召回、重排、fallback 能力。
    """
    if _tool_manager is None:
        return "", False
    if not _should_use_knowledge(message, intent=intent):
        return "", False
    try:
        result = await _tool_manager.search_with_rewrite("knowledge_search", message, top_k=top_k)
        if not result.success or not isinstance(result.data, list) or not result.data:
            return "", False

        parts = ["[知识库检索结果]"]
        used = False
        for i, item in enumerate(result.data[:top_k], start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "未命名文档"))
            content = str(item.get("content", "")).strip()
            score = item.get("score", "")
            if not content:
                continue
            used = True
            parts.append(f"{i}. 标题: {title}\n   相关度: {score}\n   内容: {content[:600]}")

        if not used:
            return "", False
        parts.append("请优先依据以上知识库内容回答；如果知识库内容不足，再结合通用客服能力说明。")
        return "\n".join(parts), True
    except Exception as ex:
        logger.warning(f"构建知识库上下文失败: {ex}")
        return "", False


def _should_use_knowledge(message: str, intent=None) -> bool:
    """跳过纯寒暄，业务类问题才检索知识库，避免无关 RAG 干扰回复。"""
    msg = (message or "").strip().lower()
    if not msg:
        return False
    intent_value = getattr(intent, "value", intent)
    if intent_value in {"greeting", "feedback", "escalation", "human_handoff", "other"}:
        return False
    if intent_value in {
        "query", "request", "technical", "billing", "account", "complaint",
        "order_status", "logistics", "refund", "invoice", "payment_issue",
        "account_security", "technical_login", "technical_crash",
    }:
        return True
    greetings = {"你好", "您好", "嗨", "hi", "hello", "hey", "早上好", "晚上好"}
    if msg in greetings:
        return False
    business_keywords = [
        "退款", "订单", "物流", "配送", "发票", "扣款", "支付", "账单", "订阅",
        "登录", "报错", "错误", "崩溃", "会员", "积分", "账户", "密码", "地址",
        "refund", "order", "invoice", "payment", "error", "login",
    ]
    return len(msg) >= 4 or any(kw in msg for kw in business_keywords)


@app.get("/monitor")
async def monitor_summary():
    """实时监控摘要：Agent 成功率、工具统计、告警、优化建议。"""
    if _monitor is None:
        raise HTTPException(503, "服务未就绪")
    return _monitor.summary()


@app.get("/trace/tool/{request_id}", response_model=ToolTraceResponse)
async def get_tool_trace(request_id: str):
    """查看某次请求的工具调用明细。"""
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    trace = _orchestrator.get_tool_trace(request_id)
    return ToolTraceResponse(
        request_id=request_id,
        found=trace is not None,
        trace=trace or {},
    )


@app.get("/trace/tools", response_model=RecentToolTracesResponse)
async def list_recent_tool_traces(limit: int = 20):
    """查看最近 N 次请求的工具调用明细。"""
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    return RecentToolTracesResponse(items=_orchestrator.get_recent_tool_traces(limit=limit))


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus 指标入口。"""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/search")
async def search(query: str, top_k: int = 5):
    """
    演示检索优化链路：查询改写 → 并行召回 → 重排 → Top-K。
    展示 MCP 工具调用的核心亮点。
    """
    if _tool_manager is None:
        raise HTTPException(503, "服务未就绪")
    result = await _tool_manager.search_with_rewrite("knowledge_search", query, top_k=top_k)
    return {"query": query, "results": result.data, "reranked": result.reranked}


class DocInput(BaseModel):
    """单篇文档输入。"""
    title:   str
    content: str


class BatchDocInput(BaseModel):
    """批量文档导入请求体。"""
    documents: List[DocInput]


class EvalIntentInput(BaseModel):
    """意图识别评测用例。"""
    message: str
    expected_intent: str
    context: Optional[Dict[str, Any]] = None


class EvalDialogInput(BaseModel):
    """对话质量评测用例。question 单轮，turns 多轮。"""
    question: Optional[str] = None
    turns: Optional[List[str]] = None
    user_id: Optional[str] = None
    conv_id: Optional[str] = None


class EvalRunInput(BaseModel):
    """评测请求。为空时使用内置默认用例。"""
    intent_cases: Optional[List[EvalIntentInput]] = None
    dialog_cases: Optional[List[EvalDialogInput]] = None


@app.post("/knowledge/add", tags=["知识库"])
async def add_knowledge(body: BatchDocInput):
    """
    批量导入文档到知识库。

    文档会自动切片（每片 500 字）并存入 ChromaDB，ChromaDB 内置 Embedding 模型自动向量化。

    示例请求体：
    ```json
    {
      "documents": [
        {"title": "退款政策", "content": "用户在购买后 7 天内可以申请无理由退款..."},
        {"title": "配送说明", "content": "标准配送 3-5 个工作日..."}
      ]
    }
    ```
    """
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    count = await kb.add_documents_async([{"title": d.title, "content": d.content} for d in body.documents])
    total = await kb.doc_count_async()
    return {"message": f"成功导入 {count} 个文档片段", "added_chunks": count, "total_chunks": total}


@app.post("/knowledge/upload", tags=["知识库"])
async def upload_knowledge(file: UploadFile = File(...)):
    """
    上传文件导入知识库。

    支持格式：
    - `.txt` / `.md`：整个文件作为一篇文档，文件名作为标题
    - `.json`：JSON 数组格式 `[{"title": "...", "content": "..."}, ...]`

    文件大小限制：10MB
    """
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件大小超过 10MB 限制")

    text = content.decode("utf-8", errors="ignore")
    filename = file.filename or "unknown"

    if filename.endswith(".json"):
        import json as _json
        try:
            docs = _json.loads(text)
            if not isinstance(docs, list):
                raise HTTPException(400, "JSON 文件应为数组格式: [{title, content}, ...]")
        except _json.JSONDecodeError as e:
            raise HTTPException(400, f"JSON 解析失败: {e}")
    else:
        # txt / md：整个文件作为一篇文档
        title = filename.rsplit(".", 1)[0] if "." in filename else filename
        docs = [{"title": title, "content": text}]

    count = await kb.add_documents_async(docs)
    total = await kb.doc_count_async()
    return {
        "message": f"文件 {filename} 导入成功",
        "added_chunks": count,
        "total_chunks": total,
    }


@app.get("/knowledge/stats", tags=["知识库"])
async def knowledge_stats():
    """查看知识库统计信息（文档片段总数）。"""
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    return {"total_chunks": await kb.doc_count_async()}


@app.post("/eval/run")
async def run_eval(body: Optional[EvalRunInput] = None):
    """Explicit live legacy evaluation, isolated from online conversation state."""
    if _chat_service is None:
        raise HTTPException(503, "服务未就绪")
    from dataclasses import asdict
    from evaluation.evaluator import DEFAULT_DIALOG_CASES, DEFAULT_INTENT_CASES, IntentTestCase, EndToEndEvaluator

    intent_cases = [IntentTestCase(**case.model_dump()) for case in body.intent_cases] if body and body.intent_cases is not None else DEFAULT_INTENT_CASES
    dialog_cases = [case.model_dump(exclude_none=True) for case in body.dialog_cases] if body and body.dialog_cases is not None else DEFAULT_DIALOG_CASES
    run_id = uuid.uuid4().hex
    runtime = await _isolated_runtime(namespace=f"eval_{run_id}")
    evaluator = None
    try:
        cfg = _anthropic_cfg()
        evaluator = EndToEndEvaluator(
            orchestrator=runtime.orchestrator, recognizer=runtime.recognizer,
            chat_service=runtime.service, **cfg,
            baseline_path=os.getenv("EVAL_BASELINE_PATH", "/app/data/eval/baseline.json"),
        )
        report = await evaluator.run(intent_cases=intent_cases, dialog_cases=dialog_cases, metadata={"run_id": run_id})
        return {key: getattr(report, key) for key in (
            "pass_rate", "total", "passed", "avg_scores", "regressions", "recommendations",
            "run_id", "metadata", "invalid", "valid_count",
        )} | {"results": [asdict(result) for result in report.results]}
    finally:
        try:
            client = getattr(getattr(evaluator, '_judge', None), '_client', None)
            if client is not None:
                await client.close()
        finally:
            await runtime.close(cleanup=True)


class ScenarioRunInput(BaseModel):
    mode: Literal['validate', 'live'] = 'validate'
    scenario: Optional[str] = None
    limit: int = Field(default=5, ge=1, le=120)
    model_config = {'extra': 'forbid'}


class CompareRunInput(BaseModel):
    left_run_id: str = Field(min_length=1, max_length=128)
    right_run_id: str = Field(min_length=1, max_length=128)
    vary: List[Literal['model', 'prompt', 'skills', 'code', 'retrieval']] = Field(default_factory=list)
    model_config = {'extra': 'forbid'}


def _scenario_dataset():
    from evaluation.scenarios import load_dataset
    return load_dataset()


def _report_store():
    from evaluation.report_store import ReportStore
    return ReportStore(os.getenv('ECHOMIND_EVAL_REPORT_DIR', str(pathlib.Path(_ROOT) / 'data/eval/scenarios')))


@app.get('/eval/scenarios')
async def scenario_catalog():
    from evaluation.scenarios import validate_dataset
    dataset = _scenario_dataset()
    summary = validate_dataset(dataset)
    groups = {}
    for case in dataset['cases']:
        groups[case['scenario']] = groups.get(case['scenario'], 0) + 1
    return {key: dataset[key] for key in ('id', 'version', 'status')} | {
        'summary': {**summary, 'cases': len(dataset['cases']), 'by_scenario': groups},
        'cases': [{'id': case['id'], 'scenario': case['scenario'], 'description': case['description'],
                   'turns': len(case['turns'])} for case in dataset['cases']],
    }


@app.post('/eval/scenarios/run')
async def run_scenarios(body: Optional[ScenarioRunInput] = None):
    import copy
    from datetime import datetime, timezone
    from evaluation.scenarios import validate_dataset, build_fingerprints
    from evaluation.scenario_runner import ScenarioRunner
    from services.runtime import runtime_config_from_env
    from observability.telemetry import get_telemetry

    options = body or ScenarioRunInput()
    dataset = copy.deepcopy(_scenario_dataset())
    if options.scenario:
        dataset['cases'] = [case for case in dataset['cases'] if case['scenario'] == options.scenario]
        if not dataset['cases']:
            raise HTTPException(422, 'Unknown scenario')
    if options.mode == 'live':
        dataset['cases'] = dataset['cases'][:options.limit]
    summary = validate_dataset(dataset)
    config = runtime_config_from_env()
    config['judge_enabled'] = options.mode == 'live'
    metadata = {'mode': options.mode, 'fingerprints': build_fingerprints(dataset, config)}
    if options.mode == 'validate':
        report = {'run_id': uuid.uuid4().hex, 'timestamp': datetime.now(timezone.utc).isoformat(),
                  'dataset': {key: dataset[key] for key in ('id', 'version', 'status')},
                  'mode': 'validate', 'status': 'validated', 'metadata': metadata,
                  'total': 0, 'passed': 0, 'pass_rate': None, 'invalid': 0, 'timeout': 0,
                  'error': 0, 'hard_fail': 0, 'cases': [], 'validation': summary}
    else:
        try:
            _anthropic_cfg()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        from evaluation.evaluator import LLMJudge
        from anthropic import AsyncAnthropic
        cfg = _anthropic_cfg()
        from observability.instrumentation import AnthropicClientProxy
        judge_client = AnthropicClientProxy(AsyncAnthropic(**{key: cfg[key] for key in ('api_key', 'base_url') if cfg.get(key)}))
        try:
            judge_options = {'mode': config['judge_mode']} if config.get('judge_mode', 'scores') != 'scores' else {}
            runner = ScenarioRunner(_isolated_runtime, judge=LLMJudge(judge_client, config['judge_model'], **judge_options), telemetry=get_telemetry())
            report = await runner.run(dataset, metadata=metadata)
        finally:
            await judge_client.close()
    await asyncio.to_thread(_report_store().save, report)
    return report


@app.get('/eval/runs')
async def list_eval_runs():
    return {'items': await asyncio.to_thread(_report_store().list)}


@app.get('/eval/runs/{run_id}')
async def get_eval_run(run_id: str):
    try:
        return await asyncio.to_thread(_report_store().load, run_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, 'Unknown evaluation run') from exc


@app.post('/eval/compare')
async def compare_eval_runs(body: CompareRunInput):
    try:
        return await asyncio.to_thread(_report_store().compare, body.left_run_id, body.right_run_id, vary=body.vary)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, 'Unknown evaluation run') from exc


# ── 交互式 CLI ────────────────────────────────────────────────────────────────
async def _cli():
    print(BANNER)
    print("mas-agent CLI — 输入 quit 退出\n")

    from agents.agent_orchestrator import AgentOrchestrator, Request
    from memory.conversation_memory import MemoryManager, MsgRole
    from core.skill_loader import SkillManager

    cfg = _anthropic_cfg()
    skill_manager = SkillManager(
        root_dir=os.getenv("ECHOMIND_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills")),
        max_prompt_chars=int(os.getenv("ECHOMIND_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    skill_manager.load()
    orch = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=skill_manager,
    )
    mem  = MemoryManager(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        chroma_host=os.getenv("CHROMA_HOST", "localhost"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/tmp/chroma"),
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    user_id, conv_id = "cli_user", str(uuid.uuid4())

    while True:
        try:
            msg = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见 ʕ•ᴥ•ʔ")
            break
        if not msg or msg.lower() in ("quit", "exit", "退出"):
            print("再见 ʕ•ᴥ•ʔ")
            break

        ctx = await mem.get_context(user_id, conv_id, query=msg)
        history = [
            {"role": m.role.value, "content": m.content}
            for m in ctx.recent_messages[-5:]
        ] if ctx.recent_messages else None
        req = Request(message=msg, user_id=user_id, conv_id=conv_id, context=ctx.to_prompt_text(), history=history)
        result = await orch.run(req)

        await mem.add_message(user_id, conv_id, MsgRole.USER, msg)
        await mem.add_message(user_id, conv_id, MsgRole.ASSISTANT, result.response)

        print(f"\nmas-agent [{result.agent_type.value}]: {result.response}\n")

    await mem.close()


if __name__ == "__main__":
    if "--cli" in sys.argv:
        asyncio.run(_cli())
    else:
        uvicorn.run(
            "api.main:app",
            host=os.getenv("API_HOST", "0.0.0.0"),
            port=int(os.getenv("API_PORT", "8000")),
            reload=os.getenv("APP_ENV") == "development",
        )
