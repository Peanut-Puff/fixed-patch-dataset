"""No package binding: each selected CLI is frozen independently for each run."""
from pathlib import Path
import shutil
from .common import sha256, write_json, read_json


def fingerprint(path):
    path = Path(path).expanduser().resolve()
    with path.open('rb') as f:
        header = f.read(20)
    if len(header) < 20 or header[:6] != b'\x7fELF\x02\x01' or int.from_bytes(header[18:20], 'little') != 62:
        raise ValueError('Expected Linux x86_64 ELF64 executable: ' + str(path))
    return {'sha256': sha256(path), 'bytes': path.stat().st_size, 'source_path': str(path)}


def snapshot(source, directory, agent):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    before = fingerprint(source)
    target = directory / ('bitfun' if agent == 'BF' else 'opencode')
    shutil.copyfile(source, target)
    target.chmod(0o555)
    if sha256(target) != before['sha256'] or fingerprint(source) != before:
        raise RuntimeError('Binary changed while copying; use a completed build')
    record = {**before, 'agent': agent, 'filename': target.name}
    write_json(directory / 'manifest.json', record)
    return record


def verify(directory):
    directory = Path(directory)
    record = read_json(directory / 'manifest.json')
    if sha256(directory / record['filename']) != record['sha256']:
        raise RuntimeError('Frozen executable changed')
    return record
