"""Local opt-in agent orchestration. Request authority lives only in memory."""
from collections import deque
from copy import deepcopy
import json
import logging
from pathlib import Path
import threading
import time
from polaris_platform.protocol import PlatformError
from .agent import AgentResult, WeixinAgent
from .model import DeepSeek, ModelError
from .storage import atomic_write


def operations(record):
    return {name: meta for name, meta in record.get('description', {}).get('operations', {}).items()
            if isinstance(meta, dict) and meta.get('available', True) and meta.get('profile') != 'qt-native'}


class ToolExecutor:
    def __init__(self, ctx, allowed, directory_id, own_id='weixin', completions=None):
        self.ctx, self.directory_id, self.own_id = ctx, directory_id, own_id
        self.allowed = {(r['target'], r['operation']) for r in allowed}
        self.explained = {}
        self.writes = {}
        self._records = None
        self.completions = completions

    def resolve_component(self, value):
        if not isinstance(value, str) or not value.strip():
            raise PlatformError('invalid_params', '缺少组件名称')
        value = value.strip()
        if value in (self.directory_id, self.own_id):
            raise PlatformError('denied', '该组件未在微信自动回复允许列表中启用')
        allowed_targets = {target for target, _ in self.allowed}
        if value in allowed_targets:
            return value
        if self._records is None:
            self._records = self.ctx.call(self.directory_id, 'discover', {}).get('components', [])
        exact = next((row for row in self._records if row.get('id') == value), None)
        matches = [exact] if exact else [row for row in self._records if value.casefold() in
            (str(row.get('id', '')).casefold(), str(row.get('manifest', {}).get('name', '')).casefold(),
             str(row.get('id', '')).rsplit('.', 1)[-1].casefold())]
        if len(matches) > 1:
            raise PlatformError('ambiguous_component', '组件名称不唯一，请使用组件列表中的完整 id')
        if not matches:
            raise PlatformError('component_not_found', '组件名称不存在，请从组件列表中选择完整 id；这不是权限错误')
        identity = matches[0]['id']
        if identity not in allowed_targets or identity in (self.directory_id, self.own_id):
            raise PlatformError('denied', '该组件未在微信自动回复允许列表中启用')
        return identity

    def execute(self, name, args, cancel):
        if cancel.is_set():
            raise InterruptedError()
        if name == 'components':
            records = self.ctx.call(self.directory_id, 'discover', {}).get('components', [])
            self._records = records
            filtered = [self.filtered(r) for r in records if any(t == r.get('id') for t, _ in self.allowed)]
            return {'components': [r for r in filtered if r['description']['operations']]}
        target = self.resolve_component(args.get('component'))
        if name == 'help':
            record = self.filtered(self.ctx.call(self.directory_id, 'describe', {'target': target}))
            self.explained.update({(target, op): deepcopy(meta) for op, meta in record['description']['operations'].items()})
            return record
        if name != 'call':
            raise PlatformError('invalid_params', '未知工具')
        operation = args.get('operation')
        if (target, operation) not in self.allowed:
            raise PlatformError('denied', '该操作未在微信自动回复允许列表中启用')
        if (target, operation) not in self.explained:
            raise PlatformError('help_required', '请先查询此组件的操作说明；这不是权限错误')
        arguments = args.get('arguments', {})
        if not isinstance(arguments, dict):
            raise PlatformError('invalid_params', '操作参数必须是对象')
        live = self.filtered(self.ctx.call(self.directory_id, 'describe', {'target': target}))
        metadata = live['description']['operations'].get(operation)
        if metadata != self.explained.get((target, operation)):
            raise PlatformError('stale_handle', '操作说明已改变，请重新查询帮助')
        if cancel.is_set():
            raise InterruptedError()
        if metadata.get('access') != 'write':
            return self.ctx.call(target, operation, arguments)
        key = (target, operation, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
        if key in self.writes:
            result, error = self.writes[key]
            if error:
                raise PlatformError('unknown_result', '该写操作已尝试，本轮不会重复提交')
            return deepcopy(result)
        self.writes[key] = (None, True)
        if self.completions is not None:
            arguments = self.completions.prepare(target, metadata, arguments)
        result = self.ctx.call(target, operation, arguments)
        self.writes[key] = (deepcopy(result), False)
        return result

    def filtered(self, record):
        return {'id': record.get('id'),
                'name': record.get('manifest', {}).get('name', record.get('id')),
                'description': {'operations': {n: deepcopy(m) for n, m in operations(record).items()
                                               if (record.get('id'), n) in self.allowed}}}

    def failure_reply(self, reason):
        lines = ['本次处理未能正常结束：' + reason]
        completed, unknown, pending, accepted, rejected = [], [], [], [], []
        for (target, operation, _), (result, failed) in self.writes.items():
            metadata = self.explained.get((target, operation), {})
            label = target + ' · ' + metadata.get('title', operation)
            status = result.get('status') if isinstance(result, dict) else None
            bucket = unknown if failed else {'pending': pending, 'unknown': unknown,
                                              'accepted': accepted, 'rejected': rejected}.get(status, completed)
            bucket.append(label)
        if completed:
            lines.append('已确认执行成功：' + '、'.join(dict.fromkeys(completed)) + '。无需重复操作。')
        if unknown:
            lines.append('结果未确认：' + '、'.join(dict.fromkeys(unknown)) + '。请先查询实际状态。')
        if pending:
            lines.append('已提交，等待接收方确认：' + '、'.join(dict.fromkeys(pending)) + '。请查询投递状态。')
        if accepted:
            lines.append('接收方已接收：' + '、'.join(dict.fromkeys(accepted)) + '。运行结果需另行查询。')
        if rejected:
            lines.append('接收方已拒绝：' + '、'.join(dict.fromkeys(rejected)) + '。')
        return '\n'.join(lines)[:1800]



class AgentController:
    def __init__(self, client, path, changed, directory_id='collaboration', own_id='weixin', agent=None):
        self.client, self.path, self.changed = client, Path(path), changed
        self.directory_id, self.own_id = directory_id, own_id
        self.agent = agent
        self.has_key = agent is not None
        if agent is None:
            try:
                self.agent = WeixinAgent(DeepSeek())
                self.has_key = True
            except ModelError:
                pass
        self.lock = threading.RLock()
        self.enabled, self.busy, self.error = False, False, ''
        self.desired_enabled = False
        self.allowed, self.history, self.queue = [], [], deque()
        self.ctx, self.since = None, 0
        self._resume = None
        self.completions = None
        self.cancel = threading.Event()
        self.worker_running = False
        self.bad_file = False
        try:
            if self.path.exists():
                if self.path.stat().st_size > 100000:
                    raise ValueError()
                data = json.loads(self.path.read_text('utf8'))
                if not isinstance(data, dict) or data.get('version') != 1:
                    raise ValueError()
                self.allowed = self.validate(data['allowed'])
                self.desired_enabled = data.get('enabled', False) is True
        except (OSError, ValueError, TypeError, KeyError, PlatformError):
            self.bad_file = True
            self.error = '自动回复设置无法读取，请修复或移走设置文件'

    def bind_identity(self, identity):
        with self.lock:
            self._cancel()
            self.own_id = identity
            # Settings created before the identity was known cannot authorize self calls.
            self.allowed = [r for r in self.allowed if r['target'] != identity]

    def bind_resume(self, resume):
        self._resume = resume
        self.since = time.time()
        self.resume()

    def resume(self):
        with self.lock:
            if not self.desired_enabled or self.enabled or self.error or self._resume is None:
                return
            try:
                context = self._resume()
                if context is not None:
                    self.ctx, self.enabled = context, True
            except PlatformError as error:
                self.error = '自动回复恢复失败：' + error.code

    def validate(self, allowed):
        if not isinstance(allowed, list) or len(allowed) > 100:
            raise PlatformError('invalid_params', '请选择允许操作')
        rows = []
        for row in allowed:
            if not isinstance(row, dict) or set(row) != {'target', 'operation'} or any(not isinstance(row[k], str) or not row[k] or len(row[k]) > 256 for k in row):
                raise PlatformError('invalid_params', '允许操作格式无效')
            if row['target'] in (self.directory_id, self.own_id):
                raise PlatformError('denied', '请选择业务组件公开操作')
            if row not in rows:
                rows.append(dict(row))
        return rows

    def settings(self):
        with self.lock:
            return dict(enabled=self.enabled, busy=self.busy, error=self.error,
                        desiredEnabled=self.desired_enabled,
                        resumePending=self.desired_enabled and not self.enabled and not self.error,
                        hasKey=self.has_key, model='deepseek-flash',
                        allowed=deepcopy(self.allowed), queued=len(self.queue))

    def catalog(self, ctx):
        records = ctx.call(self.directory_id, 'discover', {}).get('components', [])
        result = []
        for record in records:
            if record.get('id') in (self.directory_id, self.own_id):
                continue
            item = {'id': record.get('id'),
                    'name': record.get('manifest', {}).get('name', record.get('id')),
                    'description': {'operations': deepcopy(operations(record))}}
            if item['description']['operations']:
                result.append(item)
        return {'components': result}

    def task_operations(self, rows):
        return [(row['target'], row['operation']) for row in rows] + [
            (self.own_id, 'get'), (self.own_id, 'send'), (self.own_id, 'complete_agent_run'),
            (self.directory_id, 'discover'), (self.directory_id, 'describe')]

    def configure(self, enabled, allowed, ctx):
        if not isinstance(enabled, bool):
            raise PlatformError('invalid_params', 'enabled 必须为布尔值')
        rows = self.validate(self.allowed if allowed is None else allowed)
        if enabled:
            catalog = self.catalog(ctx)
            available = {(r['id'], n) for r in catalog['components'] for n in r['description']['operations']}
            if any((r['target'], r['operation']) not in available for r in rows):
                raise PlatformError('invalid_params', '所选操作当前不可用')
            if self.agent is None:
                try:
                    self.agent = WeixinAgent(DeepSeek())
                    self.has_key = True
                except ModelError:
                    self.has_key = False
                    raise PlatformError('invalid_state', '请配置有效的 DEEPSEEK_API_KEY 后重新开启') from None
        with self.lock:
            if self.bad_file:
                raise PlatformError('invalid_state', self.error)
            selected_operations = self.task_operations(rows)
            if (enabled or allowed is not None) and hasattr(ctx, 'require_permissions'):
                ctx.require_permissions(selected_operations)
            previous = self.path.read_bytes() if self.path.exists() else None
            atomic_write(self.path, json.dumps({'version': 1, 'allowed': rows, 'enabled': enabled}, ensure_ascii=False).encode('utf8'))
            try:
                if enabled and hasattr(ctx, 'remember_task'):
                    ctx.remember_task('auto_reply', selected_operations)
                elif not enabled and hasattr(ctx, 'forget_task'):
                    ctx.forget_task('auto_reply')
            except Exception:
                if previous is None:
                    self.path.unlink(missing_ok=True)
                else:
                    atomic_write(self.path, previous)
                raise
            self.allowed = rows
            self.desired_enabled = enabled
            self._cancel()
            self.enabled, self.ctx, self.since = enabled, ctx if enabled else None, time.time()
            self.error = ''
        self.changed()
        return self.settings()

    def _cancel(self):
        self.cancel.set()
        self.cancel = threading.Event()
        self.enabled, self.ctx = False, None
        self.queue.clear()

    def invalidate(self):
        with self.lock:
            self._cancel()
        self.changed()

    def reset_conversation(self):
        with self.lock:
            self._cancel()
            self.history = []
        self.changed()

    def new_conversation(self):
        with self.lock:
            enabled, ctx = self.enabled, self.ctx
            self._cancel()
            self.history = []
            self.enabled, self.ctx, self.since = enabled, ctx, time.time()
            self.error = ''
        self.changed()
        return self.settings()

    def receive(self, text, timestamp):
        self.resume()
        with self.lock:
            if not self.enabled or timestamp is None or not self.since <= timestamp <= time.time() + 300:
                return
            if len(self.queue) >= 3:
                self.error = '待回复消息已满，请重新开启自动回复'
                self._cancel()
            else:
                self.queue.append(text)
                if not self.worker_running:
                    self.worker_running = True
                    threading.Thread(target=self.run, daemon=True, name='weixin-agent').start()
        self.changed()

    def run(self):
        try:
            while True:
                with self.lock:
                    if not self.enabled or not self.queue:
                        return
                    text = self.queue.popleft()
                    cancel, ctx = self.cancel, self.ctx
                    allowed, history = deepcopy(self.allowed), deepcopy(self.history)
                    self.busy = True
                    self.error = ''
                self.changed()
                stage = 'check_authority'
                try:
                    # Revalidate the initiating request before forwarding new text to the model.
                    ctx.call(self.own_id, 'get', {})
                    if cancel.is_set():
                        return
                    stage = 'model_and_tools'
                    executor = ToolExecutor(ctx, allowed, self.directory_id, self.own_id, self.completions)
                    try:
                        result = self.agent.run(text, history, executor, cancel)
                    except ModelError as error:
                        if cancel.is_set():
                            return
                        reason = str(error)
                        history_text = text
                        if isinstance(self.agent, WeixinAgent):
                            reason = self.agent._safe(reason)
                            history_text = self.agent._safe(text)
                        reply = executor.failure_reply(reason)
                        result = AgentResult(reply, [{'role': 'user', 'content': history_text[:8000]},
                                                     {'role': 'assistant', 'content': reply}])
                        logging.getLogger(__name__).warning('Weixin agent model response failed; completed_writes=%d',
                                                            sum(not failed for _, failed in executor.writes.values()))
                    # Manual and automatic sends share Client's one-flight sender.
                    stage = 'send_reply'
                    send_deadline = time.monotonic() + 30
                    while not cancel.is_set():
                        if time.monotonic() >= send_deadline:
                            raise TimeoutError('等待发送超时')
                        with self.client._lock:
                            if not self.client.sending:
                                if cancel.is_set():
                                    return
                                receipt = ctx.call(self.own_id, 'send', {'text': result.text})
                                break
                        cancel.wait(.05)
                    else:
                        return
                    while not cancel.wait(.05):
                        if time.monotonic() >= send_deadline:
                            raise TimeoutError('发送结果未确认')
                        with self.client._lock:
                            row = next((r for r in self.client.messages if r['id'] == receipt['messageId']), None)
                            status = row['status'] if row else ''
                        if status != '发送中':
                            if status != '已发送':
                                raise RuntimeError('发送未确认，不会自动重发，请核实后重新开启')
                            with self.lock:
                                if not cancel.is_set():
                                    self.history.extend(result.history)
                                    self.history = self.history[-12:]
                                    while len(json.dumps(self.history, ensure_ascii=False)) > 16000 and self.history:
                                        del self.history[:2]
                            break
                except Exception as error:
                    logging.getLogger(__name__).warning('Weixin agent failed: stage=%s code=%s',
                                                        stage, getattr(error, 'code', type(error).__name__))
                    with self.lock:
                        if not cancel.is_set():
                            self._cancel()
                            self.error = ('回复发送未确认，请在微信核实后重新开启' if stage == 'send_reply' else
                                          '自动回复已停止：' + str(getattr(error, 'code', type(error).__name__)))
                    return
                finally:
                    with self.lock:
                        self.busy = False
                    self.changed()
        finally:
            with self.lock:
                self.worker_running = False
                # A newly enabled epoch can queue while the previous worker unwinds.
                if self.enabled and self.queue:
                    self.worker_running = True
                    threading.Thread(target=self.run, daemon=True, name='weixin-agent').start()
