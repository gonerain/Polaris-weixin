import json
from pathlib import Path
import sys
import threading
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/weixin'))
from polaris_weixin.agent import WeixinAgent
from polaris_weixin.model import DeepSeek, ModelError


class FakeModel:
    def __init__(self, replies):
        self.replies = iter(replies)
    def complete(self, *args, **kwargs):
        return next(self.replies)


def tool(name, args):
    return {'id': name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


class Executor:
    def __init__(self):
        self.calls = []
    def execute(self, name, args, cancel):
        self.calls.append((name, args))
        return {'ok': True}


class CoreTests(unittest.TestCase):
    def test_large_tool_results_reach_generation_and_review_with_redaction(self):
        key = 'sk-full-result-test-secret'
        payload = {'rows': [{'id': index, 'text': '正文' * 800 + str(index)}
                            for index in range(25)], 'secret': key}
        expected = {**payload, 'secret': '[已隐藏密钥]'}

        class RecordingModel(FakeModel):
            def redact(self, value):
                return DeepSeek.redact(self, value)
            def complete(self, messages, *args, **kwargs):
                self.messages = list(messages)
                return super().complete(messages, *args, **kwargs)
            def review(self, request, draft, evidence, cancel, timeout=30):
                self.evidence = evidence
                return {'needs_tools': False, 'reply': draft, 'reason': ''}

        class LargeResult(Executor):
            def execute(self, name, args, cancel):
                return payload

        model = RecordingModel([{'tool_calls': [tool('components', {})]},
                                {'content': '已查询'}])
        model._key = key
        WeixinAgent(model).run('查询', [], LargeResult(), threading.Event())
        content = next(row['content'] for row in model.messages if row.get('role') == 'tool')
        self.assertGreater(len(content), 12000)
        self.assertEqual(json.loads(content), expected)
        self.assertEqual(model.evidence[0]['result'], expected)
        self.assertNotIn(key, content)
        self.assertNotIn(key, json.dumps(model.evidence))

    def test_review_keeps_original_context_after_tool_rounds(self):
        class Reviewed(FakeModel):
            def review(self, request, draft, evidence, cancel, timeout=30):
                self.request = json.loads(request)
                return {'needs_tools':False,'reply':draft,'reason':''}
        history=[{'role':'user','content':'请把上一段转发给目标'},
                 {'role':'assistant','content':'请提供正文和发送模式'}]
        model=Reviewed([{'tool_calls':[tool('components',{})]}]*6+[{'content':'已处理'}])
        WeixinAgent(model).run('start，待转发的正文',history,Executor(),threading.Event())
        self.assertEqual(model.request['recent_messages'][:2],history)
        self.assertEqual(model.request['current'],'start，待转发的正文')

    def test_unexecuted_success_draft_is_checked_before_sending(self):
        class ReviewedModel(FakeModel):
            def review(self, request, draft, evidence, cancel, timeout=30):
                if not evidence:
                    self.asserted_empty_evidence = True
                    return {'needs_tools': True, 'reply': '', 'reason': '没有实际取消标记'}
                return {'needs_tools': False, 'reply': draft, 'reason': ''}
        model = ReviewedModel([{'role': 'assistant', 'content': '已取消标记'},
            {'tool_calls': [tool('help', {'component': 'x'})]},
            {'tool_calls': [tool('call', {'component': 'x', 'operation': 'unmark', 'arguments': {}})]},
            {'content': '已核实取消标记'}])
        executor = Executor()
        result = WeixinAgent(model).run('取消标记', [], executor, threading.Event())
        self.assertTrue(model.asserted_empty_evidence)
        self.assertEqual(result.text, '已核实取消标记')
        self.assertEqual([name for name, _ in executor.calls], ['help', 'call'])

    def test_catalog_reply_review_receives_directory_evidence(self):
        class Reviewed(FakeModel):
            def review(self, request, draft, evidence, cancel, timeout=30):
                self.evidence = evidence
                return {'needs_tools': False, 'reply': draft, 'reason': ''}
        model = Reviewed([{'tool_calls': [tool('components', {})]}, {'content': '可用组件'}])
        result = WeixinAgent(model).run('你能做什么', [], Executor(), threading.Event())
        self.assertEqual(result.text, '可用组件')
        self.assertEqual(model.evidence[0]['tool'], 'components')

    def test_repeated_unsupported_claims_fail_instead_of_being_sent(self):
        class Unsupported(FakeModel):
            def review(self, *args, **kwargs):
                return {'needs_tools': True, 'reply': '', 'reason': '没有执行证据'}
        model = Unsupported([{'role': 'assistant', 'content': '已完成'}] * 3)
        with self.assertRaisesRegex(ModelError, '未能确认操作结果'):
            WeixinAgent(model).run('执行', [], Executor(), threading.Event())

    def test_two_tool_corrections_still_review_final_evidence_without_resubmitting(self):
        from polaris_weixin.controller import ToolExecutor
        class Context:
            writes = 0
            queries = 0
            def call(self, target, operation, arguments):
                if operation == 'describe':
                    return {'id':'queue','description':{'operations':{
                        'submit':{'access':'write'},'receipt':{'access':'read'}}}}
                if operation == 'submit':
                    self.writes += 1
                    return {'status':'pending','id':'receipt'}
                if operation == 'receipt':
                    self.queries += 1
                    return {'status':'pending' if self.queries == 1 else 'accepted'}
                raise AssertionError(operation)
        class Reviewed(FakeModel):
            reviews = 0
            def review(self, request, draft, evidence, cancel, timeout=30):
                self.reviews += 1
                if self.reviews <= 2:
                    return {'needs_tools':True,'reply':'','reason':'还需查询回执'}
                self.final_request = json.loads(request)
                assert evidence[-1]['result']['status'] == 'accepted'
                return {'needs_tools':False,'reply':'已接收，运行结果尚未确认。','reason':''}
        submit = tool('call',{'component':'queue','operation':'submit','arguments':{'text':'one'}})
        query = tool('call',{'component':'queue','operation':'receipt','arguments':{'id':'receipt'}})
        model = Reviewed([
            {'tool_calls':[tool('help',{'component':'queue'})]},
            {'tool_calls':[submit]}, {'content':'已提交。'},
            {'tool_calls':[query]}, {'content':'仍在等待。'},
            {'tool_calls':[submit,query]}, {'content':'已接收，运行结果尚未确认。'}])
        context = Context()
        executor = ToolExecutor(context,[{'target':'queue','operation':op} for op in ('submit','receipt')],'directory')
        result = WeixinAgent(model).run('发送并确认接收',[],executor,threading.Event())
        self.assertEqual(result.text,'已接收，运行结果尚未确认。')
        self.assertEqual(model.reviews,3)
        self.assertFalse(model.final_request['can_use_tools'])
        self.assertEqual(context.writes,1)
        self.assertEqual(context.queries,2)

    def test_final_tool_budget_cannot_request_impossible_extra_tools(self):
        class Reviewed(FakeModel):
            reviews = 0
            completions = 0
            def complete(self,*args,**kwargs):
                self.completions += 1
                return super().complete(*args,**kwargs)
            def review(self,request,*args,**kwargs):
                self.reviews += 1
                assert json.loads(request)['can_use_tools'] is False
                return {'needs_tools':True,'reply':'','reason':'还需查询'}
        model = Reviewed([{'tool_calls':[tool('components',{})]}]*6+[{'content':'尚未确认。'}])
        with self.assertRaisesRegex(ModelError,'未能确认操作结果'):
            WeixinAgent(model).run('查询',[],Executor(),threading.Event())
        self.assertEqual(model.completions,7)
        self.assertEqual(model.reviews,1)

    def test_final_budget_review_can_return_honest_pending_acknowledgement(self):
        class Pending(Executor):
            def execute(self,name,args,cancel):
                super().execute(name,args,cancel)
                return {'status':'pending','id':'receipt'} if name == 'call' else {'ok':True}
        class Reviewed(FakeModel):
            def review(self,request,draft,evidence,*args,**kwargs):
                assert json.loads(request)['can_use_tools'] is False
                assert evidence[-1]['result']['status'] == 'pending'
                return {'needs_tools':False,'reply':'已提交，尚未确认接收。','reason':''}
        replies = [{'tool_calls':[tool('help',{'component':'queue'})]}]
        replies += [{'tool_calls':[tool('components',{})]}]*4
        replies += [{'tool_calls':[tool('call',{'component':'queue','operation':'submit','arguments':{'text':'one'}})]},
                    {'content':'已完成。'}]
        executor = Pending()
        result = WeixinAgent(Reviewed(replies)).run('执行',[],executor,threading.Event())
        self.assertEqual(result.text,'已提交，尚未确认接收。')
        self.assertEqual(sum(name == 'call' for name,_ in executor.calls),1)

    def test_cancel_during_review_never_returns_answer(self):
        cancel = threading.Event()
        class CancelReview(FakeModel):
            def review(self, *args, **kwargs):
                cancel.set()
                return {'needs_tools': False, 'reply': '迟到的回复', 'reason': ''}
        with self.assertRaises(ModelError):
            WeixinAgent(CancelReview([{'content': '完成'}])).run('查询', [], Executor(), cancel)

    def test_review_transport_forces_structured_verdict_and_rejects_invalid_reply(self):
        class Response:
            def __init__(self, message): self.data = json.dumps({'choices': [{'message': message}]}).encode()
            def read(self, size):
                result, self.data = self.data, b''
                return result
            def close(self): pass
        class Opener:
            message = {'tool_calls': [tool('review_reply', {'needs_tools': True, 'reply': '', 'reason': '缺少实际标记'})]}
            def open(self, request, timeout):
                self.payload = json.loads(request.data)
                return Response(self.message)
        opener = Opener()
        model = DeepSeek('fake-key12345', opener)
        result = model.review('标记会话', '已标记', [], threading.Event())
        self.assertTrue(result['needs_tools'])
        self.assertEqual(opener.payload['tool_choice']['function']['name'], 'review_reply')
        opener.message = {'content': '已完成'}
        with self.assertRaisesRegex(ModelError, '核对失败'):
            model.review('标记会话', '已标记', [], threading.Event())

    def test_platform_denial_keeps_code_and_exact_missing_permission(self):
        from polaris_platform.protocol import PlatformError
        class Denied:
            def execute(self, *args):
                raise PlatformError('permission_required', 'Explicit permission required',
                                    {'requester': 'canvas', 'capability': ['installation', 'operation', 'sessions', 1, 'read', 1]})
        class RecordingModel(FakeModel):
            def complete(self, messages, *args, **kwargs):
                self.messages = list(messages)
                return super().complete(messages, *args, **kwargs)
        model = RecordingModel([{'tool_calls': [tool('help', {'component': 'org.polaris.agent'})]}, {'content': '缺少 sessions 读取权限'}])
        WeixinAgent(model).run('查看会话', [], Denied(), threading.Event())
        error = json.loads(next(row['content'] for row in model.messages if row.get('role') == 'tool'))
        self.assertEqual(error['code'], 'permission_required')
        self.assertEqual(error['requester'], 'canvas')
        self.assertEqual(error['operation'], 'sessions')
        self.assertIn('不是调用参数错误', error['error'])

    def test_help_gate_and_order(self):
        model = FakeModel([
            {'role': 'assistant', 'tool_calls': [tool('call', {'component': 'x'})]},
            {'role': 'assistant', 'tool_calls': [tool('help', {'component': 'x'}),
                tool('call', {'component': 'x', 'operation': 'get', 'arguments': {}})]},
            {'role': 'assistant', 'content': '完成'}])
        executor = Executor()
        result = WeixinAgent(model).run('查询', [], executor, threading.Event())
        self.assertEqual([x[0] for x in executor.calls], ['help', 'call'])
        self.assertEqual(result.text, '完成')
        self.assertEqual([x['role'] for x in result.history], ['user', 'assistant'])

    def test_unknown_tool_and_nonobject(self):
        model = FakeModel([{'tool_calls': [tool('__class__', {}), tool('components', [])]}, {'content': '好'}])
        executor = Executor()
        self.assertEqual(WeixinAgent(model).run('a', [], executor, threading.Event()).text, '好')
        self.assertEqual(executor.calls, [])

    def test_cancel_does_not_call(self):
        cancel = threading.Event(); cancel.set()
        executor = Executor()
        with self.assertRaises(ModelError):
            WeixinAgent(FakeModel([])).run('a', [], executor, cancel)
        self.assertEqual(executor.calls, [])

    def test_output_bound(self):
        result = WeixinAgent(FakeModel([{'content': '字' * 5000}])).run('a', [], Executor(), threading.Event())
        self.assertLessEqual(len(result.text), 2000)

    def test_fake_transport_and_redaction(self):
        key = 'sk-fakekey123456'
        class Response:
            def __init__(self):
                self.done = False
            def read(self, size):
                if self.done: return b''
                self.done = True
                return json.dumps({'choices': [{'message': {'content': key, 'reasoning_content': 'hidden'}}]}).encode()
            def close(self): pass
        class Opener:
            def open(self, request, timeout):
                self.payload = json.loads(request.data)
                return Response()
        opener = Opener()
        result = DeepSeek(key, opener).complete([], [], threading.Event())
        self.assertNotIn(key, str(result))
        self.assertNotIn('reasoning_content', result)
        self.assertEqual(opener.payload['model'], 'deepseek-flash')
        self.assertFalse(opener.payload['stream'])

    def test_empty_or_invalid_answer_is_error(self):
        for content in (None, '', '   ', {}, 1):
            with self.subTest(content=content), self.assertRaises(ModelError):
                WeixinAgent(FakeModel([{'content': content}])).run('a', [], Executor(), threading.Event())

    def test_model_error_propagates(self):
        class Broken:
            def complete(self, *args, **kwargs):
                raise ModelError('失败')
        with self.assertRaises(ModelError):
            WeixinAgent(Broken()).run('a', [], Executor(), threading.Event())

    def test_late_model_reply_cancelled(self):
        cancel = threading.Event()
        class Late:
            def complete(self, *args, **kwargs):
                cancel.set()
                return {'content': '迟到的正文'}
        with self.assertRaises(ModelError):
            WeixinAgent(Late()).run('a', [], Executor(), cancel)

    def test_interrupted_tool_propagates(self):
        class Interrupted:
            def execute(self, *args):
                raise InterruptedError('取消')
        with self.assertRaises(InterruptedError):
            WeixinAgent(FakeModel([{'tool_calls': [tool('components', {})]}])).run(
                'a', [], Interrupted(), threading.Event())

    def test_request_budget_raises(self):
        model = FakeModel([{'tool_calls': [tool('components', {})]}] * 7)
        with self.assertRaises(ModelError):
            WeixinAgent(model).run('a', [], Executor(), threading.Event())

    def test_last_tool_round_still_gets_final_reply_without_tools(self):
        class Model(FakeModel):
            def complete(self, messages, tools, *args, **kwargs):
                response = super().complete(messages, tools, *args, **kwargs)
                if response.get('content'):
                    self.final_tools = tools
                return response
        replies = [{'tool_calls': [tool('help', {'component': 'x'})]}] * 5
        replies += [{'tool_calls': [tool('call', {'component': 'x', 'operation': 'mark', 'arguments': {'session_id': 's'}})]},
                    {'content': '已标记'}]
        model, executor = Model(replies), Executor()
        result = WeixinAgent(model).run('标记', [], executor, threading.Event())
        self.assertEqual(result.text, '已标记')
        self.assertEqual(model.final_tools, [])
        self.assertEqual(sum(name == 'call' for name, _ in executor.calls), 1)

    def test_printable_key_and_whitespace(self):
        model = DeepSeek('  fake!key-123  ')
        self.assertEqual(model.redact('fake!key-123'), '[已隐藏密钥]')
        DeepSeek('x' * 4096)
        for key in ('x' * 4097, 'fake key-123', 'fake\x7fkey-123'):
            with self.assertRaises(ModelError):
                DeepSeek(key)

    def test_transport_rejects_invalid_types_and_late_cancel(self):
        for message in ({'content': {}}, {'content': 'ok', 'tool_calls': {}},
                        {'content': 'ok', 'tool_calls': ['bad']}):
            class Response:
                def __init__(self): self.done = False
                def read(self, size):
                    if self.done: return b''
                    self.done = True
                    return json.dumps({'choices': [{'message': message}]}).encode()
                def close(self): pass
            class Opener:
                def open(self, *args, **kwargs): return Response()
            with self.assertRaises(ModelError):
                DeepSeek('fake-key123', Opener()).complete([], [], threading.Event())
        cancel = threading.Event()
        class LateResponse:
            def read(self, size):
                cancel.set()
                return b''
            def close(self): pass
        class LateOpener:
            def open(self, *args, **kwargs): return LateResponse()
        with self.assertRaises(ModelError):
            DeepSeek('fake-key123', LateOpener()).complete([], [], cancel)

    def test_bad_key(self):
        with self.assertRaises(ModelError): DeepSeek('bad\nkey')


if __name__ == '__main__': unittest.main()
