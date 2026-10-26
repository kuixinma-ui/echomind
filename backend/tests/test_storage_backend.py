"""Evaluation must not hide remote storage failures behind local fallback."""
import sys
from types import SimpleNamespace

import pytest

from services.storage import create_chroma_client


def chroma_stub(monkeypatch):
    persistent_calls = []
    def http(**kwargs):
        raise ConnectionError('unavailable')
    def persistent(**kwargs):
        persistent_calls.append(kwargs)
        return 'local-client'
    monkeypatch.setitem(sys.modules, 'chromadb', SimpleNamespace(
        Settings=lambda **kwargs: kwargs, HttpClient=http, PersistentClient=persistent))
    return persistent_calls


def test_explicit_http_mode_propagates_failure_without_local_fallback(monkeypatch):
    local = chroma_stub(monkeypatch)
    with pytest.raises(ConnectionError):
        create_chroma_client('unreachable', 8000, 'unused', mode='http')
    assert local == []


def test_explicit_local_mode_never_connects_to_http(monkeypatch):
    local = chroma_stub(monkeypatch)
    assert create_chroma_client('unreachable', 8000, 'isolated', mode='local') == 'local-client'
    assert local[0]['path'] == 'isolated'


def test_legacy_auto_fallback_remains_explicit_and_logs_only_safe_text(monkeypatch, caplog):
    local = chroma_stub(monkeypatch)
    assert create_chroma_client('secret-host', 8000, 'isolated', mode='auto') == 'local-client'
    assert len(local) == 1
    assert 'fallback' in caplog.text.lower()
    assert 'secret-host' not in caplog.text


def test_unknown_mode_is_rejected_before_import_or_connection(monkeypatch):
    local = chroma_stub(monkeypatch)
    with pytest.raises(ValueError):
        create_chroma_client('localhost', 8000, 'unused', mode='typo')
    assert local == []
