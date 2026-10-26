"""Generate local integration secrets from the example, preserving existing files.

Uses only the standard library. No network, model calls, or account creation.
"""
import json
from pathlib import Path
import secrets
from uuid import uuid4


def prepare(template: Path, target: Path):
    if target.exists():
        return {'status': 'existing_preserved', 'file': str(target)}
    names = {
        'INTEGRATION_REDIS_PASSWORD', 'INTEGRATION_POSTGRES_PASSWORD',
        'INTEGRATION_CLICKHOUSE_PASSWORD', 'INTEGRATION_LANGFUSE_REDIS_PASSWORD',
        'INTEGRATION_MINIO_PASSWORD', 'INTEGRATION_LANGFUSE_SALT',
        'INTEGRATION_LANGFUSE_ENCRYPTION_KEY', 'INTEGRATION_LANGFUSE_NEXTAUTH_SECRET',
        'ECHOMIND_TELEMETRY_HASH_SALT',
    }
    values = {name: secrets.token_hex(32) for name in names}
    values.update(LANGFUSE_INIT_ORG_ID='echomind-org-' + uuid4().hex,
                  LANGFUSE_INIT_PROJECT_ID='echomind-eval-' + uuid4().hex,
                  LANGFUSE_PUBLIC_KEY='pk-lf-' + str(uuid4()),
                  LANGFUSE_SECRET_KEY='sk-lf-' + secrets.token_hex(32))
    values['REDIS_URL'] = 'redis://:' + values['INTEGRATION_REDIS_PASSWORD'] + '@127.0.0.1:16379/0'
    lines, seen = [], set()
    for line in template.read_text(encoding='utf-8').splitlines():
        name, separator, _ = line.partition('=')
        if separator and name in values:
            line = name + '=' + values[name]
            seen.add(name)
        lines.append(line)
    lines.extend(name + '=' + value for name, value in values.items() if name not in seen)
    try:
        with target.open('x', encoding='utf-8', newline='\n') as output:
            output.write('\n'.join(lines) + '\n')
    except FileExistsError:
        return {'status': 'existing_preserved', 'file': str(target)}
    return {'status': 'created', 'file': str(target), 'model_credentials_included': False,
            'secrets_printed': False}


if __name__ == '__main__':
    backend = Path(__file__).resolve().parents[1]
    print(json.dumps(prepare(backend / '.env.integration.example', backend / '.env.integration')))
