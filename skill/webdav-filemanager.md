```!
WebDAV网盘技能：一键部署网页网盘及WebDAV服务。安装: sudo bash skill/script/webdav-filemanager/install.sh install。日常更新: sudo bash skill/script/webdav-filemanager/install.sh update (注意：日常更新严禁用install，否则会覆盖账号)。卸载: 传参数 uninstall。默认端口 8989。同名文件严格不覆盖。详细功能、常见故障排查请先 read 本文档全文。
```

# 服务器网盘 + WebDAV + HTTP 测速文件管理器

本文是 XGent for Telegram 项目内置 skill 文档，用于指导 Agent 模式在服务器上使用 `skill/script/webdav-filemanager/install.sh` 搭建一个带网页文件管理界面、WebDAV 协议支持和 HTTP 测速能力的私有网盘。

## 1. 文件位置

```text
skill/webdav-filemanager.md
skill/script/webdav-filemanager/install.sh
skill/script/webdav-filemanager/server.py
skill/script/webdav-filemanager/index.html
```

其中：

- `install.sh`：一键安装、重新安装、更新、卸载脚本。
- `server.py`：零依赖 Python 文件管理服务器，提供 Web UI API、统一下载流、远程下载、文本读写、临时下载链接、链接访问统计、链接回收站、测速文件自动清理、网页登录认证、改账号/密码、WebDAV 协议和压缩/解压 API。
- `index.html`：网页文件管理器前端界面，包含传输中心、文件/文件夹上传、后台上传/下载任务、文本编辑页、链接管理页、设置页、创建文件夹/测速文件/文本文件、右键生成下载链接、右键压缩/解压等功能。
- `webdav-filemanager.md`：当前 skill 说明文件。

## 2. 基本定位

这是一个适合个人 VPS、轻量服务器、家用 Linux 主机使用的私有网盘方案。

核心特点：

- 不依赖 npm（除非使用 pm2）、pip、数据库、nginx。
- 只需要系统有 `python3`，以及 `systemd` 或 `pm2` 作为进程管理器。
- Web 页面可直接管理文件。
- 顶部“传输”按钮进入传输中心，右上角红点显示任务数量。
- 传输中心包含远程下载、上传、下载三个子项，任务可后台执行。
- 远程下载会把 URL 下载到文件根目录下的 `download` 文件夹。
- 上传支持选择文件或整个文件夹，上传弹窗支持后台继续。
- 上传、下载任务支持进度、百分比、暂停、继续、取消、删除和详情查看。
- 任务详情会显示速度、已传输大小、总大小、状态、速度图表、MB/s 与 Mbps 切换、完成统计。
- 传统直链下载直接交给浏览器或下载器处理，适合大文件、下载器、断点续传等场景。
- 右键文件可生成临时下载链接，可自由设置有效时间；链接可在浏览器、下载器等任意客户端直接下载。
- 链接管理页可查看所有临时链接，显示时效、剩余时效、文件状态，并支持复制、下载、删除。
- 链接详情会记录访问 IP、每个 IP 的下载次数和访问明细；删除链接会先进入回收站，回收站内仍可查看统计，支持彻底删除和清空。
- 上传、复制、移动、重命名都禁止覆盖同名文件或文件夹。
- 新建按钮支持创建文件夹、测速文件和文本文件。
- 测速文件可自定义大小、后缀和有效时间，过期后自动删除。
- 文本文件可在专业编辑页中读取、修改、保存，并支持自定义后缀；新建文本文件后默认回到文件列表。
- `/dav/` 路径提供 WebDAV，可被系统文件管理器、手机文件管理器、支持 WebDAV 的客户端挂载。
- WebDAV 客户端上传的文件会自动存放在网盘根目录下的 `webdav/` 文件夹中。
- Web 页面使用 Cookie 登录认证。
- WebDAV 使用 Basic Auth 认证。
- 设置页可查看当前用户、根目录、磁盘容量、网盘文件大小、实际占盘，并支持修改登录账号、修改密码和退出登录。
- 桌面端支持从文件列表空白区域拖拽框选，移动端支持长按空白区域后拖拽框选；移动端模拟右键/操作菜单长按触发时间为 2 秒，降低误触；长按震动反馈可在设置页开关。
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
进程管理方式：1 (pm2)
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

