#!/usr/bin/env python3
"""Explicit, model-free amd64 controls and native Pier Oracle. No AEO calls."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bfoc.catalog import discover, validate_task
from bfoc.common import call, read_json, write_json
from bfoc.pier_dataset import PIER_REVISION
from bfoc.runner import Runner


def oracle_config(output):
    return {
        'job_name': 'oracle', 'jobs_dir': str(output / 'pier'), 'n_attempts': 1,
        'n_concurrent_trials': 1, 'retry': {'max_retries': 0},
        'agents': [{'name': 'oracle'}],
        'datasets': [{'path': str(output / 'dataset/tasks')}],
        'environment': {'type': 'docker', 'force_build': True, 'delete': False},
        # Match AEO: pinned Pier hardcodes two verifier timeout attempts.
        'verifier_timeout_multiplier': 0.5,
    }


def validate_oracle_results(job_dir, case_ids):
    results = {}
    for path in sorted(Path(job_dir).glob('*/result.json')):
        result = read_json(path)
        case_id = result.get('task_name', '').rsplit('/', 1)[-1]
        if case_id not in case_ids or case_id in results:
            raise RuntimeError('Unexpected or duplicate Oracle case: ' + case_id)
        verifier = result.get('verifier_result') or {}
        if (not result.get('finished_at') or result.get('exception_info') or
                (verifier.get('rewards') or {}).get('reward') != 1):
            raise RuntimeError('Oracle did not pass: ' + case_id + '; inspect ' + str(path))
        if result.get('agent_info', {}).get('name') != 'oracle':
            raise RuntimeError('Expected native Oracle: ' + case_id)
        exit_file = path.parent / 'agent/exit-code.txt'
        if exit_file.exists() and exit_file.read_text().strip() != '0':
            raise RuntimeError('Oracle script failed: ' + case_id)
        results[case_id] = {'reward': 1, 'result': str(path)}
    if set(results) != set(case_ids):
        raise RuntimeError('Missing Oracle results: ' + ', '.join(sorted(set(case_ids) - results.keys())))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pier', default='pier', help='Pier executable installed from the pinned commit')
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if output.exists():
        raise ValueError('Output already exists; choose a fresh directory')
    for source in (REPO / name for name in ('tasks', 'bfoc', 'scripts', 'tests', 'configs', '.git', 'docs')):
        if output == source or source in output.parents or output in source.parents:
            raise ValueError('Output overlaps source inputs')
    if ':' in str(output) or '\n' in str(output):
        raise ValueError('Output path cannot contain colon or newline')
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise RuntimeError('Run this command on a native Linux amd64 Docker host')
    host = json.loads(call(['docker', 'info', '--format', '{{json .}}']).stdout)
    if host.get('OSType') != 'linux' or host.get('Architecture') not in ('x86_64', 'amd64'):
        raise RuntimeError('Docker must run native Linux amd64')
    if call([args.pier, '--version']).stdout.decode().strip() != '0.3.1':
        raise RuntimeError('Use Pier 0.3.1 from ' + PIER_REVISION)
    selected = discover(REPO / 'tasks')
    output.mkdir(parents=True)
    resources = {'concurrency': 1, 'cpu': 2, 'memory_mib': 8192,
                 'verifier_timeout_seconds': 1800, 'minimum_free_disk_gib': 40}
    plan = {'output': str(output), 'batch': output.name,
            'run_id': 'controls-' + hashlib.sha256(str(output).encode()).hexdigest()[:16],
            'resources': resources, 'tasks': [], 'pier_source_revision': PIER_REVISION}
    runner = Runner(plan, REPO / 'bfoc', {})
    runner.check_resources()
    if (os.cpu_count() or 0) < 3:
        raise RuntimeError('At least 3 logical CPUs required to reserve one for the host')
    for task, directory in selected.values():
        target = output / 'dataset/tasks' / task['id']
        shutil.copytree(directory, target)
        validate_task(target)
        plan['tasks'].append({'task': task, 'directory': str(target)})
    write_json(output / 'plan.json', plan)
    try:
        for image in sorted({task['image'] for task, _ in selected.values()}):
            subprocess.run(['docker', 'pull', '--platform', 'linux/amd64', image], check=True)
        runner.controls()
        runner.check_resources()
        write_json(output / 'oracle.json', oracle_config(output))
        with (output / 'pier.log').open('wb') as log:
            subprocess.run([args.pier, 'run', '--config', str(output / 'oracle.json')],
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        results = validate_oracle_results(output / 'pier/oracle', selected)
        write_json(output / 'summary.json', {'status': 'passed', 'oracle': results,
                   'controls': {task_id: task['expectations']
                                for task_id, (task, _) in selected.items()}})
        print('All controls and native Oracle passed: ' + str(output / 'summary.json'))
    except Exception as error:
        write_json(output / 'summary.json', {'status': 'failed', 'error': str(error)})
        raise


if __name__ == '__main__':
    main()
