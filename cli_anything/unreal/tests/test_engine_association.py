"""Engine selection must not guess across a declared association."""
import json

import pytest
from click.testing import CliRunner

from cli_anything.unreal.utils import ue_backend
from cli_anything.unreal.unreal_cli import cli


@pytest.mark.parametrize('association', ['{UNREGISTERED-GUID}', '99.9', 'Z:/missing/engine'])
@pytest.mark.parametrize('override', [False, True])
def test_unresolved_association_requires_explicit_engine(tmp_path, monkeypatch, association, override):
    project = tmp_path / 'Test.uproject'
    project.write_text(json.dumps({'EngineAssociation': association}), encoding='utf-8')
    engine = tmp_path / 'EngineRoot'
    (engine / 'Engine' / 'Build').mkdir(parents=True)
    monkeypatch.setattr(ue_backend, '_find_engine_by_association', lambda assoc: None)
    def unexpected_registry():
        pytest.fail('unrelated registry fallback must not run')
    monkeypatch.setattr(ue_backend, '_find_engine_from_registry', unexpected_registry)
    monkeypatch.delenv('UE_ENGINE_ROOT', raising=False)
    if override:
        monkeypatch.setenv('UE_ENGINE_ROOT', str(engine))
    assert ue_backend.find_engine_root(str(project)) == (str(engine) if override else None)


@pytest.mark.parametrize('association', ['', None])
def test_no_association_retains_discovery(tmp_path, monkeypatch, association):
    project = tmp_path / 'Test.uproject'
    project.write_text(json.dumps({'EngineAssociation': association}), encoding='utf-8')
    monkeypatch.delenv('UE_ENGINE_ROOT', raising=False)
    monkeypatch.setattr(ue_backend, '_find_engine_from_registry', lambda: 'discovered-engine')
    assert ue_backend.find_engine_root(str(project)) == 'discovered-engine'


@pytest.mark.parametrize('args', [
    ['compile'], ['compile', '--no-wait'], ['cook'], ['package'],
])
def test_build_rejects_unresolved_engine_before_submission(tmp_path, monkeypatch, args):
    from cli_anything.unreal.commands import build
    project = tmp_path / 'Test.uproject'
    project.write_text(json.dumps({'EngineAssociation': '{UNREGISTERED-GUID}'}), encoding='utf-8')
    monkeypatch.delenv('UE_ENGINE_ROOT', raising=False)
    monkeypatch.setattr(ue_backend, '_find_engine_by_association', lambda assoc: None)
    monkeypatch.setattr(ue_backend, '_find_engine_from_registry', lambda: 'wrong-engine')
    monkeypatch.setattr(build, 'find_running_editors', lambda: [])
    def unexpected_submit(*args, **kwargs):
        pytest.fail('unresolved engine must not submit a build')
    monkeypatch.setattr(build, 'submit_task', unexpected_submit)
    result = CliRunner().invoke(cli, ['--output', 'json', '--project', str(project), 'build', *args])
    assert result.exit_code == 3, result.output
    data = json.loads(result.output)
    assert data['code'] == 'ENGINE_NOT_FOUND'
    assert 'UE_ENGINE_ROOT' in data['suggestion']
    assert not (tmp_path / 'Saved' / 'Logs').exists()
