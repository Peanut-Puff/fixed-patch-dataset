#!/usr/bin/env python3
"""Offline parser/materialization check. Requires AEO libraries and pinned Pier."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bfoc.catalog import discover
from bfoc.common import sha256
from bfoc.pier_dataset import PIER_REVISION


def main():
    if not __debug__:
        raise RuntimeError('Run compatibility checks without Python -O/PYTHONOPTIMIZE')
    from aeo_assets.upstream import FilesystemDatasetAdapter, UpstreamDatasetSpec
    from pier.models.job.config import JobConfig
    from pier.models.task.task import Task
    from pier.models.task.verifier_mode import resolve_effective_verifier_env_config
    from scripts.verify_pier_dataset import oracle_config

    if importlib.metadata.version('datacurve-pier') != '0.3.1':
        raise RuntimeError('Install Pier 0.3.1 from commit ' + PIER_REVISION)
    selected = discover(REPO / 'tasks')
    for task, directory in selected.values():
        native = Task(directory)
        config = native.config
        verifier = resolve_effective_verifier_env_config(config, None)
        assert native.paths.is_valid()
        assert native.instruction == (directory / task['prompt']).read_text()
        assert native.paths.discovered_solve_path.is_file()
        assert config.environment.docker_image is None
        assert config.environment.workdir == '/app'
        assert not config.environment.allow_internet
        assert verifier is not None and not verifier.allow_internet
        assert verifier.docker_image is None
        assert config.agent.timeout_sec == 10800 and config.verifier.timeout_sec == 1800
        assert len(config.verifier.collect) == 1
        assert ' HEAD > ' in config.verifier.collect[0].command
        for environment in (config.environment, verifier):
            assert (environment.cpus, environment.memory_mb, environment.storage_mb) == (2, 8192, 20480)
    # Use a clean source snapshot: the edited worktree is not its HEAD commit.
    # Never hash .local/, credentials, runtime outputs or the virtualenv as source.
    with tempfile.TemporaryDirectory(prefix='fixed-patch-compat-') as temporary:
        temporary = Path(temporary)
        job = JobConfig.model_validate(oracle_config(temporary))
        assert job.agents[0].name == 'oracle' and job.n_concurrent_trials == 1
        assert job.retry.max_retries == 0
        root = temporary / 'source'; root.mkdir()
        paths = subprocess.check_output(
            ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=REPO)
        for relative in sorted(set(paths.split(b'\0')) - {b''}):
            source = REPO / relative.decode()
            if source.is_symlink():
                raise RuntimeError('AEO source archives cannot contain symlinks: ' + str(source))
            if source.is_file():
                target = root / source.relative_to(REPO)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
        spec = UpstreamDatasetSpec(
            adapter_key='pier', adapter_version='1.0.0', dataset_slug='fixed-patch-dataset',
            display_name='Fixed Patch Dataset', summary='', publisher='Peanut-Puff', engine='pier',
            source_url='https://github.com/Peanut-Puff/fixed-patch-dataset', dataset_profile='pier',
            tasks_path='tasks', expected_case_count=len(selected),
        )
        adapter = FilesystemDatasetAdapter(spec, root, revision)
        first, second = adapter.materialize(), adapter.materialize()
        assert first.manifest == second.manifest
        assert {case.case_id for case in first.cases} == set(selected)
        assert first.manifest.engine == 'pier' and first.manifest.tasks_path == 'tasks'
        for case in first.cases:
            task, directory = selected[case.case_id]
            assert case.instructions == (directory / task['prompt']).read_bytes()
            assert case.workspace_capture.roots == ('/app',)
            assert not any(item.path.startswith('solution/') for item in case.files)
            resources = case.resource_requirements
            for environment in (resources.execution_environment, resources.verifier_environment):
                assert environment is not None
                assert (environment.cpu_millis, environment.memory_bytes, environment.storage_bytes) == (
                    2000, 8192 * 1024**2, 20480 * 1024**2)
        archive = temporary / 'source.tar.gz'
        with tarfile.open(archive, 'w:gz') as stream:
            stream.add(root, arcname='source')
        with tarfile.open(archive) as stream:
            for task, directory in selected.values():
                for relative in ('solution/solve.sh', 'solution/historical-final.patch'):
                    file = stream.extractfile('source/tasks/' + task['id'] + '/' + relative)
                    assert file is not None
                    assert hashlib.sha256(file.read()).hexdigest() == sha256(directory / relative)
        print(json.dumps({
            'status': 'passed', 'case_count': len(first.cases), 'pier_version': '0.3.1',
            'pier_source_revision': PIER_REVISION, 'aeo_adapter': 'pier',
            'working_tree_snapshot': True, 'parent_commit': revision,
            'content_sha256': first.manifest.content_sha256,
            'source_sha256': first.manifest.source_sha256,
            'docker_verification': 'not_run',
        }, indent=2))


if __name__ == '__main__':
    main()
