```!
Google Drive 网盘技能：用 exec 模式操作 Google Drive 文件（列出/搜索/上传/下载/创建文件夹/移动/删除等全靠 Python 代码片段，灵活组合）。凭证已存入环境变量 GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET, GDRIVE_REFRESH_TOKEN, GDRIVE_TOKEN_URI，命令前需先 `set -a && . /etc/environment && set +a` 加载。脚本预置 service 对象（凭证构造全包好），用法：`python3 skill/script/google-drive/gdrive_manager.py exec '<代码>'` 或 `echo '<代码>' | python3 skill/script/google-drive/gdrive_manager.py exec`。已注入 service/io/os/MediaFileUpload/MediaIoBaseDownload。输出前缀 SUCCESS:/ERROR:。安全规范：严禁打印、回显或读取密钥明文！配置详情请看正文。
```

# Google Drive 管理技能

本技能用 exec 模式操作 Google Drive：脚本包好凭证构造与 service 初始化，Agent 只写业务逻辑那几行 Python 代码，`service` 对象直接可用。免去手写 import 与凭证构造（最易写错、最占上下文的部分），同时保留纯代码的灵活性——组合操作在一个进程里串起来，变量自由传递。凭证已存入环境变量，无本地 JSON 敏感文件。

## 环境变量要求
凭证已保存在 `/etc/environment` 与 `~/.bashrc` 中：
- `GDRIVE_CLIENT_ID`
- `GDRIVE_CLIENT_SECRET`
- `GDRIVE_REFRESH_TOKEN`
- `GDRIVE_TOKEN_URI`

## 1. 脚本用法

配套脚本路径：`skill/script/google-drive/gdrive_manager.py`（相对项目根目录）

所有命令需先加载凭证：

```bash
set -a && . /etc/environment && set +a
```

调用方式（两种）：
```bash
# 方式一：代码作为命令行参数
python3 skill/script/google-drive/gdrive_manager.py exec '<代码>'

# 方式二：代码通过 stdin 传入（推荐，不用转义单引号）
echo '<代码>' | python3 skill/script/google-drive/gdrive_manager.py exec
```

### 预置符号
脚本已注入以下符号，代码中直接用，不用 import：
- `service` — 已构造好的 Drive service 对象（`build('drive','v3',credentials=creds)`）
- `io` — 标准库 io 模块
- `os` — 标准库 os 模块
- `MediaFileUpload` — 上传辅助类
- `MediaIoBaseDownload` — 下载辅助类

### 输出前缀
- `SUCCESS:` 操作成功
- `ERROR:` 操作失败（含原因）
- 代码内 `print()` 的内容原样输出，可自行打印进度

## 2. 常用操作示例

### 列出文件
```bash
echo '
results = service.files().list(pageSize=10, fields="files(id, name, mimeType, size)").execute()
for f in results.get("files", []):
    print(f"- {f[\"name\"]} (ID: {f[\"id\"]}) | {f[\"mimeType\"]} | {f.get(\"size\", \"N/A\")}B")
' | python3 skill/script/google-drive/gdrive_manager.py exec
```

### 按名搜索
```bash
echo '
results = service.files().list(q="name contains '\''报告'\''", fields="files(id, name)").execute()
for f in results.get("files", []):
    print(f["name"], f["id"])
' | python3 skill/script/google-drive/gdrive_manager.py exec
```

### 查询文件信息
```bash
echo '
meta = service.files().get(fileId="1abc文件ID", fields="id,name,mimeType,size,createdTime,modifiedTime").execute()
print(meta)
' | python3 skill/script/google-drive/gdrive_manager.py exec
```

### 上传文件
```bash
echo '
media = MediaFileUpload("/tmp/report.pdf", resumable=True)
r = service.files().create(body={"name":"report.pdf"}, media_body=media, fields="id,name").execute()
print("uploaded", r["name"], r["id"])
' | python3 skill/script/google-drive/gdrive_manager.py exec
```

### 上传到指定文件夹
```bash
echo '
folder_id = "1abc文件夹ID"
media = MediaFileUpload("/tmp/report.pdf", resumable=True)
r = service.files().create(body={"name":"report.pdf","parents":[folder_id]}, media_body=media, fields="id,name").execute()
print("uploaded", r["name"], r["id"])
' | python3 skill/script/google-drive/gdrive_manager.py exec
```

