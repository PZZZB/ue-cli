import json
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from cli_anything.unreal.utils import ue_backend as backend

@pytest.mark.parametrize('fallback', [False, True])
def test_discovery_excludes_commandlets_preserves_gui(fallback):
    lines = ['UnrealEditor-Cmd.exe F:/Test/Test.uproject -run=Cook', 'UE4Editor.exe F:/Test/Test.uproject -RUN=ResavePackages', 'UnrealEditor.exe F:/Test/Test.uproject -cookworker', 'UnrealEditor-Cmd.exe F:/Test/Test.uproject', 'UE4Editor.exe F:/Test/Test.uproject']
    rows = [{'ProcessId': i+1, 'CommandLine': line} for i,line in enumerate(lines)]
    output = '\n'.join(f'HOST,{r["CommandLine"]},{r["ProcessId"]}' for r in rows) if fallback else json.dumps(rows)
    good = SimpleNamespace(returncode=0, stdout=output)
    results = [SimpleNamespace(returncode=1,stdout=''),good] if fallback else [good]
    with patch.object(backend.sys,'platform','win32'), patch.object(backend.subprocess,'run',side_effect=results):
        assert [r['pid'] for r in backend.find_running_editors()] == [4,5]

@pytest.mark.parametrize('line', [None, '', 'UnrealEditor.exe -ExecCmds="echo -run=Cook"', 'UnrealEditor.exe -cookworkerExtra'])
def test_no_commandlet_false_positive(line):
    assert not backend._is_commandlet_process(line)
