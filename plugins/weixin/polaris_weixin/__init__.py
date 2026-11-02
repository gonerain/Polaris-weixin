"""Weixin component capabilities; presentation imports Qt only on demand."""
import os
from pathlib import Path
from polaris_platform.sdk import Component
from polaris_platform.protocol import PlatformError
from .client import Client
from .credentials import CredentialStore
from .storage import ChatStore
from .controller import AgentController
from .completion import CompletionAdapter

WRITES = ('login', 'verify', 'connect', 'disconnect', 'logout', 'send', 'clear_messages')


def default_data_dir():
    base = (Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
            if os.name == 'nt' else Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share'))
    return base / 'polaris-weixin'


class Weixin(Component):
    def __init__(self, data_dir=None, transport=None, credentials=None, directory_id="collaboration", component_id="weixin"):
        super().__init__()
        directory = Path(data_dir) if data_dir is not None else default_data_dir()
        self._presentation = None
        self.client = Client(credentials or CredentialStore(directory / 'credentials.bin'),
                             ChatStore(directory / 'chats'), transport, self._changed)
        self.agent = AgentController(self.client, directory / "agent.json", self._changed, directory_id, component_id, self.reply_provider, self.reply_available)
        self.completions = CompletionAdapter(self.client, directory / 'agent-completions.json', component_id)

        self.client.on_text = self.agent.receive
        self.client.on_invalidate = self.agent.invalidate
        self.client.on_account_changed = self.agent.reset_conversation
        self._lock = self.client._lock
        self.views = [{'id': 'main', 'adapter': 'qt-quick-item', 'title': '微信',
                       'create': 'create_view', 'release': 'release_view',
                       'permissions': [{'name': name, 'access': 'read'} for name in ('get', 'agent_settings', 'agent_catalog')] +
                                      [{'name': name, 'access': 'write'} for name in ('configure_agent', 'new_conversation', 'complete_agent_run')] +
                                      [{'target': directory_id, 'name': name, 'access': 'read'} for name in ('discover', 'describe')] +
                                      [{'name': name, 'access': 'write'} for name in WRITES]}]
        self.operation('generate_reply', access='write', requires={
            'reply': dict(contract='polaris.agent.reply@1', access='write', group='agent'),
            'status': dict(contract='polaris.agent.status@1', group='agent'),
        })(self.generate_reply)
        self.operation('reply_control', access='write')(self.reply_control)
        self.operation('get')(self.get)
        self.operation('agent_settings')(self.agent_settings)
        self.operation('agent_catalog')(self.agent_catalog)
        self.operation('configure_agent', access='write')(self.configure_agent)
        self.operation('new_conversation', access='write')(self.new_conversation)
        self.operation('complete_agent_run', access='write')(self.complete_agent_run)
        for name in WRITES:
            self.operation(name, access='write')(self.logout if name == 'logout' else getattr(self.client, name))
        self.operation('create_view', access='write')(self.create_view)
        self.operation('release_view', access='write')(self.release_view)

    def start(self, port):
        # The platform assigns the installation's actual component identity.
        self.agent.bind_identity(port.id)
        self.completions.identity = port.id
        super().start(port)
        self.agent.bind_resume(lambda: self.resume_task('auto_reply'))
        if self.client.account and not self.client.account.get('expired'):
            self.client.connect()
            self.agent.resume()

    def logout(self):
        with self._lock:
            self.agent.reset_conversation()
            return self.client.logout()

    def _changed(self):
        if self._presentation is not None:
            self._presentation.changed.emit()

    def get(self):
        result = self.client.get()
        result['agent'] = self.agent_settings()
        return result

    def reply_available(self):
        return self.port and hasattr(self.port, 'bind') and self.port.bind(self.port.id, 'generate_reply')

    def reply_provider(self):
        if self.port is None or not hasattr(self.port, 'requirements'):
            raise PlatformError('unavailable', '回复组件未就绪')
        bindings = self.port.requirements('generate_reply')
        return {name: bindings.handle(name) for name in ('reply', 'status')}

    def generate_reply(self, text, conversation, parent, turn, allowed, ctx):
        with self.agent.lock:
            if (ctx.context.caller != self.port.id or self.agent.active_turn != turn or not self.agent.enabled
                    or self.agent.cancel.is_set()):
                raise PlatformError('cancelled', '这轮自动回复已结束')
            self.agent.responder = self.reply_provider()['reply'].target
        return ctx.call_required('reply', dict(text=text, conversation=conversation, parent=parent, turn=turn,
                                               allowed=allowed, control='reply_control', max_reply_chars=2000))

    def reply_control(self, turn, action, ctx, target=None, metadata=None, arguments=None):
        with self.agent.lock:
            active = (self.agent.active_turn == turn and self.agent.enabled and not self.agent.cancel.is_set()
                      and getattr(self.agent, 'responder', None) == ctx.context.caller)
            if action == 'active':
                return {'active': active}
            if not active or action != 'prepare':
                raise PlatformError('denied', '无效的回复上下文')
            return self.completions.prepare(target, metadata, arguments)

    def agent_settings(self):
        return self.agent.settings()

    def agent_catalog(self, ctx):
        return self.agent.catalog(ctx)

    def configure_agent(self, enabled, ctx, allowed=None):
        return self.agent.configure(enabled, allowed, ctx)

    def new_conversation(self):
        return self.agent.new_conversation()

    def complete_agent_run(self, notification_id, result, ctx):
        source = getattr(getattr(ctx, 'context', None), 'caller', None)
        return self.completions.receive(notification_id, result, source)

    def describe(self):
        result = super().describe()
        result['operations']['complete_agent_run'].update(title='接收任务结果',
            description='将本组件发起任务的确定结束结果直接通知绑定者，不调用模型；按任务去重。')
        for name in ('create_view', 'release_view'):
            result['operations'][name] = dict(result['operations'][name], profile='qt-native')
        return result

    def create_view(self, view, parent, ctx):
        if view != 'main':
            raise PlatformError('missing_view', '微信视图不存在')
        with self._lock:
            if self.port is None or self.client.stopped:
                raise PlatformError('component_unavailable', '微信组件未运行')
            if self._presentation is None:
                from .view import Presentation
                self._presentation = Presentation(self)
            presentation = self._presentation
        return presentation.create(parent, ctx, self.port, ctx.local_view_input(parent))

    def release_view(self, handle):
        return self._presentation.release(handle) if self._presentation else {'released': False}

    def stop(self):
        self.client.stop()
        with self._lock:
            presentation, self._presentation = self._presentation, None
            if presentation is not None:
                presentation.closed = True
        if presentation is not None:
            presentation.close()
        super().stop()


def create(data_dir=None, transport=None, credentials=None, directory_id="collaboration", component_id="weixin"):
    return Weixin(data_dir, transport, credentials, directory_id, component_id)
