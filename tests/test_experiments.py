import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
from bfoc import adapters, binaries, catalog, cli
from bfoc.common import read_json, source_hash, write_json
from bfoc.runner import Runner, create_plan, prepare, matrix, native_outcome

REPO = Path(__file__).resolve().parents[1]
RESOURCES = {'concurrency': 3, 'cpu': 2, 'memory_mib': 8192, 'agent_timeout_seconds': 10800,
             'verifier_timeout_seconds': 1800, 'minimum_free_disk_gib': 40}


def elf(path, payload=b'test'):
    header = bytearray(64)
    header[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<H', header, 18, 62)
    path.write_bytes(header + payload)
    return path


class TaskTests(unittest.TestCase):
    def test_all_task_inputs_and_positive_control_restore(self):
        tasks = catalog.discover(REPO / 'tasks')
        self.assertEqual(len(tasks), 5)
        self.assertEqual(hashlib.sha256((REPO / 'tasks/koota-pair-relation-tracking/controls/historical-final.patch').read_bytes()).hexdigest(),
                         'a0914bff16d572c5bcaddb10900f8bf362e4fc56df51439231207b21619517ab')

    def test_extension_is_discovered_and_tamper_fails(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / 'new-task'
            shutil.copytree(REPO / 'tasks/koota-pair-relation-tracking', task)
            spec = read_json(task / 'task.json'); spec['id'] = 'new-task'
            write_json(task / 'task.json', spec)
            from bfoc.pier_dataset import sync
            sync(task)
            catalog.seal(task)
            self.assertEqual(list(catalog.discover(root)), ['new-task'])
            (task / 'prompt.txt').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'hash changed'):
                catalog.discover(root)

    def test_selection_and_path_validation(self):
        with self.assertRaisesRegex(ValueError, 'Unknown tasks'):
            catalog.select(REPO / 'tasks', ['missing'])
        with self.assertRaisesRegex(ValueError, 'unique'):
            catalog.select(REPO / 'tasks', ['expr-try-catch-errors'] * 2)
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / 'expr-try-catch-errors'
            shutil.copytree(REPO / 'tasks/expr-try-catch-errors', task)
            spec = read_json(task / 'task.json'); spec['prompt'] = '../outside'
            write_json(task / 'task.json', spec)
            with self.assertRaisesRegex(ValueError, 'inside its task'):
                catalog.validate_task(task, hashes=False)


class SnapshotTests(unittest.TestCase):
    def test_per_run_binaries_and_source_independence(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); source = elf(tmp / 'bf', b'one')
            first = binaries.snapshot(source, tmp / 'one', 'BF')
            elf(source, b'two')
            second = binaries.snapshot(source, tmp / 'two', 'BF')
            self.assertNotEqual(first['sha256'], second['sha256'])
            self.assertEqual(binaries.verify(tmp / 'one'), first)
            target = tmp / 'one/bitfun'; target.chmod(0o755); target.write_bytes(b'changed')
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                binaries.verify(tmp / 'one')

    def test_plan_and_prepare_are_self_contained(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); source = elf(tmp / 'oc')
            selected = catalog.select(REPO / 'tasks', ['expr-try-catch-errors'])
            plan = create_plan(REPO, selected, ['OC'], {'OC': source}, read_json(REPO / 'configs/model.json'), tmp / 'run', RESOURCES)
            prepare(plan)
            self.assertTrue((tmp / 'run/binaries/OC/opencode').exists())
            self.assertTrue((tmp / 'run/harness/experiment.py').exists())
            self.assertEqual((tmp / 'run/.gitignore').read_text(), '*\n')
            self.assertEqual(source_hash(tmp / 'run/harness/bfoc'), plan['source_hash'])
            self.assertNotEqual(plan['tasks'][0]['directory'], str(REPO / 'tasks/expr-try-catch-errors'))
            with self.assertRaises(FileExistsError):
                prepare(plan)
            with self.assertRaisesRegex(ValueError, 'each selected agent'):
                create_plan(REPO, selected, ['BF', 'OC'], {'BF': source}, {}, tmp / 'bad', RESOURCES)

    def test_reject_macos_and_source_output_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'bad'; p.write_bytes(b'not-ELF')
            with self.assertRaisesRegex(ValueError, 'Linux x86_64'):
                binaries.fingerprint(p)
            elf(p)
            with self.assertRaisesRegex(ValueError, 'overlaps'):
                create_plan(REPO, [], ['BF'], {'BF': p}, {}, REPO / 'tasks/new', RESOURCES)


class AdapterTests(unittest.TestCase):
    def test_both_agents_same_model_fields_and_different_native_argv(self):
        model = read_json(REPO / 'configs/model.json')
        bf = adapters.configs('BF', model, 'http://mock/v1', 'fake')
        oc = adapters.configs('OC', model, 'http://mock/v1', 'fake')
        self.assertEqual(bf['ai']['models'][0]['model_name'], oc['model']['model'])
        self.assertIn('--no-skills', adapters.command('BF', '/bin/BF', model, 'session'))
        self.assertIn('--standalone', adapters.command('OC', '/bin/OC', model, 'session'))
        self.assertNotIn('--no-skills', adapters.command('OC', '/bin/OC', model, 'session'))

    def test_native_success_separate_from_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); db = tmp / 'oc-state/data/opencode/opencode.db'; db.parent.mkdir(parents=True)
            with contextlib.closing(sqlite3.connect(db)) as conn:
                conn.execute('create table session_v2 (idle_outcome text)')
                conn.execute("insert into session_v2 values ('succeeded')")
                conn.commit()
            self.assertEqual(native_outcome(tmp, 'OC')['outcome'], 'succeeded')


