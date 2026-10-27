"""Fingerprint boundaries between application sources and local runtime artifacts."""

import pytest

from evaluation import scenarios


@pytest.fixture
def backend_tree(tmp_path, monkeypatch):
    backend = tmp_path / 'backend'
    for relative, content in {
        'evaluation/scenarios.py': '# scenario loader\n',
        'evaluation/evaluator.py': '# evaluator\n',
        'core/llm_utils.py': '# structured response policy\n',
        'agents/general.py': '# agent prompt\n',
        'skills/example/SKILL.md': '# Example skill\n',
    }.items():
        path = backend / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    monkeypatch.setattr(scenarios, '__file__', str(backend / 'evaluation/scenarios.py'))
    return backend


def fingerprints(config=None):
    return scenarios.build_fingerprints({'knowledge': []}, config or {})


def test_runtime_python_driver_lifecycle_does_not_change_code_fingerprint(backend_tree):
    before = fingerprints()['code']
    driver = backend_tree / 'data' / 'integration' / 'batch_driver.py'
    driver.parent.mkdir(parents=True)
    driver.write_text('completed_groups = []\n', encoding='utf-8')
    assert fingerprints()['code'] == before

    driver.write_text('completed_groups = [1]\n', encoding='utf-8')
    assert fingerprints()['code'] == before

    driver.unlink()
    assert fingerprints()['code'] == before


def test_application_python_inside_nested_data_directory_changes_code_fingerprint(backend_tree):
    source = backend_tree / 'core' / 'data' / 'policy.py'
    source.parent.mkdir()
    source.write_text('policy_version = 1\n', encoding='utf-8')
    before = fingerprints()['code']

    source.write_text('policy_version = 2\n', encoding='utf-8')

    assert fingerprints()['code'] != before


@pytest.mark.parametrize('relative', ['data/policy.txt', 'data/driver.py', 'docs/data/policy.csv'])
def test_skill_data_resources_change_skill_fingerprint(backend_tree, relative):
    resource = backend_tree / 'skills' / 'example' / relative
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_text('original resource\n', encoding='utf-8')
    before = fingerprints()['skills']

    resource.write_text('updated resource\n', encoding='utf-8')

    assert fingerprints()['skills'] != before


def test_explicit_skills_root_inside_runtime_data_keeps_resource_fingerprint(backend_tree):
    skills_dir = backend_tree / 'data' / 'custom_skills'
    skills_dir.mkdir(parents=True)
    resource = skills_dir / 'policy.txt'
    resource.write_text('original resource\n', encoding='utf-8')
    config = {'skills_dir': skills_dir}
    before = fingerprints(config)['skills']

    resource.write_text('updated resource\n', encoding='utf-8')

    assert fingerprints(config)['skills'] != before
