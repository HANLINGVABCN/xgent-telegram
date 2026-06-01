```!
用户有配置网页、静态站点、域名、DNS、HTTPS 或反向代理需求时使用。先询问并确认两件事：是否已有 Caddy；Cloudflare API Token 是否已写入系统级环境变量 `/etc/environment` 的 `CF_TOKEN`。用户回答是后，再询问/确认域名、目标目录或本机端口等必要需求，然后开始部署。默认 Web 服务器是 Caddy，配置路径 `/etc/caddy/Caddyfile`；先检查现有 Caddy 配置和服务状态，默认已有配置，直接修改 Caddyfile 完成静态托管、反向代理、HTTPS 等操作，缺失时再向用户请求处理方式。Cloudflare DNS 必须直接在终端通过 `$CF_TOKEN` 调 API 操作，例如修改/创建 DNS 记录；默认关闭 CDN 代理。禁止读取、打印、回复或暴露完整 token，无需重新声明 `CF_TOKEN`，不要无脑索要密码，不要安装无关环境或工具。
```
