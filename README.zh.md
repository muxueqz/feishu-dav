# feishu-dav

[English](README.md) | [中文](README.zh.md)

把飞书云盘（Drive）通过 WebDAV 暴露为 HTTP 服务，配合 `rclone mount` 挂载到 Linux。
纯 OAuth `user_access_token` 直连飞书开放平台，不依赖 lark-cli。单文件实现（`feishu_dav.py`）。

## 安装

```bash
pip install wsgidav cheroot requests
```

## 快速开始（自动引导，推荐）

```bash
python feishu_dav.py --init
```

自动引导流程（larkfs init 风格）：
1. 检查应用凭证：已有配置直接使用；否则按顺序尝试 命令行参数 `--app-id/--app-secret` → 环境变量 `FEISHU_APP_ID/FEISHU_APP_SECRET` → 自动探测本机 `~/.lark-cli/config.json` 里的应用并询问是否复用 → 最后交互输入（并给出开发者后台创建应用的指引）
2. 未授权则自动打开浏览器完成 OAuth（本地回调 127.0.0.1:8577）
3. 自动验证云盘访问权限并保存配置

非交互式（CI / 脚本）：

```bash
python feishu_dav.py --init \
  --app-id cli_xxx --app-secret xxx \
  --mount Shared:A14LfVzzTlnOftd19MFcbCXsnld --read-only
```

自检：

```bash
python feishu_dav.py --doctor   # 检查配置/凭证/挂载点，给出修复建议
```

SSH/headless 场景：加 `--no-browser` 只打印授权链接（可复制到本机浏览器或手机飞书扫码打开）。

授权采用 **OAuth Device Flow（RFC 8628）**：无需在开放平台配置重定向 URL，用户在任意设备打开链接、输入用户码（或扫码）即可。

## 故障排查

- **文件大小显示为 0**：飞书 `drive/v1/files` 列表接口不返回 size 字段，PROPFIND 只能报 0；不影响实际读取/下载（rclone 流式传输按实际内容计）。上传后的文件大小正常
- **授权超时**：链接有效期约 4 分钟，超时后重跑 `--init` 即可
- **部分文件下载 403（空响应体）**：自动创建的 PersonalAgent 应用即使授权页显示包含 `drive:file:download`，媒体下载接口仍可能返回 403（lark-cli 等后台开通了权限的应用可正常下载）。解决：到[开发者后台](https://open.feishu.cn/app)对应应用 →「权限管理」确认 drive 相关权限已开通 →「版本管理与发布」**创建版本并发布**（仅开通不发布不生效），然后重新 `--init` 授权

## 手动向导（可选）

```bash
python feishu_dav.py            # 无配置时自动进入向导
python feishu_dav.py --wizard   # 重新配置
```

向导流程：
1. 输入飞书自建应用的 `app_id` / `app_secret`（开发者后台创建，需开通 drive 相关权限并发布应用）
2. 自动打开浏览器完成 OAuth 授权（本地回调 127.0.0.1:8577），获取并保存 `user_access_token` / `refresh_token`（offline_access，自动刷新）
3. 可选：填入额外挂载的 folder_token（例如别人共享给你的文件夹 URL 中的 token），多级目录填多个
4. 选择只读模式 / 是否允许删除

配置保存在 `~/.feishu-dav/config.json`（权限 600，含 token，勿外传）。

## 启动

```bash
python feishu_dav.py [--host 127.0.0.1] [--port 8765] [--read-only] [--allow-delete]
```

## 用 rclone 挂载

```bash
rclone config create feishu webdav url=http://127.0.0.1:8765 vendor=other
mkdir -p ~/feishu
rclone mount feishu: ~/feishu --vfs-cache-mode writes --daemon
```

不带 rclone 也可直接用 WebDAV 客户端或 `rclone lsf feishu:` 浏览。

## 说明

- 在线文档（docx/sheet/bitable/slides）映射为只读虚拟文件，内容为导出产物（docx/xlsx/pdf），结果缓存于 `~/.feishu-dav/export_cache/`
- 同名文件会显示为 `name~token前8位` 以保证路径唯一
- 目录列表有 60s TTL 缓存；写操作后主动失效
- 「共享给我」的文件夹：在向导里把其 folder_token 加为挂载点即可（需要应用 scope 覆盖，且你有相应访问权限）
- 删除操作默认禁止，需 `--allow-delete` 或向导中开启；`--read-only` 时一切写操作返回 403
