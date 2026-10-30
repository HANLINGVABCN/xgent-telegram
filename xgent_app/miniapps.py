"""Small Telegram pages reuse the configured Web origin and existing services."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode, urlsplit

SESSION_SECONDS = 3600
SESSION_KINDS = frozenset({'form', 'read', 'settings', 'tasks', 'files'})


def page_url(namespace, page, **params):
    data = namespace.get('UserDataManager')
    if data is None or not data.get('web_enabled', False) or not data.get('_web_has_password', False):
        return None
    base = str(data.get('web_public_url', '') or '').rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    return base + '/mini/' + page + ('?' + urlencode(params) if params else '')


def form_display(namespace, pending, status='pending'):
    if getattr(pending,'chat_id',1)<=0:
        return None
    url = page_url(namespace, 'form', id=pending.ask_id, conversation_id=pending.conversation_id)
    if not url:
        return None
    count = len(pending.form.questions)
    text = {'pending':f'📝 待填写表单 · {count} 个问题', 'submitted':'✅ 表单已提交',
            'cancelled':'已取消表单', 'expired':'表单已失效'}.get(status, '表单已结束')
    return {'text': text, 'parse_mode': None,
            'reply_markup': [[{'text':'填写表单', 'url':url, 'web_app':True, 'callback_data':''}]] if status=='pending' else None}


def _encode(raw):
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def make_session(key, user_id, kind, resource, *, now=None):
    if kind not in SESSION_KINDS or not isinstance(resource, dict):
        raise ValueError('无效页面')
    stamp = time.time() if now is None else now
    body = {'version':1,'user_id':int(user_id),'kind':kind,'resource':resource,
            'issued_at':stamp,'expires_at':stamp+SESSION_SECONDS}
    payload = _encode(json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode())
    signature = _encode(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    return payload + '.' + signature


def verify_session(key, token, user_id, *, kind=None, now=None):
    if not isinstance(token, str) or len(token)>8192:
        return None
    try:
        payload, signature = token.split('.',1)
        expected = _encode(hmac.new(key,payload.encode(),hashlib.sha256).digest())
        if not hmac.compare_digest(signature,expected):
            return None
        body = json.loads(base64.urlsafe_b64decode(payload + '='*(-len(payload)%4)))
        stamp = time.time() if now is None else now
        if (not isinstance(body,dict) or body.get('version')!=1 or body.get('user_id')!=user_id
                or body.get('kind') not in SESSION_KINDS or kind is not None and body['kind']!=kind
                or not isinstance(body.get('resource'),dict)
                or not isinstance(body.get('issued_at'),(int,float)) or body['issued_at']>stamp+30
                or not isinstance(body.get('expires_at'),(int,float)) or body['expires_at']<=stamp):
            return None
        return body
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None
