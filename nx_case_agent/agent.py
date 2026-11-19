"""Standalone web and CLI entry point."""
import argparse,json,sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
ROOT=Path(__file__).resolve().parent
if str(ROOT.parent) not in sys.path:sys.path.insert(0,str(ROOT.parent))
from nx_case_agent.core import plan,execute,models
class Web(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path!='/':self.send_error(404);return
        data=(ROOT/'index.html').read_bytes();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(data)
    def do_POST(self):
        try:
            if self.path not in ['/plan','/build']:self.send_error(404);return
            size=int(self.headers.get('Content-Length','0'))
            if size>65536:raise ValueError('输入过长')
            body=json.loads(self.rfile.read(size))
            if set(body)-{'description','parameters'}:raise ValueError('未知请求字段')
            result=(plan if self.path=='/plan' else execute)(body['description'],body.get('parameters'));status=200
        except Exception as exc:result={'ok':False,'error':str(exc)};status=400
        data=json.dumps(result,ensure_ascii=False).encode('utf-8');self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.end_headers();self.wfile.write(data)
def main():
    p=argparse.ArgumentParser(description='NX 12 learned two-case modeling agent');p.add_argument('action',choices=['list','plan','build','web']);p.add_argument('description',nargs='?',default='');p.add_argument('--port',type=int,default=8771);args=p.parse_args()
    if args.action=='web':
        print('Open http://127.0.0.1:'+str(args.port),flush=True);ThreadingHTTPServer(('127.0.0.1',args.port),Web).serve_forever()
    else:
        try:
            value=models() if args.action=='list' else (plan if args.action=='plan' else execute)(args.description);print(json.dumps(value,ensure_ascii=False,indent=2))
            if value.get('ok') is False:raise SystemExit(1)
        except Exception as exc:print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False));raise SystemExit(1)
if __name__=='__main__':main()
