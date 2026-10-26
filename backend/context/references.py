from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import re


def effective_time(value):
    """Normalize ISO timestamps so offsets and date-only inputs compare correctly."""
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def version_key(value):
    return tuple((1, int(part)) if part.isdigit() else (0, part)
                 for part in re.split(r'(\d+)', str(value)))


@dataclass(frozen=True)
class ContextRef:
    id: str
    type: str = 'knowledge'
    version: str = '1'
    source: str = 'legacy_import'
    scope: str = 'public'
    effective_at: str = '1970-01-01T00:00:00Z'

    def __post_init__(self):
        for key in ('id', 'type', 'version', 'source', 'scope'):
            if not isinstance(getattr(self, key), str) or not getattr(self, key).strip():
                raise ValueError(f'ContextRef {key} must be a nonempty string')
        effective_time(self.effective_at)

    def to_dict(self):
        return asdict(self)

    def to_metadata(self):
        fields = self.to_dict()
        fields['context_id'] = fields.pop('id')
        fields['context_type'] = fields.pop('type')
        return fields

    def evidence_id(self, chunk=0):
        identity = [self.id, self.type, self.version, self.scope, chunk]
        return 'ctx_' + hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()

    @classmethod
    def from_document(cls, document):
        supplied = document.get('context_ref') or document
        if isinstance(supplied, cls):
            return supplied
        legacy_id = hashlib.sha256(json.dumps(
            [document.get('title', ''), document.get('content', '')], ensure_ascii=True).encode()).hexdigest()
        return cls(id=str(supplied.get('id') or supplied.get('context_id') or legacy_id),
                   type=str(supplied.get('type') or supplied.get('context_type') or 'knowledge'),
                   version=str(supplied.get('version') or '1'),
                   source=str(supplied.get('source') or document.get('title') or 'legacy_import'),
                   scope=str(supplied.get('scope') or 'public'),
                   effective_at=str(supplied.get('effective_at') or '1970-01-01T00:00:00Z'))
