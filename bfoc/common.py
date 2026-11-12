"""Small, standard-library-only helpers shared with container entry points."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def call(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, capture_output=True, **kwargs)


def log(message):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), message, flush=True)


def safe_child(root, relative):
    root = Path(root).resolve()
    rel = Path(relative)
    target = (root / rel).resolve()
    if rel.is_absolute() or target == root or root not in target.parents:
        raise ValueError('Path must remain inside its task: ' + str(relative))
    return target


def source_hash(root):
    root = Path(root)
    rows = [{'path': str(p.relative_to(root)), 'sha256': sha256(p)}
            for p in sorted(root.rglob('*.py')) if '__pycache__' not in p.parts]
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
