"""Docker execution, explicit controls, isolated agents and final verification."""
import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import uuid
from . import binaries
from .adapters import configs
from .catalog import discover, validate_task
from .common import call, log, read_json, safe_child, sha256, source_hash, write_json


def matrix(tasks, agents):
    """Alternate paired startup order to avoid always giving BF the first slot."""
    result = []
    for i, task in enumerate(tasks):
        order = agents if i % 2 == 0 else list(reversed(agents))
        result.extend((task, agent) for agent in order)
    return result


def create_plan(repo, selected, agents, paths, model, output, resources):
    if not agents or len(set(agents)) != len(agents) or set(agents) - {'BF', 'OC'}:
        raise ValueError('Select BF, OC or BF OC')
    if set(paths) != set(agents):
        raise ValueError('Supply exactly one binary for each selected agent')
    for path in paths.values():
        binaries.fingerprint(path)
    output = Path(output).expanduser().resolve()
    repo = Path(repo).resolve()
    if ':' in str(output) or '\n' in str(output):
        raise ValueError('Output path cannot contain colon or newline')
    if output.exists():
        raise ValueError('Output already exists; choose a new directory')
    if output == repo or output in repo.parents:
        raise ValueError('Output cannot replace the repository')
    for protected in ['bfoc', 'tasks', 'configs', 'tests', 'docs', '.git']:
        p = repo / protected
        if p == output or p in output.parents:
            raise ValueError('Output overlaps source inputs')
    for task, directory in selected:
        directory = Path(directory).resolve()
        if output == directory or output in directory.parents or directory in output.parents:
            raise ValueError('Output overlaps a selected task directory')
    runid = 'bfoc-' + hashlib.sha256(str(output).encode()).hexdigest()[:20]
    return {'schema_version': 1, 'batch': output.name, 'output': str(output), 'run_id': runid,
            'agents': agents, 'binaries_source': {a: str(Path(p).expanduser().resolve()) for a, p in paths.items()},
            'model': model, 'resources': resources,
            'source_hash': source_hash(repo / 'bfoc'),
            'tasks': [{'task': task, 'directory': str(directory), 'checksums_sha256': sha256(directory / 'checksums.json')}
                      for task, directory in selected],
            'policy': 'Fresh session from C0; committed BASE..HEAD only; native outcome/CLI/verifier separate; no auto resume.'}


