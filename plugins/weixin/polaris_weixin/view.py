"""Owned Qt Quick resources and request-scoped UI access."""
import json
import base64
import io
from pathlib import Path
import threading
import uuid
from PySide6.QtCore import QObject, Property, Signal, Slot, QThread, QUrl, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlContext, qmlEngine, qmlContext
from PySide6.QtQuick import QQuickItem
import shiboken6
from polaris_platform.protocol import PlatformError


class Dispatcher(QObject):
    requested = Signal(object)

    def __init__(self):
        super().__init__()
        app = QGuiApplication.instance()
        if app is None:
            raise PlatformError('gui_unavailable', '请先启动图形界面')
        self.requested.connect(self.execute)
        self.moveToThread(app.thread())

    @Slot(object)
    def execute(self, job):
        function, event, result = job
        try:
            result.append((True, function()))
        except Exception as error:
            result.append((False, error))
        finally:
            event.set()

    def run(self, function):
        if QThread.currentThread() == self.thread():
            return function()
        event, result = threading.Event(), []
        self.requested.emit((function, event, result))
        if not event.wait(15):
            raise PlatformError('gui_unavailable', '界面暂时没有响应')
        success, value = result[0]
        if not success:
            raise value
        return value


class Bridge(QObject):
    changed = Signal()
    errorChanged = Signal()

    def __init__(self, component, context, parent, local_input=None):
        super().__init__(parent)
        self._component, self._context = component, context
        self._local_input = local_input
        self._data = '{}'
        self._agent = '{}'
        self._catalog = '[]'
        self._read_error = ''
        self._operation_error = ''
        self._active = True
        self._qr_content = None
        self._qr_image = ''
        self._qr_error = ''
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.notified)
        self._timer.start()

    @Property(str, notify=changed)
    def dataJson(self):
        return self._data

    @Property(str, notify=changed)
    def agentJson(self):
        return self._agent

    @Property(str, notify=changed)
    def catalogJson(self):
        return self._catalog

    @Slot(result=bool)
    def loadAgentCatalog(self):
        try:
            if not self._active:
                raise ValueError('微信视图已经关闭')
            # Recheck read authorization before exposing any directory metadata.
            self._context.call(self._component.port.id, 'get', {})
            result = self._context.call(self._component.port.id, 'agent_catalog', {})
            rows = []
            for record in result.get('components', []):
                description = record.get('description', {})
                for operation, metadata in description.get('operations', {}).items():
                    rows.append({'target': record['id'], 'component': record.get('name', record['id']),
                                 'operation': operation, 'title': metadata.get('title', operation),
                                 'access': metadata.get('access', 'read')})
            self._catalog = json.dumps(rows, ensure_ascii=False)
            self.changed.emit()
            return True
        except Exception as error:
            self._catalog = '[]'
            self.changed.emit()
            return self._failure(error, operation=True)

    @Property(str, notify=errorChanged)
    def lastError(self):
        return '\n'.join(filter(None, (self._operation_error, self._read_error)))

    def _failure(self, error, operation=False):
        message = getattr(error, 'message', str(error))
        if operation:
            self._operation_error = message
        else:
            self._read_error = message
        self.errorChanged.emit()
        return False

    @Slot(result=bool)
    def refresh(self):
        if not self._active:
            return False
        try:
            data = self._context.call(self._component.port.id, 'get', {})
            data = dict(data)
            settings = data.pop('agent', {})
            safe = {key: settings.get(key) for key in ('enabled', 'desiredEnabled', 'resumePending', 'busy', 'error', 'providerAvailable', 'allowed', 'queued')}
            agent = json.dumps(safe, ensure_ascii=False)
            if agent != self._agent:
                self._agent = agent
                self.changed.emit()
            content = data.get('qrContent', '')
            if content != self._qr_content:
                self._qr_content, self._qr_image, self._qr_error = content, '', ''
                if content:
                    try:
                        import qrcode
                        import qrcode.image.svg
                        output = io.BytesIO()
                        qrcode.make(content, image_factory=qrcode.image.svg.SvgPathImage).save(output)
                        self._qr_image = 'data:image/svg+xml;base64,' + base64.b64encode(output.getvalue()).decode('ascii')
                    except ImportError:
                        self._qr_error = '请安装二维码依赖 qrcode'
                    except (ValueError, OSError):
                        self._qr_error = '二维码生成失败，请重试扫码'
            data['qrImage'], data['qrError'] = self._qr_image, self._qr_error
            data.pop('qrContent', None)
            serialized = json.dumps(data, ensure_ascii=False)
            if serialized != self._data:
                self._data = serialized
                self.changed.emit()
        except Exception as error:
            # A revoked read permission must not keep exposing the cached snapshot.
            self._qr_content, self._qr_image, self._qr_error = None, '', ''
            self._agent, self._catalog = '{}', '[]'
            self.changed.emit()
            if self._data != '{}':
                self._data = '{}'
                self.changed.emit()
            return self._failure(error)
        self._read_error = ''
        self.errorChanged.emit()
        return True

    @Slot()
    def notified(self):
        self.refresh()

    @Slot(str, str, result=bool)
    def invoke(self, operation, payload):
        try:
            if not self._active:
                raise ValueError('微信视图已经关闭')
            if operation not in ('login', 'verify', 'connect', 'disconnect', 'logout', 'send', 'clear_messages', 'configure_agent', 'new_conversation'):
                raise ValueError('未知微信操作')
            params = json.loads(payload)
            if not isinstance(params, dict):
                raise ValueError('操作参数必须是对象')
            if operation == 'configure_agent' and self._local_input is not None:
                controller = self._component.agent
                selected = controller.validate(params.get('allowed', controller.allowed))
                chosen = {(row['target'], row['operation']) for row in selected}
                previous = {(row['target'], row['operation']) for row in controller.allowed}
                permissions = {(self._component.port.id, 'configure_agent')}
                if params.get('enabled') is True:
                    permissions |= set(controller.task_operations(selected))
                elif 'allowed' in params:
                    permissions |= chosen
                self._local_input.call(operation, params, permissions=permissions, revoked=previous - chosen)
            else:
                self._context.call(self._component.port.id, operation, params)
        except Exception as error:
            return self._failure(error, operation=True)
        self._operation_error = ''
        self.errorChanged.emit()
        QTimer.singleShot(0, self.notified)
        return True


