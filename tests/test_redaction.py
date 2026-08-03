"""凭据脱敏的回归测试。

provider 的 api_key 存在数据库里而不是环境变量，之前完全不在脱敏名单内，
会明文出现在 trace 日志、错误响应体和写进 AI 可见记忆的异常串里。
"""

import logging
import os
import re
import sys
import unittest
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_redaction_helpers():
    """只加载脱敏相关函数，避免拉起整个 bot。"""
    source = (ROOT / 'xgent_app' / 'sections' / 'core.py').read_text(encoding='utf-8')
    start = source.index('def redact_sensitive_text')
    end = source.index('def format_provider_exception')

    class _BotConfig:
        TOKEN = '123456:BOT-TOKEN-VALUE'
        UPDATE_GITHUB_TOKEN = 'ghp_UPDATETOKENVALUE'
        TAVILY_API_KEY = 'tvly-SEARCHKEYVALUE'

    _WS = re.compile(r'\s+')

    def parse_api_keys(field: str) -> List[str]:
        return [_WS.sub('', part) for part in str(field or '').split(',') if _WS.sub('', part)]

    namespace: Dict[str, Any] = {
        're': re, 'os': os, 'logger': logging.getLogger('test'),
        'Any': Any, 'Dict': Dict, 'List': List, 'Optional': Optional, 'Tuple': Tuple,
        'BotConfig': _BotConfig,
        'parse_api_keys': parse_api_keys,
        'urllib': urllib,
    }
    exec(compile(source[start:end], 'core.py', 'exec'), namespace)
    return namespace


class RedactionTests(unittest.TestCase):
    def setUp(self):
        self.ns = _load_redaction_helpers()
        self.redact = self.ns['redact_sensitive_text']
        self.ns['_RUNTIME_SECRETS'].clear()

    def test_redacts_env_secrets(self):
        text = 'failed with token 123456:BOT-TOKEN-VALUE end'
        self.assertNotIn('123456:BOT-TOKEN-VALUE', self.redact(text))

    def test_redacts_registered_provider_key(self):
        self.ns['register_runtime_secret']('sk-abcdef1234567890')
        text = 'HTTP 401: {"error": "invalid key sk-abcdef1234567890"}'
        redacted = self.redact(text)
        self.assertNotIn('sk-abcdef1234567890', redacted,
                         'provider 的 api_key 没有被脱敏')
        self.assertIn('[REDACTED_API_KEY]', redacted)

    def test_redacts_all_keys_from_provider_dict(self):
        self.ns['register_provider_secrets']({
            'openai': {'api_key': 'sk-first1234567890'},
            'claude': {'api_key': 'sk-second1234567890'},
        })
        text = 'sk-first1234567890 and sk-second1234567890'
        redacted = self.redact(text)
        self.assertNotIn('sk-first1234567890', redacted)
        self.assertNotIn('sk-second1234567890', redacted)

    def test_redacts_comma_separated_multi_keys(self):
        # 这个项目支持一个 provider 配多个逗号分隔的 key
        self.ns['register_runtime_secret']('sk-aaaa11112222, sk-bbbb33334444')
        redacted = self.redact('leak sk-bbbb33334444 here')
        self.assertNotIn('sk-bbbb33334444', redacted)

    def test_ignores_short_values(self):
        """太短的值不登记，避免在正常文本里误伤。"""
        self.ns['register_runtime_secret']('abc')
        self.assertEqual('abc def', self.redact('abc def'))


class ProviderUrlValidationTests(unittest.TestCase):
    def setUp(self):
        source = (ROOT / 'xgent_app' / 'sections' / 'core.py').read_text(encoding='utf-8')
        start = source.index('def validate_provider_base_url')
        end = source.index('PROVIDER_HTTP_HEADERS = {')
        namespace: Dict[str, Any] = {
            'urllib': urllib, 'Tuple': Tuple, 'Optional': Optional,
        }
        exec(compile(source[start:end], 'core.py', 'exec'), namespace)
        self.validate = namespace['validate_provider_base_url']

    def test_rejects_non_url_starting_with_http(self):
        # 旧的 startswith("http") 校验会放行这个
        with self.assertRaises(ValueError):
            self.validate('httpevil.com')

    def test_rejects_missing_scheme(self):
        with self.assertRaises(ValueError):
            self.validate('api.example.com/v1')

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            self.validate('   ')

    def test_accepts_https_without_warning(self):
        url, warning = self.validate('https://api.example.com/v1')
        self.assertEqual('https://api.example.com/v1', url)
        self.assertIsNone(warning)

    def test_warns_on_plain_http_remote(self):
        _url, warning = self.validate('http://api.example.com/v1')
        self.assertIsNotNone(warning, 'http:// 远程地址应该给出明文传输警告')

    def test_no_warning_for_localhost(self):
        for url in ('http://localhost:8080/v1', 'http://127.0.0.1:1234/v1'):
            _u, warning = self.validate(url)
            self.assertIsNone(warning, f'本地地址不该警告: {url}')


if __name__ == '__main__':
    unittest.main()
