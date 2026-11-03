"""Forward pending questions and consume exact human replies before the model."""
import json
from pathlib import Path
import re
import threading
import time
from .storage import atomic_write
from polaris_platform.protocol import PlatformError


class HumanReplies:
    def __init__(self, client, path, completions):
        self.client, self.path, self.completions = client, Path(path), completions
        self.lock = threading.RLock()
        self.records = json.loads(self.path.read_text()) if self.path.exists() else {}
        for record in self.records.values():
            if record.get('state') == 'responding': record['state'] = 'unknown'

    def save(self):
        atomic_write(self.path,json.dumps(self.records,ensure_ascii=False).encode())

    def receive(self, notification_id, result, source):
        with self.lock, self.client._lock:
            task = self.completions.records.get(notification_id)
            if (not task or source != task['target'] or task['account'] != self.completions.account()
                    or not isinstance(result,dict) or result.get('deliveryId') != task['deliveryId']
                    or result.get('sessionId') != task['sessionId'] or result.get('kind') not in ('approval','question')
                    or not isinstance(result.get('id'),str) or not re.fullmatch('[0-9a-f]{32}',result['id'])
                    or not isinstance(result.get('responseToken'),str) or len(result['responseToken']) < 32
                    or not isinstance(result.get('prompt'),str) or len(result['prompt']) > 6000
                    or type(result.get('expiresAt')) not in (int,float)):
                raise PlatformError('invalid_params','交互通知与原任务不匹配')
            if result['expiresAt'] <= time.time(): return {'accepted':True,'expired':True}
            identity = result['id']
            if identity in self.records: return {'accepted':True,'duplicate':True}
            if not self.client.get()['canSend']: raise PlatformError('interaction_not_ready','微信暂时不能发送交互通知')
            code = identity[:10]
            if any(r['code']==code for r in self.records.values()):
                raise PlatformError('invalid_params','交互编号冲突，请返回原任务处理')
            options = result.get('options') or []
            if not isinstance(options,list) or any(not isinstance(o,str) or len(o)>500 for o in options):
                raise PlatformError('invalid_params','问题选项无效')
            heading = '请求许可' if result['kind']=='approval' else '等待回答'
            message = f'【{heading} {code}】\n' + result['prompt'][:1400]
            if options: message += '\n' + '\n'.join(f'{i+1}. {o}' for i,o in enumerate(options))[:300]
            message += (f'\n\n回复：批准 {code} 或 拒绝 {code}' if result['kind']=='approval' else f'\n\n回复：回答 {code} 你的答案（有选项时可填编号）')
            self.records[identity] = dict(code=code,kind=result['kind'],options=options,account=task['account'],
                target=source,sessionId=result['sessionId'],responseToken=result['responseToken'],
                expiresAt=result['expiresAt'],createdAt=time.time(),state='submitted')
            self.save()
            try:
                receipt = self.client.send(message[:1950])
                self.records[identity]['messageId']=receipt['messageId']
                self.save()
                return {'accepted':True,'messageId':receipt['messageId']}
            except Exception:
                self.records[identity]['state']='unknown'
                self.save()
                raise

    def handle(self, text, timestamp, context):
        match = re.fullmatch(r'\s*(批准|拒绝|回答)\s+([0-9a-f]{10})(?:\s+([\s\S]+))?\s*', text)
        if not match: return False
        action, code, answer = match.groups()
        with self.lock:
            identity, record = next(((k,r) for k,r in self.records.items() if r['code']==code),(None,None))
            if not record: return False
            if (record['account'] != self.completions.account() or timestamp is None or timestamp < record['createdAt']-1 or timestamp > time.time()+300):
                return True
            if record['expiresAt'] <= time.time() or record['state'] != 'submitted':
                self.feedback('该请求已过期或已处理，请查看当前任务状态。')
                return True
            if context is None:
                self.feedback('未获得交互回复授权，请检查自动回复及对应组件操作授权。')
                return True
            if record['kind']=='approval':
                if action not in ('批准','拒绝') or answer:
                    self.feedback(f'请回复：批准 {code} 或 拒绝 {code}')
                    return True
                answer = 'allow' if action=='批准' else 'deny'
            else:
                if action != '回答' or not answer:
                    self.feedback(f'请回复：回答 {code} 你的答案')
                    return True
                answer=answer.strip()
                if answer.isascii() and answer.isdigit() and record['options'] and 1 <= int(answer) <= len(record['options']):
                    answer=record['options'][int(answer)-1]
            record['state']='responding'
            self.save()
            token, target = record['responseToken'], record['target']
        def respond(generation,cancel):
            try:
                if cancel.is_set(): return
                result=context.call(target,'respond',{'request_id':identity,'response_token':token,'answer':answer})
                if not isinstance(result,dict) or result.get('accepted') is not True: raise ValueError('回复未确认')
                message='已回传你的' + ('决定。' if record['kind']=='approval' else '回答。')
                state='answered'
            except Exception as error:
                state='unknown'
                message='交互回复未确认，请查看原任务。'
                if isinstance(error,PlatformError) and error.code in ('permission_required','permission_denied','stale_handle','component_unavailable'):
                    state='submitted'
                    message='交互回复未执行：' + error.code + '，请检查对应授权。'
            with self.lock:
                self.records[identity]['state']=state
                if state!='submitted': self.records[identity]['responseToken']=''
                self.save()
            self.feedback(message)
        try: self.client.worker(respond)
        except PlatformError:
            with self.lock:
                record['state']='submitted'
                self.save()
        return True

    def feedback(self,message):
        def send(generation,cancel):
            deadline=time.monotonic()+10
            while not cancel.is_set() and time.monotonic()<deadline:
                with self.client._lock:
                    if not self.client.active(generation): return
                    if self.client.get()['canSend']:
                        self.client.send(message)
                        return
                cancel.wait(.05)
        try: self.client.worker(send)
        except PlatformError: pass
