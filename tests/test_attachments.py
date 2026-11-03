import base64
import hashlib
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from polaris_weixin.attachments import AttachmentStore, decrypt, media_url
from polaris_weixin.client import Client
from polaris_weixin.storage import ChatStore


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store=AttachmentStore(Path(self.directory.name)/'files')

    def test_private_files_round_trip_and_bounds(self):
        saved=self.store.write('说明.txt','你好')
        self.assertEqual(self.store.read(saved['file_id'])['content'],'你好')
        self.assertEqual(Path(saved['path']).stat().st_mode & 0o777,0o600)
        self.assertEqual(self.store.root.stat().st_mode & 0o777,0o700)
        binary=self.store.write('data.bin',base64.b64encode(b'\x00\xff').decode(),'base64')
        self.assertEqual(self.store.read(binary['file_id'],'base64',1,1)['content'],'/w==')
        for name in ('../x','/tmp/x','a\\x','..','a\n.txt'):
            with self.assertRaises(ValueError): self.store.write(name,'x')
        with self.assertRaises(ValueError): self.store.read('/etc/passwd')
        with self.assertRaises(ValueError): self.store.read(saved['file_id'],limit=1048577)
        p=Path(saved['path']);p.unlink();p.symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):self.store.read(saved['file_id'])

    def encrypted(self,raw):
        return subprocess.run(['openssl','enc','-aes-128-ecb','-K','00'*16],input=raw,capture_output=True,check=True).stdout

    def test_file_decryption_checks_account_size_digest_and_key(self):
        raw=b'file bytes\x00'
        cipher=self.encrypted(raw)
        self.store.fetch=lambda media,cancel:cipher
        item={'type':4,'file_item':{'file_name':'a.bin','len':str(len(raw)),'md5':hashlib.md5(raw).hexdigest(),
              'media':{'encrypt_query_param':'SECRET','aes_key':base64.b64encode(('00'*16).encode()).decode()}}}
        identifier=self.store.register('message',[item],('bot','user'))[0]
        with self.assertRaises(ValueError): self.store.save(identifier,('bot','other'),threading.Event())
        saved=self.store.save(identifier,('bot','user'),threading.Event())
        self.assertEqual(Path(saved['path']).read_bytes(),raw)
        self.assertNotIn('SECRET',str(saved))
        self.assertEqual(self.store.save(identifier,('bot','user'),threading.Event()),saved)
        with self.assertRaises(ValueError):decrypt(cipher,b'1'*16)
        bad=dict(item['file_item'],md5='f'*32)
        identity=self.store.register('bad',[{'type':4,'file_item':bad}],('bot','user'))[0]
        with self.assertRaises(ValueError):self.store.save(identity,('bot','user'),threading.Event())

    def test_image_top_level_key_and_cdn_validation(self):
        raw=b'\x89PNG\r\nexample'
        self.store.fetch=lambda media,cancel:self.encrypted(raw)
        item={'type':2,'image_item':{'aeskey':'00'*16,'media':{'aes_key':'bad'}}}
        identifier=self.store.register('image',[item],('b','u'))[0]
        saved=self.store.save(identifier,('b','u'),threading.Event())
        self.assertTrue(saved['path'].endswith('.png'))
        for url in ('http://novac2c.cdn.weixin.qq.com/a','https://evil.test/a','https://cdn.weixin.qq.com.evil.test/a','https://user@novac2c.cdn.weixin.qq.com/a'):
            with self.assertRaises(ValueError):media_url({'full_url':url})
        self.assertIn('encrypted_query_param=a%2Bb',media_url({'encrypt_query_param':'a+b'}))

    def test_incoming_attachment_is_saved_before_model_receives_path(self):
        class Credentials:
            def load(self):return {'token':'test-token','botId':'b','userId':'u','baseUrl':'https://ilinkai.weixin.qq.com'}
        self.store.fetch=lambda media,cancel:b'\x89PNG\r\nexample'
        client=Client(Credentials(),ChatStore(Path(self.directory.name)/'chat'))
        self.addCleanup(client.stop)
        client.attachments=self.store
        messages=[];ready=threading.Event()
        client.on_text=lambda text,timestamp:(messages.append(text),ready.set())
        row={'message_id':'m','message_type':1,'message_state':2,'from_user_id':'u','create_time_ms':int(time.time()*1000),'context_token':'context',
             'item_list':[{'type':2,'image_item':{'media':{'encrypt_query_param':'SECRET'}}}]}
        client.receive(row)
        self.assertTrue(ready.wait(2))
        self.assertIn('本地路径',messages[0])
        self.assertNotIn('SECRET',str(client.get()))
        client.receive(row)
        self.assertEqual(len(messages),1)
