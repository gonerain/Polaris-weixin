"""Channel policy and delivery receipts for opt-in automatic replies."""
from collections import deque
from copy import deepcopy
import json
import logging
from pathlib import Path
import threading
import time
from polaris_platform.protocol import PlatformError
import uuid
from .storage import atomic_write


class AgentController:
    def __init__(self, client, path, changed, directory_id='collaboration', own_id='weixin', provider=None, available=None):
        self.client, self.path, self.changed = client, Path(path), changed
        self.directory_id, self.own_id = directory_id, own_id
        self.provider, self.available = provider, available
        self.active_turn = None
        self.cursor = ''
        self.conversation = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.enabled, self.busy, self.error = False, False, ''
        self.desired_enabled = False
        self.allowed, self.queue = [], deque()
        self.ctx, self.since = None, 0
        self._resume = None
        self.cancel = threading.Event()
        self.worker_running = False
        self.bad_file = False
        try:
            if self.path.exists():
                if self.path.stat().st_size > 100000:
                    raise ValueError()
                data = json.loads(self.path.read_text('utf8'))
                if not isinstance(data, dict) or data.get('version') not in (1, 2):
                    raise ValueError()
                self.allowed = self.validate(data['allowed'])
                self.desired_enabled = data.get('enabled', False) is True
                if data['version'] == 1 and self.desired_enabled:
                    self.error = '回复提供方已拆分，请在微信设置中保存一次以授权新组件'
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
                self.error = '自动回复授权已变化，请重新保存设置' if error.code == 'permission_required' else '自动回复恢复失败：' + error.code

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

    def provider_ready(self):
        try:
            return bool(self.available and self.available())
        except PlatformError:
            return False

    def settings(self):
        with self.lock:
            return dict(enabled=self.enabled, busy=self.busy, error=self.error,
                        desiredEnabled=self.desired_enabled,
                        resumePending=self.desired_enabled and not self.enabled and not self.error,
                        providerAvailable=self.provider_ready(),
                        allowed=deepcopy(self.allowed), queued=len(self.queue))

    def catalog(self, ctx):
        records = ctx.call(self.directory_id, 'discover', {}).get('components', [])
        result = []
        for record in records:
            if record.get('id') in (self.directory_id, self.own_id):
                continue
            operations = {
                name: deepcopy(meta) for name, meta in record.get('description', {}).get('operations', {}).items()
                if meta.get('available', True) and meta.get('profile') != 'qt-native'
                and meta.get('provides') not in ('polaris.agent.reply@1', 'polaris.agent.status@1')
            }
            if operations:
                result.append({'id': record['id'], 'name': record.get('manifest', {}).get('name', record['id']),
                               'description': {'operations': operations}})
        return {'components': result}

    def task_operations(self, rows):
        tools = [(row['target'], row['operation']) for row in rows]
        provider = [(handle.target, handle.operation) for handle in self.provider().values()]
        return tools + provider + [
            (self.own_id, 'generate_reply'), (self.own_id, 'reply_control'),
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
            handle = self.provider()['status']
            status = ctx.call(handle.target, handle.operation, {})
            if not status['ready']:
                raise PlatformError('invalid_state', status['detail'])
        with self.lock:
            if self.bad_file:
                raise PlatformError('invalid_state', self.error)
            selected_operations = self.task_operations(rows) if enabled else [(r["target"], r["operation"]) for r in rows]
            if (enabled or allowed is not None) and hasattr(ctx, 'require_permissions'):
                ctx.require_permissions(selected_operations)
            previous = self.path.read_bytes() if self.path.exists() else None
            atomic_write(self.path, json.dumps({'version': 2, 'allowed': rows, 'enabled': enabled}, ensure_ascii=False).encode('utf8'))
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
        self.active_turn = None
        self.responder = None
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
            self.cursor = ''
            self.conversation = uuid.uuid4().hex
        self.changed()

    def new_conversation(self):
        with self.lock:
            enabled, ctx = self.enabled, self.ctx
            self._cancel()
            self.cursor = ''
            self.conversation = uuid.uuid4().hex
            self.enabled, self.ctx, self.since = enabled, ctx, time.time()
            self.error = ''
        self.changed()
        return self.settings()

    def receive(self, text, timestamp):
        self.resume()
        with self.lock:
            if (not self.enabled or timestamp is None or not self.since <= timestamp <= time.time() + 300
                    or not self.provider_ready()):
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
                    turn = uuid.uuid4().hex
                    self.active_turn = turn
                    params = dict(text=text, allowed=deepcopy(self.allowed), conversation=self.conversation, parent=self.cursor, turn=turn)
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
                    result = ctx.call(self.own_id, 'generate_reply', params)
                    if not isinstance(result, dict) or not isinstance(result.get('text'), str) or result.get('turn') != turn:
                        raise PlatformError('invalid_result', '回复组件返回了无效结果')
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
                                receipt = ctx.call(self.own_id, 'send', {'text': result['text']})
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
                                    self.cursor = turn
                            break
                except Exception as error:
                    logging.getLogger(__name__).warning('Weixin agent failed: stage=%s code=%s',
                                                        stage, getattr(error, 'code', type(error).__name__))
                    with self.lock:
                        if not cancel.is_set():
                            self._cancel()
                            self.error = ('回复发送未确认，请在微信核实后重新开启' if stage == 'send_reply' else
                                          '自动回复授权已变化，请重新保存设置' if getattr(error, 'code', '') == 'permission_required' else
                                          '自动回复已停止：' + str(getattr(error, 'code', type(error).__name__)))
                    return
                finally:
                    with self.lock:
                        self.active_turn = None
                        self.busy = False
                    self.changed()
        finally:
            with self.lock:
                self.worker_running = False
                # A newly enabled epoch can queue while the previous worker unwinds.
                if self.enabled and self.queue:
                    self.worker_running = True
                    threading.Thread(target=self.run, daemon=True, name='weixin-agent').start()
