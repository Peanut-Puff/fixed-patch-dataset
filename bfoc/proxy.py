#!/usr/bin/env python3
"""Shared OpenAI-compatible forwarder; no fallback or added retries."""
import hashlib,http.client,http.server,json,os,pathlib,socket,threading,time,uuid
from urllib.parse import urlsplit

CONFIG_DIR=pathlib.Path(os.environ.get('BFOC_PROXY_CONFIG','/cfg'))
CONFIG=json.loads((CONFIG_DIR/'upstream.json').read_text())
TOKENS=json.loads((CONFIG_DIR/'tokens.json').read_text())
LEDGER=pathlib.Path(os.environ.get('BFOC_PROXY_LEDGER','/ledger'))
LOCK=threading.Lock()

class Proxy(http.server.BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*args):pass
 def error(self,status,message):
  self.close_connection=True
  body=json.dumps({'error':{'message':message,'type':'experiment_proxy'}}).encode()
  self.send_response(status);self.send_header('Content-Type','application/json')
  self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def do_GET(self):
  if self.path!='/health':return self.error(404,'Unknown route')
  body=b'OK';self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(body)
 def do_POST(self):
  token=self.headers.get('Authorization','').removeprefix('Bearer ')
  run=TOKENS.get(token)
  if not run:return self.error(403,'Unknown experiment credential')
  if self.path!='/v1/chat/completions':return self.error(404,'Unknown route')
  length=int(self.headers.get('Content-Length','0'))
  if not 0<length<64*1024*1024:return self.error(413,'Invalid request length')
  raw=self.rfile.read(length)
  try:body=json.loads(raw)
  except Exception:return self.error(400,'Invalid JSON')
  if not isinstance(body,dict):return self.error(400,'JSON object required')
  limit=body.get('max_tokens',body.get('max_completion_tokens'))
  ident=uuid.uuid4().hex;directory=LEDGER/run;directory.mkdir(parents=True,exist_ok=True)
  (directory/(ident+'.request.json')).write_bytes(raw)
  reasons=[]
  if body.get('model')!=CONFIG['model']:reasons.append('model_mismatch')
  if not isinstance(limit,int) or isinstance(limit,bool) or not 0<limit<=CONFIG['max_output_tokens']:
   reasons.append('output_budget_outside_contract')
  if any(value!=limit for field,value in body.items() if field in ['max_tokens','max_completion_tokens']):
   reasons.append('conflicting_output_fields')
  if body.get('reasoning_effort')!=CONFIG['reasoning_effort']:reasons.append('reasoning_effort_mismatch')
  if body.get('stream') is not True:reasons.append('stream_mismatch')
  if 'temperature' in body:reasons.append('temperature_present')
  if 'top_p' in body:reasons.append('top_p_present')
  if reasons:
   record={'id':ident,'run':run,'started_unix':time.time(),'duration_seconds':0,
     'status':422,'forwarded':False,'error':'local_contract_rejection','violations':reasons,
     'model':body.get('model'),'output_limit':limit,'max_tokens':body.get('max_tokens'),
     'max_completion_tokens':body.get('max_completion_tokens'),
     'reasoning_effort':body.get('reasoning_effort'),'stream':body.get('stream'),
     'request_sha256':hashlib.sha256(raw).hexdigest(),'usage_observations':[],'proxy_retries':0}
   with LOCK:
    with (LEDGER/'requests.jsonl').open('a') as f:f.write(json.dumps(record)+'\n')
   return self.error(422,'Frozen model contract: '+','.join(reasons))
  url=urlsplit(CONFIG['base_url'].rstrip('/')+'/chat/completions')
  conn=(http.client.HTTPSConnection if url.scheme=='https' else http.client.HTTPConnection)(url.hostname,url.port,timeout=CONFIG.get('request_timeout_seconds',600))
  started=time.time();status=None;usage=[];error=None;response_started=False;buffer=b''
  # A start record survives proxy shutdown even if an upstream attempt never finishes.
  with LOCK:
   with (LEDGER/'attempts-started.jsonl').open('a') as f:
    f.write(json.dumps({'id':ident,'run':run,'started_unix':started,'forwarded':True,
      'status':None,'error':'pending_or_unfinished','usage_observations':[],
      'request_sha256':hashlib.sha256(raw).hexdigest(),'model':body.get('model'),
      'output_limit':limit,'reasoning_effort':body.get('reasoning_effort'),'proxy_retries':0})+'\n')
  try:
   conn.request('POST',url.path,raw,{'Authorization':'Bearer '+CONFIG['api_key'],'Content-Type':'application/json','Accept':'text/event-stream'})
   response=conn.getresponse();status=response.status
   self.send_response(status);self.send_header('Content-Type',response.getheader('Content-Type','application/octet-stream'))
   self.send_header('Connection','close');self.end_headers();response_started=True;self.close_connection=True
   with (directory/(ident+'.response.raw')).open('wb') as output:
    while True:
     chunk=response.read1(65536)
     if not chunk:break
     output.write(chunk);buffer+=chunk
     try:
      self.wfile.write(chunk);self.wfile.flush()
     except (BrokenPipeError,ConnectionResetError):
      pass # Continue capturing upstream usage after a client-side retry/disconnect.
     while b'\n' in buffer:
      line,buffer=buffer.split(b'\n',1)
      if line.startswith(b'data:'):
       try:
        item=json.loads(line[5:].strip())
        if item.get('usage') is not None:usage.append(item['usage'])
       except (ValueError,AttributeError):pass
  except Exception as exc:
   error=type(exc).__name__
   if not response_started:
    try:self.error(502,'Model connection failed: '+error)
    except (BrokenPipeError,ConnectionResetError):pass
  finally:
   conn.close()
   record={'id':ident,'run':run,'started_unix':started,'duration_seconds':time.time()-started,
           'status':status,'forwarded':True,'error':error,'request_sha256':hashlib.sha256(raw).hexdigest(),
           'model':body.get('model'),'output_limit':limit,'reasoning_effort':body.get('reasoning_effort'),
           'usage_observations':usage,'proxy_retries':0}
   with LOCK:
    with (LEDGER/'requests.jsonl').open('a') as f:f.write(json.dumps(record)+'\n')

if __name__=='__main__':
 http.server.ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('BFOC_PROXY_PORT','8080'))),Proxy).serve_forever()
