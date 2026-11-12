"""Offline task extension helper: calculate pre/postimages and the C0 tree."""
import os
from pathlib import Path
import subprocess
from bfoc.common import read_json, write_json
from bfoc.container_entry import tree


def main():
    spec = read_json('/capture-input.json')
    app = Path('/app')
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=app)
    git('config', '--global', '--add', 'safe.directory', '/app')
    if git('rev-parse', 'HEAD').decode().strip() != spec['base_commit'] or git('status', '--porcelain'):
        raise RuntimeError('Capture requires the clean exact baseline image')
    before = tree(app)
    git('apply', '--check', '/candidate.patch')
    git('apply', '/candidate.patch')
    after = tree(app)
    b = {r['path']: r for r in before['files']}
    a = {r['path']: r for r in after['files']}
    changed = sorted(p for p in b.keys() | a.keys() if b.get(p) != a.get(p))
    rows = []
    for p in changed:
        value = a.get(p) or b[p]
        if value['mode'] == '120000':
            raise RuntimeError('Candidate file manifest currently supports regular files, not changed symlinks')
        rows.append({'path': p, 'mode': value['mode'], 'base_sha256': b.get(p, {}).get('sha256'),
                     'sha256': a.get(p, {}).get('sha256')})
    write_json('/capture-output/candidate-files.json', {'files': rows})
    write_json('/capture-output/initial-tree.json', after)


if __name__ == '__main__':
    main()
