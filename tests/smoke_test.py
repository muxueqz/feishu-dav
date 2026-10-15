#!/usr/bin/env python3
"""冒烟测试：用内存 Fake API 客户端（与 FeishuClient 同接口）对 WebDAV 层做
list/download/upload/export 全链路验证（不含真实飞书服务端）。

用法: python tests/smoke_test.py
"""

import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from feishu_dav import FeishuClient, FeishuProvider  # noqa: E402


class FakeResponse:
    def __init__(self, blob):
        self.blob = blob

    def iter_content(self, n):
        yield self.blob


class FakeClient(FeishuClient):
    """内存版 FeishuClient：不触网，数据与真实实现接口一致。"""

    def __init__(self):
        self.entries = {
            None: [
                {"token": "f-fold1", "name": "docs", "type": "folder", "size": 0,
                 "mtime": 1700000000, "url": ""},
                {"token": "file1", "name": "hello.txt", "type": "file", "size": 6,
                 "mtime": 1700000001, "url": ""},
                {"token": "docx1", "name": "在线文档", "type": "docx", "size": 0,
                 "mtime": 1700000002, "url": ""},
            ],
            "f-fold1": [
                {"token": "file2", "name": "inner.txt", "type": "file", "size": 3,
                 "mtime": 1700000003, "url": ""},
                {"token": "docx1", "name": "在线文档", "type": "docx", "size": 0,
                 "mtime": 1700000002, "url": ""},
            ],
        }
        self.blobs = {"file1": b"hello!", "file2": b"abc"}
        self.exports = {"docx1": b"EXPORTED-DOCX"}
        self.uploaded = []
        self.dir_ttl = 0

    def list_dir(self, folder_token=None, use_cache=True):
        return list(self.entries.get(folder_token, []))

    def invalidate_dir(self, folder_token=None):
        pass

    def download(self, file_token, chunk_size=1 << 20):
        if file_token in self.blobs:
            return FakeResponse(self.blobs[file_token])
        raise RuntimeError(f"no such file {file_token}")

    def upload(self, folder_token, filename, data_or_path, size):
        self.uploaded.append((folder_token, filename, size))
        return f"new-{filename}"

    def create_folder(self, parent_token, name):
        entry = {"token": f"new-{name}", "name": name, "type": "folder",
                 "size": 0, "mtime": 1700000004, "url": ""}
        self.entries.setdefault(parent_token, []).append(entry)
        return entry["token"]

    def delete(self, entry):
        return True

    def export_content(self, entry):
        return "docx", self.exports[entry["token"]]


def main():
    cfg = {"mounts": [{"name": "Shared", "token": "f-fold1"}],
           "read_only": False, "allow_delete": True}
    fake = FakeClient()
    provider = FeishuProvider(fake, cfg)
    ENV = {"wsgidav.provider": provider}
    ro_ENV = None

    # --- 1. 路径解析 / exists ---
    assert provider.exists("/", ENV)
    assert provider.is_collection("/", ENV)
    assert provider.exists("/Shared", ENV)
    assert provider.is_collection("/Shared", ENV)
    assert provider.exists("/Shared/inner.txt", ENV)
    assert not provider.exists("/Shared/nope.txt", ENV)
    assert not provider.exists("/Unknown", ENV)
    print("✓ 路径解析/exists/is_collection")

    # --- 2. PROPFIND 列目录 ---
    root = provider.get_resource_inst("/", ENV)
    names = root.get_member_names()
    assert names == ["Shared"], names  # 配置挂载点后根目录只显示挂载点
    sub = provider.get_resource_inst("/Shared", ENV)
    sub_names = sub.get_member_names()
    assert "inner.txt" in sub_names and "在线文档" in sub_names, sub_names
    sub_members = {r.get_display_name(): r for r in sub.get_member_list()}
    assert sub_members["inner.txt"].get_content_length() == 3
    assert sub_members["inner.txt"].get_content_type() == "text/plain"
    assert sub_members["在线文档"].get_content_length() == len(b"EXPORTED-DOCX")
    assert "officedocument" in sub_members["在线文档"].get_content_type()
    print(f"✓ 根目录=挂载点 {names}，挂载点列取: {sub_names}")

    # --- 2b. 无挂载点时根目录 = 我的云空间 ---
    no_mount_provider = FeishuProvider(fake, {"mounts": [], "read_only": False, "allow_delete": True})
    root_names = no_mount_provider.get_resource_inst("/", {"wsgidav.provider": no_mount_provider}).get_member_names()
    assert "hello.txt" in root_names and "docs" in root_names, root_names
    print(f"✓ 无挂载点根目录列取: {root_names}")

    # --- 3. 下载（普通文件 + 在线文档导出）---
    buf = io.BytesIO()
    provider.get_resource_inst("/Shared/inner.txt", ENV).get_content().read() == b"abc"
    buf = io.BytesIO()
    got = provider.get_resource_inst("/Shared/inner.txt", ENV).get_content().read()
    assert got == b"abc", got
    got2 = provider.get_resource_inst("/Shared/在线文档", ENV).get_content().read()
    assert got2 == b"EXPORTED-DOCX", got2
    print("✓ 下载文件与在线文档导出")

    # --- 4. PUT 上传链路 ---
    coll = provider.get_resource_inst("/Shared", ENV)
    res = coll.create_empty_resource("put-test.txt")
    res.pending_name = "put-test.txt"
    w = res.begin_write()
    w.write(b"uploaded-data")
    w.close()
    res.end_write()
    assert fake.uploaded == [("f-fold1", "put-test.txt", 13)], fake.uploaded
    print("✓ PUT 上传（begin_write→end_write→upload）")

    # --- 5. 只读模式拒绝写 ---
    from wsgidav.dav_error import DAVError
    ro = FeishuProvider(fake, {**cfg, "read_only": True})
    try:
        ro.get_resource_inst("/Shared", {"wsgidav.provider": ro}).create_empty_resource("x")
        raise AssertionError("只读模式应拒绝写")
    except DAVError:
        pass
    print("✓ 只读模式拒绝写操作")

    # --- 6. allow_delete 关闭时拒绝删除 ---
    nd = FeishuProvider(fake, {**cfg, "allow_delete": False})
    try:
        nd.get_resource_inst("/Shared/inner.txt", {"wsgidav.provider": nd}).delete()
        raise AssertionError("应拒绝删除")
    except DAVError:
        pass
    print("✓ 未开启 allow_delete 时拒绝删除")

    print("\n全部通过")


if __name__ == "__main__":
    main()
