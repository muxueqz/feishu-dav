"""最小捕获代理：记录请求方法/URL/头/表单字段与分片大小，然后转发到真实网关。"""
import http.server
import json
import re
import ssl
import sys
import threading

import requests

LOG = "/tmp/captured_requests.log"
UPSTREAM = "https://open.feishu.cn"


class Proxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        ua = self.headers.get("User-Agent", "")
        auth = self.headers.get("Authorization", "")
        dpop = self.headers.get("DPoP", "")
        info = {"method": "POST", "path": self.path,
                "user_agent": ua, "auth_scheme": auth.split(" ")[0] if auth else "",
                "dpop": bool(dpop), "content_type": self.headers.get("Content-Type", "")}
        # multipart: 提取各字段名与文件分片的实际字节数
        ct = self.headers.get("Content-Type", "")
        if "multipart/form-data" in ct:
            m = re.search(r"boundary=(.+)", ct)
            if m:
                boundary = m.group(1).encode()
                for part in body.split(b"--" + boundary):
                    nm = re.search(rb'name="([^"]+)"', part)
                    if b"filename=" in part:
                        hdr_end = part.find(b"\r\n\r\n")
                        payload = part[hdr_end + 4:]
                        if payload.endswith(b"\r\n"):
                            payload = payload[:-2]
                        info.setdefault("parts", []).append(
                            {"field": nm.group(1).decode() if nm else "?",
                             "bytes": len(payload)})
                    elif nm:
                        val = part.split(b"\r\n\r\n", 1)[-1].rstrip(b"\r\n")
                        info.setdefault("form", {})[nm.group(1).decode()] = val.decode(errors="replace")
        with open(LOG, "a") as f:
            f.write(json.dumps(info, ensure_ascii=False) + "\n")

        # 转发
        fwd_headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "content-length", "connection", "accept-encoding")}
        try:
            r = requests.post(UPSTREAM + self.path, data=body,
                              headers=fwd_headers, timeout=300)
            resp = r.content
            self.send_response(r.status_code)
            for k, v in r.headers.items():
                if k.lower() not in ("content-encoding", "transfer-encoding", "connection", "content-length"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        except Exception as e:  # noqa: BLE001
            self.send_error(502, str(e))

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 18899), Proxy)
    srv.serve_forever()