class OrchestrationTests(unittest.TestCase):
    def scenario(self, agents, mismatch=False):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            paths = {agent: elf(tmp / agent) for agent in agents}
            selection = catalog.select(REPO / 'tasks', ['expr-try-catch-errors', 'koota-pair-relation-tracking'])
            plan = create_plan(REPO, selection, agents, paths, read_json(REPO / 'configs/model.json'), tmp / 'out', RESOURCES)
            Path(plan['output']).mkdir()
            write_json(Path(plan['output']) / 'plan.json', plan)
            for agent in agents:
                binaries.snapshot(paths[agent], Path(plan['output']) / 'binaries' / agent, agent)
            runner = Runner(plan, REPO / 'bfoc', {})
            calls = []; active = 0; peak = 0; mutex = threading.Lock()
            def controls(): calls.append('controls')
            def proxy():
                self.assertIn('controls', calls)
                return {i['task']['id'] + '-' + a: 'fake' for i, a in matrix(plan['tasks'], agents)}
            def create(item, agent, token):
                calls.append(('create', agent))
                if mismatch: raise RuntimeError('Initial C0 tree mismatch')
                d = runner.output / 'agents' / (item['task']['id'] + '-' + agent)
                (d / 'artifacts').mkdir(parents=True)
                return 'fake-' + d.name, d
            def finish(name, d):
                nonlocal active, peak
                with mutex: active += 1; peak = max(peak, active)
                (d / 'artifacts/delivery.patch').write_text('mock')
                time.sleep(.03)
                with mutex: active -= 1
                return {'cli_exit_code': 0}
            def verify(item, label, path):
                self.assertEqual(active, 0)
                calls.append(('verify', label)); return {'reward': {'reward': 1}}
            with contextlib.ExitStack() as stack:
                for name, value in [('preflight', lambda: calls.append('preflight')), ('controls', controls),
                    ('check_resources', lambda: None), ('check_resources_for_disk', lambda: None),
                    ('start_proxy', proxy), ('create_agent', create), ('finish_agent', finish),
                    ('verify', verify), ('remove', lambda n: None)]:
                    stack.enter_context(patch.object(runner, name, value))
                stack.enter_context(patch('bfoc.runner.subprocess.run',return_value=types.SimpleNamespace(stdout=b'',stderr=b'')))
                with contextlib.redirect_stdout(io.StringIO()):
                    if mismatch:
                        with self.assertRaisesRegex(RuntimeError, 'tree mismatch'): runner.execute()
                    else:
                        runner.execute()
                        summary = read_json(runner.output / 'summary.json')
                        self.assertEqual(len(summary['runs']), 2 * len(agents))
                        self.assertEqual({r['agent'] for r in summary['runs']}, set(agents))
                        self.assertLessEqual(peak, 3)
                        self.assertGreaterEqual(peak, min(3, 2 * len(agents)))
                        self.assertEqual(read_json(runner.output / 'status.json')['stage'], 'complete')
            if mismatch:
                self.assertEqual(peak, 0)
                self.assertFalse((runner.output / 'summary.json').exists())
                self.assertEqual(read_json(runner.output / 'status.json')['stage'], 'stopped')

    def test_bf_only(self): self.scenario(['BF'])
    def test_oc_only(self): self.scenario(['OC'])
    def test_both(self): self.scenario(['BF', 'OC'])
    def test_mismatch_no_real_agent(self): self.scenario(['BF', 'OC'], mismatch=True)

    def test_hidden_tests_not_mounted_into_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); source = elf(tmp / 'bf')
            selected = catalog.select(REPO / 'tasks', ['expr-try-catch-errors'])
            plan = create_plan(REPO, selected, ['BF'], {'BF': source}, read_json(REPO / 'configs/model.json'), tmp / 'out', RESOURCES)
            prepare(plan); runner = Runner(plan, REPO / 'bfoc', {}); argv = []
            def docker(*args, **kwargs):
                argv.append(args)
                if args[:2] == ('exec', '-e'): return types.SimpleNamespace(stdout=b'')
                if args[:1] == ('exec',) and args[-1] == '/run/experiment/initial-tree.json':
                    return types.SimpleNamespace(stdout=json.dumps({'sha256': selected[0][0]['expected_initial_tree_sha256']}).encode())
                return types.SimpleNamespace(stdout=b'')
            with patch.object(runner, 'docker', docker):
                runner.create_agent(plan['tasks'][0], 'BF', 'fake-token')
            creation = next(a for a in argv if a[0] == 'create')
            self.assertFalse(any(':/tests:' in str(a) or ':/controls:' in str(a) for a in creation))
            inputs = tmp / 'out/agents/expr-try-catch-errors-BF/inputs'
            self.assertEqual({p.name for p in inputs.iterdir()}, {'candidate.patch', 'candidate-files.json', 'prompt.txt', 'run.json', 'config.json'})


class CLITests(unittest.TestCase):
    def test_dry_run_no_credentials_or_docker_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); path = elf(tmp / 'oc')
            text = io.StringIO()
            with patch('bfoc.cli.subprocess.Popen') as proc, contextlib.redirect_stdout(text):
                rc = cli.main(['run', '--agents', 'OC', '--oc-binary', str(path), '--output', str(tmp / 'new'), '--dry-run'])
            self.assertEqual(rc, 0); proc.assert_not_called()
            self.assertFalse((tmp / 'new').exists())
            self.assertEqual(len(json.loads(text.getvalue())['tasks']), 5)

    def test_credentials_only_from_runtime(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError): cli.upstream_config(None)
        with patch.dict(os.environ, {'MODEL_BASE_URL': 'http://model/v1', 'MODEL_API_KEY': 'fake'}):
            self.assertEqual(cli.upstream_config(None)['api_key'], 'fake')


if __name__ == '__main__': unittest.main()
