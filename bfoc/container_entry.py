"""Runs inside disposable task containers. Hidden tests are never mounted here."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from bfoc.adapters import command, environment
from bfoc.common import read_json, sha256, write_json
# Keep the historical imports available to capture.py and callers.
from bfoc.candidate_state import tree, initialize as initialize_candidate


def initialize(spec, app, run, inputs=Path('/inputs')):
    snapshot = initialize_candidate(spec, app, inputs)
    write_json(run / 'initial-tree.json', snapshot)
    (run / 'initial-status.txt').write_bytes(subprocess.check_output(
        ['git', 'status', '--porcelain=v1', '--untracked-files=all'], cwd=app))
    print('INITIAL_TREE ' + snapshot['sha256'], flush=True)


def execute(spec, app, run):
    binary = Path('/agent-bin') / spec['binary_filename']
    if sha256(binary) != spec['binary_sha256']:
        raise RuntimeError('Frozen executable changed')
    env = environment(spec['agent'], run, read_json('/inputs/config.json'), os.environ)
    argv = command(spec['agent'], binary, spec['model'], spec['session_id'])
    started = time.time()
    state = {'agent': spec['agent'], 'session_id': spec['session_id'], 'command': argv,
             'binary_sha256': spec['binary_sha256'], 'started_unix': started}
    write_json(run / 'process.json', state)
    with Path('/inputs/prompt.txt').open('rb') as prompt, (run / 'stdout.jsonl').open('wb') as stdout, (run / 'stderr.log').open('wb') as stderr:
        proc = subprocess.Popen(argv, cwd=app, env=env, stdin=prompt, stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            rc = proc.wait(timeout=spec['timeout_seconds'])
            state['status'] = 'exited'
        except subprocess.TimeoutExpired:
            state['status'] = 'timeout'
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            rc = 124
    state.update(returncode=rc, ended_unix=time.time(), duration_seconds=time.time() - started)
    write_json(run / 'process.json', state)
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=app)
    (run / 'delivery.patch').write_bytes(git('diff', '--binary', spec['base_commit'], 'HEAD'))
    (run / 'working-tree.patch').write_bytes(git('diff', '--binary', spec['base_commit']))
    (run / 'final-status.txt').write_bytes(git('status', '--porcelain=v1', '--untracked-files=all'))
    (run / 'final-head.txt').write_bytes(git('rev-parse', 'HEAD'))
    write_json(run / 'final-tree.json', tree(app))
    untracked = git('ls-files', '--others', '--exclude-standard', '-z')
    (run / 'untracked-files.zlist').write_bytes(untracked)
    if untracked:
        subprocess.run(['tar', '--null', '-T', '-', '-czf', str(run / 'untracked-files.tar.gz')], cwd=app, input=untracked, check=True)
    return rc


def main():
    run = Path('/run/experiment')
    run.mkdir(parents=True, exist_ok=True)
    spec = read_json('/inputs/run.json')
    if sys.argv[1] == 'init':
        initialize(spec, Path('/app'), run)
        return 0
    if sys.argv[1] == 'run':
        return execute(spec, Path('/app'), run)
    raise ValueError('Expected init or run')


if __name__ == '__main__':
    raise SystemExit(main())
