```!
便签技能：一键部署一个轻量私有网页便签。功能包括账号密码登录、便签列表、新建/编辑/删除、标题与正文搜索。安装: sudo bash skill-public/script/notes/install.sh install。更新: sudo bash skill-public/script/notes/install.sh update。卸载: sudo bash skill-public/script/notes/install.sh uninstall。默认端口 8899，部署时交互填写端口、账号、密码。
```

# 私有网页便签

本文用于指导 Agent 模式在服务器上部署一个简单完整的私有便签服务。

## 文件位置

```text
skill/notes.md
skill-public/script/notes/install.sh
skill-public/script/notes/server.py
```

## 功能

- 网页账号密码登录。
- 便签列表。
- 新建、编辑、保存、删除便签。
- 按标题和正文搜索。
- 数据保存在服务器本地 JSON 文件中。
- 零第三方 Python 依赖，只需要 `python3`。
- 可选择 `pm2` 或 `systemd` 托管，支持开机自启。

## 安装

在项目根目录执行：

```bash
chmod +x skill-public/script/notes/install.sh
sudo bash skill-public/script/notes/install.sh install
```

脚本会询问：

```text
进程管理方式：pm2 或 systemd
端口号：默认 8899
登录账号：默认 admin
登录密码：部署时自行填写
数据目录：默认 /opt/simple-notes/data
```

安装完成后浏览器访问：

```text
http://服务器IP:端口/
```

如果外部无法访问，检查服务器防火墙或云安全组是否放行该 TCP 端口。

## 更新

更新程序但保留端口、账号密码和便签数据：

```bash
sudo bash skill-public/script/notes/install.sh update
```

日常修复或升级时使用 `update`，不要重新 `install`，避免覆盖账号密码配置。

## 卸载

```bash
sudo bash skill-public/script/notes/install.sh uninstall
```

卸载会删除服务和程序目录。默认不删除已配置的数据目录，避免误删便签数据。

## 常用管理命令

systemd 模式：

```bash
systemctl status simple-notes --no-pager
systemctl restart simple-notes
journalctl -u simple-notes -n 100 --no-pager
```

pm2 模式：

```bash
pm2 show simple-notes
pm2 restart simple-notes
pm2 logs simple-notes
```
