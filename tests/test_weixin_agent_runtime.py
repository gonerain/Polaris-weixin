"""Real SDK delegation with isolated models, transport, and user authority."""
from pathlib import Path
import sys
import threading
import time
import unittest
import itertools
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/weixin'))
import test_weixin_component as fixtures
from components import manifest
from components.assistant import Assistant
from components.assistant.loop import AgentLoop
from polaris_platform.component import Component
from polaris_platform.protocol import PlatformError


class Model:
    def __init__(self, replies=None): self.replies, self.requests = iter(replies or [{'content': '你好'}]), []
    def complete(self, messages, tools, cancel, timeout=30):
        self.requests.append(messages)
        reply = next(self.replies)
        return reply(messages, cancel) if callable(reply) else reply


_call_ids = itertools.count()

def tool(name, args, identity=None):
    identity = identity or 'c'+str(next(_call_ids))
    import json
    return {'tool_calls': [{'id': identity, 'function': {'name': name, 'arguments': json.dumps(args)}}]}


class DelegationTests(unittest.TestCase):
    call = fixtures.WeixinComponentTests.call
    wait = fixtures.WeixinComponentTests.wait

    def setUp(self):
        fixtures.WeixinComponentTests.setUp(self)
        self.model = Model()
        self.service = Assistant(AgentLoop(self.model), data_dir=Path(self.directory.name)/'assistant')
        self.runtime.install(manifest('assistant'), lambda: self.service)
        self.runtime.start_component('assistant')
        self.toy_calls = []
        toy = Component()
        toy.operation('echo', access='write')(lambda p,c: self.toy_calls.append(p) or {'ok':True})
        self.runtime.install(dict(id='toy', name='测试组件', version='1', platformApi=1, entry=dict(mode='inprocess')),lambda:toy)
        self.runtime.start_component('toy')
        for target, names in [('assistant',('reply','status')), ('collaboration',('discover','describe')), ('toy',('echo',))]:
            for name in names:self.runtime.grant('user',target,name)
        self.context=self.runtime.user_context('user')
        self.component.client.account=dict(fixtures.ACCOUNT)
        self.component.client.credentials=fixtures.Credentials(fixtures.ACCOUNT)
        self.call('connect')
        self.wait(lambda:self.component.client.state=='connected')
        self.sequence=0

    def tearDown(self):
        self.component.agent.invalidate()
        self.wait(lambda:not self.component.agent.worker_running)
        fixtures.WeixinComponentTests.tearDown(self)

    def enable(self, model=None, allowed=None):
        if model is not None:self.model=model;self.service.engine=AgentLoop(model)
        self.call('configure_agent',enabled=True,allowed=allowed or [])

    def incoming(self,text):
        self.sequence+=1
        self.component.client.receive(dict(message_id='msg'+str(self.sequence),message_type=1,message_state=2,
            from_user_id='fake-user',context_token='fake-context',create_time_ms=time.time()*1000,
            item_list=[{'type':1,'text_item':{'text':text}}]))

    def reply(self,count=1):
        self.wait(lambda:len(self.fake.sent)>=count and not self.component.agent.busy)
        self.assertTrue(self.component.agent.enabled,self.component.agent.settings())
        return self.fake.sent[-1]['item_list'][0]['text_item']['text']

    def test_weixin_delegates_once_and_history_lives_in_service(self):
        self.enable(Model([{'content':'one'},{'content':'two'}]))
        self.incoming('first');self.assertEqual(self.reply(),'one')
        self.incoming('second');self.assertEqual(self.reply(2),'two')
        self.assertEqual(len(self.model.requests),2)
        self.assertIn({'role':'assistant','content':'one'},self.model.requests[1])
        self.assertFalse(hasattr(self.component.agent,'agent'))
        self.assertFalse(hasattr(self.component.agent,'history'))

    def test_authorized_tool_runs_in_provider_with_user_scope(self):
        self.enable(Model([tool('help',{'component':'toy','operation':'echo'}),
            tool('call',{'component':'toy','operation':'echo','arguments':{'value':'hello'}}),{'content':'已执行'}]),
            [{'target':'toy','operation':'echo'}])
        self.incoming('执行');self.assertEqual(self.reply(),'已执行')
        self.assertEqual(self.toy_calls,[{'value':'hello'}])

    def test_unselected_tool_cannot_be_used_by_provider(self):
        self.enable(Model([tool('call',{'component':'toy','operation':'echo','arguments':{}}),{'content':'未执行'}]))
        self.incoming('执行');self.reply()
        self.assertEqual(self.toy_calls,[])
        self.assertIn('denied',str(self.model.requests[-1]))

    def test_new_conversation_drops_previous_delivered_parent(self):
        self.enable(Model([{'content':'private'},{'content':'fresh'}]))
        self.incoming('old');self.reply()
        old=self.component.agent.conversation
        self.call('new_conversation')
        self.incoming('new');self.reply(2)
        self.assertNotEqual(old,self.component.agent.conversation)
        self.assertNotIn('private',str(self.model.requests[1]))

    def test_cancelled_reply_is_not_sent(self):
        entered=threading.Event()
        def slow(messages,cancel):
            entered.set()
            deadline=time.monotonic()+2
            while not cancel.is_set() and time.monotonic()<deadline:time.sleep(.01)
            return {'content':'late'}
        self.enable(Model([slow]));self.incoming('wait')
        self.assertTrue(entered.wait(2))
        self.call('new_conversation')
        self.wait(lambda:not self.component.agent.worker_running)
        self.assertEqual(self.fake.sent,[])

    def test_control_callback_rejects_wrong_caller(self):
        self.component.agent.turns['test']=dict(provider='assistant',cancel=threading.Event(),account=self.component.agent._account(),allowed=[])
        with self.assertRaises(PlatformError):self.call('agent_control',turn='test',action='active')
        self.component.agent.turns.clear()

    def test_missing_provider_has_no_embedded_fallback(self):
        self.runtime.stop_component('assistant',persist=False)
        with self.assertRaises(PlatformError):self.call('configure_agent',enabled=True,allowed=[])
        self.assertFalse(self.component.agent.enabled)

    def test_catalog_excludes_provider_and_settings_have_no_local_key_requirement(self):
        catalog=self.call('agent_catalog')
        self.assertNotIn('assistant',[r['id'] for r in catalog['components']])
        self.assertTrue(self.call('agent_settings')['ready'])
        self.assertNotIn('keyEnv',self.call('agent_settings'))
