#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音包 Release（voice-*）→ 阿里云 OSS voice/ 目录。

语音运行库与模型单个文件超过 git 100 MB 上限，不能像 models 分支那样走 mirror_oss.py，
因此直接把指定 Release 的全部附件原样上传到 OSS 的 voice/<文件名>（application/zip，长缓存），
上传后回读大小校验。已存在且大小、MD5 一致的对象跳过；不删除 OSS 上的任何对象。
凭据与桶、端点约定与 mirror_oss.py 相同（OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / OSS_BUCKET / OSS_ENDPOINT）。
"""
import argparse
import base64
import hashlib
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mirror_oss as m  # noqa: E402

PREFIX = "voice/"


def put_zip(oss, key, path):
    with open(path, "rb") as f:
        data = f.read()
    md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
    ctype = "application/zip" if key.endswith(".zip") else "application/octet-stream"
    h = {"Content-MD5": md5, "Content-Type": ctype, "Cache-Control": m.LONG_CACHE}
    last = ""
    for attempt in range(3):
        status, _, body = oss.request("PUT", key, data=data, headers=h)
        if status == 200:
            return len(data)
        last = f"HTTP {status} {body.decode('utf-8', 'replace')}"
        time.sleep(1 + attempt)
    raise m.Fatal(oss.scrub(f"上传失败：{key}（{last}）"))


def main():
    ap = argparse.ArgumentParser(description="把语音包 Release 的附件镜像到 OSS voice/")
    ap.add_argument("--tag", required=True)
    args = ap.parse_args()
    if not args.tag.startswith("voice-"):
        raise m.Fatal(f"只镜像 voice- 开头的 Release：{args.tag}")
    for n in m.REQUIRED_ENV:
        if not os.environ.get(n):
            raise m.Fatal(f"未配置 {n}")
    token = os.environ.get("GITHUB_TOKEN", "")
    rel = m.gh_json(f"https://api.github.com/repos/{m.REPO}/releases/tags/{args.tag}", token)
    assets = rel.get("assets", [])
    if not assets:
        raise m.Fatal(f"Release {args.tag} 没有附件")
    oss = m.Oss()
    existing = oss.list_prefix(PREFIX)
    with tempfile.TemporaryDirectory() as tmp:
        for a in assets:
            key = PREFIX + a["name"]
            path = os.path.join(tmp, a["name"])
            m.download(a["url"], token, path)
            size = os.path.getsize(path)
            if size != a["size"]:
                raise m.Fatal(f"下载大小不符：{a['name']} {size} != {a['size']}")
            md5 = m.md5_hex(path)
            if existing.get(key) == (size, md5):
                m.log(f"已一致，跳过：{key}（{size} 字节）")
            else:
                put_zip(oss, key, path)
                m.log(f"已上传：{key}（{size} 字节，sha256 {m.sha256_hex(path)}）")
            oss.verify(key, size)
            os.remove(path)
    m.log(f"完成：{len(assets)} 个文件，公网地址前缀 {oss.public}{PREFIX}")


if __name__ == "__main__":
    try:
        main()
    except m.Fatal as e:
        print(f"::error::{e}", flush=True)
        sys.exit(1)
