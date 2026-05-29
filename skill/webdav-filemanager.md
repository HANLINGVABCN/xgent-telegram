```!
WebDAV 文件管理器：一键部署私有网页网盘和 WebDAV 服务，支持文件上传下载、下载队列、临时下载链接、文本编辑和网页登录。脚本目录：skill/script/webdav-filemanager/，核心文件：install.sh、server.py、index.html。安装命令：chmod +x skill/script/webdav-filemanager/install.sh && sudo bash skill/script/webdav-filemanager/install.sh install（root 用户可去掉 sudo）。卸载命令：sudo bash skill/script/webdav-filemanager/install.sh uninstall。默认端口 8989，默认服务名 webdav-filemanager，部署后访问 http://服务器IP:端口/，WebDAV 地址 http://服务器IP:端口/dav/。
```

# 服务器网盘 + WebDAV + HTTP 测速文件管理器

本文是 Telegram AI Bot 项目内置 skill 文档，用于指导 Agent 模式在服务器上使用 `skill/script/webdav-filemanager/install.sh` 搭建一个带网页文件管理界面、WebDAV 协议支持和 HTTP 测速能力的私有网盘。

## 1. 文件位置

```text
skill/webdav-filemanager.md
skill/script/webdav-filemanager/install.sh
skill/script/webdav-filemanager/server.py
skill/script/webdav-filemanager/index.html
```

其中：

- `install.sh`：一键安装、重新安装、卸载脚本。
- `server.py`：零依赖 Python 文件管理服务器，提供 Web UI API、统一下载流、文本读写、临时下载链接、链接管理、测速文件自动清理、网页登录认证、改密码和 WebDAV 协议。
- `index.html`：网页文件管理器前端界面，包含上传速度面板、下载队列、文本编辑页、链接管理页、设置页、创建文件夹/测速文件/文本文件、右键生成下载链接等功能。
- `webdav-filemanager.md`：当前 skill 说明文件。

## 2. 基本定位

这是一个适合个人 VPS、轻量服务器、家用 Linux 主机使用的私有网盘方案。

核心特点：

- 不依赖 npm（除非使用 pm2）、pip、数据库、nginx。
- 只需要系统有 `python3`，以及 `systemd` 或 `pm2` 作为进程管理器。
- Web 页面可直接管理文件。
- 上传会显示速度面板。
- 网页内下载会进入浏览器下载队列，支持单文件进度、百分比、暂停、继续、删除和拖拽排序。
- 下载队列右侧会显示速度详情、速度图表、MB/s 与 Mbps 切换、总大小和完成统计。
- 传统直链下载直接交给浏览器或下载器处理，适合大文件、下载器、断点续传等场景。
- 右键文件可生成临时下载链接，可自由设置有效时间；链接可在浏览器、下载器等任意客户端直接下载。
- 链接管理页可查看所有临时链接，显示时效、剩余时效、文件状态，并支持复制、下载、删除。
- 新建按钮支持创建文件夹、测速文件和文本文件。
- 测速文件可自定义大小、后缀和有效时间，过期后自动删除。
- 文本文件可在专业编辑页中读取、修改、保存，并支持自定义后缀；新建文本文件后默认回到文件列表。
- `/dav/` 路径提供 WebDAV，可被系统文件管理器、手机文件管理器、支持 WebDAV 的客户端挂载。
- Web 页面使用 Cookie 登录认证。
- WebDAV 使用 Basic Auth 认证。
- 设置页可查看当前用户、根目录、磁盘容量、网盘文件大小、实际占盘，并支持修改密码和退出登录。
- 浏览器返回按钮已适配应用内目录和页面切换。
- 文件根目录可自定义，例如 `/data/files`。
- 安装后会根据选择创建 systemd 或 pm2 服务，支持开机自启和自动重启。

## 3. 一键安装

在项目根目录执行：

```bash
chmod +x skill/script/webdav-filemanager/install.sh
sudo bash skill/script/webdav-filemanager/install.sh install
```

如果当前已经是 `root` 用户，可以去掉 `sudo`：

```bash
bash skill/script/webdav-filemanager/install.sh install
```

脚本会交互式询问：

```text
进程管理方式，默认 pm2（可选 systemd）
端口号，默认 8989
登录账号，默认 admin
登录密码
文件根目录，默认 /data/files
```

推荐填写示例：

```text
进程管理方式：2 (pm2)
端口号：8989
登录账号：admin
登录密码：自行设置强密码
文件根目录：/data/files
```

安装完成后会输出：

```text
Web 页面：  http://服务器IP:8989/
WebDAV：    http://服务器IP:8989/dav/
登录账号：  admin
文件目录：  /data/files
```

如果云服务器有安全组，需要在云厂商控制台手动放行对应 TCP 端口，例如 `8989/tcp`。

