#!/usr/bin/env python3
"""feishu_dav.py — 把飞书云盘(Drive)通过 WebDAV 暴露出去，配合 rclone mount 使用。

纯 OAuth user_access_token 直连飞书开放平台，不依赖 lark-cli。

用法:
    python feishu_dav.py               # 使用 ~/.feishu-dav/config.json 启动（无配置则进入向导）
    python feishu_dav.py --wizard      # 重新运行配置向导
    python feishu_dav.py --port 8765 --read-only
"""

import argparse
import getpass
import json
import mimetypes
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

OPEN_BASE = "https://open.feishu.cn"
AUTH_BASE = "https://accounts.feishu.cn"
CONFIG_DIR = os.path.expanduser("~/.feishu-dav")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# 需要 user_access_token 具备的 OAuth scope（offline_access 用于刷新）
OAUTH_SCOPES = " ".join([
    "offline_access",
    "drive:drive:readonly",
    "drive:drive",
    "drive:file:upload",
    "drive:file:download",
    "docs:document:export",
    "space:folder:create",
])

TOKEN_EXPIRE_MARGIN = 300  # 过期前 5 分钟开始刷新


# ---------------------------------------------------------------- config

def load_config(path=CONFIG_PATH):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg, path=CONFIG_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- oauth wizard

class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """本地回调：捕获 ?code= 与 ?state=。"""

    code = None
    state = None
    error = None

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _OAuthCallbackHandler.code = (q.get("code") or [None])[0]
        _OAuthCallbackHandler.state = (q.get("state") or [None])[0]
        _OAuthCallbackHandler.error = (q.get("error") or [None])[0]
        body = b"<h2>feishu-dav: authorization received, you can close this page.</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _exchange_token(cfg, payload):
    """POST /authen/v2/oauth/token，返回完整响应 dict 或抛 RuntimeError。"""
    r = requests.post(
        f"{OPEN_BASE}/open-apis/authen/v2/oauth/token",
        json=payload,
        timeout=30,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"oauth token 接口失败: {data}")
    return data


def oauth_authorize(cfg, redirect_port):
    """跑一次授权码流程，返回 {access_token, refresh_token, expires_in}。"""
    state = secrets.token_urlsafe(16)
    redirect_uri = f"http://127.0.0.1:{redirect_port}/callback"
    params = {
        "app_id": cfg["app_id"],
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": OAUTH_SCOPES,
    }
    url = f"{AUTH_BASE}/open-apis/authen/v1/authorize?{urllib.parse.urlencode(params)}"

    server = HTTPServer(("127.0.0.1", redirect_port), _OAuthCallbackHandler)
    server.timeout = 300
    print(f"\n浏览器即将打开授权页面（若未打开请手动访问）:\n  {url}\n")
    webbrowser.open(url)
    print("等待授权回调（最多 300 秒）...")
    while _OAuthCallbackHandler.code is None and _OAuthCallbackHandler.error is None:
        server.handle_request()
    server.server_close()

    if _OAuthCallbackHandler.error:
        raise RuntimeError(f"授权失败: {_OAuthCallbackHandler.error}")
    if _OAuthCallbackHandler.state != state:
        raise RuntimeError("state 校验失败（可能的 CSRF），请重试")
    code = _OAuthCallbackHandler.code
    if not code:
        raise RuntimeError("未收到授权码，请重试")

    data = _exchange_token(cfg, {
        "grant_type": "authorization_code",
        "client_id": cfg["app_id"],
        "client_secret": cfg["app_secret"],
        "code": code,
        "redirect_uri": redirect_uri,
    })
    return {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expire_at": time.time() + int(data.get("expires_in", 6900)),
    }


