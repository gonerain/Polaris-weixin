import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/weixin'))
from polaris_weixin.storage import ChatStore
from polaris_weixin.credentials import CredentialStore


class StorageTests(unittest.TestCase):
    def test_account_isolation_secrets_and_atomic_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ChatStore(directory)
            row = dict(id='1', text='hello', direction='in', status='已接收', time='12:00', token='secret', context_token='secret')
            store.save('bot', 'user', [row] * 101)
            data = store.path('bot', 'user').read_text()
            self.assertNotIn('secret', data)
            self.assertEqual(len(store.load('bot', 'user')), 100)
            self.assertEqual(store.load('bot', 'other'), [])
            if sys.platform != 'win32':
                self.assertEqual(store.path('bot', 'user').stat().st_mode & 0o777, 0o600)
            store.clear('bot', 'user')
            self.assertEqual(store.load('bot', 'user'), [])

    def test_corrupt_chat_is_preserved_until_explicit_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ChatStore(directory)
            path = store.path('b', 'u')
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                store.load('b', 'u')
            with self.assertRaises(ValueError):
                store.save('b', 'u', [])
            self.assertEqual(path.read_text(), '{broken')
            store.clear('b', 'u')
            store.save('b', 'u', [])
            self.assertEqual(store.load('b', 'u'), [])

    @staticmethod
    def account():
        return dict(token='private-token', botId='bot', userId='user',
                    baseUrl='https://ilinkai.weixin.qq.com', cursor='saved-cursor', seen=['old'])

    def test_linux_component_restores_in_independent_process_and_logout_clears(self):
        if sys.platform == 'win32':
            self.skipTest('POSIX file permissions')
        from polaris_weixin import create
        class NoNetwork:
            def request(self, *args, **kwargs):
                raise AssertionError('unexpected network')
        with tempfile.TemporaryDirectory() as directory:
            CredentialStore(Path(directory) / 'credentials.bin').save(self.account())
            component = create(data_dir=directory, transport=NoNetwork())
            client = component.client
            client.context = 'private-context'
            client.receive(dict(message_id='new', from_user_id='user', message_type=1,
                message_state=2, context_token='private-context',
                item_list=[{'type': 1, 'text_item': {'text': 'saved chat'}}]))
            client.account.update(seen=list(client.seen), context_token='private-context', qr='private-qr')
            client.credentials.save(client.account)
            component.stop()
            path = Path(directory) / 'credentials.bin'
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('private-context', path.read_text())
            self.assertNotIn('private-qr', path.read_text())
            code = """
import sys
from polaris_weixin import create
class NoNetwork:
    def request(self, *args, **kwargs):
        raise AssertionError('unexpected network')
c = create(data_dir=sys.argv[1], transport=NoNetwork())
assert c.get()['hasAccount']
assert c.get()['messages'][0]['text'] == 'saved chat'
assert c.client.account['cursor'] == 'saved-cursor'
assert list(c.client.seen) == ['old', 'new']
assert c.client.context == ''
assert 'private-' not in str(c.get())
c.client.logout()
assert not c.get()['hasAccount']
c.stop()
"""
            root = Path(__file__).resolve().parents[1]
            code = 'import sys;sys.path[:0]=' + repr([str(root), str(root / 'plugins/weixin')]) + '\n' + code
            result = subprocess.run([sys.executable, '-c', code, directory], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(path.exists())
            self.assertEqual(list((Path(directory) / 'chats').glob('*.json')), [])

    @patch('polaris_weixin.credentials.sys.platform', 'linux')
    def test_atomic_failure_preserves_existing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(Path(directory) / 'credentials.bin')
            store.save(self.account())
            original = store.path.read_bytes()
            with patch('polaris_weixin.storage.os.replace', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    store.save(dict(self.account(), token='new-token'))
            self.assertEqual(store.path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [store.path])

    @patch('polaris_weixin.credentials.sys.platform', 'linux')
    def test_invalid_credentials_are_preserved_until_explicit_clear(self):
        for contents in (b'{broken', b'{"version": 99}', b'x' * 262145,
                         b'{"version": 1, "token": "invalid"}'):
            with self.subTest(size=len(contents)), tempfile.TemporaryDirectory() as directory:
                store = CredentialStore(Path(directory) / 'credentials.bin')
                store.path.write_bytes(contents)
                with self.assertRaises(ValueError):
                    store.load()
                with self.assertRaises(ValueError):
                    store.save(self.account())
                # Also protect the file when save is called on a fresh store.
                with self.assertRaises(ValueError):
                    CredentialStore(store.path).save(self.account())
                self.assertEqual(store.path.read_bytes(), contents)
                store.clear()
                store.save(self.account())
                self.assertEqual(store.load(), self.account())

    @patch('polaris_weixin.credentials.sys.platform', 'linux')
    def test_seen_is_bounded_and_legacy_version_one_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(Path(directory) / 'credentials.bin')
            account = dict(self.account(), seen=[str(i) for i in range(600)])
            store.save(account)
            self.assertEqual(store.load()['seen'], account['seen'][-512:])
            store.path.write_text(json.dumps(dict(self.account(), version=1)))
            self.assertEqual(store.load(), self.account())

    def test_mock_windows_encrypted_format_roundtrip_and_failure(self):
        # Tests the store contract using simulated encryption, not real DPAPI.
        def simulated_protect(data, decrypt=False):
            if decrypt:
                if not data.startswith(b'encrypted:'):
                    raise OSError('decryption failed')
                return data[len(b'encrypted:'):][::-1]
            return b'encrypted:' + data[::-1]
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(Path(directory) / 'credential')
            account = self.account()
            with patch('polaris_weixin.credentials.sys.platform', 'win32'), patch('polaris_weixin.credentials.protect', side_effect=simulated_protect):
                self.assertTrue(store.save(account))
                ciphertext = store.path.read_bytes()
                self.assertNotIn(b'private-token', ciphertext)
                self.assertEqual(store.load(), account)
            with patch('polaris_weixin.credentials.sys.platform', 'win32'), patch('polaris_weixin.credentials.protect', side_effect=OSError('encryption failed')):
                with self.assertRaises(OSError): store.save(dict(account, token='new-secret'))
                self.assertEqual(store.path.read_bytes(), ciphertext)
                with self.assertRaises(OSError): store.load()
            store.clear()
            self.assertFalse(store.path.exists())
