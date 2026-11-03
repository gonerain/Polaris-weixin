"""Isolated Qt checks with fake metadata and no model/network access."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]

class AgentUIProcessTests(unittest.TestCase):
    def test_agent_ui(self):
        if importlib.util.find_spec('PySide6') is None:
            self.skipTest('PySide6 unavailable')
        result = subprocess.run([sys.executable, __file__, 'qt'], env=dict(os.environ,
            QT_QPA_PLATFORM='offscreen', QT_QUICK_BACKEND='software'), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

if __name__ == '__main__' and sys.argv[-1] == 'qt':
    from types import SimpleNamespace
    from PySide6.QtCore import QUrl, QPoint, QPointF, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtTest import QTest
    from PySide6.QtQuick import QQuickWindow
    sys.path.insert(0, str(ROOT / 'plugins' / 'weixin'))
    from polaris_weixin.view import Bridge
    app = QGuiApplication([])
    class Context:
        revoked = False
        write_revoked = False
        catalog_calls = 0
        settings = dict(enabled=False, desiredEnabled=False, busy=False, error='', hasKey=True, model='deepseek-v4-flash', allowed=[], queued=0)
        def call(self, target, operation, params):
            if self.revoked: raise ValueError('读取已撤销')
            if operation == 'get': return dict(agent=self.settings, state='connected', hasAccount=True, canSend=True, messages=[dict(text='测试消息', direction='in', time='12:00')])
            if operation == 'agent_settings': raise AssertionError('UI must use the get snapshot')
            if operation == 'agent_catalog':
                self.catalog_calls += 1
                return {'components':[{'id':'example','name':'示例组件','description':{'operations':{'list':{'access':'read'},'add':{'access':'write'}}}}]}
            if self.write_revoked: raise ValueError('写入已撤销')
            if operation == 'configure_agent':
                self.settings.update(params)
                self.settings['desiredEnabled'] = params['enabled']
            return self.settings
    ctx = Context()
    bridge = Bridge(SimpleNamespace(port=SimpleNamespace(id='weixin')), ctx, None)
    assert bridge.refresh()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty('weixinModel', bridge)
    engine.load(QUrl.fromLocalFile(str(ROOT / 'plugins/weixin/polaris_weixin/Weixin.qml')))
    assert engine.rootObjects(), 'QML did not load'
    root = engine.rootObjects()[0]
    window = QQuickWindow()
    root.setParentItem(window.contentItem()); root.setWidth(370); root.setHeight(220)
    window.resize(370,220); window.show(); QTest.qWait(100)
    def find(item, name):
        if item.objectName() == name: return item
        for child in item.childItems():
            found = find(child, name)
            if found is not None: return found
    toggle = find(root, 'weixinAgentToggle')
    track = find(root, 'weixinAgentToggleTrack')
    thumb = find(root, 'weixinAgentToggleThumb')
    def check_switch(checked):
        assert toggle.property('checked') is checked
        assert track.property('color').name() == ('#28a36b' if checked else '#a9b1b5')
        assert thumb.x() == (track.width() - thumb.width() - 3 if checked else 3)
    check_switch(False)
    ctx.settings['hasKey'] = False
    bridge.refresh(); QTest.qWait(30)
    assert toggle.property('enabled')
    assert track.property('opacity') == 1.0
    ctx.settings['hasKey'] = True
    bridge.refresh(); QTest.qWait(30)
    assert track.property('opacity') == 1.0

    def click_toggle():
        point = toggle.mapToScene(QPointF(toggle.width()/2, toggle.height()/2))
        QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, QPoint(round(point.x()), round(point.y())))
        QTest.qWait(50)
    click_toggle()
    assert ctx.settings['enabled'] is True and toggle.property('checked') is True
    check_switch(True)
    window.grabWindow().save('/tmp/polaris-weixin-switch-on.png')
    ctx.write_revoked = True
    click_toggle()
    assert ctx.settings['enabled'] is True and toggle.property('checked') is True
    check_switch(True)
    assert bridge.lastError == '写入已撤销'
    ctx.write_revoked = False
    click_toggle()
    assert ctx.settings['enabled'] is False and toggle.property('checked') is False
    check_switch(False)
    ctx.write_revoked = True
    click_toggle()
    assert ctx.settings['enabled'] is False and toggle.property('checked') is False
    check_switch(False)
    ctx.write_revoked = False
    assert ctx.catalog_calls == 0
    window.grabWindow().save('/tmp/polaris-weixin-switch-off.png')
    window.grabWindow().save('/tmp/polaris-weixin-agent-chat.png')
    root.openAgent(); QTest.qWait(100)
    assert ctx.catalog_calls == 1
    assert len(json.loads(bridge.catalogJson)) == 2
    assert find(root, 'weixinAgentOperations').property('count') == 1
    assert find(root, 'weixinAgentOperation_example_add') is None
    group = find(root, 'weixinAgentComponent_example')
    point = group.mapToScene(QPointF(group.width()/2, group.height()/2))
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, QPoint(round(point.x()), round(point.y())))
    QTest.qWait(40)
    assert len(root.property('allowedDraft').toVariant()) == 2
    expand = find(root, 'weixinAgentExpand_example')
    expand.clicked.emit(); QTest.qWait(30)
    assert find(root, 'weixinAgentOperation_example_add') is not None
    expand.clicked.emit(); QTest.qWait(30)
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, QPoint(round(point.x()), round(point.y())))
    QTest.qWait(40)
    assert root.property('allowedDraft').toVariant() == []
    root.selectOperation('example','add',True)
    window.grabWindow().save('/tmp/polaris-weixin-agent-settings.png')
    window.resize(290,220); root.setWidth(290); QTest.qWait(100)
    window.grabWindow().save('/tmp/polaris-weixin-agent-narrow.png')
    for _ in range(3): bridge.refresh()
    assert ctx.catalog_calls == 1
    assert bridge.invoke('configure_agent', json.dumps({'enabled':True,'allowed':[{'target':'example','operation':'add'}]}))
    bridge.refresh()
    assert json.loads(bridge.agentJson)['enabled'] is True
    assert ctx.settings['allowed'] == [{'target':'example','operation':'add'}]
    ctx.write_revoked = True
    assert not bridge.invoke('configure_agent', json.dumps({'enabled':True,'allowed':[{'target':'example','operation':'add'}]}))
    assert bridge.lastError == '写入已撤销'
    bridge.refresh(); assert bridge.lastError == '写入已撤销'
    ctx.revoked = True
    assert not bridge.refresh()
    assert bridge.dataJson == '{}' and bridge.catalogJson == '[]' and bridge.agentJson == '{}'
    assert root.property('allowedDraft').toVariant() == [{'target':'example','operation':'add'}]
    root.setProperty('agentOpen', False); root.openAgent()
    assert bridge.catalogJson == '[]'
    bridge._timer.stop()
    window.close()
    sys.exit(0)
elif __name__ == '__main__':
    unittest.main()
