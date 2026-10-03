import copy
import unittest
import radar

class Guardrails(unittest.TestCase):
    def setUp(self):
        self.state=radar.empty(); self.day='2026-09-09'
    def add(self, n='01'):
        job=radar.read('demo/postings/'+n+'.json')
        k=radar.ingest(self.state,job,self.day)['key']
        radar.assess(self.state,k,radar.read('demo/assessments/'+n+'.json'),self.day)
        return k
    def test_expected_outcomes(self):
        for n, expected in [('01','RECOMMEND'),('02','VERIFY'),('03','SKIP'),('04','SKIP'),('05','SKIP')]:
            self.assertEqual(radar.decision(self.state['jobs'][self.add(n)],self.day),expected)
    def test_missing_quote_cannot_pass(self):
        k=self.add();a=copy.deepcopy(self.state['jobs'][k]['assessment']);a['education']['quote']=''
        with self.assertRaises(ValueError):radar.assess(self.state,k,a,self.day)
    def test_fabricated_quote_rejected(self):
        k=self.add();a=copy.deepcopy(self.state['jobs'][k]['assessment']);a['education']['quote']='Invented eligibility'
        with self.assertRaises(ValueError):radar.assess(self.state,k,a,self.day)
    def test_no_padding_and_same_day_lock(self):
        self.add('02'); self.assertEqual(radar.shortlist(self.state,self.day)['targets'],[])
        self.add('01'); self.assertEqual(radar.shortlist(self.state,self.day)['targets'],[])
    def test_closed_and_stale(self):
        k=self.add();e=self.state['jobs'][k]
        self.assertEqual(radar.decision(e,'2026-09-10'),'VERIFY')
        e['posting']['open']=False;self.assertEqual(radar.decision(e,self.day),'SKIP')
    def test_duplicate_ingestion(self):
        self.add(); self.add();self.assertEqual(len(self.state['jobs']),1)
    def test_confirmation_and_idempotency(self):
        k=self.add();self.state['jobs'][k]['posting']['source_kind']='user_copy'
        with self.assertRaises(ValueError):radar.confirm(self.state,k,self.day,False)
        self.assertTrue(radar.confirm(self.state,k,self.day,True)['counted'])
        self.assertFalse(radar.confirm(self.state,k,self.day,True)['counted'])
        self.assertEqual(radar.summary(self.state,self.day)['total_confirmed_in_this_tracker'],1)
    def test_demo_cannot_count(self):
        k=self.add()
        with self.assertRaises(ValueError):radar.confirm(self.state,k,self.day,True)
    def test_real_requires_human_check(self):
        k=self.add();e=self.state['jobs'][k];e['posting']['source_kind']='user_copy'
        self.assertEqual(radar.decision(e,self.day),'VERIFY')

if __name__=='__main__':unittest.main()
