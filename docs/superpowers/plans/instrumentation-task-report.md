# Task 6 runtime instrumentation report

## Delivered scope

- Added `backend/observability/instrumentation.py` and `backend/tests/test_instrumentation.py`.
- Added observation wrappers to `backend/agents/agent_orchestrator.py`, retaining Task 1's per-invocation tool evidence and fallback merge behavior.
- No changes to ChatService, runtime creation, API, memory, MCP, or telemetry adapter files in this subtask. Runtime creation and chat binding are integrated by the root task.

## Integration API

```python
from observability.instrumentation import bind_usage, current_usage, instrument_runtime

instrument_runtime(runtime)  # mutates the runtime and returns it; safe to repeat
with bind_usage() as usage:  # synchronous context manager around async chat work
    result = await service_work()
    totals = usage.snapshot()
```

`current_usage()` returns the current `UsageCounter` or `None`. A new binding creates a fresh counter; nested bindings restore their parent. Asyncio child tasks inherit the same mutable request counter, while independently bound requests cannot share totals.

Snapshots contain five integer fields: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, and `model_calls`. Model calls count attempted `messages.create` invocations, including provider failures. Token counts come only from nonnegative integer values in actual response usage, supporting SDK objects and dictionaries. Missing token fields remain zero in totals and are omitted from the generation's usage details. No dollar costs are inferred.

Read `usage.snapshot()` when constructing the response. It returns a detached dictionary: online profile work finishing after the response appears in its generation trace and cannot change that returned snapshot. Evaluation that awaits profile completion before reading the snapshot includes that work's usage.

## Client and observation behavior

All distinct clients referenced by the orchestrator agent pool, composer, intent recognizer, memory, and tool manager are wrapped. A shared underlying client gets the same proxy, including repeated instrumentation calls. Attributes and lifecycle methods such as `close()` forward to the original client. `messages.create()` returns the identical response object and propagates the identical provider exception.

Generation observations record requested/provider model, generation parameters, and actual usage. They attach to the active context. Agent nodes include success/escalation/tool names; routing nodes include primary/supporting agents, confidence and reason. Orchestration and composer spans preserve structure across parallel dispatch. Escalation has an agent observation even though its deterministic handoff performs no LLM call.

Tool observations cover successful, business-unsuccessful, rejected, validation-failed, and exception-failed attempts. Existing trace fields remain intact. New `evidence_ids` are bounded, deduplicated IDs from `results[].evidence_id`, `id`, or `context_ref` (including dictionary references). New `result_summary` contains selected booleans, field-presence maps, missing/required field names, arithmetic amounts/difference, and triage/status values. Full tool data and document text are excluded.

## Verification

TDD RED, before the implementation: **9 failed, 22 passed in 36.94s**. Failures were absent instrumentation imports and missing tool/routing observations; all existing Agent regressions passed.

TDD GREEN with the required command, from `backend/`, using sandbox escalation for Windows asyncio:

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_instrumentation.py tests/test_agent_trace.py tests/test_agent_orchestrator.py -q
```

Result: **31 passed in 24.70s**. Tests exercise real Agents and orchestration with offline provider fakes, nested generation/agent/tool observations, shared-client deduplication, actual usage fields, concurrent request isolation, missing usage, provider errors, failed-tool evidence retention, deterministic escalation, and disabled telemetry compatibility.

Only the requested focused suite was run. No commit or external operation was made.
