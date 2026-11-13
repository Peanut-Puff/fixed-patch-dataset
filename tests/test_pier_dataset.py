import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from bfoc import candidate_state, catalog, cli, pier_dataset
from bfoc.common import read_json, sha256, write_json
from scripts.verify_pier_dataset import validate_oracle_results
from scripts import verify_pier_dataset

REPO = Path(__file__).resolve().parents[1]


class PierDatasetTests(unittest.TestCase):
    def test_sync_is_deterministic_and_check_rejects_stale_sealing(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / 'expr-try-catch-errors'
            shutil.copytree(REPO / 'tasks' / task.name, task)
            before = {p.relative_to(task): p.read_bytes() for p in task.rglob('*') if p.is_file()}
            pier_dataset.sync(task)
            self.assertEqual(before, {p.relative_to(task): p.read_bytes() for p in task.rglob('*') if p.is_file()})
            (task / 'prompt.txt').write_text('New continuation instructions\n')
            with self.assertRaisesRegex(ValueError, 'sync-pier'):
                catalog.seal(task)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['tasks', 'sync-pier', str(task)]), 0)
            self.assertEqual((task / 'instruction.md').read_bytes(), (task / 'prompt.txt').read_bytes())
            with self.assertRaisesRegex(ValueError, 'hash changed'):
                catalog.validate_task(task)
            catalog.seal(task)
            catalog.validate_task(task)

    def test_contexts_are_allowlisted_and_do_not_leak_hidden_inputs(self):
        for task, directory in catalog.discover(REPO / 'tasks').values():
            agent_files = {p.name for p in (directory / 'environment').iterdir()}
            self.assertEqual(agent_files, {'Dockerfile', '.dockerignore', 'candidate.patch',
                                          'candidate-files.json', 'state.json', 'candidate_state.py'})
            self.assertNotIn(b'docker_image', (directory / 'task.toml').read_bytes())
            for context in ('environment', 'tests'):
                self.assertIn('FROM ' + task['image'], (directory / context / 'Dockerfile').read_text())
            self.assertEqual((directory / 'solution/historical-final.patch').read_bytes(),
                             (directory / task['positive_control']).read_bytes())
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'expr-try-catch-errors'
            shutil.copytree(REPO / 'tasks' / directory.name, directory)
            shutil.copy2(directory / 'tests/test.patch', directory / 'environment/test.patch')
            with self.assertRaisesRegex(ValueError, 'Unexpected files'):
                catalog.seal(directory)

    def test_legacy_tasks_remain_supported_and_can_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'expr-try-catch-errors'
            shutil.copytree(REPO / 'tasks' / directory.name, directory)
            task = read_json(directory / 'task.json')
            del task['pier']
            write_json(directory / 'task.json', task)
            catalog.seal(directory)
            catalog.validate_task(directory)
            pier_dataset.sync(directory)
            catalog.seal(directory)
            self.assertEqual(catalog.validate_task(directory)['pier']['schema_version'], 1)


class CandidateStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.app, self.inputs = self.root / 'app', self.root / 'inputs'
        self.app.mkdir(); self.inputs.mkdir()
        self.env_patch = patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(self.root / 'gitconfig'),
                                               'GIT_CONFIG_NOSYSTEM': '1'})
        self.env_patch.start(); self.addCleanup(self.env_patch.stop)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test'); self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'core.fileMode', 'true')
        (self.app / '.gitignore').write_text('node_modules/\n')
        (self.app / 'edit').write_text('base\n')
        (self.app / 'delete').write_text('remove this\n')
        (self.app / 'exec').write_text('#!/bin/sh\n')
        self.git('add', '.'); self.git('commit', '-qm', 'baseline')
        self.base = self.git('rev-parse', 'HEAD').decode().strip()
        before = {r['path']: r for r in candidate_state.tree(self.app)['files']}
        (self.app / 'node_modules').mkdir()
        (self.app / 'node_modules/dependency').write_text('preserve offline dependency\n')
        (self.app / 'edit').write_text('candidate\n')
        (self.app / 'delete').unlink()
        (self.app / 'new').write_text('candidate only\n')
        (self.app / 'exec').chmod(0o755)
        self.git('add', '-A')
        patch_bytes = self.git('diff', '--cached', '--binary', self.base)
        (self.inputs / 'candidate.patch').write_bytes(patch_bytes)
        after_tree = candidate_state.tree(self.app)
        after = {r['path']: r for r in after_tree['files']}
        rows = [{'path': name, 'base_sha256': before.get(name, {}).get('sha256'),
                 'sha256': after.get(name, {}).get('sha256')}
                for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)]
        write_json(self.inputs / 'candidate-files.json', {'files': rows})
        self.spec = {'base_commit': self.base, 'expected_initial_tree_sha256': after_tree['sha256'],
                     'candidate_patch_sha256': hashlib.sha256(patch_bytes).hexdigest()}
        self.git('reset', '--hard', self.base)
        (self.app / 'edit').write_text('positive control\n')
        (self.inputs / 'historical-final.patch').write_bytes(self.git('diff', '--binary', self.base))
        self.spec['positive_control_sha256'] = sha256(self.inputs / 'historical-final.patch')
        self.git('reset', '--hard', self.base)
        write_json(self.inputs / 'state.json', self.spec)

    def git(self, *args):
        return candidate_state.git(self.app, *args)

    def test_build_helper_loads_exact_uncommitted_tree(self):
        helper = REPO / 'tasks/expr-try-catch-errors/environment/candidate_state.py'
        subprocess.run([os.sys.executable, str(helper), 'init', str(self.app), str(self.inputs)],
                       check=True, capture_output=True)
        self.assertEqual(candidate_state.tree(self.app)['sha256'], self.spec['expected_initial_tree_sha256'])
        self.assertEqual(self.git('rev-parse', 'HEAD').decode().strip(), self.base)
        self.assertEqual(self.git('diff', '--cached'), b'')
        self.assertEqual((self.app / 'new').read_text(), 'candidate only\n')
        self.assertFalse((self.app / 'delete').exists())

    def test_wrong_baseline_and_dirty_image_fail_before_apply(self):
        with self.assertRaisesRegex(RuntimeError, 'baseline commit'):
            candidate_state.initialize({**self.spec, 'base_commit': 'f' * 40}, self.app, self.inputs)
        (self.app / 'extra').write_text('unexpected')
        with self.assertRaisesRegex(RuntimeError, 'pre-existing'):
            candidate_state.initialize(self.spec, self.app, self.inputs)

    def test_bad_preimage_postimage_and_full_tree_fail(self):
        original = read_json(self.inputs / 'candidate-files.json')
        for field, message in [('base_sha256', 'preimage'), ('sha256', 'postimage')]:
            with self.subTest(field=field):
                altered = copy.deepcopy(original)
                altered['files'][0][field] = '0' * 64
                write_json(self.inputs / 'candidate-files.json', altered)
                with self.assertRaisesRegex(RuntimeError, message):
                    candidate_state.initialize(self.spec, self.app, self.inputs)
                self.git('reset', '--hard', self.base)
                (self.app / 'new').unlink(missing_ok=True)
        write_json(self.inputs / 'candidate-files.json', original)
        with self.assertRaisesRegex(RuntimeError, 'tree mismatch'):
            candidate_state.initialize({**self.spec, 'expected_initial_tree_sha256': '0' * 64}, self.app, self.inputs)

    def test_hash_and_symlink_rejection(self):
        with self.assertRaisesRegex(RuntimeError, 'patch hash'):
            candidate_state.initialize({**self.spec, 'candidate_patch_sha256': '0' * 64}, self.app, self.inputs)
        (self.app / 'outside').symlink_to(self.inputs, target_is_directory=True)
        for path in ['../outside', 'outside/state.json', '/tmp/absolute']:
            with self.assertRaises(RuntimeError):
                candidate_state.candidate_path(self.app, path)

    def test_committed_delivery_excludes_uncommitted_fixes(self):
        candidate_state.initialize(self.spec, self.app, self.inputs)
        output = self.root / 'artifacts/model.patch'
        command = pier_dataset.collect_command(self.base, str(self.app), str(output))
        subprocess.run(['bash', '-c', command], check=True)
        self.assertEqual(output.read_bytes(), b'')
        self.git('add', '-A'); self.git('commit', '-qm', 'commit candidate')
        committed = self.git('diff', '--binary', self.base, 'HEAD')
        (self.app / 'edit').write_text('uncommitted correction\n')
        (self.app / 'extra').write_text('not submitted\n')
        subprocess.run(['bash', '-c', command], check=True)
        self.assertEqual(output.read_bytes(), committed)
        self.assertNotIn(b'uncommitted correction', output.read_bytes())

    def test_generated_oracle_replaces_c0_and_preserves_dependencies(self):
        candidate_state.initialize(self.spec, self.app, self.inputs)
        helper = REPO / 'tasks/expr-try-catch-errors/solution/candidate_state.py'
        subprocess.run([os.sys.executable, str(helper), 'oracle', str(self.app), str(self.inputs)],
                       check=True, capture_output=True)
        self.assertEqual(self.git('diff', '--binary', self.base, 'HEAD'),
                         (self.inputs / 'historical-final.patch').read_bytes())
        self.assertFalse((self.app / 'new').exists())
        self.assertTrue((self.app / 'delete').exists())
        self.assertTrue((self.app / 'node_modules/dependency').exists())
        self.assertEqual(self.git('status', '--porcelain'), b'')

    def test_oracle_refuses_wrong_positive_hash_without_resetting(self):
        candidate_state.initialize(self.spec, self.app, self.inputs)
        with self.assertRaisesRegex(RuntimeError, 'Oracle patch hash'):
            candidate_state.solve({**self.spec, 'positive_control_sha256': '0' * 64}, self.app, self.inputs)
        self.assertEqual(candidate_state.tree(self.app)['sha256'], self.spec['expected_initial_tree_sha256'])


