"""Small, bounded DeepSeek transport, independent of the UI."""
import json
import os
import threading
import time
import urllib.request
from .instructions import REVIEW_SYSTEM


class ModelError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DeepSeek:
    def __init__(self, api_key=None, opener=None):
        self._key = os.environ.get('DEEPSEEK_API_KEY', '') if api_key is None else api_key
        if isinstance(self._key, str):
            self._key = self._key.strip()
        if (not isinstance(self._key, str) or not 8 <= len(self._key) <= 4096
                or any(not 33 <= ord(char) <= 126 for char in self._key)):
            raise ModelError('请配置有效的 DEEPSEEK_API_KEY')
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    def redact(self, value):
        return str(value).replace(self._key, '[已隐藏密钥]')

    def review(self, request, draft, evidence, cancel, timeout=30):
        schema = {'type': 'function', 'function': {'name': 'review_reply',
            'description': '核对回复是否有本轮实际工具结果支持。',
            'parameters': {'type': 'object', 'properties': {
                'needs_tools': {'type': 'boolean'}, 'reply': {'type': 'string'}, 'reason': {'type': 'string'}},
                'required': ['needs_tools', 'reply', 'reason'], 'additionalProperties': False}}}
        messages = [{'role': 'system', 'content': REVIEW_SYSTEM},
            {'role': 'user', 'content': json.dumps({'request': request, 'draft': draft, 'evidence': evidence}, ensure_ascii=False)}]
        response = self.complete(messages, [schema], cancel, timeout,
                                 tool_choice={'type': 'function', 'function': {'name': 'review_reply'}})
        try:
            calls = response['tool_calls']
            if len(calls) != 1 or calls[0]['function']['name'] != 'review_reply':
                raise ValueError()
            result = json.loads(calls[0]['function']['arguments'])
            if (type(result['needs_tools']) is not bool or not isinstance(result['reply'], str)
                    or not isinstance(result['reason'], str) or not result['needs_tools'] and not result['reply'].strip()):
                raise ValueError()
            return result
        except (KeyError, TypeError, ValueError):
            raise ModelError('回复结果核对失败') from None

    def complete(self, messages, tools, cancel, timeout=30, tool_choice=None):
        if cancel.is_set():
            raise ModelError('请求已取消')
        payload = {'model': 'deepseek-flash', 'messages': messages,
                   'thinking': {'type': 'disabled'}, 'stream': False, 'max_tokens': 1024}
        if tools:
            payload['tools'] = tools
        if tool_choice is not None:
            payload['tool_choice'] = tool_choice
        request = urllib.request.Request('https://api.deepseek.com/chat/completions',
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={'Authorization': 'Bearer ' + self._key, 'Content-Type': 'application/json'})
        deadline = time.monotonic() + timeout
        response = None
        finished = threading.Event()
        def watch():
            while not finished.wait(.05):
                if cancel.is_set() or time.monotonic() >= deadline:
                    if response is not None:
                        response.close()
                    return
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            response = self._opener.open(request, timeout=max(.1, timeout))
            data = bytearray()
            while True:
                if cancel.is_set() or time.monotonic() >= deadline:
                    raise ModelError('请求已取消或超时')
                block = response.read(16384)
                if not block:
                    break
                data.extend(block)
                if len(data) > 262144:
                    raise ModelError('模型响应过大')
            if cancel.is_set() or time.monotonic() >= deadline:
                raise ModelError('请求已取消或超时')
            message = json.loads(data)['choices'][0]['message']
            if not isinstance(message, dict):
                raise ValueError()
            content = message.get('content')
            if content is not None and not isinstance(content, str):
                raise ModelError('模型正文格式无效')
            calls = message.get('tool_calls', [])
            if calls is None:
                calls = []
            if not isinstance(calls, list):
                raise ModelError('模型工具响应格式无效')
            result = {'role': 'assistant', 'content': self.redact(content) if content is not None else None}
            if calls:
                normalized = []
                for call in calls:
                    if not isinstance(call, dict) or not isinstance(call.get('function'), dict):
                        raise ModelError('模型工具响应格式无效')
                    function = call['function']
                    if (not isinstance(call.get('id'), str) or not isinstance(function.get('name'), str)
                            or not isinstance(function.get('arguments'), str)):
                        raise ModelError('模型工具响应格式无效')
                    normalized.append({'id': call['id'], 'type': 'function', 'function': {
                        'name': function['name'], 'arguments': function['arguments']}})
                result['tool_calls'] = json.loads(self.redact(json.dumps(normalized)))
            return result
        except ModelError:
            raise
        except Exception:
            # Network exceptions can contain headers or response bodies; never expose them.
            raise ModelError('DeepSeek 请求失败，请检查网络或配置') from None
        finally:
            finished.set()
            if response is not None:
                response.close()
