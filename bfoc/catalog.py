"""Task directories are discovered dynamically; sealed files supply provenance."""
from pathlib import Path
import re
from .common import read_json, safe_child, sha256, write_json

ID = re.compile(r'^[a-z0-9][a-z0-9-]{0,79}$')
HASH = re.compile(r'^[0-9a-f]{64}$')


def seal(directory):
    directory = Path(directory).resolve()
    validate_task(directory, hashes=False)
    rows = []
    for p in sorted(directory.rglob('*')):
        if p.is_symlink():
            raise ValueError('Task inputs must not contain symlinks: ' + str(p))
        if p.is_file() and p.name != 'checksums.json':
            rows.append({'path': str(p.relative_to(directory)), 'bytes': p.stat().st_size, 'sha256': sha256(p)})
    write_json(directory / 'checksums.json', {'schema_version': 1, 'files': rows})
    return len(rows)


def validate_task(directory, hashes=True):
    directory = Path(directory).resolve()
    if any(p.is_symlink() for p in directory.rglob('*')):
        raise ValueError('Task inputs must not contain symlinks')
    t = read_json(directory / 'task.json')
    if t.get('schema_version') != 1 or not ID.fullmatch(t.get('id', '')):
        raise ValueError('Invalid task schema/id: ' + str(directory))
    if directory.name != t['id']:
        raise ValueError('Task id must equal its directory name')
    if not re.fullmatch(r'.+@sha256:[0-9a-f]{64}', t.get('image', '')):
        raise ValueError('Task image must be pinned by digest')
    if not re.fullmatch('[0-9a-f]{40}', t.get('base_commit', '')):
        raise ValueError('base_commit must be a full Git commit')
    if not HASH.fullmatch(t.get('expected_initial_tree_sha256', '')):
        raise ValueError('Expected C0 tree SHA-256 is required')
    for key in ['prompt', 'candidate_patch', 'candidate_files', 'positive_control']:
        if not safe_child(directory, t[key]).is_file():
            raise ValueError('Missing task file: ' + key)
    tests = safe_child(directory, t['tests'])
    for filename in ['test.sh', 'test.patch', 'grader.py', 'config.json']:
        if not (tests / filename).is_file():
            raise ValueError('Missing verifier input: ' + filename)
    if not isinstance(t.get('verifier_command'), list) or not t['verifier_command'] or not all(isinstance(v, str) for v in t['verifier_command']):
        raise ValueError('verifier_command must be a nonempty argv list')
    expected = t.get('expectations', {})
    if any(expected.get(k) not in [0, 1] for k in ['baseline_reward', 'positive_reward', 'candidate_reward']):
        raise ValueError('Control rewards must be explicit 0/1 values')
    files = read_json(safe_child(directory, t['candidate_files']))['files']
    seen = set()
    for item in files:
        safe_child(Path('/app'), item['path'])
        if item['path'] in seen:
            raise ValueError('Duplicate candidate path')
        seen.add(item['path'])
        for key in ['sha256', 'base_sha256']:
            if item.get(key) is not None and not HASH.fullmatch(item[key]):
                raise ValueError('Invalid candidate file hash')
    if hashes:
        inventory = read_json(directory / 'checksums.json')['files']
        actual = {str(p.relative_to(directory)) for p in directory.rglob('*') if p.is_file() and p.name != 'checksums.json'}
        if actual != {r['path'] for r in inventory}:
            raise ValueError('Task file inventory changed; inspect and seal intentionally: ' + t['id'])
        for row in inventory:
            p = safe_child(directory, row['path'])
            if p.is_symlink() or p.stat().st_size != row['bytes'] or sha256(p) != row['sha256']:
                raise ValueError('Task input hash changed: ' + t['id'] + '/' + row['path'])
    return t


def discover(root):
    result = {}
    for p in sorted(Path(root).glob('*/task.json')):
        task = validate_task(p.parent)
        if task['id'] in result:
            raise ValueError('Duplicate task id')
        result[task['id']] = (task, p.parent.resolve())
    if not result:
        raise ValueError('No tasks found in ' + str(root))
    return result


def select(root, ids=None, suite=None):
    tasks = discover(root)
    if ids and suite:
        raise ValueError('Choose --tasks or --suite')
    names = ids or (read_json(suite)['tasks'] if suite else list(tasks))
    if not names or len(set(names)) != len(names):
        raise ValueError('Task selection must be nonempty and unique')
    unknown = set(names) - tasks.keys()
    if unknown:
        raise ValueError('Unknown tasks: ' + ', '.join(sorted(unknown)))
    return [tasks[name] for name in names]