### 创建文件夹并立即上传（组合操作）
```bash
echo '
folder = service.files().create(body={"name":"2024报告","mimeType":"application/vnd.google-apps.folder"}).execute()
media = MediaFileUpload("/tmp/a.txt", resumable=True)
service.files().create(body={"name":"a.txt","parents":[folder["id"]]}, media_body=media, fields="id").execute()
print("uploaded to", folder["id"])
' | python3 skill/script/google-drive/gdrive_manager.py exec
```

### 下载文件
```bash
echo '
request = service.files().get_media(fileId="1abc文件ID")
fh = io.FileIO("/tmp/save.pdf", "wb")
downloader = MediaIoBaseDownload(fh, request)
done = False
while not done:
    _, done = downloader.next_chunk()
print("downloaded")
' | python3 skill/script/google-drive/gdrive_manager.py exec
```

### 批量下载搜索结果（组合操作）
```bash
echo '
results = service.files().list(q="name contains '\''报告'\''", fields="files(id,name)").execute()
for f in results.get("files", []):
    request = service.files().get_media(fileId=f["id"])
    fh = io.FileIO("/tmp/" + f["name"], "wb")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    print("downloaded", f["name"])
' | python3 skill/script/google-drive/gdrive_manager.py exec
```

### 移动文件
```bash
echo '
meta = service.files().get(fileId="1abc文件ID", fields="parents").execute()
old = meta.get("parents", [])
service.files().update(fileId="1abc文件ID", addParents="1abc目标文件夹ID", removeParents=",".join(old)).execute()
print("moved")
' | python3 skill/script/google-drive/gdrive_manager.py exec
```

### 删除文件
```bash
echo '
service.files().delete(fileId="1abc文件ID").execute()
print("deleted")
' | python3 skill/script/google-drive/gdrive_manager.py exec
```

### 清空文件夹内容后删除文件夹（组合操作）
```bash
echo '
folder_id = "1abc文件夹ID"
items = service.files().list(q="parents in '\''%s'\''" % folder_id, fields="files(id)").execute().get("files", [])
for f in items:
    service.files().delete(fileId=f["id"]).execute()
service.files().delete(fileId=folder_id).execute()
print("deleted folder", folder_id)
' | python3 skill/script/google-drive/gdrive_manager.py exec
```

### 单引号转义说明
stdin 方式（`echo '...' | exec`）中，代码内的单引号需转义为 `'\''`。如果代码复杂、单引号多，可改用 here-doc：
```bash
python3 skill/script/google-drive/gdrive_manager.py exec <<'EOF'
results = service.files().list(pageSize=10, fields="files(id,name)").execute()
for f in results.get("files", []):
    print(f["name"], f["id"])
EOF
```
here-doc 用 `<<'EOF'`（引号包裹 EOF）可完全避免转义，推荐复杂代码用这种方式。

## 3. JSON 凭证授权与环境变量提取流程

仅首次配置或更换账号时需要执行，日常使用无需重复。

1. 接收 Google Cloud 导出的 OAuth Client ID / Secret JSON 凭证（`redirect_uri` 设为 `http://localhost`）。
2. 构造授权 URL：
   `https://accounts.google.com/o/oauth2/v2/auth?response_type=code&client_id=...&redirect_uri=http://localhost&scope=https://www.googleapis.com/auth/drive&access_type=offline&prompt=consent`
3. 在浏览器授权后，从回调的 `localhost` 网址中解析出 `code` 参数。
4. 向 `token_uri` (`https://oauth2.googleapis.com/token`) 发送 POST 请求（`grant_type=authorization_code`），换取 `refresh_token`。
5. 将 `GDRIVE_CLIENT_ID`、`GDRIVE_CLIENT_SECRET`、`GDRIVE_REFRESH_TOKEN`、`GDRIVE_TOKEN_URI` 静悄悄写入 `/etc/environment` 与 `~/.bashrc`，随后彻底删除本地 JSON 凭证与 Token 文件。

## 4. 安全规范
- 严禁打印、回显或暴露出 `GDRIVE_CLIENT_SECRET` 或 `GDRIVE_REFRESH_TOKEN` 明文。
- 随用随调，绝不在磁盘留存任何临时脚本与凭证文件。
