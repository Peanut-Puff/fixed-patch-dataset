import contextlib
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from bfoc.common import read_json, sha256, source_hash, write_json
from bfoc.container_entry import initialize, tree
from bfoc.metrics import summarize
from bfoc.probe import run as probe_run
from bfoc import cli

REPO = Path(__file__).resolve().parents[1]


class EntryTests(unittest.TestCase):
    def test_initialization_exact_patch_preimage_postimage_and_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); app = tmp / 'app'; app.mkdir(); inputs = tmp / 'inputs'; inputs.mkdir(); logs = tmp / 'logs'; logs.mkdir()
            env = {**os.environ, 'GIT_CONFIG_GLOBAL': str(tmp / 'gitconfig'), 'GIT_CONFIG_NOSYSTEM': '1'}
            def git(*args): return subprocess.check_output(['git', *args], cwd=app, env=env)
            git('init', '-q'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid')
            (app / 'file').write_text('before\n'); git('add', 'file'); git('commit', '-qm', 'baseline')
            base = git('rev-parse', 'HEAD').decode().strip(); before = sha256(app / 'file')
            (app / 'file').write_text('after\n'); (inputs / 'candidate.patch').write_bytes(git('diff', '--binary'))
            expected = tree(app)['sha256']; after = sha256(app / 'file'); git('checkout', '--', 'file')
            write_json(inputs / 'candidate-files.json', {'files': [{'path': 'file', 'base_sha256': before, 'sha256': after}]})
            with patch_environment(env):
                initialize({'base_commit': base}, app, logs, inputs)
            self.assertEqual(read_json(logs / 'initial-tree.json')['sha256'], expected)
            self.assertEqual((app / 'file').read_text(), 'after\n')


class patch_environment:
    def __init__(self, env): self.env = env
    def __enter__(self):
        from unittest.mock import patch
        self.patch = patch.dict(os.environ, self.env); return self.patch.__enter__()
    def __exit__(self, *args): return self.patch.__exit__(*args)


class ProbeTests(unittest.TestCase):
    def test_bf_and_oc_probe_real_processes_loopback_only(self):
        # A tiny fake CLI exercises process/env/config/mock HTTP, not the real model.
        source = '''#!/usr/bin/env python3
import json,os,sys,urllib.request
from pathlib import Path
if '--version' in sys.argv:
 print('offline fake CLI 1.0');raise SystemExit(0)
if sys.argv[1]=='exec':
 m=json.loads((Path(os.environ['BITFUN_USER_ROOT'])/'config/app.json').read_text())['ai']['models'][0]
 url=m['request_url'];name=m['model_name'];limit=m['max_tokens'];effort=json.loads(m['custom_request_body'])['reasoning_effort']
else:
 c=json.loads(os.environ['OPENCODE_CONFIG_CONTENT']);p=c['providers']['openai-compatible'];name=c['model']['model'];m=p['models'][name]
 url=p['settings']['baseURL']+'/chat/completions';limit=m['limit']['output'];effort=m['settings']['reasoningEffort']
data=json.dumps({'model':name,'max_tokens':limit,'reasoning_effort':effort,'stream':True}).encode()
print(urllib.request.urlopen(urllib.request.Request(url,data=data,headers={'Content-Type':'application/json'})).read().decode())
'''
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for agent, name in [('BF', 'bitfun'), ('OC', 'opencode')]:
                binary = tmp / name; binary.write_text(source); binary.chmod(0o755)
                spec = {'agent': agent, 'binary_filename': name, 'binary_sha256': sha256(binary),
                        'model': read_json(REPO / 'configs/model.json')}
                result = tmp / agent
                self.assertEqual(probe_run(spec, tmp, result), 0)
                contract = read_json(result / 'contract.json')
                self.assertTrue(contract['all_pass']); self.assertEqual(len(contract['requests']), 1)


class ProxyTests(unittest.TestCase):
    def test_raw_forwarding_rejections_and_usage_ledger(self):
        forwarded = []
        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                payload = self.rfile.read(int(self.headers['Content-Length'])); forwarded.append(payload)
                body = b'data: {"usage":{"prompt_tokens":12,"completion_tokens":3}}\n\ndata: [DONE]\n\n'
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp); cfg = tmp / 'private'; cfg.mkdir(); ledger = tmp / 'ledger'; ledger.mkdir()
                write_json(cfg / 'upstream.json', {'base_url': 'http://127.0.0.1:' + str(server.server_port) + '/v1',
                    'api_key': 'fake-upstream', 'model': 'test-model', 'max_output_tokens': 100, 'reasoning_effort': 'high'})
                write_json(cfg / 'tokens.json', {'fake-client': 'task-OC'})
                with socket.socket() as port_socket:
                    port_socket.bind(('127.0.0.1', 0)); port = port_socket.getsockname()[1]
                env = {**os.environ, 'BFOC_PROXY_CONFIG': str(cfg), 'BFOC_PROXY_LEDGER': str(ledger), 'BFOC_PROXY_PORT': str(port)}
                proc = subprocess.Popen([sys.executable, str(REPO / 'bfoc/proxy.py')], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    for _ in range(40):
                        try:
                            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=.2): break
                        except OSError: time.sleep(.03)
                    def post(body):
                        raw = json.dumps(body).encode()
                        req = urllib.request.Request(f'http://127.0.0.1:{port}/v1/chat/completions', data=raw,
                            headers={'Authorization': 'Bearer fake-client'})
                        try:
                            with urllib.request.urlopen(req, timeout=5) as response: return response.status, response.read(), raw
                        except urllib.error.HTTPError as error:
                            with error:
                                return error.code, error.read(), raw
                    good = {'model': 'test-model', 'max_tokens': 90, 'reasoning_effort': 'high', 'stream': True}
                    status, response, raw = post(good)
                    self.assertEqual(status, 200); self.assertEqual(forwarded, [raw]); self.assertIn(b'[DONE]', response)
                    self.assertEqual(post({**good, 'max_tokens': 101})[0], 422)
                    self.assertEqual(post({**good, 'temperature': 0})[0], 422)
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        records = [json.loads(v) for v in (ledger / 'requests.jsonl').read_text().splitlines()]
                        if len(records) == 3: break
                        time.sleep(.02)
                    self.assertEqual(len(records), 3); self.assertEqual(len(forwarded), 1)
                    self.assertEqual(records[0]['usage_observations'][0]['prompt_tokens'], 12)
                    for row in records:
                        self.assertEqual(sha256(ledger / 'task-OC' / (row['id'] + '.request.json')), row['request_sha256'])
                finally:
                    proc.terminate(); proc.communicate(timeout=5)
        finally:
            server.shutdown(); server.server_close()


