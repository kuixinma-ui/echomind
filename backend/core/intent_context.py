"""Bounded archived user originals for classification, without prose role inference."""
import json
import re


def normalize_archived_user_messages(value) -> list[dict[str, str]]:
    """Accept host-supplied provenance from MemoryManager; this is not authentication."""
    if not isinstance(value, (list, tuple)):
        return []
    result, seen = [], set()
    for item in value[:30]:
        if not isinstance(item, dict) or item.get('role') != 'user':
            continue
        content, source, evidence = (item.get(key) for key in ('content', 'source_conv', 'evidence_id'))
        if not all(isinstance(part, str) for part in (content, source, evidence)):
            continue
        if not source.strip() or len(source) > 256 or re.fullmatch(r'mem_[0-9a-f]{64}', evidence) is None:
            continue
        content = content[:500].encode('utf-8', errors='ignore').decode('utf-8')
        source = source.encode('utf-8', errors='ignore').decode('utf-8')
        if not content.strip() or not source.strip():
            continue
        key = (source, evidence, content)
        if key in seen:
            continue
        row = {'role': 'user', 'content': content, 'source_conv': source, 'evidence_id': evidence}
        if len(json.dumps([*result, row], ensure_ascii=False)) > 3000:
            continue
        result.append(row)
        seen.add(key)
        if len(result) == 5:
            break
    return result
