"""Weixin directory contract and authorization, with isolated fake transport."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'plugins' / 'weixin'
ACCOUNT = dict(token='test-token', botId='fake-bot', userId='fake-user', baseUrl='https://ilinkai.weixin.qq.com', cursor='')

class Credentials:
    def __init__(self, account=None):
        self.account = dict(account) if account else None
    def load(self):
        return self.account
    def save(self, account):
        self.account = dict(account)
        return True
    def clear(self):
        self.account = None

class FakeTransport:
    def __init__(self):
        self.calls, self.sent = [], []
        self.incoming = []
        self.lock = threading.Lock()
    def reply(self, text, identity='reply'):
        with self.lock:
            self.incoming.append(dict(message_id=identity, message_type=1, message_state=2,
                from_user_id='fake-user', context_token='fake-context',
                item_list=[{'type': 1, 'text_item': {'text': text}}]))
    def request(self, origin, endpoint, body, token='', timeout=15, cancel=None):
        self.calls.append(endpoint)
        if endpoint == 'ilink/bot/getupdates':
            with self.lock:
                rows, self.incoming = self.incoming, []
            return {'msgs': rows}
        if endpoint == 'ilink/bot/sendmessage':
            self.sent.append(body['msg'])
            return {}
        if endpoint.startswith('ilink/bot/get_bot_qrcode'):
            return {'qrcode': 'fake-qr', 'qrcode_img_content': 'https://weixin.qq.com/fake-qr'}
        if endpoint.startswith('ilink/bot/get_qrcode_status'):
            return {'status': 'confirmed', 'bot_token': 'test-token', 'ilink_bot_id': 'fake-bot', 'ilink_user_id': 'fake-user'}
        raise AssertionError(endpoint)

class WeixinComponentTests(unittest.TestCase):
    def setUp(self):
        from polaris_platform.bootstrap import build, default_config, load_factory
        from polaris_platform.component import Component
        self.directory = tempfile.TemporaryDirectory()
        self.runtime = build(default_config())
        self.runtime.install({'id': 'user', 'name': 'User', 'version': '1', 'platformApi': 1,
            'entry': {'mode': 'inprocess'}}, Component)
        self.runtime.start_component('user')
        self.manifest = json.loads((PLUGIN / 'manifest.json').read_text())
        self.manifest['entry']['kwargs'] = {'data_dir': self.directory.name}
        self.identity = self.manifest['id']
        self.runtime.install(self.manifest, load_factory(self.manifest, PLUGIN, self.runtime))
        self.runtime.start_component(self.identity)
        self.component = self.runtime.component_instance(self.identity)
        self.fake = FakeTransport()
        self.component.client.transport = self.fake
        self.component.client.credentials = Credentials()
        for operation in self.runtime.status(self.identity)['description']['operations']:
            self.runtime.grant('user', self.identity, operation)
        self.context = self.runtime.user_context('user')
    def tearDown(self):
        for row in reversed(self.runtime.snapshot()):
            if row['state'] == 'running':
                self.runtime.stop_component(row['id'], force=True)
        self.directory.cleanup()
    def call(self, operation, **params):
        return self.runtime.client('user').call(self.identity, operation, params, context=self.context)
    def wait(self, predicate):
        end = time.monotonic() + 4
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.01)
        self.assertTrue(predicate())
    def test_unbound_is_offline_and_directory_contract(self):
        self.assertFalse(self.call('get')['hasAccount'])
        self.assertEqual(self.fake.calls, [])
        description = self.runtime.status(self.identity)['description']
        self.assertEqual(description['dependencies'], [])
        self.assertFalse(description['operations']['generate_reply']['available'])
        self.assertTrue(description['operations']['send'].get('available', True))
        self.assertFalse(self.call('agent_settings')['providerAvailable'])
        self.assertEqual(description['views'][0]['adapter'], 'qt-quick-item')
        self.assertEqual(description['operations']['create_view']['profile'], 'qt-native')
        self.assertEqual(self.call('release_view', handle='missing'), {'released': False})

    def test_saved_binding_connects_on_start_without_opening_view(self):
        from types import SimpleNamespace
        from polaris_weixin import Weixin
        component = Weixin(data_dir=self.directory.name, credentials=Credentials(ACCOUNT), transport=self.fake)
        try:
            self.assertEqual(self.fake.calls, [])
            component.start(SimpleNamespace(id=self.identity))
            self.wait(lambda: component.get()['state'] == 'connected')
            self.assertIn('ilink/bot/getupdates', self.fake.calls)
            self.assertEqual(self.fake.sent, [])
            component.client.disconnect()
            time.sleep(.05)
            self.assertEqual(component.get()['state'], 'offline')
        finally:
            component.stop()
    def test_fake_login_receive_send_clear_and_logout(self):
        self.call('login')
        self.wait(lambda: self.call('get')['state'] == 'connected')
        self.assertFalse(self.call('get')['canSend'])
        self.fake.reply('Incoming')
        self.wait(lambda: self.call('get')['canSend'])
        self.call('send', text='Outgoing')
        self.wait(lambda: not self.call('get')['sending'])
        self.assertEqual([r['text'] for r in self.call('get')['messages']], ['Incoming', 'Outgoing'])
        self.assertEqual(self.fake.sent[0]['to_user_id'], 'fake-user')
        self.call('clear_messages')
        self.assertEqual(self.call('get')['messages'], [])
        self.call('disconnect')
        self.assertTrue(self.call('get')['hasAccount'])
        self.call('logout')
        self.assertFalse(self.call('get')['hasAccount'])
    def test_revocation_does_not_bypass_live_runtime(self):
        from polaris_platform.protocol import PlatformError
        self.runtime.revoke('user', self.identity, 'login')
        with self.assertRaises(PlatformError):
            self.call('login')
        self.runtime.revoke('user', self.identity, 'get')
        with self.assertRaises(PlatformError):
            self.call('get')
        self.assertEqual(self.fake.calls, [])
    def test_stop_prevents_queued_send_from_starting(self):
        client = self.component.client
        client.account = dict(ACCOUNT)
        client.state, client.context = 'connected', 'fake-context'
        queued = []
        client.worker = lambda function: queued.append((function, client.generation, client.cancel))
        self.call('send', text='Queued')
        self.runtime.stop_component(self.identity)
        function, generation, cancel = queued.pop()
        function(generation, cancel)
        self.assertEqual(self.fake.sent, [])
        self.assertFalse(client.get()['sending'])
        self.assertNotEqual(client.get()['messages'][-1]['status'], '已发送')

    def test_business_import_does_not_load_qt(self):
        code = ('import sys,tempfile;sys.path[:0]=' + repr([str(ROOT), str(PLUGIN)]) + ';'
            'from polaris_weixin import create;'
            'd=tempfile.TemporaryDirectory();c=create(data_dir=d.name);c.get();'
            'assert not any(k.startswith("PySide6") for k in sys.modules)')
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
    def test_dedicated_config_only_installs_weixin(self):
        config = json.loads((ROOT / 'config.weixin.json').read_text())
        self.assertEqual([r['directory'] for r in config['components'] if 'directory' in r], ['plugins/weixin'])

if __name__ == '__main__':
    unittest.main()
