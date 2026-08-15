"""webdav-filemanager 远程下载的 SSRF 防护测试。

原实现只检查 scheme 是 http/https，不校验目标 IP，且 urlopen 会自动跟随
重定向。攻击者提交 http://169.254.169.254/（云元数据）或 http://127.0.0.1/
即可让服务器代为访问，响应落盘后再通过 /api/download 读出。
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / 'skill-public' / 'script' / 'webdav-filemanager' / 'server.py'


def _load_ssrf_helpers(allow_private=False):
    """只加载 SSRF 校验相关函数，不启动服务器。"""
    import ipaddress
    import socket
    import urllib.parse
    import urllib.request

    source = SERVER.read_text(encoding='utf-8')
    start = source.index('class _NoRedirect')
    end = source.index('def remote_download_worker')

    namespace = {
        'urllib': urllib,
        'socket': socket,
        'ipaddress': ipaddress,
        'os': os,
        'ALLOW_PRIVATE_REMOTE': allow_private,
    }
    exec(compile(source[start:end], 'server.py', 'exec'), namespace)
    return namespace


class RemoteUrlValidationTests(unittest.TestCase):
    def setUp(self):
        self.ns = _load_ssrf_helpers()
        self.validate = self.ns['validate_remote_url']

    def test_rejects_cloud_metadata_address(self):
        ok, err = self.validate('http://169.254.169.254/latest/meta-data/')
        self.assertFalse(ok, '云元数据地址必须被拒绝')
        self.assertIn('169.254.169.254', err)

    def test_rejects_loopback(self):
        for url in ('http://127.0.0.1:8080/x', 'http://localhost/x'):
            ok, _err = self.validate(url)
            self.assertFalse(ok, f'环回地址必须被拒绝: {url}')

    def test_rejects_private_ranges(self):
        for url in ('http://10.0.0.5/x', 'http://192.168.1.1/x', 'http://172.16.0.1/x'):
            ok, _err = self.validate(url)
            self.assertFalse(ok, f'私网地址必须被拒绝: {url}')

    def test_rejects_non_http_scheme(self):
        for url in ('file:///etc/passwd', 'ftp://example.com/x', 'gopher://x/'):
            ok, _err = self.validate(url)
            self.assertFalse(ok, f'非 http(s) scheme 必须被拒绝: {url}')

    def test_rejects_unresolvable_host(self):
        ok, _err = self.validate('http://this-host-should-not-exist.invalid/x')
        self.assertFalse(ok)

    def test_allows_public_address(self):
        ok, err = self.validate('http://93.184.216.34/x')  # example.com 的公网 IP
        self.assertTrue(ok, f'公网地址不该被拒绝: {err}')

    def test_opt_in_env_allows_private(self):
        ns = _load_ssrf_helpers(allow_private=True)
        ok, _err = ns['validate_remote_url']('http://127.0.0.1:8080/x')
        self.assertTrue(ok, '显式开启后应允许内网地址')

    def test_redirect_handler_raises_instead_of_following(self):
        """重定向必须交回调用方逐跳校验，而不是自动跟随。"""
        handler = self.ns['_NoRedirect']()
        with self.assertRaises(self.ns['_RedirectTo']) as ctx:
            handler.redirect_request(None, None, 302, 'Found', {},
                                     'http://169.254.169.254/')
        self.assertEqual('http://169.254.169.254/', ctx.exception.url)


if __name__ == '__main__':
    unittest.main()
