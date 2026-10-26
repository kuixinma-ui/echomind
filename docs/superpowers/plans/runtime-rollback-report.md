# Runtime initialization rollback repair

Date: 2026-10-26

Scope: Important finding 2 in `backend/data/review/review-findings.md`. Changes are confined to `backend/services/runtime.py`, `backend/tests/test_runtime.py`, the resource-allocation section of `MemoryManager.__init__`, and the separately authorized resource-publication order in `AgentOrchestrator.__init__`. `services/storage.py` did not need a change. Caller changes and full-suite validation are owned by the root agent.

## Cause and resulting behavior

The previous synchronous factory returned ownership only after constructing every component and importing knowledge. A constructor/import error left the caller with `runtime=None`, so it could neither remove the already-created evaluation collections nor await Redis/provider closure. `Runtime.close()` also stopped at a failing service close or memory close.

`create_runtime` is now an async ownership boundary. It retains ordinary class instances before calling `__init__`, retains factory results as soon as they exist, and unwinds all published resources on initialization failure. This also covers a knowledge constructor that creates a collection before raising, and a manager that rejects policy configuration after memory/knowledge acquisition.

Memory and knowledge construction and synchronous document import run in worker threads. Cancellation shields the worker task, waits for its result or partial instance to become owned, and then awaits rollback. Cancellation during rollback also waits for cleanup and remains a `CancelledError`.

`MemoryManager.__init__` creates the two Chroma collections before allocating Redis/provider clients. On failure it synchronously removes only successfully acquired collections under a validated unique evaluation namespace; failed deletions remain published for the async runtime to retry. Production collection names remain `episodic`, `user_profile`, and `knowledge_base` and are never removed by this rollback.

The shared orchestrator provider is attached to its composer before constructing the intent recognizer, allowing the runtime to close it if recognizer construction fails. No routing, orchestration, or agent behavior was changed by this repair.

## Public interface

All existing arguments and injected factory parameters are preserved. Calls must now await the factory:

```python
runtime = await create_runtime(config, namespace=namespace, ...)
```

After resource acquisition begins, ordinary initialization errors become `RuntimeInitializationError` with:

- `error_type`: a restricted exception class identifier, without the original exception message;
- `cleanup_status`: `rolled_back` after all cleanup succeeds, or `rollback_failed` if cleanup has errors;
- `cleanup_errors`: a tuple of restricted cleanup exception class identifiers.

The exception message is the fixed string `Runtime initialization failed`. The original exception remains the internal cause. Early namespace/API-key validation still raises `ValueError` before resources are created.

Cancellation remains `asyncio.CancelledError`, with `error_type='CancelledError'` and `cleanup_status` attached after cleanup. A caller must preserve cancellation semantics rather than convert it into a normal successful result.

`Runtime.close(cleanup=True)` still requires a unique evaluation namespace. Service shutdown, Redis cleanup, each memory collection deletion, knowledge cleanup, and client closures are attempted independently. Client identities are deduplicated. Failures are reported after the remaining work through `RuntimeCleanupError.errors`; its public message is fixed. A missing collection during rollback is treated as already removed, while storage outages remain cleanup errors.

## Regression coverage and observed RED

The old test named `cleans_up_on_error` only exercised successful construction and normal closure. It is now named `test_runtime_factory_isolates_components_and_closes_successfully` and awaits the factory.

Added coverage verifies:

- knowledge import failure after documents have been inserted;
- knowledge collection creation failure, including a not-found response while attempting rollback;
- knowledge constructor failure after the collection was created;
- invalid MCP retrieval policy after storage acquisition;
- episodic creation followed by profile collection failure, both directly and through runtime ownership;
- cleanup failure producing `rollback_failed` while other collections and clients still close;
- service, memory, and client closure failures continuing all remaining cleanup actions;
- cancellation during memory construction and knowledge import, without losing the worker result;
- cancellation arriving during rollback, without converting cancellation into an initialization error;
- recognizer constructor failure closing the provider already acquired by the orchestrator;
- preservation of production collections, another evaluation namespace, and production Redis keys.

Observed RED before implementation: synchronous factory calls could not be awaited; knowledge/policy failures escaped as unwrapped `ValueError`; partial-memory construction left its episodic collection; service-close failure prevented the other cleanup actions; synchronous construction blocked the cancellation tests. Two additional narrow RED runs caught cancellation swallowed during rollback and the unpublished orchestrator client. The missing-knowledge-collection case separately reproduced an incorrect `rollback_failed` status before its fix.

An initial test command from the repository root failed package imports because the backend packages were not on the module path. It was rerun from the backend directory; this invocation error is separate from the expected regression failures.

## Final verification

Executed with escalated shell permission from `D:\project\echomind\github-release\EchoMind\backend`:

```text
..\.venv\Scripts\python.exe -m pytest tests/test_runtime.py tests/test_context_storage.py tests/test_agent_trace.py -q
37 passed in 14.07s
```

`git diff --check` for the touched implementation/test paths exited successfully with no whitespace errors. No real provider, Redis server, Chroma server, or embedding download was invoked. Root owns the full project test run and caller integration.

## Ownership limits

Ordinary class factories, including the production components and injected subclasses, are retained before initialization. A custom callable factory with hidden allocations that raises before returning must unwind those hidden allocations itself; a caller cannot recover an object never published by that factory. Production persistent collections are preserved even if production startup fails. Cancellation waits for a synchronous worker to finish so acquired state can be reclaimed; it cannot abort the underlying synchronous client call.
