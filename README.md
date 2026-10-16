# feishu-dav

[English](README.md) | [中文](README.zh.md)

Mount Feishu (Lark) Drive as a local filesystem via WebDAV — a single-file Python server with OAuth Device Flow, auto app provisioning, and rclone-compatible mounting. No redirect URLs, no lark-cli.

## Features

- **Single file** (`feishu_dav.py`) — depends only on `wsgidav`, `cheroot`, `requests` (+ `cryptography` for DPoP)
- **OAuth Device Flow (RFC 8628)** — authorize from any device (browser or Feishu mobile scan), no redirect URL configuration
- **Auto app provisioning** — creates a PersonalAgent app for you via device flow, larkfs-style
- **`--init` guided setup / `--doctor` self-check** — credential detection (CLI args → env vars → lark-cli config → interactive), automatic access verification
- **rclone-compatible** — `rclone mount` with `--vfs-cache-mode writes`; also works with any WebDAV client
- **Online docs as read-only files** — docx/sheets/bitables/slides exported on the fly and cached
- **Safe by default** — `--read-only` and delete gating; `~/.feishu-dav/config.json` stored with mode 600
- **DPoP token refresh** — v3 token endpoint + DPoP proofs, aligned with lark-cli behavior
- **Chunked upload for large files** — prepare/part/finish protocol matching lark-cli (tested with 70 MB+)

## Installation

```bash
pip install wsgidav cheroot requests cryptography
```

## Quick Start

```bash
python feishu_dav.py --init
```

The guided setup (larkfs-style):
1. Resolves app credentials, in order: existing config → `--app-id/--app-secret` → env vars `FEISHU_APP_ID/FEISHU_APP_SECRET` → auto-detected `~/.lark-cli/config.json` (asks to reuse) → auto-creates a PersonalAgent app via device flow → falls back to interactive input
2. Runs OAuth Device Flow — prints a verification URL + user code, opens the browser when possible
3. Verifies Drive access automatically and saves the config

Headless / SSH: add `--no-browser` to only print the link (open it on any device, or scan with the Feishu mobile app).

Non-interactive (CI / scripts):

```bash
python feishu_dav.py --init \
  --app-id cli_xxx --app-secret xxx \
  --mount Shared:A14LfVzzTlnOftd19MFcbCXsnld --read-only
```

### Mount with rclone

```bash
rclone config create feishu webdav url=http://127.0.0.1:8765 vendor=other
mkdir -p ~/feishu
rclone mount feishu: ~/feishu --vfs-cache-mode writes --daemon
```

## CLI Reference

| Flag | Description |
|---|---|
| `--init` | Guided setup + authorization (recommended) |
| `--wizard` | Interactive setup wizard |
| `--doctor` | Check config / credentials / mounts, with fix suggestions |
| `--app-id` / `--app-secret` | Use an existing OAuth app (or env `FEISHU_APP_ID` / `FEISHU_APP_SECRET`) |
| `--mount NAME:FOLDER_TOKEN` | Mount an extra folder (e.g. shared with you); repeatable |
| `--no-browser` | Print the verification link only (SSH/headless) |
| `--read-only` | Reject all write operations |
| `--allow-delete` | Enable delete/move (off by default) |
| `--host` / `--port` | Listen address (default `127.0.0.1:8765`) |

## How It Works

A wsgidav `DAVProvider` maps WebDAV operations onto the Feishu Open API with a user access token:

- **PROPFIND** → `GET /drive/v1/files` (paginated, 60 s TTL cache; the root shows your own space plus configured mounts)
- **GET** → `GET /drive/v1/files/{token}/download` (streamed; the `/medias/...` endpoint is avoided — see troubleshooting)
- **PUT** → `upload_all` (≤20 MB) or the server-planned prepare/part/finish chunked protocol (`seq` + per-chunk `size`)
- **docx/sheet/bitable/slides** → exported on demand via export tasks and cached under `~/.feishu-dav/export_cache/`
- **Token refresh** → accounts endpoint v3 with DPoP proofs (RFC 9449-style, ES256/P-256), refresh tokens rotated automatically

File sizes shown as 0 are a Feishu list-API limitation; real sizes are lazily resolved via a `Range: bytes=0-0` probe and downloads are streamed by content.

## Troubleshooting

- **Sizes show as 0 in listings**: the Feishu list API doesn't return `size`. Downloads/uploads are unaffected (streamed by content).
- **Authorization timed out**: verification links are short-lived; just rerun `--init`.
- **Download returns 403 with an empty body**: the auto-created PersonalAgent app may have drive scopes granted but not *published*. In the [developer console](https://open.feishu.cn/app) → your app → **Permissions** (confirm drive scopes) → **Version & Release** (create and publish a version — grants only take effect after publishing), then re-run `--init`.
- **`refresh token has been revoked`**: refresh tokens rotate on every refresh; if two clients share one config, the loser must re-authorize (`--init`).

## Manual Wizard (Optional)

```bash
python feishu_dav.py            # enters the wizard when no config exists
python feishu_dav.py --wizard   # reconfigure
```

Config is saved to `~/.feishu-dav/config.json` (mode 600 — it contains tokens, keep it private).

## Tests

```bash
python tests/smoke_test.py
```

Runs the full WebDAV layer (path resolution, PROPFIND listing, GET download, docx export, PUT upload, read-only/delete gating) against an in-memory fake API client — no network needed.

## License

MIT