菜单选项：

```text
1) 安装 / 重新安装
2) 更新程序：保留端口、账号、目录和网盘文件
3) 卸载：全面删除程序和服务，不删除存入的文件
4) 退出
```

**菜单选项说明**：

- **选项 1（安装 / 重新安装）**：会交互式询问端口、账号、密码、文件根目录、进程管理方式。如果已经安装过，会覆盖所有配置，相当于全新安装。适合首次部署或需要更改端口、账号、文件根目录的场景。
- **选项 2（更新程序）**：只替换程序文件（server.py、index.html），保留所有配置和数据，不会询问任何问题，自动重启服务。适合程序升级、bug 修复、功能更新的场景。**这是日常更新的推荐方式**。
- **选项 3（卸载）**：全面删除程序、服务配置、pm2 进程记录，但保留网盘文件根目录（例如 `/data/files`）。
- **选项 4（退出）**：退出脚本，不执行任何操作。

**Agent 执行建议**：

- 首次部署：选择选项 1（安装）。
- 程序更新：选择选项 2（更新），或直接使用命令 `sudo bash skill/script/webdav-filemanager/install.sh update`。
- 用户明确要求卸载：选择选项 3（卸载）。
- 用户要求"重新配置端口/账号/目录"：选择选项 1（重新安装）。

## 5. 更新程序（推荐方式）

当项目里的 `server.py` 或 `index.html` 有新版本时，**不需要重新走安装问答，不需要重新输入端口、账号、密码**。直接执行：

```bash
sudo bash skill/script/webdav-filemanager/install.sh update
```

或者通过菜单模式选择选项 2：

```bash
sudo bash skill/script/webdav-filemanager/install.sh
# 然后输入 2 选择"更新程序"
```

### 5.1 更新命令的完整行为

**会替换的文件**（程序文件）：

```text
/opt/webdav-filemanager/server.py      ← 替换为新版本
/opt/webdav-filemanager/index.html     ← 替换为新版本
/opt/webdav-filemanager/config.env     ← 刷新配置（保留原有端口、目录、账号）
/opt/webdav-filemanager/start.sh       ← 刷新启动脚本（保留原有配置）
```

**会保留的文件**（用户数据和状态）：

```text
/opt/webdav-filemanager/filemanager_auth.json    ← 网页登录账号密码持久化文件（用户在网页里修改过的账号或密码）
/opt/webdav-filemanager/filemanager_state.json   ← 临时下载链接状态文件（用户创建的临时链接）
/data/files（或用户自定义的文件根目录）          ← 网盘文件根目录，完全不动
```

**会保留的配置**：

```text
端口号（例如 8989）
文件根目录（例如 /data/files）
登录账号（例如 admin）
登录账号和密码（优先从 filemanager_auth.json 读取，再回退 config.env/start.sh）
进程管理方式（systemd 或 pm2）
监听地址（通常是 0.0.0.0）
```

**自动执行的操作**：

```text
自动重启 systemd 服务（如果使用 systemd）
自动重启 pm2 进程（如果使用 pm2）
自动刷新 systemd daemon（systemctl daemon-reload）
```

### 5.2 更新的兼容性处理

更新脚本会智能读取旧版本的配置：

1. **优先读取 `config.env`**（新版本安装或更新后生成的配置文件）。
2. **如果没有 `config.env`，会解析 `start.sh`**（老版本安装时生成的启动脚本）。
3. **最后读取 `filemanager_auth.json` 并以它为准**（用户在网页里修改过的账号或密码）。

这意味着：

- 从老版本（没有 `config.env`）更新到新版本时，脚本会自动从 `start.sh` 提取配置，并生成 `config.env`。
- 后续更新会直接复用 `config.env`，不再需要解析 `start.sh`。
- 用户在网页里修改过的账号或密码会被保留（`filemanager_auth.json` 不会被删除），并会同步刷新到 `config.env` 和 `start.sh`。

### 5.3 更新与重新安装的区别