## 4. 菜单模式运行

不带参数运行脚本会进入菜单：

```bash
sudo bash skill/script/webdav-filemanager/install.sh
```

菜单选项通常是：

```text
1) 安装 / 重新安装
2) 卸载：删除配置和项目文件，不删除存入的文件
3) 退出
```

Agent 帮用户部署时，如果用户没有明确说卸载，应默认选择安装 / 重新安装。

## 5. 卸载

卸载命令：

```bash
sudo bash skill/script/webdav-filemanager/install.sh uninstall
```

卸载脚本会自动检测使用的是 systemd 还是 pm2，并删除：

```text
/etc/systemd/system/webdav-filemanager.service (如果存在)
pm2 进程 webdav-filemanager (如果存在)
/opt/webdav-filemanager
```

卸载不会主动删除用户存入的文件根目录，例如 `/data/files`。

如果用户要求清空网盘文件，需要额外确认后再删除文件根目录，不要在普通卸载流程里删除用户数据。

注意：卸载会删除程序目录 `/opt/webdav-filemanager`，其中包含 Web 登录密码持久化文件；但不会删除安装时填写的网盘文件根目录。

## 6. 安装后服务信息

默认 systemd / pm2 服务名：

```text
webdav-filemanager
```

默认程序安装目录：

```text
/opt/webdav-filemanager
```

默认服务文件（systemd 模式）：

```text
/etc/systemd/system/webdav-filemanager.service
```

默认文件根目录：

```text
/data/files
```

常用管理命令（systemd 模式）：

```bash
systemctl status webdav-filemanager --no-pager
systemctl restart webdav-filemanager
systemctl stop webdav-filemanager
systemctl enable webdav-filemanager
journalctl -u webdav-filemanager -n 100 --no-pager
```

常用管理命令（pm2 模式）：

```bash
pm2 show webdav-filemanager
pm2 restart webdav-filemanager
pm2 stop webdav-filemanager
pm2 logs webdav-filemanager
```

查看启动命令：

```bash
cat /opt/webdav-filemanager/start.sh
```

查看 Web 登录密码持久化文件（不要公开内容）：

```bash
ls -l /opt/webdav-filemanager/filemanager_auth.json
```

查看服务配置（systemd 模式）：

```bash
cat /etc/systemd/system/webdav-filemanager.service
```

## 7. 直接运行 server.py

如果不想安装任何服务，也可以直接运行：

```bash
python3 skill/script/webdav-filemanager/server.py -H 0.0.0.0 -p 8989 -r /data/files -a admin:你的密码
```

参数说明：

```text
-H / --host    监听地址，默认 0.0.0.0
-p / --port    监听端口，server.py 默认 8080，install.sh 默认 8989
-r / --root    文件根目录
-a / --auth    认证信息，格式 用户名:密码
```

示例：

```bash
mkdir -p /data/files
python3 skill/script/webdav-filemanager/server.py \
  -H 0.0.0.0 \
  -p 8989 \
  -r /data/files \
  -a admin:change_this_password
```

这种方式适合临时测试；正式使用推荐走 `install.sh`，因为它会创建 systemd 或 pm2 服务。

## 8. Web 页面使用方式

浏览器访问：

```text
http://服务器IP:端口/
```

登录后可进行：

- 浏览目录
- 上传文件
- 下载文件和查看下载队列
- 新建文件夹
- 创建测速文件
- 创建和编辑文本文件
- 搜索文件
- 列表 / 网格视图切换
- 复制、剪切、粘贴
- 重命名
- 删除
- 查看文件属性
- 查看磁盘和网盘文件统计
- 设置中查看网页根目录对应的服务器真实目录
- 设置中修改登录密码或退出登录

网页里的“根目录”对应安装时填写的文件根目录，例如：

```text
/data/files
```

首页磁盘条使用网盘口径：

```text
网盘文件大小 / 可用剩余空间 / 服务器总容量
```

设置页会显示更详细的口径：

- 磁盘总容量、已用、剩余。
- 根目录文件大小：文件标称大小合计，稀疏测速文件会按标称大小显示。
- 根目录实际占盘：真实磁盘块占用，稀疏测速文件可能远小于标称大小。

## 9. WebDAV 使用方式

WebDAV 地址：

```text
http://服务器IP:端口/dav/
```

认证方式：

```text
账号：安装时设置的登录账号
密码：安装时设置的登录密码
```

常见客户端填写：

```text
服务器地址：http://服务器IP:8989/dav/
用户名：admin
密码：你的密码
```

如果客户端支持 HTTPS，建议在前面额外套反向代理和 TLS；当前脚本本身只提供 HTTP 服务。

## 10. Agent 推荐执行流程

