"""Optional real Chroma 0.5.23 local persistence test, with explicit test vectors.

No model or HTTP server is involved. The core CI suite may skip this file when
Chroma is absent; the integration environment executes it with the real SDK.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest


def test_real_chroma_persists_between_processes_and_scopes_deletion(tmp_path):
    chromadb = pytest.importorskip('chromadb')
    assert chromadb.__version__ == '0.5.23'
    path = tmp_path / 'owned-chroma'
    name = 'eval_' + uuid.uuid4().hex + '_integration'
    sentinel = 'eval_' + uuid.uuid4().hex + '_sentinel'
    # Both subprocesses use explicit vectors; no embedding model/download.
    writer = '''
import chromadb, sys
client = chromadb.PersistentClient(path=sys.argv[1], settings=chromadb.Settings(anonymized_telemetry=False))
for name in sys.argv[2:]:
    col = client.create_collection(name=name)
    col.add(ids=['row'], embeddings=[[1.0, 0.0, 0.0]], documents=['synthetic'],
            metadatas=[{'user_id':'u1','conv_id':'c1','version':'1','scope':'public'}])
print('WRITE_OK')
'''
    reader = '''
import chromadb, json, sys
client = chromadb.PersistentClient(path=sys.argv[1], settings=chromadb.Settings(anonymized_telemetry=False))
col = client.get_collection(sys.argv[2])
result = col.query(query_embeddings=[[1.0,0.0,0.0]], n_results=1,
                   where={'$and':[{'user_id':'u1'},{'conv_id':'c1'}]})
assert result['ids'] == [['row']]
assert col.query(query_embeddings=[[1.0,0.0,0.0]], n_results=1, where={'user_id':'u2'})['ids'] == [[]]
client.delete_collection(sys.argv[2])
assert client.get_collection(sys.argv[3]).count() == 1
client.delete_collection(sys.argv[3])
assert client.list_collections() == []
print(json.dumps({'persisted':True,'filtered_query':True,'user_isolation':True,'owned_cleanup':True}))
'''
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    arguments = [str(path), name, sentinel]
    first = subprocess.run([sys.executable, '-c', writer, *arguments], capture_output=True,
                           text=True, timeout=45, creationflags=flags)
    assert first.returncode == 0, first.stderr
    assert 'Failed to send telemetry event' not in first.stderr
    assert 'WRITE_OK' in first.stdout
    second = subprocess.run([sys.executable, '-c', reader, *arguments], capture_output=True,
                            text=True, timeout=45, creationflags=flags)
    assert second.returncode == 0, second.stderr
    assert 'Failed to send telemetry event' not in second.stderr
    assert all(json.loads(second.stdout.strip().splitlines()[-1]).values())