| 操作 | 更新（update） | 重新安装（install） |
|------|----------------|---------------------|
| 是否询问端口 | ❌ 不询问，保留原端口 | ✅ 询问 |
| 是否询问账号密码 | ❌ 不询问，保留原账号密码 | ✅ 询问 |
| 是否询问文件根目录 | ❌ 不询问，保留原目录 | ✅ 询问 |
| 是否询问进程管理方式 | ❌ 不询问，保留原方式 | ✅ 询问 |
| 是否替换 server.py | ✅ 替换 | ✅ 替换 |
| 是否替换 index.html | ✅ 替换 | ✅ 替换 |
| 是否保留 filemanager_auth.json | ✅ 保留 | ❌ 覆盖（使用新账号密码） |
| 是否保留 filemanager_state.json | ✅ 保留 | ✅ 保留 |
| 是否保留网盘文件 | ✅ 保留 | ✅ 保留 |
| 适用场景 | 程序升级、bug 修复 | 首次部署、更改端口/账号/目录 |

**重要提示**：

- **日常程序更新请使用 `update` 命令**，不要使用 `install`。
- **`install` 会覆盖 `filemanager_auth.json`**，导致用户在网页里修改过的账号密码丢失。
- **`update` 是无损更新**，只替换程序文件，不影响用户数据和配置。

### 5.4 更新示例输出

执行更新命令后，脚本会输出：

```text
即将更新 WebDAV 文件管理器程序文件。
会替换：/opt/webdav-filemanager/server.py、/opt/webdav-filemanager/index.html
会保留：登录密码持久化文件、临时链接状态、网盘文件根目录
当前端口：8989
文件根目录：/data/files

已刷新运行配置：/opt/webdav-filemanager/config.env
已重启 systemd 服务：webdav-filemanager
（或：已重启 pm2 进程：webdav-filemanager）

更新完成。
网盘文件根目录未被修改或删除。
```

## 6. 卸载

卸载命令：

```bash
sudo bash skill/script/webdav-filemanager/install.sh uninstall
```

卸载脚本会全面清理程序侧内容，并保留网盘文件根目录。会删除：

```text
systemd 服务 webdav-filemanager 及残留链接
pm2 进程 webdav-filemanager 及该进程日志/PID
残留运行进程（/opt/webdav-filemanager/start.sh 或 server.py）
/opt/webdav-filemanager（程序配置、登录密码持久化文件、临时链接状态）
```

卸载不会主动删除用户存入的文件根目录，例如 `/data/files`。

如果用户要求清空网盘文件，需要额外确认后再删除文件根目录，不要在普通卸载流程里删除用户数据。

注意：卸载会删除程序目录 `/opt/webdav-filemanager`，其中包含 Web 登录密码持久化文件；但不会删除安装时填写的网盘文件根目录。

## 7. 安装后服务信息

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
- 通过传输中心上传文件或文件夹
- 通过传输中心进行远程下载、上传、下载任务管理
- 下载文件和查看下载任务
- 新建文件夹
- 创建测速文件
- 创建和编辑文本文件
- 搜索文件
- 列表 / 网格视图切换
- 复制、剪切、粘贴
- 重命名
- 删除
- 查看文件属性
- 管理临时下载链接、查看链接访问 IP 统计和回收站
- 查看磁盘和网盘文件统计
- 设置中查看网页根目录对应的服务器真实目录
- 设置中修改登录账号、登录密码或退出登录

### 8.1 传输中心

首页顶部“传输”按钮进入传输中心，按钮右上角红点显示任务数量。

传输中心有三个子项：

- **远程下载**：输入 URL 后由服务器后台下载，文件自动保存到文件根目录下的 `download` 文件夹。
- **上传**：在上传子项中选择上传文件或上传文件夹，上传弹窗可选择后台继续，任务会留在上传队列中继续执行。
- **下载**：管理网页下载任务。

进行中的任务可查看速度、进度、已传输大小和状态。任务按钮使用常见符号：暂停 `⏸`、开始/继续 `▶`、取消 `✕`、完成 `✓`。

### 8.2 同名处理规则

任何同名文件或文件夹都不允许覆盖。

