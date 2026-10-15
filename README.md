# feishu-dav

把飞书云盘（Drive）通过 WebDAV 暴露为 HTTP 服务，配合 `rclone mount` 挂载到 Linux。
纯 OAuth `user_access_token` 直连飞书开放平台，不依赖 lark-cli。单文件实现（`feishu_dav.py`）。

## 安装

```bash
pip install wsgidav cheroot requests
```

## 配置向导（首次运行）

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
