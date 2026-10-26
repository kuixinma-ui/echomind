"""Optional, privacy-first telemetry. Importing this module never loads Langfuse.

Set ECHOMIND_TELEMETRY_ENABLED=true and configure LANGFUSE_PUBLIC_KEY /
LANGFUSE_SECRET_KEY to opt in. LANGFUSE_BASE_URL chooses the endpoint.
ECHOMIND_TRACE_CONTENT=true separately permits input/output bodies; credentials
and direct identifiers remain redacted. ECHOMIND_TELEMETRY_HASH_SALT provides
stable HMAC identifiers; otherwise an unpredictable per-process salt is used.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import hashlib
import hmac
import logging
import math
import os
import re
import secrets
from threading import Lock
from typing import Any, ContextManager, Iterator, Protocol


logger = logging.getLogger(__name__)
_REDACTED = "[REDACTED]"
_IDENTIFIERS = {"userid", "convid", "conversationid", "sessionid"}
_SECRETS = {
    "password", "passwd", "secret", "secretkey", "apikey", "token",
    "accesstoken", "refreshtoken", "authorization", "cookie", "setcookie",
    "email", "phone", "phonenumber", "mobile", "address", "idcard",
    "identitycard", "realname", "fullname", "salt", "hashsalt",
}
_CONTENT = {
    "input", "output", "message", "messages", "prompt", "completion",
    "content", "text", "query", "question", "answer", "response",
    "document", "documents", "history", "body", "expectedoutput",
}
_ERRORS = {"error", "exception", "errormessage", "exceptionmessage", "traceback", "stack", "stacktrace", "statusmessage"}
_OBSERVATION_TYPES = {"span", "agent", "tool", "chain", "retriever", "evaluator", "guardrail", "generation", "embedding"}
_UPDATE_FIELDS = {"name", "input", "output", "metadata", "version", "level", "status_message", "completion_start_time", "model", "model_parameters", "usage_details", "cost_details"}
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_CREDENTIAL = re.compile(r"(?i)(?:\bBearer\s+\S+|\b(?:sk|pk)-[A-Za-z0-9_-]+)")
_ERROR_TYPE = re.compile(r"[A-Za-z_]\w*(?:Error|Exception)$")


class Observation(Protocol):
    def update(self, **fields: Any) -> None: ...
    def score(self, name: str, value: float) -> None: ...


class Telemetry(Protocol):
    def span(self, name: str, *, kind: str = "span", input: Any = None, metadata: Any = None) -> ContextManager[Observation]: ...
    def flush(self) -> None: ...
    def shutdown(self) -> None: ...


class NoopObservation:
    def update(self, **fields: Any) -> None:
        pass

    def score(self, name: str, value: float) -> None:
        pass


_NOOP_OBSERVATION = NoopObservation()


class NoopTelemetry:
    @contextmanager
    def span(self, name: str, *, kind: str = "span", input: Any = None, metadata: Any = None) -> Iterator[Observation]:
        yield _NOOP_OBSERVATION

    def flush(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


def _diagnostic(operation: str, error: BaseException) -> None:
    # Never log exception messages, traceback, names, metadata, or credentials.
    logger.warning("Telemetry %s failed (%s)", operation, type(error).__name__)


class _SafeObservation:
    def __init__(self, observation: Any, adapter: "LangfuseTelemetry") -> None:
        self._observation = observation
        self._adapter = adapter

    def update(self, **fields: Any) -> None:
        try:
            sanitized = {}
            extra = {}
            for key, value in fields.items():
                if key in {"input", "output"}:
                    if self._adapter.trace_content:
                        sanitized[key] = self._adapter._sanitize(value, hide_content=False)
                elif key in {"usage_details", "cost_details", "model_parameters"}:
                    sanitized[key] = self._adapter._sanitize(value, hide_content=False)
                elif key == "completion_start_time":
                    # Keep this typed SDK field separate from arbitrary metadata.
                    if type(value) is datetime or value is None:
                        sanitized[key] = value
                elif key in _UPDATE_FIELDS:
                    sanitized[key] = self._adapter._sanitize(value, key=key)
                else:
                    extra[key] = self._adapter._sanitize(value, key=key)
            if extra:
                sanitized["metadata"] = {**(sanitized.get("metadata") or {}), **extra}
            if sanitized:
                self._observation.update(**sanitized)
        except Exception as error:
            _diagnostic("update", error)

    def score(self, name: str, value: float) -> None:
        try:
            # Only numeric scores cross this boundary; free text may contain PII.
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Numeric score required")
            self._observation.score(name=self._adapter._sanitize(name), value=value)
        except Exception as error:
            _diagnostic("score", error)


class LangfuseTelemetry:
    """Small v5 boundary; inject a client for offline tests or custom lifecycle."""

    def __init__(self, client: Any, *, trace_content: bool = False, hash_salt: str | None = None) -> None:
        self._client = client
        self.trace_content = trace_content
        self._salt = (hash_salt or _PROCESS_SALT).encode("utf-8")

    def _sanitize(self, value: Any, *, key: str = "", hide_content: bool | None = None, depth: int = 0) -> Any:
        """Copy JSON-shaped data, limiting depth and never calling object repr."""
        normalized = re.sub(r"[^a-z0-9]", "", key.lower())
        if isinstance(value, BaseException):
            return type(value).__name__
        if normalized in _IDENTIFIERS:
            if value is None:
                return None
            if not isinstance(value, (str, int)):
                return _REDACTED
            digest = hmac.new(self._salt, str(value).encode("utf-8"), hashlib.sha256).hexdigest()
            return "hmac-sha256:" + digest
        if normalized in _SECRETS or any(part in normalized for part in ("password", "secret", "apikey", "authorization")):
            return _REDACTED
        if normalized in _ERRORS:
            return value if isinstance(value, str) and _ERROR_TYPE.fullmatch(value) else _REDACTED
        if hide_content is None:
            hide_content = not self.trace_content
        content_parts = set(re.split(r"[^a-z0-9]+", key.lower()))
        content_variant = isinstance(value, (str, dict, list, tuple)) and (
            bool(content_parts & _CONTENT) or any(normalized.endswith(part) for part in _CONTENT)
        )
        if hide_content and (normalized in _CONTENT or content_variant):
            return _REDACTED
        if depth >= 20:
            return _REDACTED
        if isinstance(value, dict):
            return {
                child_key: self._sanitize(child_value, key=child_key, hide_content=hide_content, depth=depth + 1)
                for child_key, child_value in value.items() if isinstance(child_key, str)
            }
        if isinstance(value, (list, tuple)):
            return [self._sanitize(child, hide_content=hide_content, depth=depth + 1) for child in value]
        if isinstance(value, str):
            return _CREDENTIAL.sub(_REDACTED, _PHONE.sub(_REDACTED, _EMAIL.sub(_REDACTED, value)))
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if self.trace_content and not hide_content:
            # Only the pinned provider's content blocks are supported. Never
            # invoke arbitrary model_dump/repr methods or copy SDK extra fields.
            from anthropic.types import TextBlock, ToolUseBlock

            if type(value) is TextBlock:
                content = {"type": "text", "text": value.text}
            elif type(value) is ToolUseBlock:
                content = {"type": "tool_use", "id": value.id,
                           "name": value.name, "input": value.input}
            else:
                return _REDACTED
            return self._sanitize(content, hide_content=False, depth=depth + 1)
        return _REDACTED

    @contextmanager
    def span(self, name: str, *, kind: str = "span", input: Any = None, metadata: Any = None) -> Iterator[Observation]:
        manager = None
        observation = _NOOP_OBSERVATION
        try:
            manager = self._client.start_as_current_observation(
                name=self._sanitize(name),
                as_type=kind if kind in _OBSERVATION_TYPES else "span",
                input=self._sanitize(input, hide_content=False) if self.trace_content else None,
                metadata=self._sanitize(metadata),
            )
            raw_observation = manager.__enter__()
            observation = _SafeObservation(raw_observation, self)
        except Exception as error:
            manager = None
            _diagnostic("start", error)
        try:
            yield observation
        except BaseException as error:
            observation.update(level="ERROR", status_message=type(error).__name__, metadata={"error_type": type(error).__name__})
            raise
        finally:
            if manager is not None:
                try:
                    # SDK/OTel context managers record raw business exception
                    # messages and stacks. End normally and record only type above.
                    # Ignore any SDK suppression result: business errors propagate.
                    manager.__exit__(None, None, None)
                except Exception as error:
                    _diagnostic("exit", error)

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception as error:
            _diagnostic("flush", error)

    def shutdown(self) -> None:
        try:
            self._client.shutdown()
        except Exception as error:
            _diagnostic("shutdown", error)


_PROCESS_SALT = secrets.token_hex(32)
_NOOP = NoopTelemetry()
_BOUND: ContextVar[Telemetry | None] = ContextVar("echomind_telemetry", default=None)
_DEFAULT: Telemetry | None = None
_LOCK = Lock()


@contextmanager
def bind_telemetry(adapter: Telemetry) -> Iterator[Telemetry]:
    """Bind an adapter to this request/task; nested and asyncio contexts restore."""
    token = _BOUND.set(adapter)
    try:
        yield adapter
    finally:
        _BOUND.reset(token)


def get_telemetry() -> Telemetry:
    """Default to a no-op; initialize the optional exporter only on explicit opt-in."""
    bound = _BOUND.get()
    if bound is not None:
        return bound
    if os.getenv("ECHOMIND_TELEMETRY_ENABLED", "false").strip().lower() != "true":
        return _NOOP
    global _DEFAULT
    with _LOCK:
        if _DEFAULT is not None:
            return _DEFAULT
        public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
        secret_key = os.getenv("LANGFUSE_SECRET_KEY")
        if not public_key or not secret_key:
            logger.warning("Telemetry configuration incomplete; exporter disabled")
            _DEFAULT = _NOOP
            return _DEFAULT
        try:
            from langfuse import Langfuse

            client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                base_url=os.getenv("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com",
                debug=False,
            )
            _DEFAULT = LangfuseTelemetry(
                client,
                trace_content=os.getenv("ECHOMIND_TRACE_CONTENT", "false").strip().lower() == "true",
                hash_salt=os.getenv("ECHOMIND_TELEMETRY_HASH_SALT"),
            )
        except Exception as error:
            _diagnostic("initialization", error)
            _DEFAULT = _NOOP
        return _DEFAULT