def run_wizard(cfg_path=CONFIG_PATH, prefill=None):
    """配置向导。prefill 提供 app_id/app_secret/mounts/read_only/allow_delete 时跳过对应交互。"""
    prefill = prefill or {}
    print("=== feishu-dav 配置向导 ===")
    cfg = load_config(cfg_path) or {}

    env_id = os.environ.get("FEISHU_APP_ID", "")
    env_secret = os.environ.get("FEISHU_APP_SECRET", "")
    cfg["app_id"] = (prefill.get("app_id") or env_id
                     or input(f"app_id [{cfg.get('app_id', '')}]: ").strip()
                     or cfg.get("app_id", ""))
    if not cfg["app_id"]:
        sys.exit("app_id 不能为空")
    secret = (prefill.get("app_secret") or env_secret
              or getpass.getpass("app_secret [回车保留现有]: ").strip())
    if secret:
        cfg["app_secret"] = secret
    if "app_secret" not in cfg:
        sys.exit("app_secret 不能为空")

    mounts = prefill.get("mounts")
    if mounts is None:
        mounts = cfg.get("mounts") or []
        extra = input(
            f"额外挂载的 folder_token（逗号分隔，可留空）[{','.join(m.get('token','') for m in mounts)}]: "
        ).strip()
        if extra or not mounts:
            mounts = []
            for i, tok in enumerate([t.strip() for t in extra.split(",") if t.strip()], 1):
                name = input(f"  token {tok} 挂载目录名 [folder{i}]: ").strip() or f"folder{i}"
                mounts.append({"name": name, "token": tok})
    cfg["mounts"] = mounts

    if prefill.get("read_only") is not None or prefill.get("allow_delete") is not None:
        cfg["read_only"] = bool(prefill.get("read_only"))
        cfg["allow_delete"] = bool(prefill.get("allow_delete"))
    else:
        cfg["read_only"] = input("只读模式? [y/N]: ").strip().lower() == "y"
        cfg["allow_delete"] = input("允许删除/移动? [y/N]: ").strip().lower() == "y"

    print("\n开始 OAuth 授权（将打开浏览器）...")
    tok = oauth_authorize(cfg, redirect_port=cfg.get("redirect_port", 8577))
    cfg["tokens"] = tok
    save_config(cfg, cfg_path)
    print(f"\n配置已保存到 {cfg_path}，自动验证访问权限...")
    ok, msg = check_drive_access(cfg)
    print(f"  {'✓' if ok else '✗'} {msg}")
    if not ok:
        print("  提示: token 已获取但访问云盘失败，请确认应用已开通 drive 权限并发布")
    print(f"现在可以启动:\n  python feishu_dav.py")


def check_drive_access(cfg):
    """用配置里的凭证实际调一次 drive API，返回 (ok, 说明)。"""
    try:
        client = FeishuClient(TokenManager(cfg), cfg)
        n = len(client.list_dir(None))
        return True, f"云盘根目录可访问，共 {n} 项"
    except FeishuError as e:
        return False, f"访问失败: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"访问失败: {e}"


def _detect_lark_cli_credential():
    """从本机 lark-cli 配置 (~/.lark-cli/config.json) 探测可复用的 app_id/app_secret。"""
    candidates = [
        os.path.expanduser("~/.lark-cli/config.json"),
        os.path.join(os.environ.get("ATOMCODE_HOME", ""), "lark-cli", "config.json"),
    ]
    for p in candidates:
        if not p or not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            for app in data.get("apps") or []:
                if app.get("appId") and app.get("appSecret"):
                    return {"app_id": app["appId"], "app_secret": app["appSecret"], "source": p}
        except (OSError, ValueError):
            continue
    return None


