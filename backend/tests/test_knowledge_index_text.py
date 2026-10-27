"""Title-aware new imports keep original evidence text and legacy reads intact."""
import pytest

from context import ContextRef
from tests.test_context_storage import knowledge


@pytest.mark.parametrize('title', ['助手能力范围', '', '范围\n与权限', ' "标题" '])
def test_new_chunks_index_title_but_return_original_content_and_identity(title):
    kb = knowledge()
    document = {'id': 'arbitrary-policy', 'title': title, 'content': '工具仅分析，不执行资金操作。',
                'scope': 'evaluation', 'version': '2', 'source': 'example.md'}
    kb.add_documents([document])
    stored, meta = next(iter(kb._collection.rows.values()))
    assert stored == (title + '\n' if title else '') + document['content']
    assert type(meta.get('index_text_version')) is int and meta['index_text_version'] == 1
    row = kb.search('能力', top_k=1, scope='evaluation')[0]
    ref = ContextRef.from_document(document)
    assert row['content'] == document['content']
    assert row['context_ref'] == ref.to_dict()
    assert row['evidence_id'] == ref.evidence_id(0)
    assert row['source'] == document['source']


def test_title_is_indexed_for_each_long_chunk_without_changing_chunk_boundaries():
    kb = knowledge()
    document = {'id': 'long', 'title': '操作限制', 'content': ('说明' * 140 + '。') * 3}
    chunks = kb._chunk_text(document['content'])
    assert len(chunks) > 1
    kb.add_documents([document])
    assert [text for text, _ in kb._collection.rows.values()] == ['操作限制\n' + chunk for chunk in chunks]
    rows = kb.search('限制', top_k=5)
    assert [row['content'] for row in rows] == chunks
    assert [row['evidence_id'] for row in rows] == [ContextRef.from_document(document).evidence_id(i) for i in range(len(chunks))]


@pytest.mark.parametrize('marker', [None, 0, 2, '1', True])
def test_legacy_and_unknown_markers_do_not_strip_real_text(marker):
    kb = knowledge()
    text = '限制\n这本来就是正文的一部分。'
    meta = {'title': '限制', **ContextRef(id='legacy').to_metadata()}
    if marker is not None:
        meta['index_text_version'] = marker
    kb._collection.add(ids=['legacy'], documents=[text], metadatas=[meta])
    assert kb.search('限制', top_k=1)[0]['content'] == text


def test_marked_record_with_mismatched_prefix_does_not_drop_content():
    kb = knowledge()
    meta = {'title': '原标题', 'index_text_version': 1, **ContextRef(id='changed').to_metadata()}
    kb._collection.add(ids=['changed'], documents=['正文没有标题前缀。'], metadatas=[meta])
    assert kb.search('正文', top_k=1)[0]['content'] == '正文没有标题前缀。'


def test_retrieval_fingerprint_distinguishes_new_index_format(monkeypatch):
    from evaluation.scenarios import build_fingerprints
    from mcp.knowledge_base import KnowledgeBase
    original = build_fingerprints({'knowledge': []}, {})['retrieval']
    monkeypatch.setattr(KnowledgeBase, 'INDEX_TEXT_VERSION', 2, raising=False)
    assert build_fingerprints({'knowledge': []}, {})['retrieval'] != original
