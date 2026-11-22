#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音包 / 安卓安装包 Release → 阿里云 OSS。

voice-* → voice/<文件名>；android-v* → android/<文件名>，并写 android/latest.json（version、code、url、sha256，不缓存）。

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
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mirror_oss as m  # noqa: E402

PREFIXES = {"voice-": "voice/", "android-": "android/"}
PART = 8 << 20          # 分片 8 MB：大文件跨境单次 PUT 会超时，分片各自重试
SINGLE_MAX = 16 << 20   # 不超过 16 MB 的文件直接整体上传


def ctype_of(key):
    if key.endswith(".zip"):
        return "application/zip"
    if key.endswith(".apk"):
        return "application/vnd.android.package-archive"
    if key.endswith(".sha256") or key.endswith(".txt"):
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


def put_multipart(oss, key, path):
    """OSS 分片上传：InitiateMultipartUpload → UploadPart × N → CompleteMultipartUpload。"""
    h = {"Content-Type": ctype_of(key), "Cache-Control": m.LONG_CACHE}
    status, _, body = oss.request("POST", key, data=b"", headers=h, query="uploads", subres="?uploads")
    if status != 200:
        raise m.Fatal(oss.scrub(f"分片初始化失败：{key}（HTTP {status} {body[:300]!r}）"))
    upload_id = ET.fromstring(body).findtext("{*}UploadId") or ET.fromstring(body).findtext("UploadId")
    if not upload_id:
        raise m.Fatal(f"分片初始化未返回 UploadId：{key}")
    etags = []
    with open(path, "rb") as f:
        n = 0
        while True:
            chunk = f.read(PART)
            if not chunk:
                break
            n += 1
            md5 = base64.b64encode(hashlib.md5(chunk).digest()).decode()
            q = f"partNumber={n}&uploadId={upload_id}"
            for attempt in range(5):
                status, hdr, body = oss.request("PUT", key, data=chunk, headers={"Content-MD5": md5}, query=q, subres="?" + q)
                if status == 200:
                    etags.append((n, {k.lower(): v for k, v in hdr.items()}.get("etag", "")))
                    break
                time.sleep(2 + attempt * 2)
            else:
                raise m.Fatal(oss.scrub(f"分片 {n} 上传失败：{key}（HTTP {status}）"))
            m.log(f"  {key} 分片 {n}（{len(chunk)} 字节）")
    xml = "<CompleteMultipartUpload>" + "".join(
        f"<Part><PartNumber>{n}</PartNumber><ETag>{e}</ETag></Part>" for n, e in etags) + "</CompleteMultipartUpload>"
    q = f"uploadId={upload_id}"
    status, _, body = oss.request("POST", key, data=xml.encode(), headers={"Content-Type": "application/xml"}, query=q, subres="?" + q)
    if status != 200:
        raise m.Fatal(oss.scrub(f"分片合并失败：{key}（HTTP {status} {body[:300]!r}）"))
    return os.path.getsize(path)


def put_zip(oss, key, path):
    if os.path.getsize(path) > SINGLE_MAX:
        return put_multipart(oss, key, path)
    with open(path, "rb") as f:
        data = f.read()
    md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
    h = {"Content-MD5": md5, "Content-Type": ctype_of(key), "Cache-Control": m.LONG_CACHE}
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
    ap.add_argument("--code", default="", help="安卓 versionCode（android-v* 时写入 latest.json）")
    args = ap.parse_args()
    PREFIX = next((v for k, v in PREFIXES.items() if args.tag.startswith(k)), None)
    if PREFIX is None:
        raise m.Fatal(f"只镜像 voice- 或 android- 开头的 Release：{args.tag}")
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
    apk = None
    with tempfile.TemporaryDirectory() as tmp:
        for a in assets:
            key = PREFIX + a["name"]
            path = os.path.join(tmp, a["name"])
            m.download(a["url"], token, path)
            size = os.path.getsize(path)
            if size != a["size"]:
                raise m.Fatal(f"下载大小不符：{a['name']} {size} != {a['size']}")
            md5 = m.md5_hex(path)
            have = existing.get(key)
            if have and have[0] == size and (have[1] == md5 or size > SINGLE_MAX):
                m.log(f"已一致，跳过：{key}（{size} 字节）")
            else:
                put_zip(oss, key, path)
                m.log(f"已上传：{key}（{size} 字节，sha256 {m.sha256_hex(path)}）")
            if PREFIX == "android/" and a["name"].endswith(".apk"):
                apk = (a["name"], size, m.sha256_hex(path))
            oss.verify(key, size)
            os.remove(path)
    if PREFIX == "android/":
        if not args.code:
            raise m.Fatal("android Release 需要 --code（versionCode）")
        name, size, sha = apk
        import json
        doc = {"version": args.tag[len("android-v"):], "code": int(args.code), "url": oss.public + PREFIX + name,
               "fallback": f"https://github.com/{m.REPO}/releases/download/{args.tag}/{name}", "size": size, "sha256": sha}
        body = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
        md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
        status, _, rb = oss.request("PUT", PREFIX + "latest.json", data=body,
                                    headers={"Content-MD5": md5, "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache"})
        if status != 200:
            raise m.Fatal(oss.scrub(f"latest.json 上传失败（HTTP {status} {rb[:200]!r}）"))
        m.log(f"已写入 {PREFIX}latest.json：{doc}")
    m.log(f"完成：{len(assets)} 个文件，公网地址前缀 {oss.public}{PREFIX}")


if __name__ == "__main__":
    try:
        main()
    except m.Fatal as e:
        print(f"::error::{e}", flush=True)
        sys.exit(1)