def run_init(cfg_path=CONFIG_PATH, prefill=None):
    """larkfs init 风格的自动引导：
    1. 检查已有配置 / 探测凭证来源（lark-cli 配置、环境变量、命令行参数、交互输入）
    2. 未授权则自动完成 OAuth（打开浏览器）
    3. 自动验证访问并保存配置
    """
    prefill = prefill or {}
    print("=== feishu-dav init ===")
    cfg = load_config(cfg_path) or {}

    # --- step 1: 凭证 ---
    if cfg.get("app_id") and cfg.get("app_secret"):
        print(f"✓ 应用凭证已配置 (app_id={cfg['app_id']})")
    else:
        cred = None
        env_id = os.environ.get("FEISHU_APP_ID", "")
        env_secret = os.environ.get("FEISHU_APP_SECRET", "")
        if prefill.get("app_id") and prefill.get("app_secret"):
            cred = {"app_id": prefill["app_id"], "app_secret": prefill["app_secret"], "source": "命令行参数"}
        elif env_id and env_secret:
            cred = {"app_id": env_id, "app_secret": env_secret, "source": "环境变量 FEISHU_APP_ID/FEISHU_APP_SECRET"}
        else:
            cred = _detect_lark_cli_credential()
            if cred:
                print(f"· 检测到本机 lark-cli 应用凭证: {cred['source']}")
                use = input(f"复用该应用 (app_id={cred['app_id']})? [Y/n]: ").strip().lower()
                if use == "n":
                    cred = None
        if cred is None:
            print("\n未检测到可用凭证。请到飞书开发者后台创建自建应用并开通 drive 权限:")
            print("  https://open.feishu.cn/app  → 创建企业自建应用 → 权限管理开通 drive 相关 scope → 发布")
            cred = {
                "app_id": input("app_id: ").strip(),
                "app_secret": getpass.getpass("app_secret: ").strip(),
            }
            if not cred["app_id"] or not cred["app_secret"]:
                sys.exit("app_id / app_secret 不能为空")
        cfg["app_id"], cfg["app_secret"] = cred["app_id"], cred["app_secret"]
        print(f"✓ 使用应用凭证 (app_id={cfg['app_id']}, 来源: {cred.get('source', '交互输入')})")

    # --- step 2: 授权 ---
    tok = cfg.get("tokens") or {}
    if tok.get("refresh_token"):
        ok, msg = check_drive_access(cfg)
        if ok:
            print(f"✓ 已有授权且可用: {msg}")
        else:
            print(f"· 已有授权但不可用（{msg}），重新授权...")
            cfg["tokens"] = oauth_authorize(cfg, redirect_port=cfg.get("redirect_port", 8577))
    else:
        print("· 尚未授权，开始 OAuth（将打开浏览器）...")
        cfg["tokens"] = oauth_authorize(cfg, redirect_port=cfg.get("redirect_port", 8577))

    # --- step 3: 挂载点（非交互来源或保留现状）---
    mounts = prefill.get("mounts")
    if mounts is not None:
        cfg["mounts"] = mounts
    for m in cfg.get("mounts") or []:
        print(f"· 挂载点: {m['name']} ({m['token']})")
    if prefill.get("read_only") is not None:
        cfg["read_only"] = bool(prefill["read_only"])
    if prefill.get("allow_delete") is not None:
        cfg["allow_delete"] = bool(prefill["allow_delete"])

    # --- step 4: 验证并保存 ---
    save_config(cfg, cfg_path)
    ok, msg = check_drive_access(cfg)
    print(f"{'✓' if ok else '✗'} 验证: {msg}")
    if ok:
        print(f"\n完成！启动服务:\n  python feishu_dav.py\n挂载:\n"
              f"  rclone config create feishu webdav url=http://127.0.0.1:{cfg.get('port', 8765)} vendor=other\n"
              f"  rclone mount feishu: ~/feishu --vfs-cache-mode writes --daemon")
        save_config(cfg, cfg_path)
        return 0
    save_config(cfg, cfg_path)
    print("建议运行 python feishu_dav.py --doctor 查看详情")
    return 1


def run_doctor(cfg_path=CONFIG_PATH):
    """自检：配置 / 凭证 / 挂载点逐项检查并给出修复建议。"""
    print("=== feishu-dav doctor ===")
    cfg = load_config(cfg_path)
    problems = 0
    if not cfg:
        print("✗ 未找到配置，请运行: python feishu_dav.py --wizard")
        return 1
    print(f"✓ 配置文件: {cfg_path}")

    for key in ("app_id", "app_secret"):
        if cfg.get(key):
            print(f"✓ {key} 已配置")
        else:
            print(f"✗ 缺少 {key}（--wizard 重配，或设置环境变量 FEISHU_APP_ID/FEISHU_APP_SECRET）")
            problems += 1

    tok = cfg.get("tokens") or {}
    if tok.get("refresh_token"):
        expire = tok.get("expire_at", 0)
        state = "有效" if time.time() < expire - TOKEN_EXPIRE_MARGIN else "已过期（将自动刷新）"
        print(f"✓ user token 存在（{state}）")
        ok, msg = check_drive_access(cfg)
        print(f"  {'✓' if ok else '✗'} {msg}")
        if not ok:
            problems += 1
            print("    建议: token 可能已彻底失效，运行 python feishu_dav.py --wizard 重新授权")
    else:
        print("✗ 尚未完成 OAuth 授权，运行 python feishu_dav.py --wizard")
        problems += 1

    for m in cfg.get("mounts") or []:
        try:
            entries = FeishuClient(TokenManager(cfg), cfg).list_dir(m["token"])
            print(f"✓ 挂载点 {m['name']} ({m['token']}): {len(entries)} 项")
        except Exception as e:  # noqa: BLE001
            print(f"✗ 挂载点 {m['name']} ({m['token']}): {e}")
            print("    建议: 确认 token 正确、你有该文件夹权限、应用 scope 覆盖 drive")
            problems += 1
    if not cfg.get("mounts"):
        print("· 无额外挂载点（根目录 = 我的云空间）")

    print(f"\n{'发现问题: ' + str(problems) if problems else '全部正常'}")
    return 1 if problems else 0


