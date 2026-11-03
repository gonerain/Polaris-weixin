"""Weixin delivery and user-authorized delegation to a conversational service."""
from collections import deque
from copy import deepcopy
import json
import logging
from pathlib import Path
import threading
import time
import uuid
from polaris_platform.protocol import PlatformError
from .storage import atomic_write


def operations(record):
    return {name: meta for name, meta in record.get('description', {}).get('operations', {}).items()
            if isinstance(meta, dict) and meta.get('available', True) and meta.get('profile') != 'qt-native'}


class AgentController:
    def __init__(self, client, path, changed, directory_id='collaboration', own_id='weixin'):
        self.client, self.path, self.changed = client, Path(path), changed
        self.directory_id, self.own_id = directory_id, own_id
        self.lock = threading.RLock()
        self.enabled, self.busy, self.error = False, False, ''
        self.desired_enabled, self.allowed, self.queue = False, [], deque()
        self.ctx, self.since, self._resume = None, 0, None
        self.completions = None
        self.cancel, self.worker_running, self.bad_file = threading.Event(), False, False
        self.provider, self.ready, self.model, self.detail = None, False, '', ''
        self.conversation, self.parent, self.turns = uuid.uuid4().hex, '', {}
        try:
            if self.path.exists():
                if self.path.stat().st_size > 100000:
                    raise ValueError()
                data = json.loads(self.path.read_text('utf8'))
                if not isinstance(data, dict) or data.get('version') != 1:
                    raise ValueError()
                self.provider = data.get('provider')
                self.allowed = self.validate(data['allowed'])
                self.desired_enabled = data.get('enabled', False) is True
        except (OSError, ValueError, TypeError, KeyError, PlatformError):
            self.bad_file = True
            self.error = '自动回复设置无法读取，请修复或移走设置文件'

    def bind_identity(self, identity):
        with self.lock:
            self._cancel()
            self.own_id = identity
            self.allowed = [r for r in self.allowed if r['target'] != identity]

    def bind_resume(self, resume):
        self._resume, self.since = resume, time.time()
        self.resume()

    def _discover(self, ctx):
        records = ctx.call(self.directory_id, 'discover', {}).get('components', [])
        candidates = []
        for row in records:
            ops = operations(row)
            reply = next((name for name, meta in ops.items() if meta.get('provides') == 'polaris.agent.reply@1'), None)
            status = next((name for name, meta in ops.items() if meta.get('provides') == 'polaris.agent.status@1'), None)
            if reply and status:
                candidates.append(dict(target=row['id'], reply=reply, status=status))
        matching = [v for v in candidates if v['target'] == self.provider] if self.provider else candidates
        if len(matching) != 1:
            self.ready = False
            raise PlatformError('agent_unavailable', '没有唯一可用的对话服务，请检查组件状态')
        service = matching[0]
        self.provider = service['target']
        self._provider_operations = (service['reply'], service['status'])
        return service, records

    def refresh(self, ctx):
        try:
            service, _ = self._discover(ctx)
            state = ctx.call(service['target'], service['status'], {})
            self.ready, self.model = state.get('ready') is True, state.get('model', '')
            self.detail = state.get('detail', '')
        except Exception as error:
            self.ready = False
            self.detail = str(error)
        return self.settings()

    def resume(self):
        with self.lock:
            if not self.desired_enabled or self.enabled or self.error or self._resume is None:
                return
            try:
                context = self._resume()
                if context is not None:
                    self.refresh(context)
                    if self.ready:
                        self.ctx, self.enabled = context, True
            except PlatformError as error:
                self.detail = '自动回复待恢复：' + error.code

    def validate(self, allowed):
        if not isinstance(allowed, list) or len(allowed) > 100:
            raise PlatformError('invalid_params', '请选择允许操作')
        rows = []
        for row in allowed:
            if not isinstance(row, dict) or set(row) != {'target', 'operation'} or any(not isinstance(row[k], str) or not row[k] or len(row[k]) > 256 for k in row):
                raise PlatformError('invalid_params', '允许操作格式无效')
            if row['target'] in (self.directory_id, self.own_id, self.provider):
                raise PlatformError('denied', '请选择业务组件公开操作')
            if row not in rows:
                rows.append(dict(row))
        return rows

    def settings(self):
        with self.lock:
            return dict(enabled=self.enabled, busy=self.busy, error=self.error,
                        desiredEnabled=self.desired_enabled,
                        resumePending=self.desired_enabled and not self.enabled and not self.error,
                        ready=self.ready, hasKey=self.ready, model=self.model, detail=self.detail,
                        allowed=deepcopy(self.allowed), queued=len(self.queue))

    def catalog(self, ctx):
        service, records = self._discover(ctx)
        return {'components': [dict(id=r['id'], name=r.get('manifest', {}).get('name', r['id']),
                                    description={'operations': deepcopy(operations(r))})
                for r in records if r['id'] not in (self.directory_id, self.own_id, service['target']) and operations(r)]}

    def task_operations(self, rows):
        selected = [(row['target'], row['operation']) for row in rows]
        selected += [(self.own_id, name) for name in ('get', 'send', 'complete_agent_run', 'receive_agent_request', 'agent_control')]
        selected += [(self.directory_id, name) for name in ('discover', 'describe')]
        if self.provider:
            # Operation names are resolved from the provider's contract metadata.
            selected += [(self.provider, name) for name in getattr(self, '_provider_operations', ('reply', 'status'))]
        return selected

    def configure(self, enabled, allowed, ctx):
        if not isinstance(enabled, bool):
            raise PlatformError('invalid_params', 'enabled 必须为布尔值')
        if enabled or allowed is not None:
            service, _ = self._discover(ctx)
            self._provider_operations = (service['reply'], service['status'])
        rows = self.validate(self.allowed if allowed is None else allowed)
        if enabled:
            self.refresh(ctx)
            if not self.ready:
                raise PlatformError('agent_unavailable', self.detail or '对话服务未就绪')
            available = {(r['id'], n) for r in self.catalog(ctx)['components'] for n in r['description']['operations']}
            if any((r['target'], r['operation']) not in available for r in rows):
                raise PlatformError('invalid_params', '所选操作当前不可用')
        with self.lock:
            if self.bad_file:
                raise PlatformError('invalid_state', self.error)
            selected = self.task_operations(rows)
            if (enabled or allowed is not None) and hasattr(ctx, 'require_permissions'):
                ctx.require_permissions(selected)
            previous = self.path.read_bytes() if self.path.exists() else None
            atomic_write(self.path, json.dumps(dict(version=1, allowed=rows, enabled=enabled, provider=self.provider), ensure_ascii=False).encode())
            try:
                if enabled and hasattr(ctx, 'remember_task'):
                    ctx.remember_task('auto_reply', selected)
                elif not enabled and hasattr(ctx, 'forget_task'):
                    ctx.forget_task('auto_reply')
            except Exception:
                if previous is None: self.path.unlink(missing_ok=True)
                else: atomic_write(self.path, previous)
                raise
            self.allowed, self.desired_enabled = rows, enabled
            self._cancel()
            self.enabled, self.ctx, self.since, self.error = enabled, ctx if enabled else None, time.time(), ''
        self.changed()
        return self.settings()

    def _cancel(self):
        self.cancel.set()
        self.cancel = threading.Event()
        self.enabled, self.ctx = False, None
        self.queue.clear()

    def invalidate(self):
        with self.lock: self._cancel()
        self.changed()

    def reset_conversation(self):
        with self.lock:
            self._cancel()
            self.conversation, self.parent = uuid.uuid4().hex, ''
        self.changed()

    def new_conversation(self):
        with self.lock:
            enabled, ctx = self.enabled, self.ctx
            self._cancel()
            self.conversation, self.parent = uuid.uuid4().hex, ''
            self.enabled, self.ctx, self.since, self.error = enabled, ctx, time.time(), ''
        self.changed()
        return self.settings()

    def control(self, turn, action, ctx, target=None, metadata=None, arguments=None):
        with self.lock:
            lease = self.turns.get(turn)
            caller = getattr(getattr(ctx, 'context', None), 'caller', None)
            origin = getattr(getattr(ctx, 'context', None), 'origin_installation', None)
            if not lease or caller != lease['provider'] or origin != lease.get('origin'):
                raise PlatformError('denied', '回调来源与委托不匹配')
            active = not lease['cancel'].is_set() and lease['account'] == self._account()
            if action == 'active': return {'active': active}
            if not active: raise PlatformError('cancelled', '本轮已取消')
            if action != 'prepare' or target not in {r['target'] for r in lease['allowed']}:
                raise PlatformError('denied', '回调操作不属于本轮委托')
            return self.completions.prepare(target, metadata, arguments)

    def _account(self):
        account = self.client.account or {}
        return tuple(account.get(k) for k in ('botId', 'userId'))

    def receive(self, text, timestamp):
        self.resume()
        with self.lock:
            if not self.enabled or timestamp is None or not self.since <= timestamp <= time.time() + 300: return
            if len(self.queue) >= 3:
                self.error = '待回复消息已满，请重新开启自动回复'
                self._cancel()
            else:
                self.queue.append(text)
                if not self.worker_running:
                    self.worker_running = True
                    threading.Thread(target=self.run, daemon=True, name='weixin-reply').start()
        self.changed()

    def run(self):
        try:
            while True:
                with self.lock:
                    if not self.enabled or not self.queue: return
                    text, cancel, ctx = self.queue.popleft(), self.cancel, self.ctx
                    allowed, conversation, parent = deepcopy(self.allowed), self.conversation, self.parent
                    self.busy, self.error = True, ''
                self.changed()
                stage, turn = 'delegate', uuid.uuid4().hex
                try:
                    ctx.call(self.own_id, 'get', {})
                    if cancel.is_set(): return
                    service, _ = self._discover(ctx)
                    with self.lock:
                        self.turns[turn] = dict(provider=service['target'], cancel=cancel, account=self._account(), allowed=allowed,
                                                origin=ctx.context.origin_installation)
                    result = ctx.call(service['target'], service['reply'], dict(text=text, conversation=conversation,
                        parent=parent, turn=turn, allowed=allowed, control='agent_control', max_reply_chars=1800))
                    if cancel.is_set(): return
                    if not isinstance(result, dict) or not isinstance(result.get('text'), str) or result.get('turn') != turn:
                        raise PlatformError('invalid_response', '对话服务返回无效回执')
                    stage, deadline = 'send_reply', time.monotonic() + 30
                    while not cancel.is_set():
                        if time.monotonic() >= deadline: raise TimeoutError('等待发送超时')
                        with self.client._lock:
                            if not self.client.sending:
                                receipt = ctx.call(self.own_id, 'send', {'text': result['text']})
                                break
                        cancel.wait(.05)
                    else: return
                    while not cancel.wait(.05):
                        if time.monotonic() >= deadline: raise TimeoutError('发送结果未确认')
                        with self.client._lock:
                            row = next((r for r in self.client.messages if r['id'] == receipt['messageId']), None)
                            status = row['status'] if row else ''
                        if status != '发送中':
                            if status != '已发送': raise RuntimeError('发送未确认')
                            with self.lock:
                                if not cancel.is_set(): self.parent = turn
                            break
                except Exception as error:
                    logging.getLogger(__name__).warning('Weixin reply failed: stage=%s code=%s', stage, getattr(error, 'code', type(error).__name__))
                    with self.lock:
                        if not cancel.is_set():
                            self._cancel()
                            self.error = ('回复发送未确认，请在微信核实后重新开启' if stage == 'send_reply' else
                                          '自动回复已停止：' + str(getattr(error, 'code', type(error).__name__)))
                    return
                finally:
                    with self.lock:
                        self.turns.pop(turn, None)
                        self.busy = False
                    self.changed()
        finally:
            with self.lock:
                self.worker_running = False
                if self.enabled and self.queue:
                    self.worker_running = True
                    threading.Thread(target=self.run, daemon=True, name='weixin-reply').start()
