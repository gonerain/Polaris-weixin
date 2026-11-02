"""A bounded tool loop used only by Weixin."""
from dataclasses import dataclass
import json
import time
from .model import MODEL_TIMEOUT, ModelError
from .instructions import SYSTEM
from polaris_platform.protocol import PlatformError


RUN_TIMEOUT = 900


def _tool(name, description, properties, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties,
                       'required': list(required), 'additionalProperties': False}}}


TOOLS = [
    _tool('components', '查看当前可协作组件', {}),
    _tool('help', '查看一个组件的操作说明，调用前必须先查看',
          {'component': {'type': 'string'}}, ['component']),
    _tool('call', '按照刚查看的说明调用组件操作', {
        'component': {'type': 'string'}, 'operation': {'type': 'string'},
        'arguments': {'type': 'object'}}, ['component', 'operation', 'arguments']),
]



@dataclass
class AgentResult:
    text: str
    history: list


class WeixinAgent:
    def __init__(self, model):
        self.model = model

    def _safe(self, value):
        text = str(value)
        redact = getattr(self.model, 'redact', None)
        return redact(text) if redact else text

    def _tool_content(self, text):
        return self._safe(text)

    def run(self, text, history, tools_executor, cancel):
        started = time.monotonic()
        messages = [{'role': 'system', 'content': SYSTEM}]
        for entry in (history or [])[-12:]:
            if isinstance(entry, dict) and entry.get('role') in ('user', 'assistant'):
                messages.append({'role': entry['role'], 'content': self._safe(entry.get('content', ''))[:4000]})
        messages.append({'role': 'user', 'content': self._safe(text)[:8000]})
        review_request = {'current':self._safe(text), 'recent_messages':messages[1:]}
        helped, calls = set(), 0
        tool_rounds, corrections, evidence = 0, 0, []
        answer = None
        # Reserve a final response after the tool budget: the last tool may
        # already have committed a write and still needs an acknowledgement.
        for turn in range(9):
            if cancel.is_set():
                raise ModelError('请求已取消')
            remaining = RUN_TIMEOUT - (time.monotonic() - started)
            if remaining <= 0:
                raise ModelError('请求超时')
            final = tool_rounds >= 6 or calls >= 12 or turn == 8
            if final:
                messages.append({'role': 'system', 'content': '工具调用已结束。根据已有结果直接回复，明确哪些操作成功、失败或结果未知，不再调用工具。'})
            response = self.model.complete(messages, [] if final else TOOLS, cancel, timeout=min(MODEL_TIMEOUT, remaining))
            if cancel.is_set():
                raise ModelError('请求已取消')
            if time.monotonic() - started >= RUN_TIMEOUT:
                raise ModelError('请求超时')
            if not isinstance(response, dict):
                raise ModelError('模型响应格式无效')
            tool_calls = response.get('tool_calls', [])
            if tool_calls is None:
                tool_calls = []
            if not isinstance(tool_calls, list):
                raise ModelError('模型工具响应格式无效')
            if not tool_calls:
                answer = response.get('content')
                if not isinstance(answer, str) or not answer.strip():
                    raise ModelError('模型未返回有效正文')
                reviewer = getattr(self.model, 'review', None)
                if reviewer:
                    remaining = RUN_TIMEOUT - (time.monotonic() - started)
                    if remaining <= 0:
                        raise ModelError('请求超时')
                    # Keep the original conversational references even after many
                    # tool rounds; evidence is supplied separately.
                    can_use_tools = not final and corrections < 2
                    request = {**review_request, 'can_use_tools': can_use_tools}
                    review = reviewer(json.dumps(request, ensure_ascii=False), answer, evidence, cancel, timeout=min(MODEL_TIMEOUT, remaining))
                    if review['needs_tools']:
                        if not can_use_tools:
                            raise ModelError('本轮未能确认操作结果，请查询实际状态')
                        corrections += 1
                        answer = None
                        messages.append(response)
                        messages.append({'role': 'system', 'content':
                            '刚才的回答缺少本轮执行证据：' + self._safe(review['reason'])[:1000] +
                            '。请实际使用工具完成最新用户请求；不能完成时明确说明未执行，不可编造返回值。'})
                        continue
                    answer = review['reply']
                break
            if final:
                raise ModelError('模型未能生成最终回复')
            tool_rounds += 1
            if (not isinstance(tool_calls, list) or any(not isinstance(item, dict) for item in tool_calls)
                    or len(tool_calls) > 12 - calls):
                raise ModelError('工具调用次数超过限制')
            messages.append(response)
            for call in tool_calls:
                if cancel.is_set():
                    raise ModelError('请求已取消')
                if time.monotonic() - started >= RUN_TIMEOUT:
                    raise ModelError('请求超时')
                calls += 1
                name, args = None, {}
                try:
                    function = call.get('function', {})
                    name = function.get('name')
                    args = json.loads(function.get('arguments', '{}'))
                    if not isinstance(args, dict):
                        raise ValueError('工具参数必须是对象')
                    if name not in ('components', 'help', 'call'):
                        raise ValueError('未知工具')
                    component = args.get('component')
                    if name in ('help', 'call') and (not isinstance(component, str) or not component):
                        raise ValueError('缺少组件名称')
                    if name in ('help', 'call') and hasattr(tools_executor, 'resolve_component'):
                        component = tools_executor.resolve_component(component)
                        args['component'] = component
                    if name == 'call' and component not in helped:
                        raise ValueError('必须先查看目标组件帮助')
                    result = tools_executor.execute(name, args, cancel)
                    if name == 'help' and not (isinstance(result, dict) and result.get('error')):
                        helped.add(component)
                    result_text = json.dumps(result, ensure_ascii=False, default=str)
                except (InterruptedError, ModelError):
                    raise
                except Exception as exc:
                    if cancel.is_set():
                        raise ModelError('请求已取消') from None
                    error = {'error': self._safe(exc)}
                    if isinstance(exc, PlatformError):
                        error['code'] = exc.code
                        if exc.code == 'permission_required':
                            details = exc.details if isinstance(exc.details, dict) else {}
                            capability = details.get('capability', [])
                            error.update(error='当前调用来源缺少平台操作授权；不是调用参数错误',
                                         requester=details.get('requester'),
                                         operation=capability[2] if isinstance(capability, (list, tuple)) and len(capability) > 2 else args.get('operation'),
                                         component=args.get('component'))
                    result_text = json.dumps(error, ensure_ascii=False)
                content = self._tool_content(result_text)
                details = args if isinstance(args, dict) else {}
                evidence.append({'tool': name, 'component': details.get('component'), 'operation': details.get('operation'),
                                 'arguments': details.get('arguments'), 'result': json.loads(content)})
                messages.append({'role': 'tool', 'tool_call_id': str(call.get('id', '')),
                                 'content': content})
        if cancel.is_set():
            raise ModelError('请求已取消')
        if time.monotonic() - started >= RUN_TIMEOUT:
            raise ModelError('请求超时')
        if answer is None:
            raise ModelError('本次处理已达到请求次数限制')
        answer = self._safe(answer)
        if len(answer) > 1800:
            answer = answer[:1800] + '\n（内容较长，已截短）'
        return AgentResult(answer, [{'role': 'user', 'content': self._safe(text)[:8000]},
                                    {'role': 'assistant', 'content': answer}])