# ---------------------------------------------------------------- token manager

class TokenManager:
    """管理 user_access_token：缓存、过期前刷新、401 强制刷新。"""

    def __init__(self, cfg, cfg_path=CONFIG_PATH):
        self.cfg = cfg
        self.cfg_path = cfg_path

    def _tokens(self):
        return self.cfg.setdefault("tokens", {})

    def _refresh(self):
        tok = self._tokens()
        if not tok.get("refresh_token"):
            raise RuntimeError("没有 refresh_token，请运行: python feishu_dav.py --wizard")
        data = _exchange_token(self.cfg, {
            "grant_type": "refresh_token",
            "client_id": self.cfg["app_id"],
            "client_secret": self.cfg["app_secret"],
            "refresh_token": tok["refresh_token"],
        })
        tok["access_token"] = data["access_token"]
        tok["refresh_token"] = data.get("refresh_token", tok["refresh_token"])
        tok["expire_at"] = time.time() + int(data.get("expires_in", 6900))
        save_config(self.cfg, self.cfg_path)

    def access_token(self, force=False):
        tok = self._tokens()
        if force or not tok.get("access_token") or time.time() > tok.get("expire_at", 0) - TOKEN_EXPIRE_MARGIN:
            self._refresh()
        return tok["access_token"]

    def invalidate(self):
        """401 后立即重取一次新 token。"""
        self._refresh()


# ---------------------------------------------------------------- feishu drive client

class FeishuError(Exception):
    def __init__(self, msg, status=500, code=0):
        super().__init__(msg)
        self.status = status
        self.code = code


