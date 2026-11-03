from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from polaris_weixin.interactions import HumanReplies
from components.assistant.tools import ToolExecutor
from polaris_platform.protocol import PlatformError


class Client:
    def __init__(self):
        self._lock=threading.RLock();self.sent=[];self.cancel=threading.Event()
    def get(self):return {'canSend':True}
    def send(self,message):self.sent.append(message);return {'messageId':str(len(self.sent))}
    def worker(self,fn):fn(0,self.cancel)
    def active(self,generation):return True


class HumanReplyTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.client=Client()
        self.completions=SimpleNamespace(records={'token':{'target':'bridge','deliveryId':'delivery','sessionId':'session','account':['b','u']}},account=lambda:['b','u'])
        self.adapter=HumanReplies(self.client,Path(self.directory.name)/'requests.json',self.completions)
        self.result=dict(id='a'*32,kind='approval',prompt='pwd',options=[],deliveryId='delivery',sessionId='session',runId='run',responseToken='secret-'*8,expiresAt=time.time()+60)
        self.calls=[]
        self.context=SimpleNamespace(call=lambda *args:(self.calls.append(args) or {'accepted':True}))

    def test_only_exact_bound_human_message_answers_once_without_model(self):
        with self.assertRaises(PlatformError):self.adapter.receive('token',self.result,'other')
        self.adapter.receive('token',self.result,'bridge')
        self.assertNotIn(self.result['responseToken'],str(self.client.sent))
        self.assertFalse(self.adapter.handle('可以',time.time(),self.context))
        self.adapter.handle('批准 '+'a'*10+' please',time.time(),self.context)
        self.assertEqual(self.calls,[])
        self.adapter.handle('批准 '+'a'*10,time.time(),self.context)
        self.assertEqual(self.calls[0][0:2],('bridge','respond'))
        self.assertEqual(self.calls[0][2]['answer'],'allow')
        self.adapter.handle('批准 '+'a'*10,time.time(),self.context)
        self.assertEqual(len(self.calls),1)

    def test_question_choice_expiry_and_no_authority(self):
        self.result.update(kind='question',options=['PDF','Word'])
        self.adapter.receive('token',self.result,'bridge')
        self.adapter.handle('回答 '+'a'*10+' 2',time.time(),None)
        self.assertEqual(self.calls,[])
        self.adapter.handle('回答 '+'a'*10+' 2',time.time(),self.context)
        self.assertEqual(self.calls[0][2]['answer'],'Word')
        self.result.update(id='b'*32,expiresAt=time.time()-1)
        self.assertTrue(self.adapter.receive('token',self.result,'bridge')['expired'])
        self.assertNotIn('b'*32,self.adapter.records)

    def test_human_response_operations_are_hidden_from_model_tools(self):
        executor=ToolExecutor(None,[{'target':'bridge','operation':'respond'},{'target':'bridge','operation':'send'}],'directory')
        record={'id':'bridge','description':{'operations':{'respond':{'humanOnly':True},'send':{}}}}
        self.assertEqual(set(executor.filtered(record)['description']['operations']),{'send'})