判断范围是“当前目标目录”的直接子项：

- 上传 `a.txt` 到 `/docs` 时，只检查 `/docs/a.txt` 是否存在。
- 上传文件夹 `photos` 到 `/docs` 时，只检查 `/docs/photos` 是否存在。
- 进入已有目录 `/docs/photos` 后上传文件，只检查 `/docs/photos/` 里的直接同名项。
- 移动文件或文件夹到目标目录时，目标目录里已有同名文件或文件夹就拒绝移动。
- 重命名时，同目录里已有同名文件或文件夹就拒绝重命名。
- 复制时，目标目录里已有同名文件或文件夹就拒绝复制，不自动生成“副本”名称。

上传遇到当前目标目录直接同名时，前端弹出重命名提醒，用户只能选择重命名后继续或取消任务。

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

当用户要求”搭建网盘”、”部署 WebDAV”、”做一个私有云盘”、”服务器文件管理器”、”网页文件管理”、”挂载 WebDAV”时，应优先读取本文。

### 10.1 首次安装流程

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

如果需要观察安装过程、检查文件或查看状态，都使用 `shell` 协议；短命令会等待结束，长驻输出会记录当前输出并回灌给 AI 继续判断。

### 10.2 程序更新流程（重要）

当用户要求”更新网盘”、”升级 WebDAV”、”更新程序”、”修复 bug”、”应用新版本”时，**必须使用 `update` 命令，不要使用 `install` 命令**。

推荐命令：

```bash
cd "<项目根目录绝对路径>"
git pull  # 如果需要从 git 仓库拉取最新代码
sudo bash skill/script/webdav-filemanager/install.sh update
pm2 show webdav-filemanager
# 或者 systemctl status webdav-filemanager --no-pager
```

**为什么不能用 `install` 更新**：

- `install` 会覆盖 `filemanager_auth.json`（用户在网页里修改过的账号密码）。
- `install` 会重新询问端口、账号、密码、文件根目录，用户体验差。

**`update` 的优势**：

- 无需任何交互，自动保留所有配置和数据。
- 只替换程序文件（server.py、index.html）。
- 自动重启服务，用户无感知。
- 保留用户在网页里修改过的账号密码和创建的临时链接。

### 10.3 重新配置流程

当用户要求”更改端口”、”修改账号”、”换个目录”、”重新配置”时，使用 `install` 命令：

```bash
sudo bash skill/script/webdav-filemanager/install.sh install
```

这会重新询问所有配置项，但**不会删除网盘文件根目录**（例如 `/data/files`）。

### 10.4 卸载流程

当用户明确要求”卸载网盘”、”删除 WebDAV”、”移除程序”时，使用 `uninstall` 命令：

```bash
sudo bash skill/script/webdav-filemanager/install.sh uninstall
```

卸载会删除程序和服务，但**不会删除网盘文件根目录**。如果用户要求”连文件一起删除”，需要额外确认后再执行：

```bash
sudo rm -rf /data/files  # 仅在用户明确要求时执行
```

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

> 服务器在没有配置认证时会拒绝启动（`-a`、环境变量 `WEBDAV_AUTH` 任选其一）。
> 确需无认证运行（例如只绑定 `127.0.0.1` 给本机用）时，要显式加 `--allow-no-auth`。

### 12.4 登录失败

如果仍能登录，可在网页右上角设置里修改登录账号或密码。

如果忘记密码，先检查启动命令里的 `-a 用户名:密码`：

```bash
cat /opt/webdav-filemanager/start.sh
```

如果曾在网页里改过登录账号或密码，实际登录凭据会优先保存到：

```bash
/opt/webdav-filemanager/filemanager_auth.json
```

忘记网页里修改后的账号或密码时，可以删除该文件并重启服务，让服务回退到 `start.sh` 里的账号密码：

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
- 测速文件通常使用稀疏文件创建，文件标称大小可能很大，但真实磁盘占用可能很小；需要区分”文件大小”和”实际占盘”。
- **Agent 执行更新时，必须使用 `update` 命令，不要使用 `install` 命令**，否则会覆盖用户在网页里修改过的账号密码。

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