class OracleEvidenceTests(unittest.TestCase):
    def test_validation_command_runs_controls_and_checks_all_oracle_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'acceptance'
            cases = catalog.discover(REPO / 'tasks')
            calls = []

            def checked_call(argv):
                if argv[0] == 'docker':
                    return subprocess.CompletedProcess(argv, 0,
                        stdout=json.dumps({'OSType': 'linux', 'Architecture': 'amd64'}).encode())
                self.assertEqual(argv, ['pier', '--version'])
                return subprocess.CompletedProcess(argv, 0, stdout=b'0.3.1\n')

            def verify(runner, item, label, source):
                calls.append((item['task']['id'], label))
                field = {'baseline': 'baseline_reward', 'historical-final': 'positive_reward',
                         'C0': 'candidate_reward'}[label]
                return {'reward': {'reward': item['task']['expectations'][field]}}

            def run(argv, **kwargs):
                if argv[:2] == ['pier', 'run']:
                    for case_id in cases:
                        write_json(output / 'pier/oracle' / (case_id + '__trial') / 'result.json', {
                            'task_name': 'fixed-patch/' + case_id,
                            'finished_at': '2026-11-13T00:00:00Z',
                            'agent_info': {'name': 'oracle'},
                            'verifier_result': {'rewards': {'reward': 1}},
                        })
                return subprocess.CompletedProcess(argv, 0)

            with patch.object(os.sys, 'argv', ['verify_pier_dataset.py', '--output', str(output)]), \
                 patch.object(verify_pier_dataset.platform, 'system', return_value='Linux'), \
                 patch.object(verify_pier_dataset.platform, 'machine', return_value='x86_64'), \
                 patch.object(verify_pier_dataset.os, 'cpu_count', return_value=4), \
                 patch.object(verify_pier_dataset, 'call', side_effect=checked_call), \
                 patch.object(verify_pier_dataset.subprocess, 'run', side_effect=run), \
                 patch.object(verify_pier_dataset.Runner, 'check_resources'), \
                 patch.object(verify_pier_dataset.Runner, 'verify', verify), \
                 contextlib.redirect_stdout(io.StringIO()):
                verify_pier_dataset.main()
            self.assertEqual(len(calls), 15)
            self.assertEqual(read_json(output / 'status.json')['batch'], 'acceptance')
            summary = read_json(output / 'summary.json')
            self.assertEqual(summary['status'], 'passed')
            self.assertEqual(set(summary['oracle']), set(cases))
            self.assertEqual(summary['controls']['scriggo-method-declarations']['candidate_reward'], 1)

    def test_incomplete_errored_or_failed_script_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = root / 'case__trial'
            case.mkdir()
            result = {'task_name': 'fixed-patch/case', 'finished_at': '2026-11-13T00:00:00Z',
                      'agent_info': {'name': 'oracle'}, 'exception_info': None,
                      'verifier_result': {'rewards': {'reward': 1}}}
            write_json(case / 'result.json', result)
            self.assertEqual(set(validate_oracle_results(root, {'case'})), {'case'})
            with self.assertRaisesRegex(RuntimeError, 'Missing Oracle'):
                validate_oracle_results(root, {'case', 'missing'})
            for change in ({'finished_at': None}, {'exception_info': {'exception_type': 'TestError'}},
                           {'verifier_result': None}, {'verifier_result': {'rewards': {'reward': 0}}}):
                write_json(case / 'result.json', {**result, **change})
                with self.assertRaisesRegex(RuntimeError, 'did not pass'):
                    validate_oracle_results(root, {'case'})
            write_json(case / 'result.json', result)
            (case / 'agent').mkdir()
            (case / 'agent/exit-code.txt').write_text('1\n')
            with self.assertRaisesRegex(RuntimeError, 'script failed'):
                validate_oracle_results(root, {'case'})


if __name__ == '__main__':
    unittest.main()