class FeishuClient:
    """飞书 Drive API 直连封装。所有方法线程安全（requests 无共享状态）。"""

    def __init__(self, tm, cfg):
        self.tm = tm
        self.cfg = cfg
        self._dir_cache = {}          # folder_token -> (expire_ts, entries)
        self._dir_lock = threading.Lock()
        self.dir_ttl = 60

    # ---- http 基础 ----

    def _request(self, method, path, *, retry_auth=True, **kw):
        url = path if path.startswith("http") else OPEN_BASE + path
        headers = kw.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.tm.access_token()}"
        r = requests.request(method, url, headers=headers, timeout=120, **kw)
        if r.status_code == 401 and retry_auth:
            self.tm.invalidate()
            return self._request(method, path, retry_auth=False, headers={}, **kw)
        if r.status_code == 429:
            raise FeishuError("飞书限流 (429)", status=429, code=429)
        if r.status_code >= 400:
            try:
                msg = r.json().get("msg", r.text[:200])
            except ValueError:
                msg = r.text[:200]
            raise FeishuError(f"{method} {path} -> {r.status_code}: {msg}",
                              status=r.status_code, code=r.status_code)
        return r

    # ---- 目录 ----

    def list_dir(self, folder_token=None, use_cache=True):
        """返回 entries: [{token, name, type, size, mtime}]。folder_token=None 表示我的空间根目录。"""
        key = folder_token or "__root__"
        now = time.time()
        with self._dir_lock:
            hit = self._dir_cache.get(key)
            if use_cache and hit and hit[0] > now:
                return hit[1]
        entries = []
        page_token = ""
        while True:
            params = {"page_size": 200}
            if folder_token:
                params["folder_token"] = folder_token
            if page_token:
                params["page_token"] = page_token
            r = self._request("GET", "/open-apis/drive/v1/files", params=params)
            data = r.json().get("data", {})
            for f in data.get("files") or []:
                entries.append({
                    "token": f["token"],
                    "name": f["name"],
                    "type": f.get("type", "file"),
                    "size": int(f.get("size") or 0),
                    "mtime": int(f.get("modified_time") or 0),
                    "url": f.get("url", ""),
                })
            page_token = data.get("next_page_token") or ""
            if not data.get("has_more") or not page_token:
                break
        with self._dir_lock:
            self._dir_cache[key] = (now + self.dir_ttl, entries)
        return entries

    def invalidate_dir(self, folder_token=None):
        with self._dir_lock:
            self._dir_cache.pop(folder_token or "__root__", None)

    def _find(self, folder_token, name):
        """在目录中按 name 精确查找，返回 entry 或 None。"""
        for e in self.list_dir(folder_token):
            if e["name"] == name:
                return e
        return None

    def resolve_path(self, parts):
        """把 ['Shared', 'sub', 'a.txt'] 解析为 entry（folder_token 视为虚拟根的子目录）。"""
        mounts = self.cfg.get("mounts") or []
        root_entry = None
        if parts and parts[0]:
            m = next((m for m in mounts if m["name"] == parts[0]), None)
            if m is None:
                raise FeishuError(f"未挂载的目录: {parts[0]}", status=404, code=404)
            root_entry = {"token": m["token"], "name": m["name"], "type": "folder",
                          "size": 0, "mtime": 0, "url": ""}
            rest = parts[1:]
        else:
            rest = list(parts)
        cur = root_entry
        for name in rest:
            ft = cur["token"] if cur else None
            cur = self._find(ft, name)
            if cur is None:
                raise FeishuError(f"路径不存在: {'/'.join(parts)}", status=404, code=404)
        return cur

    # ---- 读写 ----

    def download(self, file_token, chunk_size=1 << 20):
        """流式下载 file，返回 requests 响应（可 iter_content）。"""
        return self._request(
            "GET", f"/open-apis/drive/v1/medias/{file_token}/download", stream=True
        )

    def upload(self, folder_token, filename, data_or_path, size):
        """上传文件到指定文件夹。<=20MB 走 upload_all；更大走分片。"""
        if size <= 20 << 20:
            return self._upload_all(folder_token, filename, data_or_path, size)
        return self._upload_split(folder_token, filename, data_or_path, size)

    def _upload_all(self, folder_token, filename, data_or_path, size):
        files = {}
        close = False
        if isinstance(data_or_path, (str, os.PathLike)):
            f = open(data_or_path, "rb")
            close = True
        else:
            f = data_or_path
        try:
            files["file"] = (filename, f, "application/octet-stream")
            r = self._request(
                "POST", "/open-apis/drive/v1/files/upload_all",
                data={"file_name": filename, "parent_type": "explorer",
                      "parent_node": folder_token or "", "size": str(size)},
                files=files,
            )
        finally:
            if close:
                f.close()
        data = r.json()
        if data.get("code") != 0:
            raise FeishuError(f"上传失败: {data.get('msg')}", code=data.get("code", 0))
        self.invalidate_dir(folder_token)
        return data["data"]["file_token"]

    def _upload_split(self, folder_token, filename, path, size):
        block = 4 << 20
        block_num = (size + block - 1) // block
        r = self._request("POST", "/open-apis/drive/v1/files/upload_prepare",
                          json={"file_name": filename, "parent_type": "explorer",
                                "parent_node": folder_token or "", "size": size,
                                "block_num": block_num})
        upload_id = r.json()["data"]["upload_id"]
        with open(path, "rb") as f:
            for i in range(block_num):
                chunk = f.read(block)
                self._request(
                    "POST", "/open-apis/drive/v1/files/upload_part",
                    data={"upload_id": upload_id, "block_num": str(i),
                          "block_size": str(len(chunk))},
                    files={"file": (filename, chunk, "application/octet-stream")},
                )
        r = self._request("POST", "/open-apis/drive/v1/files/upload_finish",
                          json={"upload_id": upload_id, "block_num": block_num})
        self.invalidate_dir(folder_token)
        return r.json()["data"]["file_token"]

    def create_folder(self, parent_token, name):
        r = self._request("POST", "/open-apis/drive/v1/files/create_folder",
                          json={"name": name, "folder_token": parent_token or ""})
        data = r.json()
        if data.get("code") != 0:
            raise FeishuError(f"创建文件夹失败: {data.get('msg')}", code=data.get("code", 0))
        self.invalidate_dir(parent_token)
        return data["data"]["token"]

    def delete(self, entry):
        ftype = entry["type"]
        r = self._request("DELETE", f"/open-apis/drive/v1/files/{entry['token']}",
                          params={"type": ftype if ftype in ("folder", "file", "docx",
                                                             "sheet", "bitable", "slides")
                                  else "file"})
        self.invalidate_dir(None)
        for m in self.cfg.get("mounts") or []:
            self.invalidate_dir(m["token"])
        return True

    # ---- 在线文档导出（只读虚拟文件）----

    EXPORTABLE = {"docx": "docx", "doc": "docx", "sheet": "xlsx", "bitable": "xlsx",
                  "slides": "pdf", "mindnote": "pdf"}

    def export_content(self, entry):
        """导出在线文档为字节流；带磁盘缓存。"""
        ext = self.EXPORTABLE.get(entry["type"])
        if not ext:
            raise FeishuError(f"类型 {entry['type']} 不支持导出", status=403, code=403)
        cache_dir = os.path.join(CONFIG_DIR, "export_cache")
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, f"{entry['token']}.{ext}")
        if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < self.dir_ttl:
            with open(cache_path, "rb") as f:
                return ext, f.read()
        r = self._request("POST", "/open-apis/drive/v1/export_tasks",
                          json={"file_extension": ext, "token": entry["token"],
                                "type": entry["type"]})
        ticket = r.json()["data"]["ticket"]
        for _ in range(60):
            time.sleep(1)
            r = self._request("GET", f"/open-apis/drive/v1/export_tasks/{ticket}",
                              params={"token": entry["token"]})
            result = r.json()["data"]["result"]
            if result.get("job_status", 0) == 0:  # 0 = success
                dl = self._request("GET",
                                   f"/open-apis/drive/v1/export_tasks/{ticket}/download",
                                   params={"token": entry["token"]})
                with open(cache_path, "wb") as f:
                    for chunk in dl.iter_content(1 << 20):
                        f.write(chunk)
                return ext, open(cache_path, "rb").read()
        raise FeishuError("文档导出超时", status=504, code=504)