class Presentation:
    def __init__(self, component):
        self.component = component
        self.closed = False
        self.dispatcher = Dispatcher()
        self._views = {}
        class Changes(QObject):
            changed = Signal()
        self.changes = Changes()
        self.changes.moveToThread(self.dispatcher.thread())
        self.changed = self.changes.changed

    def create(self, parent, ctx, owner, local_input=None):
        def valid():
            return not self.closed and self.component.port is owner and self.component._presentation is self
        def create():
            if not valid():
                raise PlatformError('stale_instance', '微信视图请求所属实例已经停止')
            engine = qmlEngine(parent)
            if engine is None:
                raise PlatformError('view_error', 'Native parent has no QML engine')
            context = QQmlContext(qmlContext(parent), parent)
            bridge = Bridge(self.component, ctx, context, local_input)
            bridge.refresh()
            self.changed.connect(bridge.notified, Qt.QueuedConnection)
            context.setContextProperty('weixinModel', bridge)
            definition = QQmlComponent(engine, QUrl.fromLocalFile(str(Path(__file__).with_name('Weixin.qml'))))
            item = definition.create(context)
            if item is None or not isinstance(item, QQuickItem):
                error = '\n'.join(error.toString() for error in definition.errors())
                if item is not None:
                    item.deleteLater()
                context.deleteLater()
                definition.deleteLater()
                raise PlatformError('view_error', error or 'Weixin.qml must create a QQuickItem')
            context.setParent(item)
            item.setParentItem(parent)
            item.setParent(parent)
            definition.setParent(item)
            if local_input is not None:
                local_input.bind(item)
            if not valid():
                item.setParentItem(None)
                item.deleteLater()
                raise PlatformError('stale_instance', '微信视图请求所属实例已经停止')
            handle = uuid.uuid4().hex
            self._views[handle] = (item, context, bridge, definition)
            item.destroyed.connect(lambda: self._views.pop(handle, None))
            return {'handle': handle, 'item': item}
        return self.dispatcher.run(create)

    def release(self, handle):
        def release():
            resource = self._views.pop(handle, None)
            if resource is None:
                return {'released': False}
            item, context, bridge, definition = resource
            if bridge._local_input is not None:
                bridge._local_input.close()
            bridge._active = False
            bridge._timer.stop()
            if shiboken6.isValid(item):
                self.changed.disconnect(bridge.notified)
                item.setVisible(False)
                item.setParentItem(None)
                item.deleteLater()
            return {'released': True}
        return self.dispatcher.run(release)

    def close(self):
        self.closed = True
        def close():
            for handle in list(self._views):
                self.release(handle)
            self.changes.deleteLater()
        self.dispatcher.run(close)
        # Pending native requests retain this dispatcher until their stale check runs.
