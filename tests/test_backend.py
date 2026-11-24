"""Offline backend integration checks. All WoS replies are synthetic fixtures."""
import csv
import http.client
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
core = (ROOT/'src/collector.py').read_text().split('\ndef main(argv=None):',1)[0]
with (ROOT/'data/faculty.csv').open(encoding='utf-8-sig',newline='') as f:
    faculty = list(csv.DictReader(f))[:2]
with (ROOT/'data/journal_categories.csv').open(encoding='utf-8-sig',newline='') as f:
    journals = list(csv.DictReader(f))[:1]
backend = (ROOT/'src/backend.py').read_text()
ns = {'__name__':'deh_backend_test','__file__':str(ROOT/'tests/mock_bundle.py')}
exec(compile(core+'\nFACULTY_DATA='+repr(faculty)+'\nJOURNAL_DATA='+repr(journals)+'\nUI_HTML='+repr('<!doctype html><html><head></head><body><script>const BOOT=__DEH_BOOTSTRAP_JSON__;</script></body></html>')+'\n'+backend,'backend_bundle','exec'),ns)

KEY='synthetic-api-key-for-offline-tests'

def record(uid, month, year=2025, citations=7, title=None):
    result={'uid':uid,'title':title or 'Synthetic record '+uid,'source':{'publishYear':year,'publishMonth':month,'sourceTitle':'Synthetic journal','issn':journals[0]['issn']},'types':['Article']}
    if citations is not None:
        result['citations']=[{'db':'WOS','count':citations}]
    return result

class FakeClient:
    def __init__(self, api_key, **kwargs):
        self.api_key=api_key; self.requests=0
    def get(self, query, page):
        self.requests+=1
        if faculty[0]['researcher_id'] in query:
            hits=[record('WOS:SHARED','AUG'),record('WOS:A','JUL',2026,None,title=self.api_key+' </script>')]
        else:
            hits=[record('WOS:SHARED','AUG'),record('WOS:B','AUG',2026,0)]
        return {'metadata':{'page':page,'total':len(hits)},'hits':hits},ns['now_utc'](),{'q':query,'page':page}

class DeniedClient(FakeClient):
    def get(self, query, page):
        self.requests+=1
        raise ns['CollectionError']('HTTP 401: authentication or access rejected',stop_all=True)

class UnexpectedClient(FakeClient):
    def get(self, query, page):
        raise RuntimeError('Synthetic failure '+self.api_key)

class BackendTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.path=Path(self.directory.name)/'DEH_Research_Data.json'
        self.app=ns['DehDashboardApp'](self.path,client_factory=FakeClient)
    def tearDown(self):
        if self.app.worker:
            self.app.worker.join(5)
        self.directory.cleanup()
    def refresh(self, client=None):
        if client:self.app.client_factory=client
        self.app.refresh(KEY,2026)
        self.app.worker.join(5)
        self.assertFalse(self.app.job['running'])
    def test_collection_persistence_and_reload(self):
        self.refresh()
        self.assertEqual(self.app.snapshot['run_status'],'complete')
        self.assertEqual(len(self.app.snapshot['publications']),3)
        summaries=self.app.snapshot['summary_rows']
        all_row=next(r for r in summaries if r['level']=='department_union' and r['period']=='all')
        self.assertEqual(all_row['publication_total'],3)
        self.assertIsNone(all_row['current_citations_total'])
        year=next(r for r in summaries if r['level']=='department_union' and r['period']==2026)
        self.assertEqual(year['publication_total'],1)
        self.assertEqual(year['current_citations_total'],0)
        encoded=self.path.read_text()
        self.assertNotIn(KEY,encoded)
        self.assertIn('[REDACTED]',encoded)
        self.assertEqual(len(list(self.path.parent.iterdir())),1)
        loaded=ns['DehDashboardApp'](self.path,client_factory=FakeClient)
        self.assertEqual(loaded.snapshot['summary_rows'],self.app.snapshot['summary_rows'])
        self.assertFalse(loaded.persistence_blocked)
    def test_failed_refresh_preserves_complete_cache(self):
        self.refresh()
        before=self.path.read_bytes(); version=self.app.version
        self.refresh(DeniedClient)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(self.app.version,version)
        self.assertEqual(self.app.snapshot['run_status'],'complete')
        self.assertIn('retained',self.app.job['message'])
    def test_corrupt_cache_is_not_overwritten(self):
        self.path.write_text('{broken',encoding='utf-8')
        self.app=ns['DehDashboardApp'](self.path,client_factory=FakeClient)
        self.assertTrue(self.app.persistence_blocked)
        self.refresh()
        self.assertEqual(self.path.read_text(),'{broken')
        self.assertEqual(self.app.snapshot['run_status'],'complete')
        self.assertTrue(self.app.storage_warning)
    def test_non_finite_cache_fields_are_rejected(self):
        self.refresh()
        saved=json.loads(self.path.read_text())
        saved['snapshot']['publications'][0]['title']=float('nan')
        bad=json.dumps(saved)
        self.path.write_text(bad)
        self.app=ns['DehDashboardApp'](self.path,client_factory=FakeClient)
        self.assertTrue(self.app.persistence_blocked)
        self.assertIsNone(self.app.snapshot)
        self.assertEqual(self.path.read_text(),bad)
    def test_first_failed_run_keeps_metrics_unavailable(self):
        self.refresh(UnexpectedClient)
        self.assertEqual(self.app.snapshot['run_status'],'incomplete')
        union=next(r for r in self.app.snapshot['summary_rows'] if r['level']=='department_union' and r['period']=='all')
        self.assertIsNone(union['publication_total'])
        self.assertIsNone(union['current_citations_total'])
        self.assertNotIn(KEY,self.path.read_text())
    def test_report_omits_nonce_and_storage_path(self):
        self.refresh()
        report=self.app.render_html(offline=True).decode()
        self.assertNotIn(KEY,report)
        self.assertNotIn(self.app.token,report)
        self.assertNotIn(str(self.path),report)
        self.assertIn('"mode":"offline"',report)
        self.assertIn('\\u003c/script\\u003e',report)
    def test_invalid_keys_and_years(self):
        for value in ['',KEY+'\nheader',KEY+'\rheader','x'*513]:
            with self.assertRaises(ValueError):self.app.refresh(value,2026)
        for value in [True,None,'2026',-5,10000]:
            with self.assertRaises(ValueError):self.app.refresh(KEY,value)
    def test_http_host_origin_and_nonce(self):
        server=ns['DehLocalServer'](0,self.app)
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        port=server.server_port
        base={'Host':f'127.0.0.1:{port}','X-DEH-Token':self.app.token,'Origin':f'http://127.0.0.1:{port}'}
        def call(method,path,headers=None,body=None):
            connection=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
            connection.request(method,path,body=body,headers=headers or {})
            response=connection.getresponse();data=response.read();status=response.status
            self.assertEqual(response.getheader('Access-Control-Allow-Origin'),None)
            connection.close()
            return status,data
        try:
            self.assertEqual(call('GET','/api/state',base)[0],200)
            self.assertEqual(call('GET','/api/state',{**base,'Host':'evil.example'})[0],403)
            self.assertEqual(call('GET','/api/state',{**base,'X-DEH-Token':'wrong'})[0],403)
            body=json.dumps({'api_key':KEY,'academic_year_start':2026})
            self.assertEqual(call('POST','/api/refresh',{**base,'Origin':'https://evil.example','Content-Type':'application/json'},body)[0],403)
            self.assertEqual(call('POST','/api/refresh',{**base,'Content-Type':'text/plain'},body)[0],415)
            self.assertEqual(call('GET','/../../etc/passwd',base)[0],404)
            status,_=call('POST','/api/refresh',{**base,'Content-Type':'application/json'},body)
            self.assertEqual(status,202)
            self.app.worker.join(5)
            status,data=call('GET','/api/report',base)
            self.assertEqual(status,200)
            self.assertNotIn(KEY.encode(),data)
            self.assertNotIn(self.app.token.encode(),data)
        finally:
            server.shutdown();server.server_close();t.join(5)

if __name__=='__main__':
    unittest.main(verbosity=2)
