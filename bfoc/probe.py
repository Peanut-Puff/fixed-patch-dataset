"""Offline CLI/config check against a loopback fake model, run in each image."""
import http.server
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid
from bfoc.adapters import configs, command, environment
from bfoc.common import read_json, sha256, write_json


def run(spec, binary_dir=Path('/agent-bin'), output=Path('/probe')):
    records = []
    class Mock(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_error(404)
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            records.append({'path': self.path, 'model': body.get('model'),
                            'output_limit': body.get('max_tokens', body.get('max_completion_tokens')),
                            'reasoning_effort': body.get('reasoning_effort'), 'stream': body.get('stream'),
                            'temperature_present': 'temperature' in body, 'top_p_present': 'top_p' in body})
            ident = 'offline-' + uuid.uuid4().hex
            model = spec['model']['name']
            usage = {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}
            chunks = [{'id': ident, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model,
                       'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': None}]},
                      {'id': ident, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model,
                       'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': usage}]
            data = (''.join('data: ' + json.dumps(v) + '\n\n' for v in chunks) + 'data: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(data)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        home = Path(tempfile.mkdtemp(prefix='bfoc-probe-'))
        workspace = home / 'workspace'
        workspace.mkdir()
        subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
        url = 'http://127.0.0.1:' + str(server.server_port) + '/v1'
        config = configs(spec['agent'], spec['model'], url, 'offline-probe')
        env = environment(spec['agent'], home, config, os.environ)
        env['HOME'] = str(home)
        binary = Path(binary_dir) / spec['binary_filename']
        if sha256(binary) != spec['binary_sha256']:
            raise RuntimeError('Wrong binary in probe')
        version = subprocess.run([str(binary), '--version'], env=env, cwd=workspace, capture_output=True, timeout=20)
        argv = command(spec['agent'], binary, spec['model'], str(uuid.uuid4()))
        try:
            proc = subprocess.run(argv, input=b'Reply with exactly OK. Do not use tools or modify files.',
                                  cwd=workspace, env=env, capture_output=True, timeout=90)
            rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as error:
            rc, stdout, stderr = 124, error.stdout or b'', error.stderr or b''
        out = Path(output)
        out.mkdir(parents=True, exist_ok=True)
        (out / 'stdout.jsonl').write_bytes(stdout)
        (out / 'stderr.log').write_bytes(stderr)
        model = spec['model']
        ok = rc == 0 and version.returncode == 0 and bool(records) and all(
            r['path'] == '/v1/chat/completions' and r['model'] == model['name'] and
            isinstance(r['output_limit'], int) and 0 < r['output_limit'] <= model['max_output_tokens'] and
            r['reasoning_effort'] == model['reasoning_effort'] and r['stream'] is True and
            not r['temperature_present'] and not r['top_p_present'] for r in records)
        write_json(out / 'contract.json', {'agent': spec['agent'], 'all_pass': ok, 'exit_code': rc,
            'version_exit_code': version.returncode, 'version': version.stdout.decode(errors='replace').strip(),
            'binary_sha256': spec['binary_sha256'], 'requests': records})
        return 0 if ok else 2
    finally:
        server.shutdown()
        server.server_close()


def main():
    return run(read_json('/probe-input.json'))


if __name__ == '__main__':
    raise SystemExit(main())
