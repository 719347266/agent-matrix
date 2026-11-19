import io
import json
import os
import select
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_matrix import cindy_pickup as pickup, local_sessions as ls
from agent_matrix.models import MatrixError


class ButtonHandoff(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'state').mkdir()
        self.work = self.root / 'item'
        self.work.mkdir()
        self.route = {'session':'buddy-session','schedule':'button-only'}
        (self.root / 'state/cindy-pickup.json').write_text(json.dumps(self.route))
        self.item = {'n':50,'record_id':'record-50','title':'校对样例','url':'https://example.test/v1','rejected':'按新版核对'}
        self.start = dict(protocol=pickup.PROTOCOL,type='start',context=dict(targetSessionId='buddy-session',scheduleId='button-only'))

    def click(self):
        with patch.object(ls,'CINDY_APP',self.root), patch('agent_matrix.itemwork.workdir_for',return_value=self.work), \
             patch.object(ls,'_gpt_said',return_value=['已有第一版']), patch.object(ls.subprocess,'run') as opened:
            result=ls.start_in_cindy(self.root,None,self.item,history='主人：保留第一版')
            self.assertEqual(opened.call_args.args[0][-1],'cindy://session/buddy-session')
            return result['pickup']

    def pump(self,*frames):
        out=io.StringIO()
        pickup.run(self.root,io.StringIO('\n'.join(json.dumps(f) for f in (self.start,*frames))+'\n'),out)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def row(self):
        with closing(pickup.db_for(self.root)) as db:
            return db.execute("SELECT request_key,message FROM pickup").fetchone()

    def test_click_twice_one_message_and_reopen_after_receipt(self):
        self.assertEqual([self.click(),self.click()],['queued','queued'])
        key,msg=self.row()
        for piece in ('已有第一版','保留第一版','按新版核对','https://example.test/v1'):
            self.assertIn(piece,msg)
        ack=dict(protocol=pickup.PROTOCOL,type='call_result',id=key,ok=True,result={'target_session_id':'buddy-session','delivery_id':'delivery-one'})
        sent=self.pump(ack)
        self.assertEqual([f['type'] for f in sent],['call','complete'])
        self.assertEqual(sent[0]['params']['idempotency_key'],key)
        self.assertEqual(self.click(),'delivered')
        self.assertEqual([f['type'] for f in self.pump()],['complete'])

    def test_other_target_cannot_receive(self):
        self.click()
        self.start['context']['targetSessionId']='other'
        with self.assertRaises(MatrixError): self.pump()

    def test_wrong_or_empty_receipt_never_shows_delivered(self):
        self.click()
        key,_=self.row()
        self.pump(dict(protocol=pickup.PROTOCOL,type='call_result',id=key,ok=True,result={'target_session_id':'other'}))
        self.assertEqual(self.click(),'error')
        self.assertNotIn('call',[f['type'] for f in self.pump()])

    def test_disconnected_after_send_reuses_same_request(self):
        self.click()
        key,_=self.row()
        with self.assertRaises((ValueError,MatrixError)): self.pump()
        ack=dict(protocol=pickup.PROTOCOL,type='call_result',id=key,ok=True,result={'target_session_id':'buddy-session','delivery_id':'id'})
        self.assertEqual(self.pump(ack)[0]['params']['idempotency_key'],key)

    def test_no_click_no_dispatch(self):
        self.assertEqual([v['type'] for v in self.pump()],['complete'])

    @unittest.skipUnless(hasattr(select, 'kqueue'), "macOS notification")
    def test_running_listener_wakes_for_a_click_and_waits_for_matched_receipt(self):
        # A real child process and kernel event; only the Cindy stdin/stdout peer is simulated.
        env = dict(os.environ, PYTHONPATH=str(Path(pickup.__file__).resolve().parents[1]))
        with subprocess.Popen([sys.executable,'-m','agent_matrix.cindy_pickup','--listen'],
                              cwd=self.root, env=env, text=True, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            try:
                proc.stdin.write(json.dumps(self.start)+'\n'); proc.stdin.flush()
                deadline=time.monotonic()+3
                while not pickup.signal_file(self.root).exists() and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertTrue(pickup.signal_file(self.root).exists())
                self.click()
                self.assertTrue(select.select([proc.stdout],[],[],3)[0], 'no pickup after click')
                line=proc.stdout.readline()
                if not line: self.fail(proc.stderr.read())
                call=json.loads(line)
                self.assertEqual(call['params']['target_session_id'],'buddy-session')
                ack=dict(protocol=pickup.PROTOCOL,type='call_result',id=call['id'],ok=True,
                         result={'target_session_id':'buddy-session','delivery_id':'example-receipt'})
                proc.stdin.write(json.dumps(ack)+'\n'); proc.stdin.flush()
                deadline=time.monotonic()+3
                while pickup.latest(self.root).get('record-50')!='delivered' and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertEqual(self.click(),'delivered')
                self.assertFalse(select.select([proc.stdout],[],[],.1)[0], 'sent twice')
            finally:
                proc.terminate()