当用户要求“搭建网盘”、“部署 WebDAV”、“做一个私有云盘”、“服务器文件管理器”、“网页文件管理”、“挂载 WebDAV”时，应优先读取本文。

推荐流程：

1. 确认当前环境是 Linux VPS / 服务器。
2. 确认有 root 或 sudo 权限。
3. 确认 `skill/script/webdav-filemanager/` 下存在 `install.sh`、`server.py`、`index.html`。
4. 运行安装命令。
5. 按用户要求填写进程管理方式、端口、账号、密码、文件根目录。
6. 安装完成后检查服务状态。
7. 告诉用户 Web 页面地址和 WebDAV 地址。
8. 提醒用户放行云服务器安全组端口。

推荐命令：

```bash
ls -lah skill/script/webdav-filemanager
chmod +x skill/script/webdav-filemanager/install.sh
sudo bash skill/script/webdav-filemanager/install.sh install
pm2 show webdav-filemanager
# 或者 systemctl status webdav-filemanager --no-pager
```

如果当前已经是 `root` 用户，安装命令可去掉 `sudo`。

如果需要长期观察安装过程，可使用交互式 shell；如果只是检查文件或状态，可使用普通 exec。

## 11. 防火墙和安全组提醒

脚本本身主要负责部署程序和服务。服务器外部能否访问，还取决于：

- 系统防火墙
- 云服务器安全组
- NAT / 端口映射
- 服务器公网 IP
- 运营商或机房端口限制

如果浏览器无法访问，依次检查：

```bash
# systemd 模式:
systemctl status webdav-filemanager --no-pager
# pm2 模式:
pm2 show webdav-filemanager

ss -lntp | grep 8989
curl -I http://127.0.0.1:8989/
hostname -I
```

如果本机 `curl` 正常，但外部打不开，通常是安全组或防火墙没有放行端口。

## 12. 常见故障处理

### 12.1 提示找不到 server.py 或 index.html

原因：`install.sh`、`server.py`、`index.html` 不在同一目录。

检查：

```bash
ls -lah skill/script/webdav-filemanager
```

正确目录里应该有：

```text
install.sh
server.py
index.html
```

### 12.2 提示未找到 python3

安装 Python 3：

```bash
apt update && apt install -y python3
```

或在 CentOS / Rocky / AlmaLinux：

```bash
dnf install -y python3
```

### 12.3 提示未找到 systemctl

说明当前系统可能不是 systemd 环境，例如部分容器、精简系统或 OpenRC 系统。

此时可以在安装时选择 `pm2` 作为进程管理器；如果连 npm 也没有，可以改用直接运行：

```bash
python3 server.py -H 0.0.0.0 -p 8989 -r /data/files -a admin:你的密码
```

### 12.4 登录失败

如果仍能登录，可在网页右上角设置里修改密码。

如果忘记密码，先检查启动命令里的 `-a 用户名:密码`：

```bash
cat /opt/webdav-filemanager/start.sh
```

如果曾在网页里改过密码，实际登录密码会优先保存到：

```bash
/opt/webdav-filemanager/filemanager_auth.json
```

忘记网页里修改后的密码时，可以删除该文件并重启服务，让服务回退到 `start.sh` 里的账号密码：

```bash
rm -f /opt/webdav-filemanager/filemanager_auth.json
systemctl restart webdav-filemanager
```

也可以重新运行安装脚本重新配置：

```bash
sudo bash skill/script/webdav-filemanager/install.sh install
```

### 12.5 WebDAV 客户端无法连接

检查：

```bash
curl -i -u 用户名:密码 http://127.0.0.1:8989/dav/
```

如果返回 HTTP 响应，说明服务本身正常。外部客户端仍无法连接时，优先检查端口、安全组、防火墙、客户端是否要求 HTTPS。

## 13. 安全注意事项

- 不要使用弱密码。
- 不建议把网盘端口直接暴露给不可信网络。
- 公网长期使用时，建议前置 Nginx / Caddy / Cloudflare Tunnel / HTTPS 反代。
- WebDAV 使用 Basic Auth，明文 HTTP 下密码可能被中间人截获。
- 文件删除操作不可恢复，Agent 删除文件前必须确认用户意图。
- 卸载脚本默认保留文件根目录，这是正确行为，不要擅自删除用户数据。
- 测速文件通常使用稀疏文件创建，文件标称大小可能很大，但真实磁盘占用可能很小；需要区分“文件大小”和“实际占盘”。

## 14. 推荐给用户的简短说明

部署完成后可以这样回复用户：

```text
网盘已经部署完成。

Web 页面：
http://服务器IP:端口/

WebDAV 地址：
http://服务器IP:端口/dav/

账号：
安装时设置的账号

密码：
安装时设置的密码

如果外部打不开，请先在云服务器安全组放行对应 TCP 端口。
```
