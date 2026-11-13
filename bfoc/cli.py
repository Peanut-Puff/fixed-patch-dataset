"""Local entry point. Only `run` executes model tasks; inspection stays offline."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from urllib.parse import urlsplit
from .catalog import discover, select, seal, validate_task
from .common import call, read_json, write_json
from .runner import Runner, create_plan, prepare

REPO = Path(__file__).resolve().parent.parent


def upstream_config(path):
    value = read_json(path) if path else {'base_url': os.environ.get('MODEL_BASE_URL'), 'api_key': os.environ.get('MODEL_API_KEY')}
    url, key = value.get('base_url'), value.get('api_key')
    parsed = urlsplit(url or '')
    if parsed.scheme not in ['http', 'https'] or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('Set MODEL_BASE_URL or --upstream to a valid HTTP(S) base_url')
    if not isinstance(key, str) or not key.strip() or key == 'REPLACE_AT_RUNTIME':
        raise ValueError('Provide MODEL_API_KEY or a private --upstream config')
    return {'base_url': url.rstrip('/'), 'api_key': key}


def model_config(path):
    value = read_json(path)
    if set(value) != {'name', 'context_window', 'max_output_tokens', 'reasoning_effort'}:
        raise ValueError('Model config requires name/context_window/max_output_tokens/reasoning_effort only')
    if not isinstance(value['name'], str) or not value['name'] or not isinstance(value['reasoning_effort'], str):
        raise ValueError('Model name/reasoning_effort must be strings')
    for k in ['context_window', 'max_output_tokens']:
        if type(value[k]) is not int or value[k] <= 0:
            raise ValueError(k + ' must be a positive integer')
    if value['max_output_tokens'] >= value['context_window']:
        raise ValueError('Output limit must be below context window')
    return value


def execute_frozen(output, lock_fd):
    output = Path(output).resolve()
    with os.fdopen(lock_fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = read_json(output / 'plan.json')
        upstream = read_json(output / 'private-launch.json')
        try:
            Runner(plan, output / 'harness/bfoc', upstream).execute()
        finally:
            (output / 'private-launch.json').unlink(missing_ok=True)


def run(args):
    suite = args.suite
    if not suite and not args.tasks and args.task_root.resolve() == (REPO / 'tasks').resolve():
        suite = REPO / 'configs/default-suite.json'
    selection = select(args.task_root, args.tasks, suite)
    paths = {a: getattr(args, a.lower() + '_binary') for a in args.agents}
    if any(p is None for p in paths.values()):
        raise ValueError('Selected agents require their own --bf-binary / --oc-binary')
    if any(getattr(args, a.lower() + '_binary') is not None for a in ['BF', 'OC'] if a not in args.agents):
        raise ValueError('Binary supplied for an unselected agent')
    resources = {'concurrency': args.concurrency, 'cpu': args.cpu, 'memory_mib': args.memory_mib,
                 'agent_timeout_seconds': args.agent_timeout, 'verifier_timeout_seconds': args.verifier_timeout,
                 'minimum_free_disk_gib': args.min_free_disk}
    if any(type(v) is not int or v <= 0 for k, v in resources.items() if k != 'cpu') or args.cpu <= 0:
        raise ValueError('Resource limits must be positive')
    plan = create_plan(REPO, selection, args.agents, paths, model_config(args.model), args.output, resources)
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    upstream = upstream_config(args.upstream)
    prepare(plan)
    output = Path(plan['output'])
    with (output / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.background:
            # Credential never appears in argv, plan, command output or Git files.
            write_json(output / 'private-launch.json', upstream)
            (output / 'private-launch.json').chmod(0o600)
            try:
                with (output / 'controller.log').open('ab') as log:
                    proc = subprocess.Popen([sys.executable, '-u', str(output / 'harness/experiment.py'), '_execute',
                        '--output', str(output), '--lock-fd', str(lock.fileno())], stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(lock.fileno(),))
            except OSError:
                (output / 'private-launch.json').unlink(missing_ok=True)
                raise
            (output / 'controller.pid').write_text(str(proc.pid) + '\n')
            print('PID: ' + str(proc.pid))
            print('Results: ' + str(output))
            print('Log: tail -f ' + shlex.quote(str(output / 'controller.log')))
        else:
            Runner(plan, output / 'harness/bfoc', upstream).execute()
    return 0


def parser():
    p = argparse.ArgumentParser(description='BF/OC coding-agent experiments; standard library and Docker only.')
    sub = p.add_subparsers(dest='command', required=True)
    tasks = sub.add_parser('tasks', help='Inspect, validate or extend the task set').add_subparsers(dest='task_command', required=True)
    for name in ['list', 'check']:
        t = tasks.add_parser(name); t.add_argument('--task-root', type=Path, default=REPO / 'tasks')
    t = tasks.add_parser('seal', help='Record hashes after an intentional task edit')
    t.add_argument('directory', type=Path)
    t = tasks.add_parser('sync-pier', help='Generate Pier inputs from task.json; seal separately after review')
    t.add_argument('directory', type=Path)
    t = tasks.add_parser('import', help='Copy and seal a task directory into this repository')
    t.add_argument('directory', type=Path)
    t = tasks.add_parser('capture', help='Calculate candidate file hashes/C0 tree using the pinned image, without a model')
    t.add_argument('directory', type=Path)
    images = sub.add_parser('images', help='List pinned images or download them before running')
    images.add_argument('--pull', action='store_true'); images.add_argument('--task-root', type=Path, default=REPO / 'tasks')
    summary = sub.add_parser('summarize', help='Export metrics.csv/json from a result directory, without model calls')
    summary.add_argument('output', type=Path)
    r = sub.add_parser('run', help='Fresh run, with a new output directory and runtime binaries')
    r.add_argument('--agents', nargs='+', choices=['BF', 'OC'], required=True)
    r.add_argument('--bf-binary', type=Path); r.add_argument('--oc-binary', type=Path)
    r.add_argument('--output', type=Path, required=True)
    r.add_argument('--task-root', type=Path, default=REPO / 'tasks')
    group = r.add_mutually_exclusive_group()
    group.add_argument('--tasks', nargs='+'); group.add_argument('--suite', type=Path)
    r.add_argument('--model', type=Path, default=REPO / 'configs/model.json')
    r.add_argument('--upstream', type=Path, help='Private JSON containing base_url and api_key; otherwise use environment variables')
    r.add_argument('--concurrency', type=int, default=3)
    r.add_argument('--cpu', type=float, default=2); r.add_argument('--memory-mib', type=int, default=8192)
    r.add_argument('--agent-timeout', type=int, default=10800); r.add_argument('--verifier-timeout', type=int, default=1800)
    r.add_argument('--min-free-disk', type=int, default=40)
    r.add_argument('--background', action='store_true'); r.add_argument('--dry-run', action='store_true')
    internal = sub.add_parser('_execute', help=argparse.SUPPRESS)
    internal.add_argument('--output', required=True); internal.add_argument('--lock-fd', type=int, required=True)
    return p


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        if args.command == 'tasks':
            if args.task_command in ['list', 'check']:
                tasks = discover(args.task_root)
                for key, (task, directory) in tasks.items():
                    print(key if args.task_command == 'list' else key + ': hashes OK' +
                          ('; Pier inputs OK' if 'pier' in task else ''))
                return 0
            if args.task_command == 'sync-pier':
                from .pier_dataset import sync
                print('Synchronized ' + str(sync(args.directory)) + ' Pier inputs; inspect and run tasks seal next')
                return 0
            if args.task_command == 'seal':
                print('Sealed ' + str(seal(args.directory)) + ' files')
                return 0
            if args.task_command == 'capture':
                directory = args.directory.resolve()
                task = read_json(directory / 'task.json')
                import re
                if not re.fullmatch(r'.+@sha256:[0-9a-f]{64}', task['image']) or not re.fullmatch('[0-9a-f]{40}', task['base_commit']):
                    raise ValueError('Capture requires pinned image and full base_commit')
                candidate = (directory / task['candidate_patch']).resolve()
                if directory not in candidate.parents:
                    raise ValueError('Candidate path must remain in task directory')
                name = 'bfoc-capture-' + uuid.uuid4().hex[:16]
                with tempfile.TemporaryDirectory(prefix='bfoc-capture-') as tmp:
                    tmp = Path(tmp)
                    write_json(tmp / 'input.json', {'base_commit': task['base_commit']})
                    try:
                        call(['docker', 'create', '--name', name, '--network', 'none', '--cpus', '2', '--memory', '8g', '--memory-swap', '8g',
                            '-v', str(REPO / 'bfoc') + ':/harness/bfoc:ro', '-v', str(candidate) + ':/candidate.patch:ro',
                            '-v', str(tmp / 'input.json') + ':/capture-input.json:ro', '-v', str(tmp) + ':/capture-output:rw',
                            task['image'], 'bash', '-lc', 'PYTHONPATH=/harness python3 -m bfoc.capture'])
                        call(['docker', 'start', name])
                        try:
                            rc = int(call(['docker', 'wait', name], timeout=300).stdout)
                        except subprocess.TimeoutExpired:
                            call(['docker', 'kill', name]); raise
                        if rc:
                            raise RuntimeError('Capture failed: ' + call(['docker', 'logs', name]).stdout.decode(errors='replace')[-2000:])
                        manifest = safe_task_file(directory, task['candidate_files'])
                        write_json(manifest, read_json(tmp / 'candidate-files.json'))
                        task['expected_initial_tree_sha256'] = read_json(tmp / 'initial-tree.json')['sha256']
                        write_json(directory / 'task.json', task)
                        if 'pier' in task:
                            from .pier_dataset import sync
                            sync(directory)
                        seal(directory)
                        print('Captured and sealed ' + task['id'])
                    finally:
                        subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
                return 0
            task = validate_task(args.directory, hashes=False)
            if any(p.is_symlink() for p in args.directory.rglob('*')):
                raise ValueError('Task import cannot contain symlinks')
            target = REPO / 'tasks' / task['id']
            shutil.copytree(args.directory, target, symlinks=True)
            seal(target)
            print('Imported ' + task['id'])
            return 0
        if args.command == 'images':
            images = sorted({t['image'] for t, directory in discover(args.task_root).values()})
            for image in images:
                print(image, flush=True)
                if args.pull:
                    subprocess.run(['docker', 'pull', '--platform', 'linux/amd64', image], check=True)
            return 0
        if args.command == 'summarize':
            from .metrics import summarize
            rows = summarize(args.output)
            print('Wrote metrics.csv/json for ' + str(len(rows)) + ' runs')
            return 0
        if args.command == '_execute':
            execute_frozen(args.output, args.lock_fd)
            return 0
        return run(args)
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        return 1


def safe_task_file(directory, relative):
    from .common import safe_child
    return safe_child(directory, relative)
