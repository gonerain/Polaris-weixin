from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/weixin'))
from polaris_weixin.client import Client, Transport, NoRedirect, trusted_origin
from polaris_weixin.storage import ChatStore
from polaris_platform.protocol import PlatformError

ACCOUNT = dict(token='private-token', botId='bot', userId='user', baseUrl='https://ilinkai.weixin.qq.com', cursor='', seen=['old'])

class Credentials:
    def load(self): return dict(ACCOUNT)
    def save(self, account): return False
    def clear(self): pass

class Fake:
    def __init__(self): self.calls = []
    def request(self, origin, endpoint, body, **kwargs):
        self.calls.append((endpoint, body))
        if endpoint.endswith('sendmessage'): raise TimeoutError()
        kwargs['cancel'].wait(.01)
        raise TimeoutError()

class ClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.fake = Fake()
        self.client = Client(Credentials(), ChatStore(self.directory.name), self.fake)
    def tearDown(self):
        self.client.stop()
        self.directory.cleanup()
    def receive(self, identity='1', sender='user', **extra):
        self.client.receive(dict(message_id=identity, from_user_id=sender, message_type=1, message_state=2, context_token='private-context', item_list=[{'type': 1, 'text_item': {'text': 'hello'}}], **extra))
    def test_only_bound_text_deduplicates_and_secrets_are_private(self):
        self.receive(sender='other'); self.receive(group_id='group'); self.receive('old')
        self.receive(); self.receive()
        self.assertEqual(len(self.client.messages), 1)
        self.assertNotIn('private-', str(self.client.get()))
    def test_send_timeout_never_retries(self):
        self.receive(); self.client.state = 'connected'
        result = self.client.send('reply')
        deadline = time.monotonic() + 2
        while self.client.sending and time.monotonic() < deadline: time.sleep(.01)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.client.messages[-1]['id'], result['messageId'])
        self.assertIn('未知', self.client.messages[-1]['status'])
    def test_cancel_before_dispatch_sends_nothing_and_late_result_is_ignored(self):
        self.receive(); self.client.state = 'connected'
        captured = []
        with patch.object(self.client, 'worker', side_effect=lambda f: captured.append((f, self.client.generation, self.client.cancel))):
            self.client.send('reply')
        self.client.disconnect()
        f, generation, cancel = captured[0]
        f(generation, cancel)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.client.state, 'offline')
        self.assertIn('未知', self.client.messages[-1]['status'])
    def test_busy_does_not_leave_sending_stuck(self):
        self.receive(); self.client.state = 'connected'
        with patch.object(self.client, 'worker', side_effect=PlatformError('busy', 'busy')):
            with self.assertRaises(PlatformError): self.client.send('reply')
        self.assertFalse(self.client.sending)
        self.assertIn('未发送', self.client.messages[-1]['status'])
    def test_long_poll_timeout_is_healthy(self):
        self.client.connect()
        deadline = time.monotonic() + 2
        while self.client.state != 'connected' and time.monotonic() < deadline: time.sleep(.01)
        self.assertEqual(self.client.state, 'connected')
    def test_origin_and_redirect_rejection(self):
        for origin in ('http://weixin.qq.com', 'https://weixin.qq.com.evil.test', 'https://a@weixin.qq.com', 'https://weixin.qq.com:444', 'https://weixin.qq.com/path'):
            with self.assertRaises(ValueError): trusted_origin(origin)
        self.assertEqual(trusted_origin('https://a.weixin.qq.com'), 'https://a.weixin.qq.com')
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://weixin.qq.com'))

    def test_expired_poll_stops_and_idle_poll_does_not_write(self):
        class Expired:
            def request(self, *args, **kwargs):
                return {'ret': -14}
        self.client.transport = Expired()
        self.client.connect()
        deadline = time.monotonic() + 2
        while self.client.state != 'expired' and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.client.state, 'expired')
        self.assertFalse(self.client.get()['canSend'])
        with self.assertRaises(PlatformError):
            self.client.connect()
        self.client.disconnect()
        self.assertEqual(self.client.state, 'expired')
        self.client.transport = self.fake
        self.client.account = dict(ACCOUNT)  # A fresh binding for the idle-poll check.
        with patch.object(self.client.credentials, 'save') as save:
            self.client.connect()
            deadline = time.monotonic() + 2
            while self.client.state != 'connected' and time.monotonic() < deadline:
                time.sleep(.01)
            self.client.disconnect()
            self.assertFalse(save.called)

    def test_pairing_and_untrusted_login_redirect(self):
        from urllib.parse import parse_qs, urlsplit
        class Login:
            def __init__(self): self.status = 0; self.code = ''
            def request(self, origin, endpoint, body, **kwargs):
                if 'get_bot_qrcode?' in endpoint:
                    assert body == {'local_token_list': []}
                    return {'qrcode': 'private-qr-token', 'qrcode_img_content': 'https://weixin.qq.com/login'}
                if 'get_qrcode_status?' in endpoint:
                    self.status += 1
                    if self.status == 1: return {'status': 'need_verifycode'}
                    self.code = parse_qs(urlsplit(endpoint).query).get('verify_code', [''])[0]
                    return {'status': 'scaned_but_redirect', 'redirect_host': 'evil.test'}
                raise AssertionError(endpoint)
        self.client.logout()
        login = Login()
        self.client.transport = login
        self.client.login()
        deadline = time.monotonic() + 2
        while self.client.state != 'verify' and time.monotonic() < deadline: time.sleep(.01)
        self.assertEqual(self.client.state, 'verify')
        with self.assertRaises(PlatformError): self.client.verify('bad')
        self.client.verify('1234')
        deadline = time.monotonic() + 2
        while self.client.state != 'error' and time.monotonic() < deadline: time.sleep(.01)
        self.assertEqual(login.code, '1234')
        self.assertEqual(self.client.state, 'error')
        self.assertNotIn('private-qr-token', str(self.client.get()))

    def test_login_save_failure_is_visible(self):
        class Login:
            def request(self, origin, endpoint, body, **kwargs):
                if 'get_bot_qrcode?' in endpoint:
                    return {'qrcode': 'qr', 'qrcode_img_content': 'https://weixin.qq.com/login'}
                return dict(status='confirmed', bot_token='private-token',
                            ilink_bot_id='bot', ilink_user_id='user')
        for failure in (OSError('disk full'), ValueError('corrupt file'), False):
            with self.subTest(failure=type(failure).__name__):
                self.client.logout()
                self.client.transport = Login()
                self.client.state = 'waiting'
                if isinstance(failure, Exception):
                    save = patch.object(self.client.credentials, 'save', side_effect=failure)
                else:
                    save = patch.object(self.client.credentials, 'save', return_value=False)
                with save, patch.object(self.client, 'connect'):
                    self.client.login_worker(self.client.generation, self.client.cancel)
                self.assertTrue(self.client.get()['hasAccount'])
                self.assertIn('保存失败', self.client.get()['warning'])
                self.assertNotIn('private-token', str(self.client.get()))

    def test_transport_total_deadline_closes_slow_response(self):
        class SlowResponse:
            def __init__(self): self.closed = threading.Event()
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
            def read(self, size):
                # Simulate a response which never completes while data trickles in.
                if not self.closed.wait(1):
                    raise AssertionError('total deadline failed to close response')
                return b'{}'
            def close(self): self.closed.set()
        response = SlowResponse()
        with patch('polaris_weixin.client.build_opener') as opener:
            opener.return_value.open.return_value = response
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                Transport().request('https://weixin.qq.com', 'endpoint', {}, timeout=.03)
            self.assertLess(time.monotonic() - started, .5)
            self.assertTrue(response.closed.is_set())

    def test_expired_login_preserves_history_and_rejects_old_credentials(self):
        self.receive()
        history = list(self.client.messages)
        with patch.object(self.client.credentials, 'save', return_value=True) as save:
            self.client.expire()
            self.assertTrue(save.call_args.args[0]['expired'])
        generation, cancel = self.client.generation, self.client.cancel
        self.client.disconnect()
        self.assertTrue(self.client.get()['canRelogin'])
        with self.assertRaises(PlatformError): self.client.connect()
        with self.assertRaises(PlatformError): self.client.send('no old token')
        class Login:
            def request(self, origin, endpoint, body, **kwargs):
                if 'get_bot_qrcode?' in endpoint:
                    return {'qrcode': 'new-qr', 'qrcode_img_content': 'https://weixin.qq.com/login'}
                return dict(status='confirmed', bot_token='new-token', ilink_bot_id='bot', ilink_user_id='user')
        self.client.transport = Login()
        captured = []
        with patch.object(self.client, 'worker', side_effect=lambda f: captured.append((f,self.client.generation,self.client.cancel))):
            self.client.login()
        self.assertTrue(cancel.is_set())
        self.assertFalse(self.client.active(generation))
        self.assertEqual(self.client.messages, history)
        worker, generation, cancel = captured[0]
        with patch.object(self.client, 'connect'), patch.object(self.client.credentials, 'save', return_value=True):
            worker(generation, cancel)
        self.assertEqual(self.client.account['token'], 'new-token')
        self.assertFalse(self.client.get()['canRelogin'])
        self.assertEqual(self.client.messages, history)
        with self.assertRaises(PlatformError): self.client.login()

    def test_late_expired_poll_cannot_overwrite_new_login(self):
        entered, release = threading.Event(), threading.Event()
        class Late:
            def request(self, *args, **kwargs):
                entered.set()
                release.wait(2)
                return {'ret': -14}
        self.client.transport = Late()
        self.client.connect()
        self.assertTrue(entered.wait(1))
        with patch.object(self.client.credentials, 'save', return_value=True):
            self.client.expire()
        with patch.object(self.client, 'worker'):
            self.client.login()
        release.set()
        time.sleep(.05)
        self.assertEqual(self.client.state, 'loading')

    def test_expired_binding_restores_without_reusing_token(self):
        from polaris_weixin.credentials import CredentialStore
        credentials = CredentialStore(Path(self.directory.name) / 'account.json')
        credentials.save(dict(ACCOUNT, expired=True))
        restored = Client(credentials, self.client.chats, self.fake)
        try:
            self.assertEqual(restored.state, 'expired')
            self.assertTrue(restored.get()['canRelogin'])
            with self.assertRaises(PlatformError): restored.connect()
            self.assertEqual(self.fake.calls, [])
        finally:
            restored.stop()

    def test_expired_rebind_new_account_keeps_old_history_separate(self):
        self.receive()
        history = list(self.client.messages)
        with patch.object(self.client.credentials, 'save', return_value=True):
            self.client.expire()
        class NewAccount:
            def request(self, origin, endpoint, body, **kwargs):
                if 'get_bot_qrcode?' in endpoint:
                    return {'qrcode': 'qr', 'qrcode_img_content': 'https://weixin.qq.com/login'}
                return dict(status='confirmed', bot_token='other-token', ilink_bot_id='other-bot', ilink_user_id='other-user')
        self.client.transport = NewAccount()
        with patch.object(self.client, 'worker'):
            self.client.login()
        with patch.object(self.client, 'connect'), patch.object(self.client.credentials, 'save', return_value=True):
            self.client.login_worker(self.client.generation, self.client.cancel)
        self.assertEqual(self.client.messages, [])
        self.assertEqual(self.client.chats.load('bot', 'user'), history)
        self.assertEqual(self.client.account['userId'], 'other-user')

    def test_failed_and_cancelled_relogin_remain_retryable(self):
        self.receive()
        with patch.object(self.client.credentials, 'save', return_value=True):
            self.client.expire()
        class Failed:
            def request(self, *args, **kwargs): return {}
        self.client.transport = Failed()
        with patch.object(self.client, 'worker'):
            self.client.login()
        self.client.login_worker(self.client.generation, self.client.cancel)
        self.assertEqual(self.client.state, 'error')
        self.assertTrue(self.client.get()['canRelogin'])
        self.client.disconnect()
        self.assertEqual(self.client.state, 'expired')
        with patch.object(self.client, 'worker'):
            self.assertTrue(self.client.login()['accepted'])
        self.assertEqual(self.client.state, 'loading')
        self.assertEqual(len(self.client.messages), 1)
        with self.assertRaises(PlatformError): self.client.connect()