# ---------------------------------------------------------------- webdav provider

from wsgidav import util  # noqa: E402
from wsgidav.dav_error import DAVError, HTTP_FORBIDDEN, HTTP_NOT_FOUND  # noqa: E402
from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider  # noqa: E402

SUFFIX_SEP = "~"


def _split_path(path):
    """WebDAV path -> 段列表（去掉空段与首尾斜杠）。"""
    return [p for p in path.split("/") if p]


class FeishuCollection(DAVCollection):
    """虚拟目录：根目录（挂载点集合）或飞书 folder。"""

    def __init__(self, path, environ, provider, entry=None):
        super().__init__(path, environ)
        self.provider = provider
        self.entry = entry  # None = 根目录

    @property
    def client(self):
        return self.provider.client

    def _members(self):
        ft = self.entry["token"] if self.entry else None
        return self.provider.members(ft)

    def get_member_names(self):
        return list(self._members().keys())

    def get_member_list(self):
        res = []
        for name, entry in self._members().items():
            child_path = util.join_uri(self.path, name)
            if entry["type"] == "folder":
                res.append(FeishuCollection(child_path, self.environ, self.provider, entry))
            else:
                res.append(FeishuFile(child_path, self.environ, self.provider, entry))
        return res

    def get_display_name(self):
        return self.entry["name"] if self.entry else ""

    def get_last_modified(self):
        return self.entry["mtime"] if self.entry else None

    def create_collection(self, name):
        if self.provider.readonly:
            raise DAVError(HTTP_FORBIDDEN)
        parent = self.entry["token"] if self.entry else None
        self.client.create_folder(parent, name)
        self.client.invalidate_dir(parent)

    def create_empty_resource(self, name):
        if self.provider.readonly:
            raise DAVError(HTTP_FORBIDDEN)
        child_path = util.join_uri(self.path, name)
        return FeishuFile(child_path, self.environ, self.provider, None,
                          parent=self.entry, pending_name=name)

    def delete(self):
        if not self.provider.allow_delete:
            raise DAVError(HTTP_FORBIDDEN)
        if self.entry is None:
            raise DAVError(HTTP_FORBIDDEN)
        self.client.delete(self.entry)


