from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from agent_app.web import create_server

class CaseWebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.server=create_server(SimpleNamespace(root=self.root,output_root=self.root/'runs'),port=0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def request(self,path,body=None,token=True):
        conn=HTTPConnection('127.0.0.1',self.server.server_address[1]);headers={}
        if body is not None:
            headers['Content-Type']='application/json'
            if token:headers['X-CSRF-Token']=self.server.csrf_token
        conn.request('POST' if body is not None else 'GET',path,json.dumps(body).encode() if body is not None else None,headers)
        response=conn.getresponse();status=response.status;data=response.read();conn.close();return status,data
    def test_page_plan_and_csrf(self):
        status,data=self.request('/case-modeling');self.assertEqual(status,200);self.assertIn(b'/api/nx/cases/',data)
        status,data=self.request('/api/nx/cases/plan',{'description':'做一个二维轮盘'})
        self.assertEqual(status,200);self.assertEqual(json.loads(data)['model_id'],'geom2d')
        self.assertEqual(self.request('/api/nx/cases/build',{'description':'二维轮盘'},token=False)[0],403)
    def test_execute_uses_independent_job_directory(self):
        with patch('nx_case_agent.execute',return_value={'ok':True,'output':'test.prt'}) as execute:
            status,data=self.request('/api/nx/cases/build',{'description':'二维轮盘','parameters':{'X1':23}})
            self.assertEqual(status,200)
            execute.assert_called_once_with('二维轮盘',{'X1':23},jobs_root=self.root/'nx_modeling_jobs'/'learned_cases')
    def test_invalid_parameters_return_400(self):
        self.assertEqual(self.request('/api/nx/cases/plan',{'description':'二维轮盘','parameters':{'X1':-1}})[0],400)
if __name__=='__main__':unittest.main()
