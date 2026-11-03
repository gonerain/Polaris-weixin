"""Independent iLink text transport. All I/O runs on cancellable daemon workers."""
import base64
from datetime import datetime
from collections import OrderedDict
import json
import math
import secrets
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from uuid import uuid4
from polaris_platform.protocol import PlatformError

BASE_URL = 'https://ilinkai.weixin.qq.com'
MAX_RESPONSE = 2 * 1024 * 1024


def trusted_origin(value):
    try:
        parsed = urlsplit(value if isinstance(value, str) else '')
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or not (host == 'weixin.qq.com' or host.endswith('.weixin.qq.com'))
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError()
        return 'https://' + host
    except ValueError:
        raise ValueError('微信服务地址不受信任') from None


def string(value, limit, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError('微信响应字段不完整')
    value.encode('utf8')
    return value


def message_timestamp(row, items):
    values = [row.get('create_time_ms')]
    values.extend(item.get('create_time_ms') for item in items if isinstance(item, dict))
    for value in values:
        if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 15:
            value = int(value)
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and 0 < value < 1e15):
            return float(value) / 1000
    return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    def request(self, origin, endpoint, body, token='', timeout=15, cancel=None):
        origin = trusted_origin(origin)
        headers = {'iLink-App-Id': 'bot', 'iLink-App-ClientVersion': str((2 << 16) | (4 << 8) | 10),
                   'User-Agent': 'Polaris-Weixin/0.1'}
        if body is not None:
            body = dict(body)
            headers.update({'Content-Type': 'application/json', 'AuthorizationType': 'ilink_bot_token',
                            'X-WECHAT-UIN': base64.b64encode(str(secrets.randbits(32)).encode()).decode()})
        if token:
            headers['Authorization'] = 'Bearer ' + token
            body['base_info'] = {'channel_version': '2.4.10', 'bot_agent': 'Polaris/0.1'}
        request = Request(origin + '/' + endpoint, data=None if body is None else json.dumps(body).encode(), headers=headers)
        if cancel is not None and cancel.is_set():
            raise InterruptedError()
        finished = threading.Event()
        timed_out = threading.Event()
        deadline = time.monotonic() + timeout
        response = []
        def close_cancelled():
            while not finished.wait(min(.1, max(0, deadline - time.monotonic()))):
                expired = time.monotonic() >= deadline
                if expired:
                    timed_out.set()
                if expired or cancel is not None and cancel.is_set():
                    if response:
                        response[0].close()
                    return
        watcher = threading.Thread(target=close_cancelled, daemon=True, name='weixin-cancel')
        watcher.start()
        try:
            with build_opener(NoRedirect()).open(request, timeout=timeout) as reply:
                response.append(reply)
                if time.monotonic() >= deadline:
                    timed_out.set()
                    raise TimeoutError()
                if cancel is not None and cancel.is_set():
                    raise InterruptedError()
                data = reply.read(MAX_RESPONSE + 1)
        finally:
            finished.set()
            if timed_out.is_set() or time.monotonic() >= deadline:
                raise TimeoutError('微信请求超时') from None
        if cancel is not None and cancel.is_set():
            raise InterruptedError()
        if len(data) > MAX_RESPONSE:
            raise ValueError('微信响应过大')
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError('微信响应格式无效')
        return result


