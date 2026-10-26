"""Storage naming and lazy client creation shared by isolated runtimes."""
import hashlib
import json
import logging
import re


def validate_namespace(namespace):
    if namespace and not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', namespace):
        raise ValueError('Invalid storage namespace')
    return namespace


def require_evaluation_namespace(namespace):
    if not re.fullmatch(r'eval_[a-f0-9]{32}', namespace):
        raise ValueError('Cleanup requires a unique evaluation namespace')


def collection_name(namespace, base):
    validate_namespace(namespace)
    return f'{namespace}_{base}' if namespace else base


def memory_key(namespace, kind, user_id, conv_id):
    if not namespace:
        return f'{kind}:{user_id}:{conv_id}'
    identity = hashlib.sha256(json.dumps([user_id, conv_id], ensure_ascii=True).encode()).hexdigest()
    return f'{namespace}:{kind}:{identity}'


def create_chroma_client(host, port, path, *, mode='auto'):
    if mode not in {'auto', 'http', 'local'}:
        raise ValueError('Chroma mode must be auto, http or local')
    import chromadb
    settings = chromadb.Settings(anonymized_telemetry=False)
    if mode == 'local':
        return chromadb.PersistentClient(path=path, settings=settings)
    try:
        client = chromadb.HttpClient(host=host, port=port, settings=settings)
        client.heartbeat()
        return client
    except Exception:
        if mode == 'http':
            raise
        logging.getLogger(__name__).warning('Chroma HTTP unavailable; explicit auto mode uses persistent fallback')
        return chromadb.PersistentClient(path=path, settings=settings)
