"""Optional Agent callback adapter. Completion messages never enter the model."""
import json
import threading
import uuid
from copy import deepcopy
from polaris_platform.protocol import PlatformError
from .storage import atomic_write


class CompletionAdapter:
    def __init__(self, client, path, identity):
        self.client, self.path, self.identity = client, path, identity
        self.lock = threading.RLock()
        self.records = {}
        if path.exists():
            self.records = json.loads(path.read_text(encoding='utf-8'))

    def account(self):
        return [self.client.account.get(k,'') for k in ('botId','userId')]

    def save(self):
        atomic_write(self.path, json.dumps(self.records, ensure_ascii=False).encode('utf-8'))

    def prepare(self, target, metadata, arguments):
        if metadata.get('completionCallback') != {'version':1,'parameter':'on_complete'}:
            return arguments
        with self.lock:
            token = uuid.uuid4().hex
            params = deepcopy(arguments)
            params.setdefault('delivery_id', uuid.uuid4().hex)
            self.records[token] = dict(account=self.account(), target=target,
                sessionId=params['session_id'], deliveryId=params['delivery_id'], state='waiting')
            self.save()
            params['on_complete'] = dict(target=self.identity,operation='complete_agent_run',params={'notification_id':token})
            return params

    def receive(self, notification_id, result, source):
        with self.lock, self.client._lock:
            record = self.records.get(notification_id)
            if (not record or source != record['target'] or not isinstance(result,dict) or result.get('deliveryId') != record['deliveryId']
                    or result.get('sessionId') != record['sessionId'] or not isinstance(result.get('runId'),str)
                    or result.get('status') not in ('completed','failed','stopped')):
                raise PlatformError('invalid_params','结果回传与原任务不匹配')
            if record['account'] != self.account():
                raise PlatformError('completion_account_changed','原任务的微信账号已更换，未发送通知')
            if record['state'] != 'waiting':
                return {'accepted':True,'duplicate':True,'messageId':record.get('messageId')}
            # Several steering messages can belong to the same run. Notify that
            # run once, even if it has several accepted delivery receipts.
            previous = next((r for r in self.records.values() if r is not record
                and r.get('account') == record['account'] and r.get('target') == record['target']
                and r.get('sessionId') == record['sessionId'] and r.get('runId') == result['runId']
                and r.get('state') != 'waiting'), None)
            if previous:
                record.update(state='covered',runId=result['runId'],messageId=previous.get('messageId'))
                self.save()
                return {'accepted':True,'duplicate':True,'messageId':record.get('messageId')}
            # Completion and the normal model reply share Client's one-flight sender.
            if not self.client.get()['canSend']:
                raise PlatformError('completion_not_ready','微信暂时不能发送，结束通知等待连接恢复')
            status = {'completed':'已完成','failed':'失败','stopped':'已停止'}[result['status']]
            title = str(result.get('title') or '任务')[:160]
            text = f'【任务结束】{title}：{status}'
            reply = result.get('reply')
            if isinstance(reply,str) and reply.strip():
                text += '\n\n' + reply.strip()
            if len(text) > 1950 or result.get('replyTruncated'):
                text = text[:1900] + '\n（结果较长，完整内容请在 Agent 中查看）'
            record.update(state='sending',runId=result['runId'])
            self.save()
            # Persist the claim first: even a crash after network submission cannot resend.
            try:
                receipt = self.client.send(text)
                record.update(state='submitted',messageId=receipt['messageId'])
                self.save()
                return {'accepted':True,'messageId':receipt['messageId']}
            except Exception:
                record['state'] = 'unknown'
                self.save()
                raise