class FeishuFile(DAVNonCollection):
    """文件或在线文档（只读虚拟文件）。pending_* 用于 PUT 新文件的延迟上传。"""

    def __init__(self, path, environ, provider, entry, parent=None, pending_name=None):
        super().__init__(path, environ)
        self.provider = provider
        self.entry = entry          # None = 尚未上传
        self.parent = parent        # 父目录 entry（pending 上传用）
        self.pending_name = pending_name
        self._tmp_path = None

    @property
    def client(self):
        return self.provider.client

    def get_content_length(self):
        if self.entry is None:
            return 0
        if self.entry["type"] in FeishuClient.EXPORTABLE:
            try:
                _, blob = self.client.export_content(self.entry)
                return len(blob)
            except FeishuError:
                return 0
        return self.entry["size"]

    def get_content_type(self):
        name = self.pending_name or (self.entry or {}).get("name", "")
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if self.entry and self.entry["type"] in FeishuClient.EXPORTABLE:
            ext = FeishuClient.EXPORTABLE[self.entry["type"]]
        return mimetypes.types_map.get("." + ext, "application/octet-stream")

    def get_etag(self):
        return (self.entry or {}).get("token") or "pending"

    def get_display_name(self):
        return self.pending_name or (self.entry or {}).get("name", "")

    def get_last_modified(self):
        return (self.entry or {}).get("mtime") or None

    def support_ranges(self):
        return False

    def get_content(self, dest_file):
        if self.entry is None:
            return
        if self.entry["type"] in FeishuClient.EXPORTABLE:
            _, blob = self.client.export_content(self.entry)
            dest_file.write(blob)
            return
        r = self.client.download(self.entry["token"])
        for chunk in r.iter_content(1 << 20):
            dest_file.write(chunk)

    def begin_write(self, *, content_type=None, content_encoding=None):
        if self.provider.readonly:
            raise DAVError(HTTP_FORBIDDEN)
        fd, self._tmp_path = tempfile.mkstemp(prefix="feishu-dav-")
        return os.fdopen(fd, "wb")

    def end_write(self, *, with_errors=False):
        if self._tmp_path is None:
            return
        try:
            if with_errors:
                return
            size = os.path.getsize(self._tmp_path)
            parent_token = self.parent["token"] if self.parent else None
            self.client.upload(parent_token, self.pending_name, self._tmp_path, size)
            self.client.invalidate_dir(parent_token)
        finally:
            try:
                os.unlink(self._tmp_path)
            except OSError:
                pass
            self._tmp_path = None

    def delete(self):
        if not self.provider.allow_delete or self.entry is None:
            raise DAVError(HTTP_FORBIDDEN)
        self.client.delete(self.entry)


class FeishuProvider(DAVProvider):
    """把飞书 Drive 映射为 WebDAV 树。根目录下是配置的挂载点（无挂载点则直接是我的空间根）。"""

    def __init__(self, client, cfg):
        super().__init__()
        self.client = client
        self.cfg = cfg
        self.readonly = bool(cfg.get("read_only"))
        self.allow_delete = bool(cfg.get("allow_delete")) and not self.readonly

    # ---- 名称映射（同名加 ~token 后缀保证路径唯一）----

    def members(self, folder_token):
        entries = self.client.list_dir(folder_token)
        seen = {}
        out = {}
        for e in entries:
            name = e["name"]
            if name in seen or name in out:
                disp = f"{name}{SUFFIX_SEP}{e['token'][:8]}"
                out[disp] = e
            else:
                seen[name] = e
                out[name] = e
        return out

    def _mount_for(self, first):
        mounts = self.cfg.get("mounts") or []
        return next((m for m in mounts if m["name"] == first), None)

    def _resolve(self, path):
        """path -> (entry|None, mount|None, is_folder)。失败抛 FeishuError(404)。"""
        parts = _split_path(path)
        if not parts:
            return None, None, True
        mount = self._mount_for(parts[0])
        rest = list(parts[1:]) if mount else list(parts)
        cur = None
        ft = mount["token"] if mount else None
        if not mount and self.cfg.get("mounts"):
            raise FeishuError(f"未挂载: {parts[0]}", status=404, code=404)
        for i, name in enumerate(rest):
            entry = self._find(ft, name)
            if entry is None:
                raise FeishuError(f"路径不存在: {'/'.join(parts)}", status=404, code=404)
            cur = entry
            ft = entry["token"] if entry["type"] == "folder" else None
            if i < len(rest) - 1 and entry["type"] != "folder":
                raise FeishuError(f"路径不存在: {'/'.join(parts)}", status=404, code=404)
        return cur, mount, bool(cur and cur["type"] == "folder")

    def _find(self, folder_token, name):
        members = self.members(folder_token)
        if name in members:
            return members[name]
        # 处理去重后缀 name~token8
        if SUFFIX_SEP in name:
            base, suffix = name.rsplit(SUFFIX_SEP, 1)
            for e in members.values():
                if e["name"] == base and e["token"].startswith(suffix):
                    return e
        return None

    # ---- DAVProvider 接口 ----

    def is_readonly(self):
        return self.readonly

    def get_resource_inst(self, path, environ):
        try:
            entry, mount, is_folder = self._resolve(path)
        except FeishuError as e:
            if e.status == 404:
                return None
            raise DAVError(e.status if 400 <= e.status < 500 else 502) from e
        if entry is None:  # 根目录
            return FeishuCollection("/", environ, self)
        full = "/" + "/".join(_split_path(path))
        if is_folder:
            return FeishuCollection(full, environ, self, entry)
        return FeishuFile(full, environ, self, entry)

    def exists(self, path, environ):
        try:
            self._resolve(path)
            return True
        except (FeishuError, DAVError):
            return False

    def is_collection(self, path, environ):
        try:
            _, _, is_folder = self._resolve(path)
            return is_folder
        except (FeishuError, DAVError):
            return False


