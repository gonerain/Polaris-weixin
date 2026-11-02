"""Real Weixin + Agent + authority + socket bridge, isolated external services."""
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import unittest

if not os.environ.get('POLARIS_AGENT_REPO'):
    raise unittest.SkipTest('Set POLARIS_AGENT_REPO to run optional cross-repository tests')
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
sys.path.insert(0, str(Path(os.environ['POLARIS_AGENT_REPO']) / 'plugins/agent'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/weixin'))
import test_weixin_component as fixtures
from polaris_agent import Agent
from polaris_weixin.agent import WeixinAgent
from polaris_weixin.model import ModelError

TARGET = 'org.polaris.agent'
OPERATIONS = ('get', 'sessions', 'status', 'result', 'delivery', 'mark', 'unmark', 'send')


def tool(name, **args):
    return {'role': 'assistant', 'tool_calls': [{'id': name, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args)}}]}


def call(operation, **args):
    return tool('call', component=TARGET, operation=operation, arguments=args)


class ScriptedModel:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.observed = []
        self.failure = None

    def complete(self, messages, tools, cancel, timeout=30):
        self.observed.append(messages.copy())
        step = next(self.steps)
        try:
            return step(messages) if callable(step) else step
        except ModelError:
            raise
        except Exception as error:
            self.failure = error
            raise


class Host:
    """A socket peer standing in for an agent host, not for Agent itself."""
    def __init__(self, endpoint):
        info = json.loads(endpoint.read_text('utf-8'))
        self.socket = socket.create_connection((info['host'], info['port']), timeout=3)
        self.stream = self.socket.makefile('rb')
        self.lock = threading.Lock()
        self.commands = []
        self.error = None
        self.send(type='hello', token=info['token'], adapter='fixture', clientId='host')
        assert json.loads(self.stream.readline())['ok']
        self.send(type='event', event='session.snapshot', sessionId='fixture-a62bc0',
                  title='验收会话 a62bc0', canSend=True, liveness='host')
        self.send(type='request', requestId='barrier', operation='session.list', params={})
        self.row = json.loads(self.stream.readline())['result'][0]
        self.socket.settimeout(None)
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def send(self, **frame):
        with self.lock:
            self.socket.sendall((json.dumps(frame) + '\n').encode())

    def run(self):
        try:
            for line in self.stream:
                frame = json.loads(line)
                if frame.get('type') != 'command':
                    continue
                self.commands.append(frame)
                self.send(type='event', event='command.result', requestId=frame['requestId'], status='accepted', runId='fixture-run')
                self.send(type='event', event='run.started', sessionId=frame['sessionId'], runId='fixture-run')
                self.send(type='event', event='run.ended', sessionId=frame['sessionId'], runId='fixture-run',
                          status='completed', reply='测试宿主已完成：' + frame['text'])
        except OSError:
            pass
        except Exception as error:
            self.error = error

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()
        self.worker.join(3)
        self.stream.close()


