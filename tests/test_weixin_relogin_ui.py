"""Real QML retry interaction with an isolated account and fake iLink transport."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest
ROOT = Path(__file__).resolve().parents[1]

class ReloginUIProcessTests(unittest.TestCase):
    def test_expired_account_scan_again(self):
        if importlib.util.find_spec('PySide6') is None:
            self.skipTest('Qt unavailable')
        result = subprocess.run([sys.executable, __file__, 'qt'], env=dict(os.environ,
            QT_QPA_PLATFORM='offscreen', QT_QUICK_BACKEND='software'), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

if __name__ == '__main__' and sys.argv[-1] == 'qt':
    import tempfile
    import time
    from types import SimpleNamespace
    from PySide6.QtCore import QUrl, QPoint, QPointF, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickWindow
    from PySide6.QtTest import QTest
    sys.path.insert(0, str(ROOT / 'plugins' / 'weixin'))
    from polaris_weixin import Weixin
    from polaris_weixin.credentials import CredentialStore
    from polaris_weixin.view import Bridge
    from test_weixin_client import ACCOUNT
    class Fake:
        def __init__(self): self.calls = []
        def request(self, origin, endpoint, body, **kwargs):
            self.calls.append((endpoint, kwargs.get('token')))
            if 'get_bot_qrcode?' in endpoint:
                return dict(qrcode='test-qr', qrcode_img_content='https://weixin.qq.com/test')
            kwargs['cancel'].wait(2)
            raise InterruptedError()
    app = QGuiApplication([])
    with tempfile.TemporaryDirectory() as directory:
        credentials = CredentialStore(Path(directory) / 'credentials.bin')
        credentials.save(dict(ACCOUNT, expired=True))
        fake = Fake()
        component = Weixin(data_dir=directory, transport=fake, credentials=credentials)
        component.start(SimpleNamespace(id='weixin'))
        assert component.client.state == 'expired' and fake.calls == []
        component.client.append('retained', '保留聊天', 'in', '已接收')
        class Context:
            operations = []
            def call(self, target, operation, params):
                self.operations.append(operation)
                return component.get() if operation == 'get' else getattr(component.client, operation)(**params)
        context = Context()
        bridge = Bridge(component, context, None)
        bridge.refresh()
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty('weixinModel', bridge)
        engine.load(QUrl.fromLocalFile(str(ROOT / 'plugins/weixin/polaris_weixin/Weixin.qml')))
        assert engine.rootObjects()
        root = engine.rootObjects()[0]
        window = QQuickWindow(); root.setParentItem(window.contentItem())
        root.setWidth(390); root.setHeight(560); window.resize(390,560); window.show()
        def find(item,name):
            if item.objectName() == name: return item
            for child in item.childItems():
                result = find(child,name)
                if result is not None: return result
        def click(item):
            assert item.isVisible()
            point = item.mapToScene(QPointF(item.width()/2,item.height()/2))
            QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, QPoint(round(point.x()),round(point.y())))
            QTest.qWait(50)
        root.setProperty('setupOpen', True); QTest.qWait(60)
        retry = find(root,'weixinRetryLogin')
        assert retry.isVisible() and retry.property('text') == '重新扫码'
        click(retry)
        deadline = time.monotonic()+2
        while not component.client.qr and time.monotonic()<deadline: QTest.qWait(10)
        bridge.refresh(); QTest.qWait(60)
        assert component.client.state == 'waiting'
        assert component.client.messages[0]['id'] == 'retained'
        assert 'login' in context.operations and 'logout' not in context.operations
        assert fake.calls and all(not token for _,token in fake.calls)
        assert bridge.lastError == ''
        window.grabWindow().save('/tmp/polaris-weixin-relogin.png')
        component.client.account.pop('expired')
        component.client.state = 'connected'; bridge.refresh(); QTest.qWait(30)
        assert not retry.isVisible()
        bridge._timer.stop(); component.stop(); window.close()
