#!/usr/bin/env python3
"""feishu_dav.py — 把飞书云盘(Drive)通过 WebDAV 暴露出去，配合 rclone mount 使用。

纯 OAuth user_access_token 直连飞书开放平台，不依赖 lark-cli。

用法:
    python feishu_dav.py               # 使用 ~/.feishu-dav/config.json 启动（无配置则进入向导）
    python feishu_dav.py --wizard      # 重新运行配置向导
    python feishu_dav.py --port 8765 --read-only
"""

import argparse
import base64
import getpass
import io
import json
import mimetypes
import os
import queue
import re
import sys
import tempfile
import threading
import time
import webbrowser

import requests

OPEN_BASE = "https://open.feishu.cn"
AUTH_BASE = "https://accounts.feishu.cn"
CONFIG_DIR = os.path.expanduser("~/.feishu-dav")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# 需要 user_access_token 具备的 OAuth scope（offline_access 用于刷新）
OAUTH_SCOPES = " ".join([
    "offline_access",
    "auth:user.id:read",
    "drive:drive:readonly",
    "drive:drive",
    "drive:file:upload",
    "drive:file:download",
    "drive:drive.metadata:readonly",
    "docs:document.media:download",
    "docs:document.media:upload",
    "docs:document:export",
    "docs:document:readonly",
    "docx:document:readonly",
    "docx:document:create",
    "space:folder:create",
    "space:document:move",
    "search:docs:read",
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


# ---------------------------------------------------------------- oauth (device flow, RFC 8628)

def _exchange_token(cfg, payload):
    """POST /authen/v2/oauth/token，返回完整响应 dict 或抛 RuntimeError。"""
    r = requests.post(
        f"{OPEN_BASE}/open-apis/authen/v2/oauth/token",
        json=payload,
        timeout=30,
    )
    data = r.json()
    if data.get("code") not in (0, None):
        raise RuntimeError(f"oauth token 接口失败: {data}")
    return data


def _print_verify_link(url, user_code, expires_in):
    print("\n" + "=" * 70)
    print("请在浏览器中打开以下链接完成授权（可复制到任意设备，支持手机飞书扫码）:")
    print(f"\n{url}\n")
    if user_code:
        print(f"用户码（若页面要求输入）: {user_code}")
    print(f"有效期: {expires_in} 秒")
    print("=" * 70)


def _device_authorization(cfg, scope):
    r = requests.post(
        f"{AUTH_BASE}/oauth/v1/device_authorization",
        data={"client_id": cfg["app_id"], "scope": scope},
        headers={"Authorization": "Basic " + base64.b64encode(
            f"{cfg['app_id']}:{cfg['app_secret']}".encode()).decode()},
        timeout=30,
    )
    try:
        return r.json(), r.status_code
    except ValueError:
        raise RuntimeError(f"device_authorization 请求失败: HTTP {r.status_code} {r.text[:200]}")


def _is_scope_error(dev):
    desc = str(dev.get("error_description") or "") + str(dev.get("error") or "")
    return "scope" in desc.lower() and ("invalid" in desc.lower() or "malformed" in desc.lower())


def oauth_authorize(cfg, no_browser=False):
    """Device Flow 授权（无需重定向 URL），返回 {access_token, refresh_token, expire_at}。

    应用不支持请求中的某些 scope 时，自动逐个探测并剔除无效项后重试。
    """
    dev, status = _device_authorization(cfg, OAUTH_SCOPES)
    if (status >= 400 or dev.get("error")) and _is_scope_error(dev):
        print("· 部分 scope 该应用不支持，自动筛选有效 scope...")
        accepted = []
        for s in OAUTH_SCOPES.split():
            d, st = _device_authorization(cfg, s)
            if not (st >= 400 or d.get("error")):
                accepted.append(s)
        if not accepted:
            raise RuntimeError("该应用不支持任何请求的 scope")
        dev, status = _device_authorization(cfg, " ".join(accepted))
        print(f"· 有效 scope: {' '.join(accepted)}")
    if status >= 400 or dev.get("error") or not dev.get("device_code"):
        raise RuntimeError(f"device_authorization 失败: {dev.get('error_description') or dev}")
    device_code = dev["device_code"]
    user_code = dev.get("user_code", "")
    interval = int(dev.get("interval") or 5)
    expires_in = int(dev.get("expires_in") or 240)
    verify_url = dev.get("verification_uri_complete") or dev.get("verification_uri") \
        or f"{AUTH_BASE}/oauth/v1/device/verify?user_code={user_code}"

    _print_verify_link(verify_url, user_code, expires_in)
    if not no_browser:
        try:
            opened = webbrowser.open(verify_url)
        except Exception:  # noqa: BLE001
            opened = False
        if not opened:
            print("无法自动打开浏览器，请复制上方链接手动打开")

    deadline = time.time() + expires_in
    wait = False
    while time.time() < deadline:
        if wait:
            time.sleep(interval)
        wait = True
        try:
            data = _exchange_token(cfg, {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": cfg["app_id"],
                "client_secret": cfg["app_secret"],
            })
        except RuntimeError:
            continue  # code != 0 的响应仍可能是 pending，看下面的 error 字段
        err = data.get("error")
        if err == "authorization_pending":
            print(".", end="", flush=True)
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err:
            raise RuntimeError(f"授权失败: {data.get('error_description') or err}")
        print("\n✓ 授权成功")
        return {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expire_at": time.time() + int(data.get("expires_in", 6900)),
        }
    raise RuntimeError("授权超时（device_code 已过期），请重试")


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

    print("\n开始 OAuth 授权...")
    tok = oauth_authorize(cfg, no_browser=prefill.get("no_browser", False))
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



def _register_app(brand_host="accounts.feishu.cn", open_host="https://open.feishu.cn",
                  no_browser=False):
    """对标 lark-cli config create-app：Device Flow 自动创建 PersonalAgent 应用。
    返回 {app_id, app_secret}。"""
    ep = f"https://{brand_host}/oauth/v1/app/registration"
    form = {
        "action": "begin",
        "archetype": "PersonalAgent",
        "auth_method": "client_secret",
        "request_user_info": "open_id tenant_brand",
    }
    r = requests.post(ep, data=form, timeout=30)
    data = r.json()
    if r.status_code >= 400 or data.get("error") or not data.get("device_code"):
        raise RuntimeError(f"应用注册 begin 失败: {data.get('error_description') or data}")
    user_code = data.get("user_code", "")
    device_code = data["device_code"]
    interval = int(data.get("interval") or 5)
    expires_in = int(data.get("expire_in") or data.get("expires_in") or 600)
    verify_url = f"{open_host}/page/cli?user_code={user_code}" if user_code \
        else data.get("verification_uri_complete") or data.get("verification_uri")

    print("\n将为你自动创建一个飞书应用（PersonalAgent 类型）。")
    _print_verify_link(verify_url, user_code, expires_in)
    if not no_browser:
        try:
            webbrowser.open(verify_url)
        except Exception:  # noqa: BLE001
            pass

    deadline = time.time() + expires_in
    wait = False
    while time.time() < deadline:
        if wait:
            time.sleep(interval)
        wait = True
        r = requests.post(ep, data={"action": "poll", "device_code": device_code}, timeout=30)
        try:
            data = r.json()
        except ValueError:
            continue
        err = data.get("error")
        if err == "authorization_pending":
            print(".", end="", flush=True)
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err:
            raise RuntimeError(f"应用注册失败: {data.get('error_description') or err}")
        client_id, client_secret = data.get("client_id"), data.get("client_secret")
        if client_id and client_secret:
            print(f"\n✓ 应用创建成功 (app_id={client_id})")
            return {"app_id": client_id, "app_secret": client_secret}
        # 凭证未齐：继续轮询
    raise RuntimeError("应用注册超时，请重试或手动输入 app_id/app_secret")


def run_init(cfg_path=CONFIG_PATH, prefill=None):
    """larkfs init 风格的自动引导：
    1. 检查已有配置 / 探测凭证来源（环境变量、命令行参数、交互输入）
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
        if cred is None:
            # 无任何现成凭证：优先尝试 Device Flow 自动创建应用（对标 lark-cli create-app）
            try:
                cred = _register_app(no_browser=prefill.get("no_browser", False))
                cred["source"] = "自动创建（PersonalAgent）"
            except (RuntimeError, requests.RequestException) as e:
                print(f"· 自动创建应用不可用（{e}）")
                print("\n请到飞书开发者后台创建自建应用并开通 drive 权限:")
                print("  https://open.feishu.cn/app  → 创建企业自建应用 → 权限管理开通 drive 相关 scope → 发布")
                cred = {
                    "app_id": input("app_id: ").strip(),
                    "app_secret": getpass.getpass("app_secret: ").strip(),
                }
                if not cred["app_id"] or not cred["app_secret"]:
                    sys.exit("app_id / app_secret 不能为空")
        cfg["app_id"], cfg["app_secret"] = cred["app_id"], cred["app_secret"]
        print(f"✓ 使用应用凭证 (app_id={cfg['app_id']}, 来源: {cred.get('source', '交互输入')})")

    # --- step 2: 授权（Device Flow，无需重定向 URL）---
    tok = cfg.get("tokens") or {}
    if tok.get("refresh_token"):
        ok, msg = check_drive_access(cfg)
        if ok:
            print(f"✓ 已有授权且可用: {msg}")
        else:
            print(f"· 已有授权但不可用（{msg}），重新授权...")
            cfg["tokens"] = oauth_authorize(cfg, no_browser=prefill.get("no_browser", False))
    else:
        print("· 尚未授权，开始 Device Flow 授权...")
        cfg["tokens"] = oauth_authorize(cfg, no_browser=prefill.get("no_browser", False))

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
    """管理 user_access_token：v3 端点 + DPoP proof 刷新（对齐 lark-cli 行为）。"""

    def __init__(self, cfg, cfg_path=CONFIG_PATH):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self._key = None

    def _tokens(self):
        return self.cfg.setdefault("tokens", {})

    def _dpop_key(self):
        """加载或生成 DPoP 签名钥匙（P-256，持久化在配置中保证绑定一致）。"""
        if self._key is not None:
            return self._key
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        pem = self.cfg.get("dpop_key")
        if pem:
            self._key = serialization.load_pem_private_key(pem.encode(), password=None)
        else:
            self._key = ec.generate_private_key(ec.SECP256R1())
            self.cfg["dpop_key"] = self._key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()).decode()
            save_config(self.cfg, self.cfg_path)
        return self._key

    def dpop_proof(self, htm, htu):
        """构造 DPoP proof（RFC 9449 风格 compact JWS，ES256 + P-256 JWK）。"""
        import uuid as _uuid
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        key = self._dpop_key()
        nums = key.public_key().public_numbers()
        b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        jwk = {"kty": "EC", "crv": "P-256",
               "x": b64u(nums.x.to_bytes(32, "big")),
               "y": b64u(nums.y.to_bytes(32, "big"))}
        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": jwk}
        claims = {"jti": str(_uuid.uuid4()), "htm": htm.upper(), "htu": htu, "iat": int(time.time())}
        h = b64u(json.dumps(header, separators=(",", ":")).encode())
        c = b64u(json.dumps(claims, separators=(",", ":")).encode())
        sig = key.sign((h + "." + c).encode(), ec.ECDSA(hashes.SHA256()))
        return h + "." + c + "." + b64u(sig[:32] + sig[32:])

    def _refresh(self):
        tok = self._tokens()
        if not tok.get("refresh_token"):
            raise RuntimeError("没有 refresh_token，请运行: python feishu_dav.py --init")
        # 对齐 lark-cli：token 端点在 accounts 域的 v3 路径，请求携带 DPoP proof
        htu = f"{AUTH_BASE}/oauth/v3/token"
        r = requests.post(htu,
            data={"grant_type": "refresh_token", "client_id": self.cfg["app_id"],
                  "client_secret": self.cfg["app_secret"], "refresh_token": tok["refresh_token"]},
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "DPoP": self.dpop_proof("POST", htu)},
            timeout=30)
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"token 刷新失败: HTTP {r.status_code} {r.text[:200]}")
        if r.status_code != 200 or not data.get("access_token"):
            raise RuntimeError(f"token 刷新失败: {data.get('error_description') or data}")
        tok["access_token"] = data["access_token"]
        tok["refresh_token"] = data.get("refresh_token", tok["refresh_token"])  # v3 会轮换 refresh_token
        tok["expire_at"] = time.time() + int(data.get("expires_in", 6900))
        tok["scope"] = data.get("scope", tok.get("scope", ""))
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
        self._size_lock = threading.Lock()
        self._size_queue = queue.Queue()
        self._size_queued = set()     # 已入队去重
        self._size_worker = None
        self.export_ttl = int(cfg.get("export_ttl", 86400))  # 导出缓存有效期（秒）

    def request_size_probe(self, file_token):
        """把 file_token 交给后台线程探测大小（不阻塞调用方）。"""
        with self._size_lock:
            if file_token in self._size_queued or file_token in FeishuClient._size_cache:
                return
            self._size_queued.add(file_token)
            self._size_queue.put(file_token)
            if self._size_worker is None or not self._size_worker.is_alive():
                self._size_worker = threading.Thread(
                    target=self._size_probe_loop, daemon=True, name="feishu-dav-size-probe")
                self._size_worker.start()

    def _size_probe_loop(self):
        while True:
            token = self._size_queue.get()
            try:
                self.file_size(token)
            except Exception:
                pass  # 探测失败静默：下次列目录仍会返回 0，可再次入队
            finally:
                with self._size_lock:
                    self._size_queued.discard(token)

    # ---- http 基础 ----

    def _request(self, method, path, *, retry_auth=True, **kw):
        url = path if path.startswith("http") else OPEN_BASE + path
        headers = kw.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.tm.access_token()}"
        try:
            headers["DPoP"] = self.tm.dpop_proof(method, url)
        except Exception:
            pass  # DPoP 不可用时退化为纯 Bearer
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
        """流式下载 file，返回 requests 响应（可 iter_content）。

        注意：对齐 lark-cli，使用 /files/{token}/download —— /medias/{token}/download
        对 PersonalAgent 等部分应用会返回 403（空响应体）。
        """
        return self._request(
            "GET", f"/open-apis/drive/v1/files/{file_token}/download", stream=True
        )

    _size_cache = {}

    def file_size(self, file_token):
        """懒加载文件真实大小：list API 不返回 size，用 Range 0-0 从 Content-Range 取。"""
        with self._size_lock:
            if file_token in FeishuClient._size_cache:
                return FeishuClient._size_cache[file_token]
        url = f"{OPEN_BASE}/open-apis/drive/v1/files/{file_token}/download"
        r = self._request("GET", url, headers={"Range": "bytes=0-0"}, stream=True)
        r.close()
        total = None
        cr = r.headers.get("Content-Range", "")
        if "/" in cr:
            try:
                total = int(cr.rsplit("/", 1)[1])
            except ValueError:
                total = None
        with self._size_lock:
            FeishuClient._size_cache[file_token] = total
        return total

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
        """对齐 lark-cli 的 /files 三步分片协议：
        prepare 只带 file_name/parent_type/parent_node/size，由服务端返回 block_size/block_num；
        part 表单字段为 upload_id/seq(0-based)/size(本片实际字节数)。
        """
        base = "/open-apis/drive/v1"
        r = self._request("POST", f"{base}/files/upload_prepare",
                          json={"file_name": filename, "parent_type": "explorer",
                                "parent_node": folder_token or "", "size": size})
        data = r.json()["data"]
        block_size = int(data["block_size"])
        block_num = int(data["block_num"])
        upload_id = data["upload_id"]
        with open(path, "rb") as f:
            for seq in range(block_num):
                chunk = f.read(block_size)
                if not chunk:
                    break
                self._request(
                    "POST", f"{base}/files/upload_part",
                    data={"upload_id": upload_id, "seq": str(seq), "size": str(len(chunk))},
                    files={"file": (filename, chunk, "application/octet-stream")},
                )
        r = self._request("POST", f"{base}/files/upload_finish",
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

    def export_cache_path(self, entry):
        """返回 entry 的导出缓存文件路径；类型不可导出时返回 None。"""
        ext = self.EXPORTABLE.get(entry["type"])
        if not ext:
            return None
        return os.path.join(CONFIG_DIR, "export_cache", f"{entry['token']}.{ext}")

    def export_content(self, entry):
        """导出在线文档为字节流；带磁盘缓存。"""
        ext = self.EXPORTABLE.get(entry["type"])
        if not ext:
            raise FeishuError(f"类型 {entry['type']} 不支持导出", status=403, code=403)
        cache_path = self.export_cache_path(entry)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        if os.path.exists(cache_path):
            if os.path.getsize(cache_path) == 0:
                os.unlink(cache_path)  # 历史坏缓存：空文件不命中
            elif time.time() - os.path.getmtime(cache_path) < self.export_ttl:
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
                                   f"/open-apis/drive/v1/export_tasks/file/{result['file_token']}/download",
                                   params={"token": ticket})
                with open(cache_path, "wb") as f:
                    for chunk in dl.iter_content(1 << 20):
                        f.write(chunk)
                if os.path.getsize(cache_path) == 0:
                    os.unlink(cache_path)
                    raise FeishuError("导出结果为空（请检查应用权限是否已发布）",
                                      status=502, code=502)
                with open(cache_path, "rb") as f:
                    return ext, f.read()
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
        if self.entry is None:
            members = self.provider.members(None)
            for m in self.provider.cfg.get("mounts") or []:
                # 挂载点与云空间根内容合并；同名时挂载点优先
                if m["name"] not in members:
                    members[m["name"]] = {"token": m["token"], "name": m["name"],
                                          "type": "folder", "size": 0, "mtime": 0, "url": ""}
            return members
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


EXTRA_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".md": "text/markdown",
}


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
            # lazy 导出 + 10K 哨兵：列目录不导出——有缓存报真实大小，无缓存
            # 报 10K 占位（非 0 大小才能让 rclone 打开时真正发 GET 触发导出；
            # 首次读到的内容会被截断，属预期，缓存生效后重新列目录即恢复）。
            # 实际内容在 GET 时导出并分块流式返回（不声明 Content-Length）
            cache_path = self.client.export_cache_path(self.entry)
            if cache_path and os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                return os.path.getsize(cache_path)
            return 10 << 10  # 10K 哨兵
        size = self.entry.get("size") or 0
        if not size and self.entry["type"] == "file":
            # list API 不返回 size：命中缓存直接用，未命中交后台探测，列目录不阻塞
            size = self.client.file_size(self.entry["token"])
            if size is None:
                self.client.request_size_probe(self.entry["token"])
                size = 0
            self.entry["size"] = size
        return size

    def get_content_type(self):
        name = self.pending_name or (self.entry or {}).get("name", "")
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if self.entry and self.entry["type"] in FeishuClient.EXPORTABLE:
            ext = FeishuClient.EXPORTABLE[self.entry["type"]]
        mime = mimetypes.types_map.get("." + ext) or EXTRA_MIME.get("." + ext)
        return mime or "application/octet-stream"

    def get_etag(self):
        return (self.entry or {}).get("token") or "pending"

    def get_display_name(self):
        if self.pending_name:
            return self.pending_name
        return self.provider._display_name(self.entry) if self.entry else ""

    def get_last_modified(self):
        return (self.entry or {}).get("mtime") or None

    def support_ranges(self):
        return False

    def support_content_length(self):
        # 未缓存的在线文档报 10K 哨兵大小：GET 直接声明 Content-Length: 10240，
        # wsgidav 流式读满 10240 后截断——客户端首开拿到不完整内容（预期内），
        # 导出缓存已生成，重新列目录后即报真实大小、完整读取
        # 飞书 list API 不返回 size；size 未知时不声明 Content-Length，
        # 避免 wsgidav 按 0 长度发空响应（改为流式读到 EOF）
        return bool(self.get_content_length())

    def support_etag(self):
        return False

    def get_content(self):
        """wsgidav 4.x 契约：无参，返回可迭代的文件对象。"""
        if self.entry is None:
            return io.BytesIO(b"")
        if self.entry["type"] in FeishuClient.EXPORTABLE:
            _, blob = self.client.export_content(self.entry)
            return io.BytesIO(blob)
        r = self.client.download(self.entry["token"])
        fd, tmp = tempfile.mkstemp(prefix="feishu-dav-dl-")
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        except Exception:
            os.unlink(tmp)
            raise
        return _AutoDeleteReader(tmp)

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


class _AutoDeleteReader:
    """包装临时文件：支持 read/迭代（wsgidav 按 iterable 消费），close 时自动删除磁盘副本。"""

    def __init__(self, path):
        self._f = open(path, "rb")
        self._path = path

    def read(self, *a):
        return self._f.read(*a)

    def __iter__(self):
        return self

    def __next__(self):
        line = self._f.readline()
        if not line:
            raise StopIteration
        return line

    def close(self):
        try:
            self._f.close()
        finally:
            try:
                os.unlink(self._path)
            except OSError:
                pass


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
            name = self._display_name(e)
            if name in seen or name in out:
                disp = f"{name}{SUFFIX_SEP}{e['token'][:8]}"
                out[disp] = e
            else:
                seen[name] = e
                out[name] = e
        return out

    @staticmethod
    def _display_name(e):
        """在线文档等导出型条目追加导出扩展名（如 xxx.docx），便于本地按扩展名打开。"""
        name = e["name"]
        ext = FeishuClient.EXPORTABLE.get(e["type"])
        if ext and not name.lower().endswith("." + ext):
            name = f"{name}.{ext}"
        return name

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
        # 首段不是挂载点名时，回落到自己的云空间根目录解析（根目录是「云空间+挂载点」合并视图）
        for i, name in enumerate(rest):
            entry = self._find(ft, name)
            if entry is None:
                raise FeishuError(f"路径不存在: {'/'.join(parts)}", status=404, code=404)
            cur = entry
            ft = entry["token"] if entry["type"] == "folder" else None
            if i < len(rest) - 1 and entry["type"] != "folder":
                raise FeishuError(f"路径不存在: {'/'.join(parts)}", status=404, code=404)
        # cur 为空时（根目录或挂载点本身）都是目录
        return cur, mount, cur is None or cur["type"] == "folder"

    def _find(self, folder_token, name):
        members = self.members(folder_token)
        if name in members:
            return members[name]
        # 兼容不带导出扩展名的裸文档名（如 xxx 或 xxx~token8）
        for e in members.values():
            if name in (e["name"], self._display_name(e)):
                return e
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
        if entry is None:  # 根目录或挂载点本身
            if mount:
                entry = {"token": mount["token"], "name": mount["name"], "type": "folder",
                         "size": 0, "mtime": 0, "url": ""}
            else:
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
    ap.add_argument("--no-browser", action="store_true",
                    help="授权时不尝试打开浏览器，仅打印链接（SSH/headless 场景）")
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
            "no_browser": args.no_browser,
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
            "no_browser": args.no_browser,
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