def prepare(plan):
    output = Path(plan['output'])
    output.mkdir(parents=True, exist_ok=False)
    (output / '.gitignore').write_text('*\n')
    # Snapshot executables and all selected inputs before background startup.
    for agent in plan['agents']:
        binaries.snapshot(plan['binaries_source'][agent], output / 'binaries' / agent, agent)
    source = Path(__file__).resolve().parent
    target_source = output / 'harness' / 'bfoc'
    shutil.copytree(source, target_source, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy2(source.parent / 'experiment.py', output / 'harness/experiment.py')
    if source_hash(target_source) != plan['source_hash']:
        raise RuntimeError('Runner source changed while preparing')
    for item in plan['tasks']:
        source = Path(item['directory'])
        validate_task(source)
        if sha256(source / 'checksums.json') != item['checksums_sha256']:
            raise RuntimeError('Task changed while preparing')
        target = output / 'inputs' / item['task']['id']
        shutil.copytree(source, target)
        validate_task(target)
        item['directory'] = str(target)
    write_json(output / 'plan.json', plan)


class Runner:
    def __init__(self, plan, source, upstream):
        self.plan = plan
        self.output = Path(plan['output'])
        self.source = Path(source).resolve()
        self.upstream = upstream
        self.r = plan['resources']
        self.runid = plan['run_id']
        self.network = self.runid + '-internal'
        self.external = self.runid + '-external'
        self.proxy = self.runid + '-proxy'
        self.containers = set()
        self.networks = set()

    def docker(self, *args, **kwargs):
        return call(['docker', *args], **kwargs)

    def status(self, stage, **more):
        write_json(self.output / 'status.json', {'stage': stage, 'batch': self.plan['batch'],
                    'updated_unix': time.time(), **more})
        log(stage)

    def common(self, name, image, mounts=(), network='none'):
        args = ['create', '--name', name, '--network', network, '--cpus', str(self.r['cpu']),
                '--memory', str(self.r['memory_mib']) + 'm', '--memory-swap', str(self.r['memory_mib']) + 'm',
                '--pids-limit', '2048', '-e', 'HOME=/root', '-e', 'LANG=C.UTF-8', '-e', 'TZ=UTC', '-w', '/app']
        for src, dst, mode in mounts:
            args.extend(['-v', str(src) + ':' + dst + ':' + mode])
        self.containers.add(name)
        return args + [image]

    def remove(self, name):
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
        self.containers.discard(name)

    def wait(self, name, seconds):
        try:
            return int(self.docker('wait', name, timeout=seconds).stdout.decode().strip())
        except subprocess.TimeoutExpired:
            self.docker('kill', name)
            self.docker('wait', name)
            return 124

    def check_resources(self):
        required = self.r['concurrency'] * self.r['memory_mib'] * 1024 ** 2 + 2 * 1024 ** 3
        memory = {k: int(v.split()[0]) * 1024 for k, v in
                  (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())}['MemAvailable']
        free = shutil.disk_usage(self.output).free
        if memory < required:
            raise RuntimeError('Insufficient available memory for configured concurrency')
        if free < self.r['minimum_free_disk_gib'] * 1024 ** 3:
            raise RuntimeError('Disk reserve reached')
        return {'available_memory_bytes': memory, 'free_disk_bytes': free, 'cpu_count': os.cpu_count()}

    def preflight(self):
        self.status('offline-preflight')
        host = read_json_bytes(self.docker('info', '--format', '{{json .}}').stdout)
        if host.get('OSType') != 'linux' or host.get('Architecture') not in ['x86_64', 'amd64']:
            raise RuntimeError('Experiments require a native Linux x86_64 Docker host')
        if source_hash(self.source) != self.plan['source_hash']:
            raise RuntimeError('Runner source changed after launch')
        write_json(self.output / 'preflight/host.json', self.check_resources())
        checked = set()
        for item in self.plan['tasks']:
            task, directory = item['task'], Path(item['directory'])
            validate_task(directory)
            if sha256(directory / 'checksums.json') != item['checksums_sha256']:
                raise RuntimeError('Frozen task inventory changed')
            image = read_json_bytes(self.docker('image', 'inspect', task['image']).stdout)[0]
            if image['Architecture'] != 'amd64' or image['Os'] != 'linux':
                raise RuntimeError('Pinned task image must be linux/amd64')
            for agent in self.plan['agents']:
                if (task['image'], agent) in checked:
                    continue
                checked.add((task['image'], agent))
                binary = binaries.verify(self.output / 'binaries' / agent)
                probe = self.output / 'preflight' / (task['id'] + '-' + agent)
                probe.mkdir(parents=True)
                spec = {'agent': agent, 'model': self.plan['model'], 'binary_filename': binary['filename'],
                        'binary_sha256': binary['sha256']}
                write_json(probe / 'input.json', spec)
                name = self.runid + '-p-' + str(len(checked))
                try:
                    self.docker(*self.common(name, task['image'], [
                        (self.source, '/harness/bfoc', 'ro'), (self.output / 'binaries' / agent, '/agent-bin', 'ro'),
                        (probe / 'input.json', '/probe-input.json', 'ro'), (probe, '/probe', 'rw')]),
                        'bash', '-lc', 'PYTHONPATH=/harness python3 -m bfoc.probe')
                    self.docker('start', name)
                    rc = self.wait(name, 150)
                    (probe / 'container.log').write_bytes(self.docker('logs', name).stdout)
                    if rc or not read_json(probe / 'contract.json')['all_pass']:
                        raise RuntimeError('Offline CLI contract failed: ' + task['id'] + '/' + agent)
                finally:
                    self.remove(name)

    def verify(self, item, label, patch):
        task, source = item['task'], Path(item['directory'])
        directory = self.output / 'verification' / task['id'] / label
        artifacts, logs = directory / 'artifacts', directory / 'logs'
        artifacts.mkdir(parents=True)
        logs.mkdir()
        payload = patch.read_bytes() if patch else b''
        (directory / 'model.patch').write_bytes(payload)
        (artifacts / 'model.patch').write_bytes(payload)
        name = self.runid + '-v-' + hashlib.sha256((task['id'] + label).encode()).hexdigest()[:12]
        started = time.time()
        try:
            self.docker(*self.common(name, task['image'], [
                (safe_child(source, task['tests']), '/tests', 'ro'), (artifacts, '/logs/artifacts', 'ro'),
                (logs, '/logs/verifier', 'rw')]), *task['verifier_command'])
            self.docker('start', name)
            rc = self.wait(name, self.r['verifier_timeout_seconds'])
            (directory / 'stdout.log').write_bytes(self.docker('logs', name).stdout)
            reward = read_json(logs / 'reward.json') if (logs / 'reward.json').exists() else None
            result = {'task': task['id'], 'label': label, 'exit_code': rc, 'reward': reward,
                      'duration_seconds': time.time() - started, 'patch_sha256': sha256(directory / 'model.patch')}
            write_json(directory / 'summary.json', result)
            log('Verifier ' + task['id'] + ' ' + label + ' reward=' + str((reward or {}).get('reward')))
            if rc or reward is None:
                raise RuntimeError('Verifier infrastructure failed: ' + task['id'] + '/' + label)
            return result
        finally:
            self.remove(name)

    def controls(self):
        self.status('quality-controls-and-initial-quality')
        for item in self.plan['tasks']:
            task, root = item['task'], Path(item['directory'])
            for label, path, key in [('baseline', None, 'baseline_reward'),
                                     ('historical-final', safe_child(root, task['positive_control']), 'positive_reward'),
                                     ('C0', safe_child(root, task['candidate_patch']), 'candidate_reward')]:
                result = self.verify(item, label, path)
                if result['reward']['reward'] != task['expectations'][key]:
                    raise RuntimeError('Control outcome mismatch: ' + task['id'] + '/' + label)

    def start_proxy(self):
        private = self.output / 'private'
        private.mkdir(mode=0o700)
        config = {**self.upstream, **self.plan['model']}
        config['model'] = config.pop('name')
        write_json(private / 'upstream.json', config)
        (private / 'upstream.json').chmod(0o600)
        tokens = {}; clients = {}
        for item, agent in matrix(self.plan['tasks'], self.plan['agents']):
            key = item['task']['id'] + '-' + agent
            token = uuid.uuid4().hex
            tokens[token], clients[key] = key, token
        write_json(private / 'tokens.json', tokens)
        ledger = self.output / 'model-ledger'
        ledger.mkdir()
        for network, internal in [(self.network, True), (self.external, False)]:
            self.docker('network', 'create', *(['--internal'] if internal else []), network)
            self.networks.add(network)
        image = self.plan['tasks'][0]['task']['image']
        self.containers.add(self.proxy)
        self.docker('create', '--name', self.proxy, '--network', self.external, '--cpus', '0.5',
                    '--memory', '256m', '--memory-swap', '256m',
                    '-v', str(self.source / 'proxy.py') + ':/proxy.py:ro',
                    '-v', str(private) + ':/cfg:ro', '-v', str(ledger) + ':/ledger:rw', image, 'python3', '/proxy.py')
        self.docker('network', 'connect', '--alias', 'experiment-model-proxy', self.network, self.proxy)
        self.docker('start', self.proxy)
        for _ in range(20):
            check = subprocess.run(['docker', 'exec', self.proxy, 'python3', '-c',
                "import urllib.request;assert urllib.request.urlopen('http://127.0.0.1:8080/health').read()==b'OK'"], capture_output=True)
            if check.returncode == 0:
                return clients
            time.sleep(.25)
        raise RuntimeError('Model proxy did not become ready')

    def create_agent(self, item, agent, token):
        task, source = item['task'], Path(item['directory'])
        key = task['id'] + '-' + agent
        directory = self.output / 'agents' / key
        inputs = directory / 'inputs'
        inputs.mkdir(parents=True)
        for field, filename in [('prompt', 'prompt.txt'), ('candidate_patch', 'candidate.patch'), ('candidate_files', 'candidate-files.json')]:
            shutil.copy2(safe_child(source, task[field]), inputs / filename)
        binary = binaries.verify(self.output / 'binaries' / agent)
        spec = {'task': task['id'], 'agent': agent, 'base_commit': task['base_commit'], 'model': self.plan['model'],
                'session_id': str(uuid.uuid4()), 'timeout_seconds': self.r['agent_timeout_seconds'],
                'binary_filename': binary['filename'], 'binary_sha256': binary['sha256']}
        write_json(inputs / 'run.json', spec)
        write_json(inputs / 'config.json', configs(agent, self.plan['model'], 'http://experiment-model-proxy:8080/v1', token))
        name = self.runid + '-a-' + hashlib.sha256(key.encode()).hexdigest()[:12]
        try:
            self.docker(*self.common(name, task['image'], [
                (inputs, '/inputs', 'ro'), (self.source, '/harness/bfoc', 'ro'),
                (self.output / 'binaries' / agent, '/agent-bin', 'ro')], self.network),
                'bash', '-lc', 'sleep infinity')
            self.docker('start', name)
            self.docker('exec', '-e', 'PYTHONPATH=/harness', name, 'python3', '-m', 'bfoc.container_entry', 'init')
            initial = read_json_bytes(self.docker('exec', name, 'cat', '/run/experiment/initial-tree.json').stdout)
            write_json(directory / 'initial-tree.json', initial)
            if initial['sha256'] != task['expected_initial_tree_sha256']:
                raise RuntimeError('Initial C0 tree mismatch: ' + key)
            check = "import socket,sys;s=socket.socket();s.settimeout(2)\ntry:s.connect(('1.1.1.1',443))\nexcept OSError:sys.exit(0)\nsys.exit(1)"
            self.docker('exec', name, 'python3', '-c', check)
            return name, directory
        except Exception:
            self.remove(name)
            raise

    def finish_agent(self, name, directory):
        started = time.time()
        write_json(directory / 'started.json', {'container': name, 'started_unix': started})
        limit = self.r['agent_timeout_seconds'] + 100
        execution_error = None
        rc = None
        try:
            with (directory / 'launcher.stdout.log').open('wb') as stdout, (directory / 'launcher.stderr.log').open('wb') as stderr:
                proc = subprocess.Popen(['docker', 'exec', '-e', 'PYTHONPATH=/harness', name,
                    'timeout', '--signal=TERM', '--kill-after=30', str(limit), 'python3', '-m', 'bfoc.container_entry', 'run'],
                    stdout=stdout, stderr=stderr)
                try:
                    rc = proc.wait(timeout=limit + 60)
                except subprocess.TimeoutExpired:
                    self.docker('kill', name)
                    proc.kill(); proc.wait(); rc = 124
        except OSError as error:
            execution_error = type(error).__name__
        try:
            state = read_json_bytes(self.docker('inspect', name).stdout)[0]['State']
            artifacts = directory / 'artifacts'
            artifacts.mkdir()
            self.docker('cp', name + ':/run/experiment/.', artifacts)
            for path, label in [('/root/.bitfun', 'bf-project-state'), ('/root/.config/bitfun', 'bf-default-config')]:
                subprocess.run(['docker', 'cp', name + ':' + path, str(artifacts / label)], capture_output=True)
            process = read_json(artifacts / 'process.json') if (artifacts / 'process.json').exists() else None
            spec = read_json(directory / 'inputs/run.json')
            result = {'agent': spec['agent'], 'task': spec['task'], 'cli_exit_code': rc, 'process': process,
                      'container_state': state, 'elapsed_seconds': time.time() - started,
                      'binary_sha256': spec['binary_sha256'], 'native': native_outcome(artifacts, spec['agent']),
                      'execution_error': execution_error}
            write_json(directory / 'summary.json', result)
            log('Agent finished ' + directory.name + ' exit=' + str(rc))
            return result
        finally:
            self.remove(name)

    def execute(self):
        if list((self.output / 'agents').glob('*/started.json')):
            raise RuntimeError('Agent already started; use a new output directory. No automatic continuation.')
        try:
            self.preflight()
            self.controls()
            self.check_resources()
            self.status('agent-runs')
            tokens = self.start_proxy()
            prepared = []
            # Freeze and compare every C0 before any real CLI invocation.
            for item, agent in matrix(self.plan['tasks'], self.plan['agents']):
                name, directory = self.create_agent(item, agent, tokens[item['task']['id'] + '-' + agent])
                prepared.append((item, agent, name, directory))
            results = []
            pending, running = list(prepared), {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.r['concurrency']) as pool:
                while pending or running:
                    while pending and len(running) < self.r['concurrency']:
                        self.check_resources_for_disk()
                        item, agent, name, directory = pending.pop(0)
                        running[pool.submit(self.finish_agent, name, directory)] = (item, agent, directory)
                        log('Agent started ' + directory.name)
                    write_json(self.output / 'agent-progress.json', {
                        'running': [d.name for i, a, d in running.values()], 'pending': [d.name for i, a, n, d in pending]})
                    done, _ = concurrent.futures.wait(running, return_when=concurrent.futures.FIRST_COMPLETED)
                    for future in done:
                        item, agent, directory = running.pop(future)
                        results.append((item, agent, directory, future.result()))
            captured = subprocess.run(['docker', 'logs', self.proxy], capture_output=True)
            (self.output / 'proxy.stdout.log').write_bytes(captured.stdout)
            (self.output / 'proxy.stderr.log').write_bytes(captured.stderr)
            self.remove(self.proxy)
            write_json(self.output / 'agent-progress.json', {'running': [], 'pending': []})
            self.status('independent-final-verification')
            rows = []
            for item, agent, directory, result in results:
                patch = directory / 'artifacts/delivery.patch'
                if not patch.exists():
                    raise RuntimeError('Missing committed delivery: ' + directory.name)
                rows.append({'task': item['task']['id'], 'agent': agent, 'execution': result,
                             'quality': self.verify(item, 'final-' + agent, patch)})
            write_json(self.output / 'summary.json', {'schema_version': 1, 'batch': self.plan['batch'],
                'agents': self.plan['agents'], 'runs': sorted(rows, key=lambda x: (x['task'], x['agent'])),
                'model': self.plan['model'], 'resources': self.r,
                'binaries': {a: binaries.verify(self.output / 'binaries' / a) for a in self.plan['agents']}})
            from .metrics import summarize
            summarize(self.output)
            self.status('complete')
        except Exception as error:
            self.status('stopped', error=type(error).__name__ + ': ' + str(error))
            raise
        finally:
            if self.proxy in self.containers:
                captured = subprocess.run(['docker', 'logs', self.proxy], capture_output=True)
                (self.output / 'proxy.stdout.log').write_bytes(captured.stdout)
                (self.output / 'proxy.stderr.log').write_bytes(captured.stderr)
            for name in list(self.containers):
                self.remove(name)
            for network in list(self.networks):
                subprocess.run(['docker', 'network', 'rm', network], capture_output=True)

    def check_resources_for_disk(self):
        if shutil.disk_usage(self.output).free < self.r['minimum_free_disk_gib'] * 1024 ** 3:
            raise RuntimeError('Disk reserve reached')


def read_json_bytes(value):
    return json.loads(value.decode())


def native_outcome(artifacts, agent):
    if agent == 'BF':
        completed = []
        p = artifacts / 'stdout.jsonl'
        if p.exists():
            for line in p.open():
                try:
                    value = json.loads(line).get('event', {})
                    if value.get('type') == 'DialogTurnCompleted':
                        completed.append(value)
                except (ValueError, AttributeError):
                    continue
        return {'completion': completed[-1] if completed else None}
    db = artifacts / 'oc-state/data/opencode/opencode.db'
    if not db.exists():
        return {'outcome': None, 'limitation': 'Native database unavailable'}
    try:
        with contextlib.closing(sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True)) as conn:
            rows = conn.execute('SELECT idle_outcome FROM session_v2').fetchall()
        return {'outcome': rows[-1][0] if rows else None}
    except sqlite3.Error as error:
        return {'outcome': None, 'limitation': type(error).__name__}
