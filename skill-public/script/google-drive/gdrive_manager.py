import os
import sys
import io
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from google.auth.exceptions import RefreshError

SCOPES = ['https://www.googleapis.com/auth/drive']


def _err(msg):
    """统一错误输出，便于 Agent 解析。"""
    print(f"ERROR: {msg}")
    sys.exit(1)


def get_service():
    client_id = os.environ.get("GDRIVE_CLIENT_ID")
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET")
    refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN")
    token_uri = os.environ.get("GDRIVE_TOKEN_URI", "https://oauth2.googleapis.com/token")

    if not (client_id and client_secret and refresh_token):
        _err("GDRIVE 环境变量缺失，请先 `set -a && . /etc/environment && set +a` 加载凭证")

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES
    )
    try:
        creds.refresh(Request())
    except RefreshError:
        _err("refresh_token 已失效，需按正文第 2 节重新授权")
    return build('drive', 'v3', credentials=creds)


def exec_code(code):
    """执行任意 Python 代码片段，service 对象已预置可用。

    脚本包好凭证构造与 service 初始化，Agent 只写业务逻辑那几行。
    service / io / os / MediaFileUpload / MediaIoBaseDownload 已注入可直接用。
    代码通过命令行参数或 stdin 传入。
    """
    if not code.strip():
        _err("缺少代码，用法: exec '<python 代码>' 或通过 stdin 传入")
    try:
        service = get_service()
        namespace = {
            'service': service,
            'io': io,
            'os': os,
            'MediaFileUpload': MediaFileUpload,
            'MediaIoBaseDownload': MediaIoBaseDownload,
        }
        try:
            exec(compile(code, '<exec>', 'exec'), namespace)
            print("SUCCESS: exec done喵!")
        except SystemExit:
            pass  # 代码内主动 sys.exit 不视为错误
    except RefreshError:
        _err("refresh_token 已失效，需按正文第 2 节重新授权")
    except Exception as e:
        _err(f"exec 执行失败: {e}")


USAGE = """Google Drive 管理脚本（exec 模式）
用法:
  python3 gdrive_manager.py exec '<代码>'
  echo '<代码>' | python3 gdrive_manager.py exec

脚本预置 service 对象（凭证构造、refresh、build 全包好），你只写业务逻辑。
已注入符号: service, io, os, MediaFileUpload, MediaIoBaseDownload

环境变量（需先 `set -a && . /etc/environment && set +a` 加载）:
  GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET, GDRIVE_REFRESH_TOKEN, GDRIVE_TOKEN_URI

输出前缀: SUCCESS: / ERROR: / PROGRESS:（代码内自行打印进度）"""


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == 'exec':
        code = sys.argv[2] if len(sys.argv) > 2 else sys.stdin.read()
        exec_code(code)
    else:
        print(USAGE)
        sys.exit(1)
