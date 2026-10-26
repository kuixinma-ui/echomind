"""Append-only experiment reports with explicit baseline promotion."""
from __future__ import annotations

import copy
import json
import math
import os
import re
from pathlib import Path

from evaluation.scenarios import FINGERPRINT_FIELDS

DEFAULT_REPORT_DIR = Path(os.getenv('ECHOMIND_EVAL_REPORT_DIR', str(Path(__file__).resolve().parents[1] / 'data' / 'eval' / 'scenarios')))


class ReportStore:
    def __init__(self, report_dir=DEFAULT_REPORT_DIR):
        self.report_dir = Path(report_dir)

    @staticmethod
    def validate_run_id(run_id):
        if not isinstance(run_id, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', run_id) is None:
            raise ValueError('run_id must contain only letters, digits, hyphens or underscores')
        reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
        if run_id.upper() in reserved or run_id.lower() == 'baseline':
            raise ValueError('run_id cannot be a reserved Windows filename')
        return run_id

    def _path(self, run_id):
        return self.report_dir / f'{self.validate_run_id(run_id)}.json'

    def save(self, report):
        path = self._path(report['run_id'])
        payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as output:
            output.write(payload)
        return path

    def load(self, run_id):
        return json.loads(self._path(run_id).read_text(encoding='utf-8'))

    def list(self):
        if not self.report_dir.exists():
            return []
        reports = []
        for path in sorted(self.report_dir.glob('*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True):
            if path.name == 'baseline.json':
                continue
            try:
                report = self.load(path.stem)
                reports.append({name: copy.deepcopy(report.get(name)) for name in
                                ('run_id', 'timestamp', 'dataset', 'mode', 'status', 'metadata',
                                 'total', 'passed', 'pass_rate', 'invalid', 'timeout', 'error', 'hard_fail', 'summary')})
            except (ValueError, OSError):
                continue
        return reports

    def promote_baseline(self, run_id):
        report = self.load(run_id)
        if report.get('budget', {}).get('stop_reason'):
            raise ValueError('Cannot promote a report interrupted by its evaluation budget')
        target = self.report_dir / 'baseline.json'
        temporary = self.report_dir / 'baseline.pending'
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        temporary.replace(target)
        return report

    def baseline(self):
        path = self.report_dir / 'baseline.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def compare(self, left_run_id, right_run_id, *, vary=()):
        allowed = {'model', 'prompt', 'skills', 'code', 'retrieval'}
        if not isinstance(vary, (list, tuple, set)) or any(name not in allowed for name in vary):
            raise ValueError('vary may contain only model, prompt, skills, code or retrieval')
        declared = set(vary)
        left, right = self.load(left_run_id), self.load(right_run_id)
        result = {'left_run_id': left_run_id, 'right_run_id': right_run_id,
                  'comparable': False, 'reason': '', 'deltas': {}, 'paired': [],
                  'declared_variants': sorted(declared), 'changed_factors': [],
                  'controlled_factors': [n for n in FINGERPRINT_FIELDS if n not in declared]}
        a = left.get('metadata', {}).get('fingerprints', {})
        b = right.get('metadata', {}).get('fingerprints', {})
        missing = [name for name in FINGERPRINT_FIELDS if not a.get(name) or not b.get(name)]
        changed = [name for name in FINGERPRINT_FIELDS if a.get(name) != b.get(name)]
        result['changed_factors'] = changed
        uncontrolled = [name for name in changed if name not in declared]
        left_cases, right_cases = left.get('cases', []), right.get('cases', [])
        left_ids = [c.get('id') for c in left_cases]
        right_ids = [c.get('id') for c in right_cases]
        if any(report.get('budget', {}).get('stop_reason') for report in (left, right)):
            result['reason'] = 'Evaluation budget interrupted one or both runs'
        elif missing:
            result['reason'] = 'Missing snapshot fingerprints: ' + ', '.join(missing)
        elif uncontrolled:
            result['reason'] = 'Different undeclared snapshot fingerprints: ' + ', '.join(uncontrolled)
        elif left.get('mode') != right.get('mode'):
            result['reason'] = 'Different execution modes'
        elif left.get('status') == 'validated' or right.get('status') == 'validated':
            result['reason'] = 'Validation reports contain no execution metrics'
        elif not left.get('total') or not right.get('total'):
            result['reason'] = 'No attempted cases'
        elif (len(set(left_ids)) != len(left_ids) or len(set(right_ids)) != len(right_ids)
              or None in left_ids or None in right_ids or set(left_ids) != set(right_ids)):
            result['reason'] = 'Case ids differ or are missing; paired comparison unavailable'
        else:
            result.update(comparable=True, reason='Same control snapshots; explicitly declared experimental variants only')
            for name in ('pass_rate', 'invalid', 'timeout', 'error', 'hard_fail'):
                av, bv = left.get(name), right.get(name)
                if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (av, bv)):
                    result['deltas'][name] = bv - av
            right_by_id = {case['id']: case for case in right_cases}
            for before in left_cases:
                after = right_by_id[before['id']]
                item = {'id': before['id'], 'left_status': before.get('status'), 'right_status': after.get('status'),
                        'latency_delta_ms': self._case_latency(after) - self._case_latency(before),
                        'assertions': []}
                after_rules = {r['id']: r for r in after.get('assertions', [])}
                for rule in before.get('assertions', []):
                    current = after_rules.get(rule['id'])
                    if current:
                        item['assertions'].append({'id': rule['id'], 'left_status': rule.get('status'),
                                                   'right_status': current.get('status')})
                result['paired'].append(item)
        return result

    @staticmethod
    def _case_latency(case):
        return sum(t.get('latency_ms', 0) for t in case.get('turns', [])
                   if isinstance(t.get('latency_ms', 0), (int, float))
                   and not isinstance(t.get('latency_ms'), bool) and math.isfinite(t.get('latency_ms', 0)))
