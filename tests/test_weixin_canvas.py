"""End-to-end directory weixin mounting through the generic desktop approval UI."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
ROOT = Path(__file__).resolve().parents[1]

if __name__ != '__main__':
    class WeixinCanvasProcessTests(unittest.TestCase):
        def test_weixin_canvas_approval_and_chat(self):
            import importlib.util
            if importlib.util.find_spec('PySide6') is None:
                self.skipTest('Optional PySide6 is not installed')
            result = subprocess.run([sys.executable, str(Path(__file__).resolve())],
                env=dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_QUICK_BACKEND='software'),
                capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
else:
    import json
    import tempfile
    import time
    import threading
    from test_weixin_component import FakeTransport, Credentials, ACCOUNT
    from PySide6.QtCore import QPoint, QPointF, Qt, QCoreApplication, QEvent
    from PySide6.QtGui import QGuiApplication, QCursor, QKeyEvent
    from PySide6.QtTest import QTest
    from polaris_platform.bootstrap import build
    from polaris_platform.component import Component
    from polaris_platform.desktop import WindowHost, Canvas, Manager

    def find(item, name):
        if item.objectName() == name:
            return item
        for child in item.childItems():
            match = find(child, name)
            if match is not None:
                return match
        return None

    class WeixinCanvasIntegration(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.app = QGuiApplication.instance() or QGuiApplication([])

        def setUp(self):
            self.directory = tempfile.TemporaryDirectory()
            config = json.loads((ROOT / 'config.weixin.json').read_text())
            plugin = ROOT / 'plugins' / 'weixin'
            manifest = json.loads((plugin / 'manifest.json').read_text())
            manifest['entry']['kwargs'] = {'data_dir': self.directory.name}
            config['components'][-1].update(manifest=manifest)
            self.runtime = build(config, ROOT)
            self.component = self.runtime.component_instance('org.polaris.weixin')
            self.fake = FakeTransport()
            self.component.client.transport = self.fake
            self.component.client.credentials = Credentials(ACCOUNT)
            self.component.client.account = dict(ACCOUNT)
            self.host = WindowHost(runtime=self.runtime)
            self.canvas = Canvas(runtime=self.runtime, collapse_delay=1500)
            self.manager = Manager(self.runtime)
            for identity, component in [('window', self.host), ('canvas', self.canvas), ('manager', self.manager), ('user', Component())]:
                self.runtime.install({'id': identity, 'name': identity, 'version': '1', 'platformApi': 1,
                    'entry': {'mode': 'inprocess'}}, lambda c=component: c)
                self.runtime.start_component(identity)
            for target in ('window', 'canvas'):
                for operation in self.runtime.status(target)['description']['operations']:
                    self.runtime.grant('user', target, operation)
            self.context = self.runtime.user_context('user')
            self.runtime.client('user').call('canvas', 'show', context=self.context)
            self.window = self.host._views[self.canvas.handle]
            self.wait(self.window.isVisible)
            QTest.qWait(30)

        def tearDown(self):
            self.wait(lambda: not self.canvas._workers)
            for row in reversed(self.runtime.snapshot()):
                if row['state'] == 'running':
                    self.runtime.stop_component(row['id'], force=True)
            QTest.qWait(30)
            self.directory.cleanup()

        def wait(self, condition):
            end = time.monotonic() + 4
            while not condition() and time.monotonic() < end:
                QTest.qWait(10)
            self.assertTrue(condition(), self.manager.bridge.notice)

        def click(self, name, window=None):
            window = window or self.window
            item = find(window.contentItem(), name)
            self.assertIsNotNone(item, name)
            self.assertTrue(item.isVisible(), name)
            point = item.mapToScene(QPointF(item.width()/2, item.height()/2))
            self.assertGreaterEqual(point.y(), 0, name)
            self.assertLess(point.y(), window.height(), name + ' must fit the visible window')
            QCursor.setPos(window.mapToGlobal(QPoint(round(point.x()), round(point.y()))))
            QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, QPoint(round(point.x()), round(point.y())))
            QTest.qWait(30)
            return item

        def mount_by_picker(self):
            self.click('chooseComponentView')
            self.wait(lambda: find(self.window.contentItem(), 'componentViewChoice') is not None)
            self.click('componentViewChoice')
            self.wait(lambda: (self.manager.bridge.planJson != '[]' and not self.manager.bridge.busy) or bool(self.canvas.status()['mounts']))
            if self.manager.bridge.planJson != '[]':
                permissions = json.loads(self.manager.bridge.planJson)
                self.assertTrue(any(row['name'] == 'send' for row in permissions))
                self.wait(lambda: find(self.manager._view.contentItem(), 'confirmPermission') is not None or bool(self.canvas.status()['mounts']))
                if not self.canvas.status()['mounts']:
                    self.click('confirmPermission', self.manager._view)
            self.wait(lambda: bool(self.canvas.status()['mounts']) and self.canvas.status()['mounts'][0]['state'] == 'mounted')
            self.wait(lambda: find(self.window.contentItem(), 'weixinMessageInput') is not None)

        def test_stopped_component_rejects_delayed_gui_creation(self):
            self.mount_by_picker()
            component = self.runtime.component_instance('org.polaris.weixin')
            presentation = component._presentation
            context = next(iter(presentation._views.values()))[2]._context
            original = presentation.dispatcher.run
            waiting, gate = threading.Event(), threading.Event()
            errors = []
            def delayed(function):
                from PySide6.QtCore import QThread
                if QThread.currentThread() != self.app.thread():
                    waiting.set()
                    if not gate.wait(3):
                        raise RuntimeError('test did not resume GUI creation')
                return original(function)
            presentation.dispatcher.run = delayed
            parent = find(self.window.contentItem(), 'componentViewContent')
            def request():
                try:
                    self.runtime.client('canvas').call('org.polaris.weixin', 'create_view', {'view':'main', 'parent':parent}, context=self.runtime.user_context('canvas'))
                except Exception as error:
                    errors.append(error)
            worker = threading.Thread(target=request)
            worker.start()
            try:
                self.wait(waiting.is_set)
                self.runtime.stop_component('org.polaris.weixin')
                gate.set()
                self.wait(lambda: not worker.is_alive())
                self.assertTrue(errors)
                self.assertEqual(getattr(errors[0], 'code', None), 'stale_instance')
                self.assertFalse(presentation._views)
                self.assertIsNone(component._presentation)
            finally:
                gate.set()
                presentation.dispatcher.run = original
                self.wait(lambda: not worker.is_alive())

        def test_weixin_marks_real_agent_and_both_canvas_views_refresh(self):
            if not os.environ.get('POLARIS_AGENT_REPO'):
                self.skipTest('Set POLARIS_AGENT_REPO for optional cross-repository canvas test')
            sys.path.insert(0, str(ROOT / 'integration_tests'))
            from test_weixin_agent_e2e import ScriptedModel, tool, call, TARGET, OPERATIONS
            from polaris_agent import Agent
            from polaris_weixin.agent import WeixinAgent
            agent = Agent(data_dir=Path(self.directory.name) / 'agent')
            self.runtime.install(dict(id=TARGET, name='Agent', version='1', platformApi=1,
                entry=dict(mode='inprocess')), lambda: agent)
            self.runtime.start_component(TARGET)
            for name in agent.describe()['operations']:
                self.runtime.grant('canvas', TARGET, name)
            row = agent.store.snapshot('fixture', 'ui', 'ui-session', title='画布联调会话')
            self.component.agent.agent = WeixinAgent(ScriptedModel([
                tool('help', component='Agent'), call('mark', session_id=row['id']), {'content': '画布会话已标记'},
                tool('help', component='Agent'), call('unmark', session_id=row['id']), {'content': '画布会话已取消标记'}]))
            self.mount_by_picker()
            weixin_bridge = next(iter(self.component._presentation._views.values()))[2]
            self.assertTrue(weixin_bridge.invoke('connect', '{}'))
            self.wait(lambda: self.component.client.state == 'connected')
            self.assertTrue(weixin_bridge.invoke('configure_agent', json.dumps({'enabled': True,
                'allowed': [{'target': TARGET, 'operation': name} for name in OPERATIONS]})))
            self.canvas.mount({'target': TARGET, 'descriptor': agent.views[0]}, self.runtime.user_context('canvas'))
            self.wait(lambda: len(self.canvas.status()['mounts']) == 2 and
                      all(m['state'] == 'mounted' for m in self.canvas.status()['mounts']))
            agent_bridge = next(iter(agent._presentation._views.values()))[2]
            for index, marked in enumerate((True, False)):
                with self.fake.lock:
                    self.fake.incoming.append(dict(message_id='canvas-e2e-' + str(index), message_type=1,
                        message_state=2, from_user_id='fake-user', context_token='fake-context',
                        create_time_ms=time.time()*1000,
                        item_list=[{'type': 1, 'text_item': {'text': '标记' if marked else '取消标记'}}]))
                self.wait(lambda: len(self.fake.sent) == index + 1 and not self.component.agent.busy)
                self.wait(lambda: json.loads(agent_bridge.dataJson)['sessions'][0]['marked'] is marked)
                self.wait(lambda: len(json.loads(weixin_bridge.dataJson)['messages']) == 2*(index + 1))
                self.assertTrue(json.loads(weixin_bridge.agentJson)['enabled'])
                self.assertEqual(weixin_bridge.lastError, '')
                self.assertEqual(agent_bridge.lastError, '')

        def test_weixin_save_owns_exact_permissions_and_task(self):
            from copy import deepcopy
            from unittest.mock import patch
            from polaris_platform.sdk import Component as SDKComponent
            from polaris_platform.protocol import PlatformError
            toy = SDKComponent()
            toy.operation('read')(lambda: {'count': 1})
            self.runtime.install(dict(id='toy',name='Toy',version='1',platformApi=1,
                                      entry=dict(mode='inprocess')), lambda: toy)
            self.runtime.start_component('toy')
            self.component.agent.agent = object()
            self.component.agent.has_key = True
            self.mount_by_picker()
            bridge = next(iter(self.component._presentation._views.values()))[2]
            bridge.refresh(); QTest.qWait(30)
            self.click('weixinAgentToggle')
            self.wait(lambda: self.component.agent.enabled)
            self.click('weixinAgentSettings')
            self.wait(lambda: find(self.window.contentItem(), 'weixinAgentComponent_toy') is not None)
            root = find(self.window.contentItem(), 'weixinRoot')
            root.selectOperation('toy','read',True)
            self.click('weixinAgentSave')
            self.wait(lambda: self.component.agent.allowed == [{'target':'toy','operation':'read'}])
            handle = self.component.port.bind('toy','read')
            self.assertNotIn(handle.semantic,self.runtime.authorization.scope('canvas'))
            self.assertIn(handle.semantic,self.runtime.authorization.scope('org.polaris.weixin'))
            task = next(iter(self.runtime._tasks.values()))
            self.assertEqual(task['origin'],'org.polaris.weixin')
            old_context = self.component.resume_task('auto_reply')
            self.assertEqual(old_context.call('toy','read',{}), {'count':1})
            before_scope = self.runtime.authorization.scope('org.polaris.weixin')
            before_tasks = deepcopy(self.runtime._tasks)
            self.click('weixinAgentSettings')
            root.selectOperation('toy','read',False)
            with patch('polaris_weixin.controller.atomic_write',side_effect=OSError('settings disk full')):
                self.click('weixinAgentSave')
            self.assertIn('settings disk full',bridge.lastError)
            self.assertEqual(self.component.agent.allowed,[{'target':'toy','operation':'read'}])
            self.assertEqual(self.runtime.authorization.scope('org.polaris.weixin'),before_scope)
            self.assertEqual(self.runtime._tasks,before_tasks)
            before_file = self.component.agent.path.read_bytes()
            with patch.object(self.runtime,'_persist_tasks',side_effect=OSError('tasks disk full')):
                self.click('weixinAgentSave')
            self.assertIn('tasks disk full',bridge.lastError)
            self.assertEqual(self.component.agent.path.read_bytes(),before_file)
            self.assertEqual(self.component.agent.allowed,[{'target':'toy','operation':'read'}])
            self.assertEqual(self.runtime.authorization.scope('org.polaris.weixin'),before_scope)
            self.assertEqual(self.runtime._tasks,before_tasks)
            self.click('weixinAgentSave')
            self.wait(lambda: self.component.agent.allowed == [])
            with self.assertRaises(PlatformError): old_context.call('toy','read',{})
            self.assertNotIn(handle.semantic,self.runtime.authorization.scope('org.polaris.weixin'))
            # A revoked capability is not repaired by polling or by switching off.
            self.click('weixinAgentSettings')
            root.selectOperation('toy','read',True)
            self.click('weixinAgentSave')
            self.runtime.revoke('org.polaris.weixin','toy','read')
            bridge.refresh(); bridge.refresh()
            self.assertNotIn(handle.semantic,self.runtime.authorization.scope('org.polaris.weixin'))
            self.runtime.stop_component('toy')
            self.click('weixinAgentToggle')
            self.assertFalse(self.component.agent.desired_enabled)
            self.assertNotIn(handle.semantic,self.runtime.authorization.scope('org.polaris.weixin'))
            local_input = bridge._local_input
            resource = next(iter(self.component._presentation._views))
            self.component.release_view(resource)
            with self.assertRaises(PlatformError): local_input.call('configure_agent',{'enabled':True})
            self.runtime.start_component('toy')
            parent = find(self.window.contentItem(), 'componentViewContent')
            created = self.runtime.client('canvas').call('org.polaris.weixin', 'create_view',
                {'view':'main','parent':parent}, context=self.runtime.user_context('canvas'))
            bridge = self.component._presentation._views[created['handle']][2]
            self.assertTrue(bridge.invoke('configure_agent',json.dumps({'enabled':True,
                'allowed':[{'target':'toy','operation':'read'}]})), bridge.lastError)
            self.runtime.stop_component('org.polaris.weixin',persist=False)
            self.runtime.start_component('org.polaris.weixin')
            self.component = self.runtime.component_instance('org.polaris.weixin')
            self.assertTrue(self.component.agent.enabled)
            self.assertEqual(self.component.agent.ctx.context.origin,'org.polaris.weixin')
            self.assertEqual(self.component.agent.ctx.call('toy','read',{}),{'count':1})
            self.runtime.revoke('org.polaris.weixin','toy','read')
            self.runtime.stop_component('org.polaris.weixin',persist=False)
            self.runtime.start_component('org.polaris.weixin')
            self.component = self.runtime.component_instance('org.polaris.weixin')
            with self.assertRaises(PlatformError): self.component.agent.ctx.call('toy','read',{})
            self.assertNotIn(handle.semantic,self.runtime.authorization.scope('org.polaris.weixin'))


        def test_multiple_views_release_independently(self):
            self.mount_by_picker()
            presentation = self.component._presentation
            original = next(iter(presentation._views.values()))
            parent = find(self.window.contentItem(), 'componentViewContent')
            second = self.runtime.client('canvas').call('org.polaris.weixin', 'create_view', {'view':'main', 'parent':parent}, context=self.runtime.user_context('canvas'))
            self.assertEqual(len(presentation._views), 2)
            self.component.release_view(second['handle'])
            self.assertEqual(len(presentation._views), 1)
            self.assertTrue(original[2]._active)
            self.assertFalse(self.component.release_view(second['handle'])['released'])
            self.assertFalse(self.component.client.stopped)

        def test_picker_authorizes_send_and_remove_keeps_communication(self):
            self.assertFalse(self.runtime.authorization.scope('canvas'))
            self.mount_by_picker()
            client = self.component.client
            self.fake.reply('Before mount send', 'first')
            bridge = next(iter(self.component._presentation._views.values()))[2]
            self.assertTrue(bridge.invoke('connect', '{}'))
            self.wait(lambda: client.get()['canSend'])
            self.window.requestActivate()
            self.click('weixinMessageInput')
            for char in 'From canvas':
                QCoreApplication.sendEvent(self.window, QKeyEvent(QEvent.KeyPress, 0, Qt.NoModifier, char))
                QCoreApplication.sendEvent(self.window, QKeyEvent(QEvent.KeyRelease, 0, Qt.NoModifier, char))
            self.wait(lambda: find(self.window.contentItem(), 'weixinSend').isEnabled())
            self.click('weixinSend')
            self.wait(lambda: bool(self.fake.sent))
            self.assertEqual(self.fake.sent[0]['item_list'][0]['text_item']['text'], 'From canvas')
            self.wait(lambda: not client.get()['sending'])
            self.click('canvasUnmountView')
            self.wait(lambda: not self.canvas.status()['mounts'])
            self.assertEqual(self.runtime.status('org.polaris.weixin')['state'], 'running')
            self.fake.reply('After unmount', 'second')
            self.wait(lambda: any(row['text'] == 'After unmount' for row in client.get()['messages']))
            self.assertFalse(self.component._presentation._views)
            self.mount_by_picker()
            self.wait(lambda: bool(self.component._presentation._views))
            bridges = [row[2] for row in self.component._presentation._views.values()]
            self.wait(lambda: 'After unmount' in bridges[0].dataJson)
            self.runtime.revoke('canvas', 'org.polaris.weixin', 'get')
            self.wait(lambda: bridges[0].dataJson == '{}')
            self.runtime.revoke('canvas', 'org.polaris.weixin', 'send')
            self.assertFalse(bridges[0].invoke('send', '{"text":"Denied"}'))
            self.assertEqual(len(self.fake.sent), 1)

    unittest.main()