class WeixinAgentEndToEndTests(unittest.TestCase):
    call = fixtures.WeixinComponentTests.call

    def setUp(self):
        fixtures.WeixinComponentTests.setUp(self)
        self.agent_component = Agent(data_dir=Path(self.directory.name) / 'agent')
        self.runtime.install(dict(id=TARGET, name='Agent', version='1', platformApi=1,
                                  entry=dict(mode='inprocess')), lambda: self.agent_component)
        self.runtime.start_component(TARGET)
        self.host = Host(self.agent_component.bridge.endpoint)
        self.session = self.host.row['id']
        for target, names in [(TARGET, OPERATIONS), ('collaboration', ('discover', 'describe'))]:
            for name in names:
                self.runtime.grant('user', target, name)
        self.context = self.runtime.user_context('user')
        self.component.client.account = dict(fixtures.ACCOUNT)
        self.component.client.credentials = fixtures.Credentials(fixtures.ACCOUNT)
        self.call('connect')
        self.wait(lambda: self.component.client.state == 'connected')
        self.sequence = 0

    def tearDown(self):
        self.component.agent.invalidate()
        self.wait(lambda: not self.component.agent.worker_running)
        self.host.close()
        fixtures.WeixinComponentTests.tearDown(self)

    def wait(self, predicate, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.01)
        self.assertTrue(predicate(), 'controller=' + repr(self.component.agent.settings()))

    def enable(self, model):
        self.model = model
        self.component.agent.agent = WeixinAgent(model)
        self.call('configure_agent', enabled=True,
                  allowed=[{'target': TARGET, 'operation': name} for name in OPERATIONS])

    def incoming(self, text):
        self.sequence += 1
        with self.fake.lock:
            self.fake.incoming.append(dict(message_id='e2e-' + str(self.sequence), message_type=1,
                message_state=2, from_user_id='fake-user', context_token='fake-context',
                create_time_ms=time.time()*1000, item_list=[{'type': 1, 'text_item': {'text': text}}]))

    def reply(self, count=1):
        replies = lambda: [m for m in self.fake.sent if not m['item_list'][0]['text_item']['text'].startswith('【任务结束】')]
        self.wait(lambda: len(replies()) >= count and not self.component.agent.busy)
        if isinstance(self.model, ScriptedModel) and self.model.failure:
            raise self.model.failure
        self.assertEqual(len(replies()), count)
        self.assertTrue(self.component.agent.enabled)
        self.assertFalse(self.component.agent.error)
        self.assertIsNone(self.host.error)
        return replies()[-1]['item_list'][0]['text_item']['text']

    def test_query_mark_status_unmark_through_real_components(self):
        def choose(messages):
            rows = json.loads(messages[-1]['content'])
            self.assertEqual(rows[0]['id'], self.session)
            return call('mark', session_id=rows[0]['id'])
        def marked(messages):
            self.assertTrue(json.loads(messages[-1]['content'])['marked'])
            return call('status', session_id=self.session)
        def status(messages):
            self.assertTrue(json.loads(messages[-1]['content'])['marked'])
            return {'content': '已标记验收会话'}
        model = ScriptedModel([tool('components'), tool('help', component='Agent'), call('sessions'),
                               choose, marked, status, tool('help', component=TARGET),
                               call('unmark', session_id=self.session), {'content': '已取消标记'}])
        self.enable(model)
        self.incoming('标记 a62bc0 会话')
        self.assertEqual(self.reply(), '已标记验收会话')
        self.assertTrue(self.agent_component.status(self.session)['marked'])
        self.incoming('取消刚才的标记')
        self.assertEqual(self.reply(2), '已取消标记')
        self.assertFalse(self.agent_component.status(self.session)['marked'])
        self.assertEqual(len(self.component.agent.history), 4)

    def test_mark_on_last_round_and_model_timeout_after_write(self):
        for index, final in enumerate(({'content': '标记完成'}, self.model_timeout)):
            model = ScriptedModel([tool('components'), tool('help', component='Agent'),
                call('sessions'), call('status', session_id=self.session), call('get'),
                call('mark', session_id=self.session), final])
            self.enable(model)
            self.incoming('标记会话')
            reply = self.reply(index + 1)
            self.assertTrue(self.agent_component.status(self.session)['marked'])
            self.assertIn('标记' if index == 0 else '已确认执行成功', reply)

    @staticmethod
    def model_timeout(messages):
        raise ModelError('请求超时')

    def test_send_delivery_result_and_duplicate_write(self):
        def await_result(messages):
            receipt = json.loads(messages[-1]['content'])
            self.wait(lambda: self.agent_component.delivery(receipt['id'])['status'] == 'accepted')
            self.wait(lambda: self.agent_component.result(self.session) is not None)
            return call('delivery', delivery_id=receipt['id'])
        def verify_delivery(messages):
            self.assertEqual(json.loads(messages[-1]['content'])['status'], 'accepted')
            return call('result', session_id=self.session)
        def verify_result(messages):
            result = json.loads(messages[-1]['content'])
            self.assertEqual(result['status'], 'completed')
            self.assertIn('测试任务', result['reply'])
            return {'content': '测试任务已完成'}
        send = call('send', session_id=self.session, text='测试任务', mode='start', delivery_id='fixture-delivery')
        combined = {'role': 'assistant', 'tool_calls': call('mark', session_id=self.session)['tool_calls'] +
                    send['tool_calls'] + send['tool_calls']}
        self.enable(ScriptedModel([tool('help', component='Agent'), combined, await_result,
                                   verify_delivery, verify_result]))
        self.incoming('标记并发送测试任务，查看结果')
        self.assertEqual(self.reply(), '测试任务已完成')
        self.assertEqual(len(self.host.commands), 1)
        self.assertEqual(self.host.commands[0]['sessionId'], 'fixture-a62bc0')

    def test_revoked_mark_is_reported_and_does_not_write(self):
        def denied(messages):
            error = json.loads(messages[-1]['content'])
            self.assertEqual(error['code'], 'permission_required')
            self.assertEqual(error['operation'], 'mark')
            return {'content': '缺少标记权限'}
        self.enable(ScriptedModel([tool('help', component='Agent'), call('mark', session_id=self.session), denied]))
        self.runtime.revoke('user', TARGET, 'mark')
        self.incoming('标记会话')
        self.assertEqual(self.reply(), '缺少标记权限')
        self.assertFalse(self.agent_component.status(self.session)['marked'])

    def test_offline_host_send_reports_error_without_false_completion(self):
        self.agent_component.mark(self.session)
        self.host.close()
        self.wait(lambda: not self.agent_component.status(self.session)['online'])
        def offline(messages):
            result = json.loads(messages[-1]['content'])
            self.assertIn('error', result)
            return {'content': '会话离线，消息未发送'}
        self.enable(ScriptedModel([tool('help', component='Agent'),
            call('send', session_id=self.session, text='测试', mode='start'), offline]))
        self.incoming('发送测试')
        self.assertEqual(self.reply(), '会话离线，消息未发送')
        self.assertEqual(self.host.commands, [])
        self.assertEqual(self.agent_component.store.deliveries, {})

    def test_long_result_remains_parseable_and_identifies_truncation(self):
        self.agent_component.store.run_event(self.session, 'long', 'completed', '长回复"\\\n' * 5000)
        def inspect_result(messages):
            content = messages[-1]['content']
            self.assertLessEqual(len(content), 12000)
            result = json.loads(content)
            self.assertTrue(result['truncated'])
            return {'content': '结果较长，已截取'}
        self.enable(ScriptedModel([tool('help', component='Agent'), call('result', session_id=self.session), inspect_result]))
        self.incoming('读取长结果')
        self.assertEqual(self.reply(), '结果较长，已截取')

    def test_online_host_without_send_capability_cannot_receive_commands(self):
        self.agent_component.store.snapshot('fixture', 'host', 'fixture-a62bc0', canSend=False)
        self.agent_component.mark(self.session)
        def unavailable(messages):
            self.assertIn('error', json.loads(messages[-1]['content']))
            return {'content': '会话在线，但宿主不支持接收消息'}
        self.enable(ScriptedModel([tool('help', component='Agent'),
            call('send', session_id=self.session, text='测试', mode='start'), unavailable]))
        self.incoming('发送测试')
        self.assertIn('不支持', self.reply())
        self.assertTrue(self.agent_component.status(self.session)['online'])
        self.assertFalse(self.agent_component.status(self.session)['canSend'])
        self.assertEqual(self.host.commands, [])

    @unittest.skipUnless(os.environ.get('POLARIS_WEIXIN_LIVE_MODEL') == '1', 'Opt-in real DeepSeek acceptance')
    def test_real_model_forwards_answer_and_previous_text_without_reconfirmation(self):
        from polaris_weixin.model import DeepSeek
        wait = self.wait
        self.wait = lambda predicate, timeout=100: wait(predicate,timeout)
        self.enable(DeepSeek())
        self.agent_component.mark(self.session)
        self.agent_component.store.snapshot('fixture','host','fixture-a62bc0',title='合成验收会话 SAMPLE-001',canSend=True)
        body='合成测试正文：我在考虑给演示项目增加一个天气面板，用说明文件记录设计，因为示例客户端不方便整理任务'
        question=[{'role':'user','content':'给合成验收会话 SAMPLE-001 发送消息'},
                  {'role':'assistant','content':'目标已标记且在线。需要我发什么内容？请给出文本，并说明用开始新一轮（start）还是补充当前一轮（steer）。'}]
        revised='合成测试正文：暂缓增加面板，先整理演示目录。'
        cases=[(question,'start，'+body,body),
               (question+[{'role':'user','content':'start，'+body},
                          {'role':'assistant','content':'你这个想法是在讨论架构方向；请提供具体正文和目标，我再执行。'}],
                '我让你直接发给合成验收会话 SAMPLE-001',body),
               (question+[{'role':'user','content':'新起一轮，正文是：'+body},
                          {'role':'assistant','content':'已了解目标和正文。'}],
                '目标不变，正文改为“'+revised+'”，现在发送。',revised)]
        for index,(history,prompt,expected) in enumerate(cases,1):
            self.component.agent.history=history
            self.incoming(prompt)
            reply=self.reply(index)
            self.assertEqual(len(self.host.commands),index,reply)
            command=self.host.commands[-1]
            self.assertEqual(command['mode'],'start',reply)
            self.assertEqual(command['text'],expected,reply)
        self.component.agent.history=question+[{'role':'user','content':'start，'+body}]
        self.incoming('先不要发送，我只是想讨论演示面板使用蓝色还是灰色，简短说说你的看法。')
        reply=self.reply(len(cases)+1)
        self.assertEqual(len(self.host.commands),len(cases),reply)

    @unittest.skipUnless(os.environ.get('POLARIS_WEIXIN_LIVE_MODEL') == '1', 'Opt-in real DeepSeek acceptance')
    def test_real_model_multiturn_queries_marks_sends_and_unmarks(self):
        from polaris_weixin.model import DeepSeek
        wait = self.wait
        self.wait = lambda predicate, timeout=100: wait(predicate, timeout)
        self.enable(DeepSeek())
        steps = [
            ('查看 Agent 的活跃会话', lambda: len(self.agent_component.sessions()) == 1),
            ('标记 a62bc0 这个会话', lambda: self.agent_component.status(self.session)['marked']),
            ('给刚才的会话发送“WX-AGENT-E2E”，开始新一轮', lambda: len(self.host.commands) == 1),
            ('查看刚才投递的结果和最新运行回复', lambda: self.agent_component.result(self.session)['status'] == 'completed'),
            ('取消 a62bc0 会话的标记', lambda: not self.agent_component.status(self.session)['marked']),
            ('再标记 a62bc0 并查询它的状态', lambda: self.agent_component.status(self.session)['marked']),
        ]
        for index, (prompt, check) in enumerate(steps, 1):
            with self.subTest(prompt=prompt):
                self.incoming(prompt)
                reply = self.reply(index)
                self.assertTrue(check(), reply)
        self.assertEqual(len(self.host.commands), 1)


if __name__ == '__main__':
    unittest.main()