class Client:
    def __init__(self, credentials, chats, transport=None, changed=None):
        self._lock = threading.RLock()
        self.credentials, self.chats = credentials, chats
        self.transport = transport or Transport()
        self.changed = changed or (lambda: None)
        self.attachments = None
        self.on_text = lambda text, timestamp: None
        self.on_invalidate = lambda: None
        self.on_account_changed = lambda: None
        self.account, self.messages, self.seen = {}, [], OrderedDict()
        self.context = self.qr = self.qr_token = self.verify_code = ''
        self.state, self.detail, self.warning = 'offline', '微信未连接', ''
        self.sending = False
        self.generation = 0
        self.cancel = threading.Event()
        self.stopped = False
        self._workers = threading.BoundedSemaphore(4)
        try:
            account = credentials.load()
            if account:
                self.validate_account(account)
                self.account = account
                self.messages = chats.load(account['botId'], account['userId'])
                self.seen = OrderedDict.fromkeys([key for key in account.get('seen', [])[-512:] if isinstance(key, str) and len(key) <= 256] + [r['id'] for r in self.messages if r['direction'] == 'in'])
                if account.get('expired'):
                    self.state, self.detail = 'expired', '微信登录已失效，请重新扫码'
                else:
                    self.detail = '已保存绑定，点击连接开始接收消息'
        except (OSError, ValueError, TypeError, UnicodeError):
            self.warning = '本机绑定或聊天记录无法读取，请退出后重新绑定'

    @staticmethod
    def validate_account(account):
        for key, limit in (('token', 8192), ('botId', 256), ('userId', 256)):
            string(account.get(key), limit)
        if 'expired' in account and not isinstance(account['expired'], bool):
            raise ValueError('无效微信凭证状态')
        token = account['token']
        if not token.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in token):
            raise ValueError('无效微信凭证')
        trusted_origin(account.get('baseUrl'))
        string(account.get('cursor', ''), 65536, False)
        if not isinstance(account.get('seen', []), list):
            raise ValueError('去重记录无效')

    def get(self):
        with self._lock:
            return dict(state=self.state, detail=self.detail, warning=self.warning,
                        hasAccount=bool(self.account), canRelogin=bool(self.account.get('expired')), accountId=self.account.get('botId', ''),
                        userId=self.account.get('userId', ''), qrContent=self.qr,
                        canSend=self.state == 'connected' and not self.account.get('expired') and bool(self.context) and not self.sending,
                        sending=self.sending, messages=[dict(r) for r in self.messages],
                        attachments=self.attachments.catalog((self.account.get('botId'),self.account.get('userId'))) if self.attachments else [])

    def notify(self):
        if not self.stopped:
            self.changed()

    def status(self, state, detail):
        self.state, self.detail = state, detail
        self.notify()

    def persist(self):
        if self.account:
            try:
                self.chats.save(self.account['botId'], self.account['userId'], self.messages)
            except (OSError, ValueError):
                self.warning = '聊天记录无法保存，若文件损坏请先清空消息'

    def invalidate(self):
        self.on_invalidate()
        self.cancel.set()
        self.generation += 1
        self.cancel = threading.Event()
        for row in self.messages:
            if row['status'] == '发送中':
                row['status'] = '发送结果未知，请在微信核实'
        self.sending = False
        self.qr = self.qr_token = self.verify_code = self.context = ''
        self.persist()

    def worker(self, function):
        if not self._workers.acquire(blocking=False):
            raise PlatformError('busy', '网络请求尚未结束，请稍后重试')
        generation, cancel = self.generation, self.cancel
        def run():
            try:
                if not cancel.is_set() and self.active(generation):
                    function(generation, cancel)
            finally:
                self._workers.release()
        threading.Thread(target=run, daemon=True, name='weixin-worker').start()

    def active(self, generation):
        return not self.stopped and generation == self.generation

    def request(self, origin, endpoint, body, cancel, token='', timeout=15):
        if cancel.is_set():
            raise InterruptedError()
        data = self.transport.request(trusted_origin(origin), endpoint, body, token=token, timeout=timeout, cancel=cancel)
        if data.get('ret') == -14 or data.get('errcode') == -14:
            raise PermissionError()
        if data.get('ret', 0) != 0 or data.get('errcode', 0) != 0:
            raise ValueError('微信接口拒绝请求')
        return data

    def accepted(self):
        return {'accepted': True, 'state': self.state}

    def login(self):
        with self._lock:
            if self.account and not self.account.get('expired'):
                raise PlatformError('invalid_state', '更换绑定前请先退出登录')
            if self.stopped:
                raise PlatformError('component_unavailable', '微信组件已停止')
            self.invalidate()
            self.warning = ''
            self.status('loading', '正在获取微信登录二维码')
            try:
                self.worker(self.login_worker)
            except PlatformError:
                self.status('error', '网络请求尚未结束，请稍后重新扫码')
                raise
            return self.accepted()

    def login_worker(self, generation, cancel):
        try:
            data = self.request(BASE_URL, 'ilink/bot/get_bot_qrcode?bot_type=3', {'local_token_list': []}, cancel)
            token = string(data.get('qrcode'), 8192)
            content = string(data.get('qrcode_img_content'), 2048)
            url = urlsplit(content)
            trusted_origin('https://' + url.netloc)
            if url.scheme != 'https' or not content.isascii():
                raise ValueError()
            with self._lock:
                if not self.active(generation):
                    return
                self.qr, self.qr_token = content, token
                self.status('waiting', '请用微信扫码并在手机上确认绑定')
            origin, deadline = BASE_URL, time.monotonic() + 300
            while not cancel.is_set() and time.monotonic() < deadline:
                with self._lock:
                    if not self.active(generation):
                        return
                    if self.state == 'verify':
                        code = ''
                    else:
                        code, self.verify_code = self.verify_code, ''
                if self.state == 'verify':
                    cancel.wait(.1)
                    continue
                query = {'qrcode': token}
                if code:
                    query['verify_code'] = code
                data = self.request(origin, 'ilink/bot/get_qrcode_status?' + urlencode(query), None, cancel, timeout=40)
                with self._lock:
                    if not self.active(generation):
                        return
                    state = data.get('status')
                    if state == 'confirmed':
                        account = dict(token=data.get('bot_token'), botId=data.get('ilink_bot_id'), userId=data.get('ilink_user_id'), baseUrl=trusted_origin(data.get('baseurl') or BASE_URL), cursor='')
                        self.validate_account(account)
                        self.on_account_changed()
                        self.account = account
                        self.messages = self.chats.load(account['botId'], account['userId'])
                        self.seen = OrderedDict.fromkeys([key for key in account.get('seen', [])[-512:] if isinstance(key, str) and len(key) <= 256] + [r['id'] for r in self.messages if r['direction'] == 'in'])
                        try:
                            if not self.credentials.save(account):
                                raise OSError('绑定未保存')
                        except (OSError, ValueError):
                            self.warning = '本机绑定保存失败，关闭后需重新扫码'
                        self.connect()
                        return
                    if state == 'scaned_but_redirect':
                        host = string(data.get('redirect_host'), 256)
                        origin = trusted_origin(host if '://' in host else 'https://' + host)
                    elif state == 'need_verifycode':
                        self.status('verify', '请输入手机微信显示的配对码')
                    elif state in ('wait', 'scaned'):
                        self.status('waiting', '已扫码，请在手机确认' if state == 'scaned' else '等待微信扫码确认')
                    else:
                        raise ValueError()
                cancel.wait(.5)
            if not cancel.is_set():
                raise ValueError()
        except Exception:
            with self._lock:
                if self.active(generation):
                    self.qr = self.qr_token = ''
                    self.status('error', '微信登录失败或二维码失效，请重新扫码')

    def verify(self, code):
        with self._lock:
            if self.state != 'verify' or not isinstance(code, str) or not code.isascii() or not code.isdigit() or not 4 <= len(code) <= 12:
                raise PlatformError('invalid_params', '请输入有效的配对码')
            self.verify_code = code
            self.status('waiting', '正在验证配对码')
            return self.accepted()

    def connect(self):
        with self._lock:
            if not self.account or self.stopped or self.account.get('expired'):
                raise PlatformError('invalid_state', '请先扫码绑定微信')
            self.invalidate()
            self.status('connecting', '正在连接微信')
            try:
                self.worker(self.poll_worker)
            except PlatformError:
                self.status('offline', '网络请求尚未结束，请稍后连接')
                raise
            return self.accepted()

    def poll_worker(self, generation, cancel):
        retry = 0
        while not cancel.is_set():
            with self._lock:
                if not self.active(generation):
                    return
                account = dict(self.account)
            try:
                try:
                    data = self.request(account['baseUrl'], 'ilink/bot/getupdates', {'get_updates_buf': account.get('cursor', '')}, cancel, account['token'], 45)
                except (TimeoutError, socket.timeout):
                    data = {'msgs': []}
                except URLError as error:
                    if isinstance(error.reason, (TimeoutError, socket.timeout)):
                        data = {'msgs': []}
                    else:
                        raise
                with self._lock:
                    if not self.active(generation):
                        return
                    rows = data.get('msgs', [])
                    if not isinstance(rows, list) or len(rows) > 1000:
                        raise ValueError()
                    cursor = string(data.get('get_updates_buf', account.get('cursor', '')), 65536, False)
                    previous_seen = list(self.seen)
                    for row in rows:
                        self.receive(row)
                    dirty = cursor != self.account.get('cursor', '') or previous_seen != list(self.seen)
                    self.account['cursor'] = cursor
                    if dirty:
                        self.account['seen'] = list(self.seen)[-512:]
                        try:
                            self.credentials.save(self.account)
                        except (OSError, ValueError):
                            self.warning = '绑定进度保存失败'
                    self.status('connected', '微信已连接')
                retry = 0
                cancel.wait(.5)
            except Exception as error:
                with self._lock:
                    if not self.active(generation):
                        return
                    if isinstance(error, PermissionError) or isinstance(error, HTTPError) and error.code in (401, 403):
                        self.expire()
                        return
                    retry = min(retry + 1, 6)
                    self.status('reconnecting', '微信连接中断，正在重试')
                cancel.wait(min(60, 2 ** retry))

    def receive(self, row):
        if not isinstance(row, dict) or row.get('group_id') or row.get('message_type') != 1 or row.get('message_state') in (0, 1):
            return
        if row.get('from_user_id') != self.account['userId']:
            return
        identity = row.get('message_id')
        if isinstance(identity, int) and not isinstance(identity, bool):
            identity = str(identity)
        if not isinstance(identity, str) or not identity or len(identity) > 256:
            return
        items = row.get('item_list', [])
        if not isinstance(items, list):
            return
        parts = [item['text_item']['text'][:8000] for item in items[:100] if isinstance(item, dict) and item.get('type') == 1 and isinstance(item.get('text_item'), dict) and isinstance(item['text_item'].get('text'), str)]
        media = [item for item in items[:100] if isinstance(item, dict) and item.get('type') in (2, 4)]
        if not parts and not media:
            return
        context = row.get('context_token')
        if isinstance(context, str) and 0 < len(context) <= 16384:
            self.context = context
        if identity in self.seen:
            return
        self.seen[identity] = None
        while len(self.seen) > 512:
            self.seen.popitem(last=False)
        text = '\n'.join(parts)[:8000]
        timestamp = message_timestamp(row, items)
        if media and self.attachments is not None:
            account = (self.account.get('botId'), self.account.get('userId'))
            identifiers = self.attachments.register(identity, items, account)
            self.append(identity, text + ('\n' if text else '') + f'[正在保存 {len(identifiers)} 个附件]', 'in', '已接收')
            def save_media(generation, cancel):
                lines = [text] if text else []
                for attachment_id in identifiers:
                    try:
                        saved = self.attachments.save(attachment_id, account, cancel)
                        lines.append(f'[附件 {saved["name"]}，{saved["size"]} 字节，本地路径：{saved["path"]}]')
                    except InterruptedError:
                        return
                    except Exception as error:
                        # Never expose CDN parameters or encryption keys in model input.
                        lines.append('[附件保存失败：' + (str(error) if isinstance(error, ValueError) else type(error).__name__)[:160] + ']')
                if not identifiers: lines.append('[附件格式不支持或缺少下载信息]')
                content = '\n'.join(lines)[:8000]
                with self._lock:
                    if not self.active(generation) or account != (self.account.get('botId'), self.account.get('userId')): return
                    for message in self.messages:
                        if message['id'] == identity: message['text'] = content
                    self.persist()
                    self.on_text(content, timestamp)
                    self.notify()
            try:
                self.worker(save_media)
            except PlatformError:
                self.warning = '附件处理繁忙，请稍后重新发送'
            return
        self.append(identity, text, 'in', '已接收')
        self.on_text(text, timestamp)

    def append(self, identity, text, direction, status):
        self.messages.append(dict(id=identity, text=text, direction=direction, status=status, time=datetime.now().strftime('%H:%M')))
        del self.messages[:-100]
        self.persist()

    def send(self, text):
        with self._lock:
            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                raise PlatformError('invalid_params', '请输入 1 至 2000 字的文字')
            try:
                text.encode('utf8')
            except UnicodeError:
                raise PlatformError('invalid_params', '文字编码无效') from None
            if not self.get()['canSend']:
                raise PlatformError('invalid_state', '请连接微信并先接收绑定者的文字消息')
            identity = 'polaris-' + uuid4().hex
            self.sending = True
            self.append(identity, text, 'out', '发送中')
            account, context = dict(self.account), self.context
            self.notify()
            def send_worker(generation, cancel):
                success = False
                expired = False
                try:
                    self.request(account['baseUrl'], 'ilink/bot/sendmessage', {'msg': {'from_user_id': '', 'to_user_id': account['userId'], 'client_id': identity, 'message_type': 2, 'message_state': 2, 'context_token': context, 'item_list': [{'type': 1, 'text_item': {'text': text}}]}}, cancel, account['token'])
                    success = True
                except Exception as error:
                    expired = isinstance(error, PermissionError) or isinstance(error, HTTPError) and error.code in (401, 403)
                with self._lock:
                    if not self.active(generation):
                        return
                    self.sending = False
                    for row in self.messages:
                        if row['id'] == identity:
                            row['status'] = '已发送' if success else '发送结果未知，请在微信核实'
                    if not success:
                        self.warning = '发送未确认，不会自动重发，请在微信核实'
                    if expired:
                        self.expire()
                    self.persist()
                    self.notify()
            try:
                self.worker(send_worker)
            except PlatformError:
                self.sending = False
                self.messages[-1]['status'] = '未发送，请稍后重试'
                self.persist()
                self.notify()
                raise
            return {'accepted': True, 'messageId': identity, 'state': self.state}

    def expire(self):
        # Retain account-scoped chat history, but never reuse its rejected token.
        self.invalidate()
        self.account['expired'] = True
        try:
            if not self.credentials.save(self.account):
                raise OSError('绑定状态未保存')
        except (OSError, ValueError):
            self.warning = '登录失效状态保存失败，重新启动后请重新扫码'
        self.status('expired', '微信登录已失效，请重新扫码')

    def disconnect(self):
        with self._lock:
            self.invalidate()
            if self.account.get('expired'):
                self.status('expired', '微信登录已失效，请重新扫码')
            else:
                self.status('offline', '已断开，绑定保留')
            return self.accepted()

    def logout(self):
        with self._lock:
            self.invalidate()
            warning = ''
            try:
                if self.account:
                    self.chats.clear(self.account['botId'], self.account['userId'])
            except OSError:
                warning = '聊天记录删除失败，请检查数据目录权限'
            try:
                self.credentials.clear()
            except OSError:
                warning = '本机凭证删除失败，下次启动可能仍有旧绑定'
            self.account, self.messages, self.seen = {}, [], OrderedDict()
            self.warning = warning
            self.status('offline', '本机已退出登录')
            return self.accepted()

    def clear_messages(self):
        with self._lock:
            if self.account:
                self.chats.clear(self.account['botId'], self.account['userId'])
            self.messages = []
            self.persist()
            self.notify()
            return self.accepted()

    def stop(self):
        with self._lock:
            self.invalidate()
            self.stopped = True
