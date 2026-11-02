"""Agent integration uses fake model/transport and actual platform context calls."""
from pathlib import Path
import sys
import threading
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/weixin'))
import test_weixin_component as fixtures
from assistant_fixture import install, delivered_history, seed_history
Credentials, ACCOUNT = fixtures.Credentials, fixtures.ACCOUNT
from polaris_platform.sdk import Component
from polaris_platform.protocol import PlatformError
from components.assistant.loop import AgentLoop as WeixinAgent
from components.assistant.tools import ToolExecutor


class Model:
    def __init__(self): self.step = 0
    def complete(self, messages, tools, cancel, timeout=30):
        self.step += 1
        if self.step == 1:
            return {'role': 'assistant', 'tool_calls': [{'id':'h','type':'function','function':{'name':'help','arguments':'{"component":"toy"}'}}]}
        if self.step == 2:
            return {'role':'assistant','tool_calls':[{'id':'c','type':'function','function':{'name':'call','arguments':'{"component":"toy","operation":"echo","arguments":{"text":"ok"}}'}}]}
        return {'role':'assistant','content':'完成'}


class AgentRuntimeTests(unittest.TestCase):
    call = fixtures.WeixinComponentTests.call
    wait = fixtures.WeixinComponentTests.wait
    tearDown = fixtures.WeixinComponentTests.tearDown
    def setUp(self):
        fixtures.WeixinComponentTests.setUp(self)
        self.assistant = install(self.runtime)
        self.effects = []
        owner = self
        class Toy(Component):
            def __init__(self):
                super().__init__()
                self.operation('echo', access='write')(self.echo)
            def echo(self, text):
                owner.effects.append(text)
                return {'text': text}
        self.runtime.install({'id':'toy','name':'Toy','version':'1','platformApi':1,'entry':{'mode':'inprocess'}}, Toy)
        self.runtime.start_component('toy')
        for target, op in [('toy','echo'),('collaboration','discover'),('collaboration','describe')]:
            self.runtime.grant('user', target, op)
        self.context = self.runtime.user_context('user')
        self.assistant.engine = WeixinAgent(Model())
        self.component.client.account = dict(ACCOUNT)
        self.component.client.credentials = Credentials(ACCOUNT)
        self.component.client.state = 'connected'
    def enable(self):
        return self.call('configure_agent', enabled=True, allowed=[{'target':'toy','operation':'echo'}])
    def receive(self, identity='fresh', timestamp=None):
        with self.component.client._lock:
            self.component.client.receive(dict(message_id=identity, message_type=1, message_state=2,
                from_user_id='fake-user', context_token='fake-context', create_time_ms=timestamp,
                item_list=[{'type':1,'text_item':{'text':'测试'}}]))
    def test_fake_model_real_context_and_confirmed_history(self):
        self.enable(); self.receive(timestamp=time.time()*1000)
        self.wait(lambda: len(delivered_history(self)) == 2)
        self.assertEqual(self.effects, ['ok'])
        self.assertEqual(len(self.fake.sent), 1)
        self.assertEqual(self.fake.sent[0]['item_list'][0]['text_item']['text'], '完成')

    def test_final_model_failure_acknowledges_committed_write_and_stays_enabled(self):
        from components.assistant.model import ModelError
        class BrokenFinal(Model):
            def complete(self, *args, **kwargs):
                if self.step == 2:
                    raise ModelError('请求超时')
                return super().complete(*args, **kwargs)
        self.assistant.engine = WeixinAgent(BrokenFinal())
        self.enable(); self.receive(timestamp=time.time()*1000)
        self.wait(lambda: len(delivered_history(self)) == 2)
        self.assertEqual(self.effects, ['ok'])
        self.assertEqual(len(self.fake.sent), 1)
        reply = self.fake.sent[0]['item_list'][0]['text_item']['text']
        self.assertIn('已确认执行成功', reply)
        self.assertIn('请求超时', reply)
        self.assertTrue(self.component.agent.enabled)

    def test_model_failure_before_tools_reports_failure_without_claiming_success(self):
        from components.assistant.model import ModelError
        class Broken:
            def complete(self, *args, **kwargs):
                raise ModelError('DeepSeek 请求失败，请检查网络或配置')
        self.assistant.engine = WeixinAgent(Broken())
        self.enable(); self.receive(timestamp=time.time()*1000)
        self.wait(lambda: len(delivered_history(self)) == 2)
        reply = self.fake.sent[0]['item_list'][0]['text_item']['text']
        self.assertIn('请求失败', reply)
        self.assertNotIn('执行成功', reply)
        self.assertEqual(self.effects, [])
        self.assertTrue(self.component.agent.enabled)

    def test_second_tool_correction_can_send_verified_final_reply(self):
        import json
        def tool(name,args):
            return {'id':name,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}
        class Reviewed:
            reviews = 0
            def __init__(self):
                self.replies = iter([
                    {'tool_calls':[tool('help',{'component':'toy'})]},
                    {'tool_calls':[tool('call',{'component':'toy','operation':'echo','arguments':{'text':'ok'}})]},
                    {'content':'完成'},
                    {'tool_calls':[tool('help',{'component':'toy'})]},
                    {'content':'完成'},
                    {'tool_calls':[tool('components',{})]},
                    {'content':'完成'}])
            def complete(self,*args,**kwargs): return next(self.replies)
            def review(self,request,draft,evidence,*args,**kwargs):
                self.reviews += 1
                if self.reviews <= 2:
                    return {'needs_tools':True,'reason':'补充查询','reply':''}
                return {'needs_tools':False,'reason':'','reply':'已确认执行成功。'}
        model = Reviewed()
        self.assistant.engine = WeixinAgent(model)
        self.enable(); self.receive(timestamp=time.time()*1000)
        self.wait(lambda: len(delivered_history(self)) == 2)
        self.assertEqual(model.reviews,3)
        self.assertEqual(self.effects,['ok'])
        self.assertEqual(len(self.fake.sent),1)
        self.assertEqual(self.fake.sent[0]['item_list'][0]['text_item']['text'],'已确认执行成功。')
        self.assertEqual(self.component.agent.error,'')
        self.assertTrue(self.component.agent.enabled)

    def test_waiting_for_sender_has_deadline_and_does_not_resend(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        self.enable()
        self.component.client.sending = True
        ticks = iter([0, 31])
        clock = SimpleNamespace(time=time.time, monotonic=lambda: next(ticks))
        with patch('polaris_weixin.controller.time', clock):
            self.receive(timestamp=time.time()*1000)
            self.wait(lambda: not self.component.agent.worker_running)
        self.assertFalse(self.component.agent.busy)
        self.assertFalse(self.component.agent.enabled)
        self.assertIn('发送未确认', self.component.agent.error)
        self.assertEqual(self.effects, ['ok'])
        self.assertEqual(self.fake.sent, [])
        self.component.client.sending = False

    def test_failure_summary_never_claims_unknown_write_succeeded(self):
        executor = ToolExecutor(None, [], 'collaboration')
        executor.writes[('toy', 'echo', '{}')] = (None, True)
        reply = executor.failure_reply('请求超时')
        self.assertIn('结果未确认', reply)
        self.assertNotIn('已确认执行成功', reply)

    def test_failure_summary_distinguishes_submission_from_completion(self):
        executor = ToolExecutor(None, [], 'collaboration')
        for status, expected in [('pending', '等待接收方确认'), ('accepted', '运行结果需另行查询'),
                                 ('rejected', '已拒绝'), ('unknown', '结果未确认')]:
            executor.writes[('org.polaris.agent', 'send', '{}')] = ({'status': status}, False)
            reply = executor.failure_reply('请求超时')
            self.assertIn(expected, reply)
            self.assertNotIn('已确认执行成功', reply)

    def test_display_name_help_and_canonical_call_complete_full_reply(self):
        import json
        target = 'org.polaris.agent'
        component = Component()
        component.operation('sessions')(lambda: [{'title': '测试会话', 'online': True}])
        self.runtime.install(dict(id=target, name='Agent', version='1', platformApi=1,
                                  entry=dict(mode='inprocess')), lambda: component)
        self.runtime.start_component(target)
        self.runtime.grant('user', target, 'sessions')
        self.context = self.runtime.user_context('user')
        class DisplayNameModel:
            step = 0
            def complete(self, messages, tools, cancel, timeout=30):
                self.step += 1
                if self.step == 4:
                    result = json.loads(messages[-1]['content'])
                    assert result == [{'title': '测试会话', 'online': True}], result
                    return {'role': 'assistant', 'content': 'Agent 有一个在线测试会话'}
                name, args = [('components', {}), ('help', {'component': 'Agent'}),
                    ('call', {'component': 'org.polaris.agent', 'operation': 'sessions', 'arguments': {}})][self.step-1]
                return {'role': 'assistant', 'tool_calls': [{'id': str(self.step), 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(args)}}]}
        self.assistant.engine = WeixinAgent(DisplayNameModel())
        self.call('configure_agent', enabled=True, allowed=[{'target': target, 'operation': 'sessions'}])
        self.receive(timestamp=time.time()*1000)
        self.wait(lambda: len(delivered_history(self)) == 2)
        self.assertEqual(self.fake.sent[0]['item_list'][0]['text_item']['text'], 'Agent 有一个在线测试会话')

    def test_component_aliases_do_not_bypass_allowlist_or_ambiguity(self):
        owner = self
        class Ctx:
            def call(self, target, operation, params):
                return owner.runtime.client('user').call(target, operation, params, context=owner.context)
        executor = ToolExecutor(Ctx(), [{'target': 'toy', 'operation': 'echo'}], 'collaboration')
        self.assertEqual(executor.resolve_component('TOY'), 'toy')
        with self.assertRaises(PlatformError) as unknown:
            executor.resolve_component('No such component')
        self.assertEqual(unknown.exception.code, 'component_not_found')
        with self.assertRaises(PlatformError) as denied:
            executor.resolve_component('User')
        self.assertEqual(denied.exception.code, 'denied')
        self.runtime.install(dict(id='duplicate', name='Toy', version='1', platformApi=1,
                                  entry=dict(mode='inprocess')), Component)
        self.runtime.start_component('duplicate')
        executor.execute('components', {}, threading.Event())
        with self.assertRaises(PlatformError) as ambiguous:
            executor.resolve_component('Toy')
        self.assertEqual(ambiguous.exception.code, 'ambiguous_component')
        self.assertEqual(executor.resolve_component('toy'), 'toy')
    def test_old_missing_and_duplicate_messages_never_trigger(self):
        self.receive('before', time.time()*1000)
        self.enable()
        self.receive('missing'); self.receive('old-time', 1000)
        self.receive('before', time.time()*1000)
        time.sleep(.05)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.fake.sent, [])
    def test_allowlist_and_revalidation(self):
        class Ctx:
            def call(_, target, operation, params):
                return self.runtime.client('user').call(target, operation, params, context=self.context)
        executor = ToolExecutor(Ctx(), [{'target':'toy','operation':'echo'}], 'collaboration')
        cancel = threading.Event()
        with self.assertRaises(PlatformError):
            executor.execute('call', {'component':'toy','operation':'echo'}, cancel)
        executor.execute('help', {'component':'toy'}, cancel)
        self.runtime.revoke('user','toy','echo')
        with self.assertRaises(PlatformError):
            executor.execute('call', {'component':'toy','operation':'echo','arguments':{'text':'bad'}}, cancel)
        with self.assertRaises(PlatformError):
            executor.execute('help', {'component':'collaboration'}, cancel)
        self.assertEqual(self.effects, [])
    def test_write_deduplicates_success_and_unknown_within_turn(self):
        calls = []
        class Ctx:
            def call(_, target, operation, params):
                if operation == 'describe':
                    return {'id':'toy','description':{'operations':{'echo':{'access':'write'}}}}
                calls.append(params)
                if params.get('fail'):
                    raise TimeoutError()
                return {'ok': True}
        executor = ToolExecutor(Ctx(), [{'target':'toy','operation':'echo'}], 'collaboration')
        cancel = threading.Event()
        executor.execute('help', {'component':'toy'}, cancel)
        args = {'component':'toy','operation':'echo','arguments':{'text':'same'}}
        self.assertEqual(executor.execute('call', args, cancel), executor.execute('call', args, cancel))
        args['arguments'] = {'fail': True}
        with self.assertRaises(TimeoutError): executor.execute('call', args, cancel)
        with self.assertRaises(PlatformError): executor.execute('call', args, cancel)
        self.assertEqual(len(calls), 2)

    def test_legacy_auto_reply_keeps_selection_until_new_provider_is_authorized(self):
        import json
        from polaris_weixin.controller import AgentController
        path = self.component.agent.path
        allowed = [{'target':'toy','operation':'echo'}]
        path.write_text(json.dumps(dict(version=1, enabled=True, allowed=allowed)), encoding='utf8')
        restored = AgentController(self.component.client, path, lambda: None)
        restored.bind_resume(lambda: self.fail('old grants must not be reused for the new provider'))
        self.assertTrue(restored.desired_enabled)
        self.assertFalse(restored.enabled)
        self.assertEqual(restored.allowed, allowed)
        self.assertIn('保存一次', restored.error)

    def test_settings_restart_remembers_choice_but_context_never_saved(self):
        from polaris_weixin.controller import AgentController
        self.enable()
        restored = AgentController(self.component.client, self.component.agent.path, lambda: None)
        self.assertFalse(restored.enabled)
        self.assertTrue(restored.settings()['resumePending'])
        self.assertIsNone(restored.ctx)
        self.assertEqual(restored.allowed, [{'target':'toy','operation':'echo'}])
        self.assertEqual(set(__import__('json').loads(restored.path.read_text())), {'version','allowed','enabled'})
        self.call('configure_agent', enabled=False)
        disabled = AgentController(self.component.client, self.component.agent.path, lambda: None)
        self.assertFalse(disabled.settings()['resumePending'])

    def test_restart_without_any_view_restores_reply_and_reconnects(self):
        self.enable()
        self.runtime.stop_component(self.identity, persist=False)
        self.runtime.start_component(self.identity)
        self.component = self.runtime.component_instance(self.identity)
        self.assistant.engine = WeixinAgent(Model())
        self.component.client.transport = self.fake
        self.component.client.account = dict(ACCOUNT)
        self.component.client.credentials = Credentials(ACCOUNT)
        self.call('connect')
        self.assertIsNone(self.component._presentation)
        self.receive('after-restart', time.time()*1000)
        self.wait(lambda: len(delivered_history(self)) == 2)
        self.assertEqual(self.effects, ['ok'])
        self.assertEqual(len(self.fake.sent), 1)
        self.call('disconnect')
        self.call('connect')
        self.assistant.engine = WeixinAgent(Model())
        self.receive('after-reconnect', time.time()*1000)
        self.wait(lambda: len(self.fake.sent) == 2)
        self.assertIsNone(self.component._presentation)

    def test_expired_rescan_restores_reply_without_view(self):
        self.enable()
        with self.component.client._lock:
            self.component.client.expire()
        self.assertTrue(self.component.agent.desired_enabled)
        self.assertFalse(self.component.agent.enabled)
        self.call('login')
        self.wait(lambda: self.component.client.state == 'connected')
        with self.fake.lock:
            self.fake.incoming.append(dict(message_id='after-rescan', message_type=1,
                message_state=2, from_user_id='fake-user', context_token='new-context',
                create_time_ms=time.time()*1000,
                item_list=[{'type':1,'text_item':{'text':'新消息'}}]))
        self.wait(lambda: len(delivered_history(self)) == 2)
        self.assertEqual(self.effects, ['ok'])
        self.assertEqual(len(self.fake.sent), 1)
        self.assertTrue(self.component.agent.enabled)
        self.assertIsNone(self.component._presentation)

    def test_restart_expired_then_rescan_restores_saved_reply(self):
        from polaris_weixin.credentials import CredentialStore
        self.enable()
        credentials = CredentialStore(Path(self.directory.name) / 'credentials.bin')
        self.component.client.credentials = credentials
        with self.component.client._lock:
            self.component.client.expire()
        self.runtime.stop_component(self.identity, persist=False)
        self.runtime.start_component(self.identity)
        self.component = self.runtime.component_instance(self.identity)
        self.assistant.engine = WeixinAgent(Model())
        self.component.client.transport = self.fake
        self.assertEqual(self.component.client.state, 'expired')
        self.assertTrue(self.component.agent.desired_enabled)
        self.call('login')
        self.wait(lambda: self.component.client.state == 'connected')
        self.receive('restart-rescan', time.time()*1000)
        self.wait(lambda: len(delivered_history(self)) == 2)
        self.assertEqual(self.effects, ['ok'])
        self.assertEqual(len(self.fake.sent), 1)
        self.assertIsNone(self.component._presentation)

    def test_disabled_reply_does_not_restore_after_restart(self):
        self.enable()
        self.call('configure_agent', enabled=False)
        self.runtime.stop_component(self.identity, persist=False)
        self.runtime.start_component(self.identity)
        self.component = self.runtime.component_instance(self.identity)
        self.component.client.transport = self.fake
        self.component.client.account = dict(ACCOUNT)
        self.component.client.state = 'connected'
        self.receive(timestamp=time.time()*1000)
        self.assertFalse(self.component.agent.enabled)
        self.assertEqual(self.fake.sent, [])

    def test_view_refresh_never_manufactures_missing_task_authority(self):
        from PySide6.QtCore import QCoreApplication
        from polaris_weixin.controller import AgentController
        from polaris_weixin.view import Bridge
        app = QCoreApplication.instance() or QCoreApplication([])
        self.enable()
        self.component.agent = AgentController(self.component.client, self.component.agent.path,
                                               lambda: None, 'collaboration', self.identity, self.component.reply_provider)
        owner = self
        class Context:
            calls = []
            def call(self, target, operation, params):
                self.calls.append(operation)
                return owner.call(operation, **params)
        context = Context()
        bridge = Bridge(self.component, context, None)
        try:
            self.assertTrue(bridge.refresh())
            self.assertTrue(bridge.refresh())
            self.assertFalse(self.component.agent.enabled)
            self.assertTrue(self.component.agent.settings()['resumePending'])
            self.assertNotIn('configure_agent', context.calls)
        finally:
            bridge._timer.stop()

    def test_actual_manifest_identity_cannot_be_selected_or_called(self):
        self.assertEqual(self.component.agent.own_id, self.identity)
        catalog = self.call('agent_catalog')['components']
        self.assertNotIn(self.identity, [r['id'] for r in catalog])
        with self.assertRaises(PlatformError):
            self.call('configure_agent', enabled=True, allowed=[{'target':self.identity,'operation':'send'}])
        self.component.agent.allowed = [{'target':self.identity,'operation':'configure_agent'}]
        with self.assertRaises(PlatformError):
            self.call('configure_agent', enabled=True)
        executor = ToolExecutor(None, [{'target':self.identity,'operation':'send'}], 'collaboration', self.identity)
        with self.assertRaises(PlatformError):
            executor.execute('help', {'component':self.identity}, threading.Event())

    def test_logout_and_account_change_clear_previous_history(self):
        self.enable()
        seed_history(self, [{'role':'user','content':'previous account secret'}])
        self.call('logout')
        self.assertEqual(delivered_history(self), [])
        self.assertFalse(self.component.agent.enabled)
        seed_history(self, [{'role':'user','content':'old'}])
        self.component.client.on_account_changed()
        self.assertEqual(delivered_history(self), [])

    def test_new_conversation_preserves_explicit_context_and_enabled_state(self):
        self.enable()
        controller = self.component.agent
        old_cancel, old_ctx = controller.cancel, controller.ctx
        seed_history(self, [{'role':'user','content':'old'}])
        self.call('new_conversation')
        self.assertTrue(old_cancel.is_set())
        self.assertTrue(controller.enabled)
        self.assertIs(controller.ctx, old_ctx)
        self.assertEqual(delivered_history(self), [])
        self.assertEqual(len(controller.queue), 0)

    def test_invalid_key_enable_is_safe_platform_error(self):
        from unittest.mock import patch
        self.assistant.engine = None
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY':'invalid private key'}):
            with self.assertRaises(PlatformError) as caught:
                self.enable()
        self.assertIn('DEEPSEEK_API_KEY', str(caught.exception))
        self.assertNotIn('invalid private key', str(caught.exception))
        self.assertFalse(self.component.agent.enabled)

    def test_revoked_send_prevents_reply_and_history_commit(self):
        self.enable()
        self.runtime.revoke('user', self.identity, 'send')
        self.receive(timestamp=time.time()*1000)
        self.wait(lambda: not self.component.agent.enabled)
        self.assertEqual(self.effects, ['ok'])
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(delivered_history(self), [])
        self.assertTrue(self.component.agent.error)

    def test_revoked_provider_permission_never_reaches_the_model(self):
        model = self.assistant.engine.model
        self.enable()
        self.runtime.revoke('user', 'assistant', 'reply')
        self.receive(timestamp=time.time()*1000)
        self.wait(lambda: not self.component.agent.enabled)
        self.assertEqual(model.step, 0)
        self.assertEqual(self.fake.sent, [])
        self.assertIn('重新保存', self.component.agent.error)

    def test_revoked_get_prevents_forwarding_text_to_model(self):
        model = self.assistant.engine.model
        self.enable()
        self.runtime.revoke('user', self.identity, 'get')
        self.receive(timestamp=time.time()*1000)
        self.wait(lambda: not self.component.agent.enabled)
        self.assertEqual(model.step, 0)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.fake.sent, [])
        self.assertTrue(self.component.agent.error)

    def test_disconnect_cancels_late_model(self):
        started, release = threading.Event(), threading.Event()
        class Slow:
            def run(_, text, history, tools, cancel):
                from components.assistant.loop import AgentResult
                started.set(); release.wait(2)
                return AgentResult('迟到', [])
        self.assistant.engine = Slow()
        self.enable(); self.receive(timestamp=time.time()*1000)
        self.assertTrue(started.wait(1))
        self.call('disconnect'); release.set()
        self.wait(lambda: not self.component.agent.busy)
        self.assertFalse(self.call('agent_settings')['enabled'])
        self.assertEqual(self.fake.sent, [])
    def test_unknown_send_not_retried_or_committed(self):
        original = self.fake.request
        def fail(origin, endpoint, body, **kwargs):
            if endpoint.endswith('sendmessage'):
                self.fake.sent.append(body['msg']); raise TimeoutError()
            return original(origin, endpoint, body, **kwargs)
        self.fake.request = fail
        self.enable(); self.receive(timestamp=time.time()*1000)
        self.wait(lambda: bool(self.call('agent_settings')['error']))
        self.assertEqual(len(self.fake.sent), 1)
        self.assertEqual(delivered_history(self), [])
        self.assertFalse(self.call('agent_settings')['enabled'])

if __name__ == '__main__': unittest.main()