# ---------------------------------------------------------------- main

def build_app(cfg, cfg_path=CONFIG_PATH):
    from wsgidav.wsgidav_app import WsgiDAVApp

    tm = TokenManager(cfg, cfg_path)
    client = FeishuClient(tm, cfg)
    provider = FeishuProvider(client, cfg)
    config = {
        "provider_mapping": {"/": provider},
        "http_authenticator": {
            "domain_controller": "wsgidav.dc.simple_dc.SimpleDomainController",
            "accept_basic": True,
            "accept_digest": True,
        },
        "simple_dc": {"user_mapping": {"*": True}},   # 本机监听，匿名即可
        "verbose": 1,
        "logging": {"enable_loggers": []},
        "property_manager": True,
        "lock_storage": True,
    }
    return WsgiDAVApp(config)


def main():
    ap = argparse.ArgumentParser(description="飞书云盘 WebDAV 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--init", action="store_true", help="自动引导完成配置与授权（larkfs init 风格）")
    ap.add_argument("--wizard", action="store_true", help="交互式配置向导")
    ap.add_argument("--doctor", action="store_true", help="自检配置/凭证/挂载点")
    ap.add_argument("--app-id", help="非交互配置：自建应用 app_id（也可用环境变量 FEISHU_APP_ID）")
    ap.add_argument("--app-secret", help="非交互配置：自建应用 app_secret（也可用环境变量 FEISHU_APP_SECRET）")
    ap.add_argument("--mount", action="append", metavar="NAME:FOLDER_TOKEN",
                    help="非交互配置：额外挂载点，可多次")
    ap.add_argument("--read-only", action="store_true", help="覆盖配置为只读")
    ap.add_argument("--allow-delete", action="store_true", help="覆盖配置允许删除")
    ap.add_argument("--config", default=CONFIG_PATH)
    args = ap.parse_args()

    if args.doctor:
        sys.exit(run_doctor(args.config))

    cfg = load_config(args.config)
    if args.init:
        mounts = None
        if args.mount:
            mounts = []
            for i, spec in enumerate(args.mount, 1):
                name, _, token = spec.partition(":")
                mounts.append({"name": name.strip() or f"folder{i}", "token": token.strip()})
        sys.exit(run_init(args.config, prefill={
            "app_id": args.app_id,
            "app_secret": args.app_secret,
            "mounts": mounts,
            "read_only": True if args.read_only else None,
            "allow_delete": True if args.allow_delete else None,
        }))
    if args.wizard or cfg is None:
        mounts = None
        if args.mount:
            mounts = []
            for i, spec in enumerate(args.mount, 1):
                name, _, token = spec.partition(":")
                mounts.append({"name": name.strip() or f"folder{i}", "token": token.strip()})
        run_wizard(args.config, prefill={
            "app_id": args.app_id,
            "app_secret": args.app_secret,
            "mounts": mounts,
            "read_only": True if args.read_only else None,
            "allow_delete": True if args.allow_delete else None,
        })
        cfg = load_config(args.config)
    if args.read_only:
        cfg["read_only"] = True
    if args.allow_delete:
        cfg["allow_delete"] = True
    if args.mount:
        mounts = cfg.setdefault("mounts", [])
        for i, spec in enumerate(args.mount, 1):
            name, _, token = spec.partition(":")
            mounts.append({"name": name.strip() or f"folder{i}", "token": token.strip()})
        save_config(cfg, args.config)

    app = build_app(cfg, args.config)
    from cheroot import wsgi
    server = wsgi.Server((args.host, args.port), app, server_name="feishu-dav")
    print(f"feishu-dav 监听 http://{args.host}:{args.port}  (read_only={cfg.get('read_only')}, "
          f"allow_delete={cfg.get('allow_delete')})")
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