class MetricTests(unittest.TestCase):
    def test_missing_usage_kept_partial_and_missing_cache_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); write_json(tmp / 'plan.json', {'tasks': [{'task': {'id': 't'}}], 'agents': ['OC']})
            ledger = tmp / 'model-ledger'; ledger.mkdir()
            rows = [{'run': 't-OC', 'forwarded': True, 'usage_observations': [{'prompt_tokens': 20, 'completion_tokens': 3}]},
                    {'run': 't-OC', 'forwarded': True, 'usage_observations': []}]
            (ledger / 'requests.jsonl').write_text(''.join(json.dumps(v) + '\n' for v in rows))
            result = summarize(tmp)[0]
            self.assertEqual(result['token_status'], 'partial'); self.assertEqual(result['usage_missing'], 1)
            self.assertEqual(result['input_observed'], 20); self.assertIsNone(result['uncached_observed'])

    def test_unfinished_proxy_attempt_is_counted_as_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); write_json(tmp / 'plan.json', {'tasks': [{'task': {'id': 't'}}], 'agents': ['BF']})
            ledger = tmp / 'model-ledger'; ledger.mkdir()
            (ledger / 'attempts-started.jsonl').write_text(json.dumps({'id':'inflight','run':'t-BF','forwarded':True,'usage_observations':[]})+'\n')
            row = summarize(tmp)[0]
            self.assertEqual(row['forwarded_attempts'],1);self.assertEqual(row['usage_missing'],1)
            self.assertEqual(row['token_status'],'missing');self.assertIsNone(row['input_observed'])


class BackgroundTests(unittest.TestCase):
    def test_background_real_child_lock_and_frozen_code_no_docker(self):
        from unittest.mock import patch
        from test_experiments import elf
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); binary = elf(tmp / 'bf'); output = tmp / 'output'
            args = cli.parser().parse_args(['run', '--agents', 'BF', '--bf-binary', str(binary),
                '--tasks', 'expr-try-catch-errors', '--output', str(output), '--background'])
            created = []; real = subprocess.Popen
            def launch(argv, **kwargs):
                # Replace only execute_frozen's runner with a local assertion child.
                frozen = output / 'harness/bfoc/runner.py'
                original = frozen.read_text()
                original += '''\nclass Runner:\n def __init__(self,plan,source,upstream):\n  self.output=Path(plan['output']);self.source=source\n def execute(self):\n  assert self.source==self.output/'harness/bfoc'\n  assert source_hash(self.source)==read_json(self.output/'plan.json')['source_hash']\n  (self.output/'ready').write_text('ready')\n  deadline=time.monotonic()+5\n  while not (self.output/'release').exists() and time.monotonic()<deadline:time.sleep(.01)\n'''
                frozen.write_text(original)
                plan = read_json(output / 'plan.json'); plan['source_hash'] = source_hash(output / 'harness/bfoc'); write_json(output / 'plan.json', plan)
                proc = real(argv, **kwargs); created.append(proc); return proc
            with patch.object(cli.subprocess, 'Popen', launch),patch.object(cli, 'upstream_config', return_value={'base_url':'http://fake/v1','api_key':'fake'}),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.run(args), 0)
            proc = created[0]
            try:
                deadline = time.monotonic()+3
                while not (output/'ready').exists() and time.monotonic()<deadline:time.sleep(.02)
                self.assertTrue((output/'ready').exists(), (output/'controller.log').read_text())
                import fcntl
                with (output/'controller.lock').open('a') as lock:
                    with self.assertRaises(BlockingIOError):fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:
                (output/'release').touch();proc.wait(timeout=5)
            self.assertEqual(proc.returncode,0)
            self.assertFalse((output/'private-launch.json').exists())


if __name__ == '__main__': unittest.main()
