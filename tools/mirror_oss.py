#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hex Nook IME · 发布仓库 → 阿里云 OSS 镜像（大陆首选下载源）。

发布仓库是唯一源头，OSS 只是镜像：把 setup、setup-beta、data-pack、data-pack-beta、cloud-dict
五个分支的内容按「分支名/相对路径」原样放到 OSS（路径、字节、签名与分支完全一致，ECDSA 验签不受影响），
并把最新的安装包放到固定路径（正式版 latest/，测试版 latest-beta/；新名 CorspiiIME-Setup-x64.exe 与旧名副本
HexNookIME-Setup.exe 都放），最后生成发布清单 manifest/latest*.json（带系统与架构字段 os / arch / min_os）。

顺序（任何一步失败都立即退出，且不会更新清单与固定路径，用户不会读到半成品）：
  ① 数据与分段文件（长缓存）→ ② 清单类文件 setup.txt / setup.sig / notes.txt / manifest.*（短缓存）
  → ③ 安装包上传到「版本号/」目录并回读校验 → ④ 覆盖 latest 固定路径 → ⑤ 生成并上传 manifest/latest*.json。
上传前先列出 OSS 已有对象，大小与 MD5 一致的跳过（增量）；绝不删除 OSS 上的任何对象。

只依赖 Python 3 标准库；凭据只从环境变量读取，不出现在任何输出里：
  OSS_ACCESS_KEY_ID、OSS_ACCESS_KEY_SECRET、OSS_BUCKET、OSS_ENDPOINT。
"""
import argparse
import base64
import concurrent.futures as cf
import datetime
import email.utils
import hashlib
import hmac
import http.client
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

BRANCHES = ["setup", "setup-beta", "data-pack", "data-pack-beta", "cloud-dict"]
MANIFEST_NAMES = {"setup.txt", "setup.sig", "notes.txt", "manifest.txt", "manifest.sig", "manifest.signed"}
LONG_CACHE = "max-age=31536000"
SHORT_CACHE = "max-age=60"
# 安装包：0.9.69 起名为 CorspiiIME-Setup-x64.exe；每个 Release 同时附一份内容相同的旧名副本 HexNookIME-Setup.exe，
# 供 0.9.68 及更早的客户端升级（它们按「版本号/HexNookIME-Setup.exe」从 OSS 下载完整安装包）与官网无脚本兜底直链，长期保留。
# 安装包约 80 MB，超过 jsDelivr 单文件 20 MB 上限，清单里的下载源只有 OSS 与 GitHub Release。
# 改名前的 Release 只有旧名。OSS 上两个名字都放（版本目录与 latest 固定路径），内容是同一个文件。
INSTALLER = "CorspiiIME-Setup-x64.exe"
LEGACY_INSTALLER = "HexNookIME-Setup.exe"
INSTALLER_NAMES = (INSTALLER, LEGACY_INSTALLER)
# 发布清单里的系统与架构（读取方忽略不认识的字段；旧客户端不读本清单）
TARGET = {"os": "windows", "arch": "x64", "min_os": "10.0"}
# Windows 7 安装包（0.9.69 起，与 Windows 10 / 11 版同一个 Release 的单独附件；只有构建了 Windows 7 版的 Release 才有）：
# 外壳与引擎按 Windows 7 目标编译、载荷不压缩。软件内更新在 Windows 7 / 8 / 8.1（主版本 6）上只取这个附件，
# 并用同一 Release 里签过名的 setup-win7.txt / setup-win7.sig 校验（分段镜像只有 Windows 10 / 11 版）。
WIN7_INSTALLER = "CorspiiIME-Setup-win7-x64.exe"
WIN7_MANIFESTS = ("setup-win7.txt", "setup-win7.sig")
WIN7_TARGET = {"os": "windows", "arch": "x64", "min_os": "6.1.7601", "max_os": "6.3"}
REPO = os.environ.get("GITHUB_REPOSITORY", "15055334413le-ops/HexNook-IME-Releases")
REQUIRED_ENV = ["OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_BUCKET", "OSS_ENDPOINT"]


def log(msg):
    print(msg, flush=True)


class Fatal(Exception):
    pass


class Oss:
    def __init__(self):
        self.ak = os.environ["OSS_ACCESS_KEY_ID"]
        self.sk = os.environ["OSS_ACCESS_KEY_SECRET"]
        self.bucket = os.environ["OSS_BUCKET"]
        ep = os.environ["OSS_ENDPOINT"].strip().rstrip("/")
        # OSS_PATH_STYLE 仅供本机假 OSS 测试使用（路径风格地址）；正式运行不设置
        self.path_style = os.environ.get("OSS_PATH_STYLE") == "1"
        if "://" not in ep:
            ep = "https://" + ep
        scheme, host = ep.split("://", 1)
        self.base = f"{scheme}://{host}" if self.path_style else f"{scheme}://{self.bucket}.{host}"
        self.public = f"https://{self.bucket}.{host}/"
        self._local = threading.local()

    def scrub(self, text):
        for s in (self.ak, self.sk):
            if s:
                text = text.replace(s, "***")
        return text

    def _path(self, key, query=""):
        path = urllib.parse.quote(key, safe="/")
        prefix = f"/{self.bucket}" if self.path_style else ""
        return f"{prefix}/{path}{('?' + query) if query else ''}"

    def _conn(self):
        """每个线程复用一条连接：避免为每个文件新开连接而耗尽 Windows 的端口与缓冲区（WinError 10055）。"""
        c = getattr(self._local, "conn", None)
        if c is None:
            u = urllib.parse.urlsplit(self.base)
            cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
            c = cls(u.netloc, timeout=120)
            self._local.conn = c
        return c

    def _drop(self):
        c = getattr(self._local, "conn", None)
        self._local.conn = None
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def request(self, method, key, data=None, headers=None, query="", subres=""):
        last = ""
        for attempt in range(6):
            h = dict(headers or {})
            date = email.utils.formatdate(usegmt=True)
            md5 = h.get("Content-MD5", "")
            ctype = h.get("Content-Type", "")
            sts = f"{method}\n{md5}\n{ctype}\n{date}\n/{self.bucket}/{key}{subres}"
            sig = base64.b64encode(hmac.new(self.sk.encode(), sts.encode(), hashlib.sha1).digest()).decode()
            h["Date"] = date
            h["Authorization"] = f"OSS {self.ak}:{sig}"
            try:
                c = self._conn()
                c.request(method, self._path(key, query), body=data, headers=h)
                resp = c.getresponse()
                body = resp.read()
                if resp.getheader("Connection", "").lower() == "close":
                    self._drop()
                if resp.status >= 500 or resp.status == 429:  # 服务端暂时性错误或限流：重试
                    last = f"HTTP {resp.status}"
                    time.sleep(min(2 ** attempt, 10))
                    continue
                return resp.status, dict(resp.getheaders()), body[:100000] if resp.status >= 400 else body
            except (OSError, http.client.HTTPException) as e:  # 网络错误：丢弃连接，退避后重试
                self._drop()
                last = f"{type(e).__name__}：{e}"
                time.sleep(min(2 ** attempt, 10))
        raise Fatal(self.scrub(f"无法连接 OSS（已重试 6 次，{last}）"))

    def list_prefix(self, prefix):
        out, token = {}, None
        while True:
            q = "list-type=2&max-keys=1000&prefix=" + urllib.parse.quote(prefix, safe="")
            if token:
                q += "&continuation-token=" + urllib.parse.quote(token, safe="")
            sts_key = ""  # 列表请求的资源是 /bucket/
            # 真实 OSS 的签名资源要带上 continuation-token（取原始值，不做 URL 编码）：/bucket/?continuation-token=值
            subres = f"?continuation-token={token}" if token else ""
            status, _, body = self.request("GET", sts_key, query=q, subres=subres)
            if status != 200:
                raise Fatal(self.scrub(f"列出 OSS 对象失败（HTTP {status}）：{body.decode('utf-8','replace')}"))
            root = ET.fromstring(body)
            ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
            for c in root.findall(f"{ns}Contents"):
                k = c.findtext(f"{ns}Key")
                out[k] = (int(c.findtext(f"{ns}Size")), (c.findtext(f"{ns}ETag") or "").strip('"').lower())
            if (root.findtext(f"{ns}IsTruncated") or "").lower() == "true":
                token = root.findtext(f"{ns}NextContinuationToken")
            else:
                return out

    def put(self, key, path, cache):
        with open(path, "rb") as f:
            data = f.read()
        md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
        ctype = "application/octet-stream"
        h = {"Content-MD5": md5, "Content-Type": ctype, "Cache-Control": cache}
        last = ""
        for attempt in range(3):
            status, _, body = self.request("PUT", key, data=data, headers=h)
            if status == 200:
                return len(data)
            last = f"HTTP {status} {body.decode('utf-8','replace')}"
            time.sleep(1 + attempt)
        raise Fatal(self.scrub(f"上传失败：{key}（{last}）"))

    def head_size(self, key):
        status, hdr, _ = self.request("HEAD", key)
        if status != 200:
            return None
        lower = {k.lower(): v for k, v in hdr.items()}
        return int(lower.get("content-length", "-1"))

    def verify(self, key, expect):
        for _ in range(5):
            if self.head_size(key) == expect:
                return
            time.sleep(2)
        raise Fatal(f"回读校验失败：{key} 应为 {expect} 字节，实际为 {self.head_size(key)}，已终止")


def md5_hex(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_hex(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git(*args, cwd=None, text=True):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=text).stdout


def extract_branch(branch, dest, repo_dir):
    """把某分支内容导出到 dest/<branch>/…；分支不存在返回 None。"""
    ref = f"refs/remotes/origin/{branch}"
    try:
        sha = git("rev-parse", "--verify", "-q", ref, cwd=repo_dir).strip()
    except subprocess.CalledProcessError:
        log(f"  分支 {branch} 不存在，已跳过")
        return None
    target = os.path.join(dest, branch)
    os.makedirs(target, exist_ok=True)
    # 直接读 git 里的原始字节（ls-tree + cat-file），不经过 git archive：
    # archive 会按 core.autocrlf 把文本换成 CRLF（Windows 默认如此），清单的 ECDSA 签名就会对不上
    tree = subprocess.run(["git", "ls-tree", "-r", "-z", "--full-tree", ref], cwd=repo_dir, check=True,
                          capture_output=True).stdout
    entries = []
    for rec in tree.split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        if kind != "blob" or mode == "120000":  # 子模块、符号链接不属于发布内容
            continue
        entries.append((oid, path.decode("utf-8")))
    proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=repo_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for oid, rel in entries:
            proc.stdin.write(oid.encode() + b"\n")
            proc.stdin.flush()
            head = proc.stdout.readline().split()
            if len(head) != 3 or head[1] != b"blob":
                raise Fatal(f"读取 {branch}:{rel} 失败")
            size = int(head[2])
            data = proc.stdout.read(size)
            proc.stdout.read(1)  # 结尾换行
            if len(data) != size:
                raise Fatal(f"读取 {branch}:{rel} 不完整")
            out = os.path.join(target, *rel.split("/"))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(data)
    finally:
        proc.stdin.close()
        proc.stdout.close()
        proc.wait()
    return sha


def collect(dest, branches_present):
    files = []  # (key, path, is_manifest)
    for b in branches_present:
        root = os.path.join(dest, b)
        for dp, _, names in os.walk(root):
            for n in names:
                p = os.path.join(dp, n)
                rel = os.path.relpath(p, root).replace(os.sep, "/")
                is_manifest = b != "cloud-dict" and n in MANIFEST_NAMES
                files.append((f"{b}/{rel}", p, is_manifest))
    return sorted(files)


def plan_uploads(oss, files, dry):
    prefixes = sorted({k.split("/", 1)[0] + "/" for k, _, _ in files})
    remote = {}
    if not (dry and os.environ.get("MIRROR_DRY_OFFLINE") == "1"):
        for p in prefixes:
            remote.update(oss.list_prefix(p))
    todo = []
    for key, path, is_manifest in files:
        size = os.path.getsize(path)
        r = remote.get(key)
        if r and r[0] == size and r[1] == md5_hex(path):
            continue
        todo.append((key, path, is_manifest, size))
    return todo


def upload_many(oss, items, cache, workers=None):
    workers = workers or int(os.environ.get("MIRROR_WORKERS", "8"))
    def one(it):
        key, path, _, size = it
        oss.put(key, path, cache)
        oss.verify(key, size)
        return size
    total = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for n in ex.map(one, items):
            total += n
    return total


def gh_json(url, token):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def installer_asset(rel):
    """这个 Release 的安装包附件：优先新名，改名前的 Release 只有旧名。"""
    for name in INSTALLER_NAMES:
        for a in rel.get("assets", []):
            if a["name"] == name:
                return a
    return None


def win7_assets(rel):
    """这个 Release 的 Windows 7 安装包与签名清单：三者齐全时返回 {名字: 附件}，否则 None（不镜像不完整的一套）。"""
    found = {a["name"]: a for a in rel.get("assets", []) if a["name"] in (WIN7_INSTALLER, *WIN7_MANIFESTS)}
    return found if len(found) == 1 + len(WIN7_MANIFESTS) else None


def win7_state(assets):
    """用于判断 OSS 是否已是最新：各附件的大小与更新时间。"""
    return {n: [a.get("size"), a.get("updated_at")] for n, a in sorted(assets.items())} if assets else None


def pick_release(releases, channel):
    rel = [r for r in releases if not r.get("draft") and installer_asset(r)]
    rel.sort(key=lambda r: r["published_at"], reverse=True)
    if channel == "stable":
        rel = [r for r in rel if not r["prerelease"]]
    return rel[0] if rel else None


def download(url, token, dest):
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream",
                                               **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def main():
    ap = argparse.ArgumentParser(description="把发布仓库镜像到阿里云 OSS")
    ap.add_argument("--repo-dir", default=".", help="发布仓库的本地克隆（需已 fetch 各数据分支到 refs/remotes/origin/*）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要上传的清单与总字节数，不上传")
    ap.add_argument("--skip-installer", action="store_true", help="不处理最新安装包与 latest 固定路径")
    ap.add_argument("--releases-json", help="（测试用）从本地 JSON 读取 Release 列表，不访问 GitHub API")
    ap.add_argument("--audit-only", action="store_true", help="只审计 OSS 清单与各分支、Release 是否一致")
    ap.add_argument("--check-only", action="store_true",
                    help="轻量检查（只用 git ls-remote 与 OSS 清单，不下载分支）：已一致退出 0，需要镜像退出 10")
    args = ap.parse_args()

    missing = [n for n in REQUIRED_ENV if not os.environ.get(n)]
    if missing and not (args.dry_run and os.environ.get("MIRROR_DRY_OFFLINE") == "1"):
        log("错误：未检测到环境变量 " + "、".join(missing) + "，已终止")
        return 2
    if missing:
        for n in REQUIRED_ENV:
            os.environ.setdefault(n, "dry")
    oss = Oss()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo_dir = os.path.abspath(args.repo_dir)

    if args.check_only:
        return check_needed(oss, repo_dir, token, args)

    try:
        with tempfile.TemporaryDirectory(prefix="mirror-oss-") as tmp:
            log("===== Hex Nook IME · 镜像发布仓库到阿里云 OSS =====")
            shas = {}
            for b in BRANCHES:
                sha = extract_branch(b, tmp, repo_dir)
                if sha:
                    shas[b] = sha
            if not shas:
                raise Fatal("没有找到任何数据分支，已终止")
            files = collect(tmp, list(shas))
            todo = plan_uploads(oss, files, args.dry_run)
            data = [t for t in todo if not t[2]]
            manifests = [t for t in todo if t[2]]
            log(f"分支 {len(shas)} 个，共 {len(files)} 个文件；需要上传 {len(todo)} 个"
                f"（数据 {len(data)}、清单 {len(manifests)}，{sum(t[3] for t in todo)} 字节）")
            if args.dry_run:
                for key, _, is_m, size in todo[:50]:
                    log(f"  {'清单' if is_m else '数据'}  {key}  {size}")
                if len(todo) > 50:
                    log(f"  …… 另有 {len(todo) - 50} 个")
                return 0

            if args.audit_only:
                return audit(oss, shas, token, args)

            log("① 上传数据与分段文件（长缓存）…")
            upload_many(oss, data, LONG_CACHE)
            log("② 上传清单与签名文件（短缓存，最后传）…")
            upload_many(oss, manifests, SHORT_CACHE, workers=4)

            installers = {}
            if not args.skip_installer:
                if args.releases_json:
                    releases = json.load(open(args.releases_json, encoding="utf-8"))
                else:
                    releases = gh_json(f"https://api.github.com/repos/{REPO}/releases?per_page=100", token)
                for channel, prefix, mkey in (("stable", "latest", "manifest/latest.json"),
                                              ("beta", "latest-beta", "manifest/latest-beta.json")):
                    rel = pick_release(releases, channel)
                    if not rel:
                        log(f"  {channel}：没有可用的 Release，已跳过")
                        continue
                    version = rel["tag_name"].lstrip("vV")
                    asset = installer_asset(rel)
                    # 主文件名 + 旧名副本（改名前的 Release 只有旧名，两者相同）
                    names = list(dict.fromkeys([asset["name"], LEGACY_INSTALLER]))
                    keys = [f"{v}/{n}" for n in names for v in (version, prefix)]
                    # 已有清单与 OSS 对象都与这个附件一致时，不再重复下载（定时兜底每 10 分钟跑一次，避免白下几十 MB）
                    old = None
                    status, _, body = oss.request("GET", mkey)
                    if status == 200:
                        try:
                            old = json.loads(body)
                        except ValueError:
                            old = None
                    w7 = win7_assets(rel)
                    w7keys = [(f"{v}/{n}", a.get("size")) for n, a in (w7 or {}).items() for v in (version, prefix)]
                    same = bool(old and old.get("version") == version and old.get("size") == asset.get("size")
                                and old.get("asset_updated_at") == asset.get("updated_at") and old.get("installer") == asset["name"]
                                and all(oss.head_size(k) == asset.get("size") for k in keys)
                                and old.get("win7_assets") == win7_state(w7)
                                and all(oss.head_size(k) == n for k, n in w7keys))
                    if same:
                        log(f"③ {channel}：安装包 {version} 已是最新，跳过下载")
                        installers[channel] = (prefix, mkey, version, None, asset["size"], old["sha256"], rel, asset, names,
                                               old.get("variants", []))
                        continue
                    local = os.path.join(tmp, f"{channel}-{asset['name']}")
                    download(asset["browser_download_url"], token, local)
                    size = os.path.getsize(local)
                    if asset.get("size") and asset["size"] != size:
                        raise Fatal(f"安装包下载不完整：{asset['size']} ≠ {size}")
                    md5 = md5_hex(local)
                    # 先把各个名字都放进版本目录并回读校验，再覆盖固定路径
                    for n in names:
                        vkey = f"{version}/{n}"
                        log(f"③ {channel}：安装包 {version}（{size} 字节）→ {vkey}")
                        cur = oss.list_prefix(vkey).get(vkey)
                        if not (cur and cur[0] == size and cur[1] == md5):
                            oss.put(vkey, local, LONG_CACHE)
                        oss.verify(vkey, size)
                    for n in names:
                        lkey = f"{prefix}/{n}"
                        cur = oss.list_prefix(lkey).get(lkey)
                        if not (cur and cur[0] == size and cur[1] == md5):
                            log(f"④ 覆盖固定路径 {lkey}")
                            oss.put(lkey, local, SHORT_CACHE)
                        oss.verify(lkey, size)
                    # Windows 7 安装包与它的签名清单：同样先进版本目录，再覆盖固定路径
                    variants = []
                    if w7:
                        w7local = {}
                        for n, a in w7.items():
                            path = os.path.join(tmp, f"{channel}-{n}")
                            download(a["browser_download_url"], token, path)
                            if a.get("size") and a["size"] != os.path.getsize(path):
                                raise Fatal(f"{n} 下载不完整：{a['size']} ≠ {os.path.getsize(path)}")
                            w7local[n] = path
                        for keyprefix, cache in ((version, LONG_CACHE), (prefix, SHORT_CACHE)):
                            for n, path in w7local.items():
                                key = f"{keyprefix}/{n}"
                                n_size, n_md5 = os.path.getsize(path), md5_hex(path)
                                cur = oss.list_prefix(key).get(key)
                                if not (cur and cur[0] == n_size and cur[1] == n_md5):
                                    log(f"③ {channel}：Windows 7 版 {version} → {key}")
                                    oss.put(key, path, SHORT_CACHE if n in WIN7_MANIFESTS else cache)
                                oss.verify(key, n_size)
                        w7path = w7local[WIN7_INSTALLER]
                        w7size, w7sha = os.path.getsize(w7path), sha256_hex(w7path)
                        variants = [{
                            "id": "win7-x64", "system": "Windows 7 SP1 / 8 / 8.1", **WIN7_TARGET,
                            "installer": WIN7_INSTALLER, "latest": f"{oss.public}{prefix}/{WIN7_INSTALLER}",
                            "size": w7size, "sha256": w7sha,
                            "signed_manifest": [f"{oss.public}{version}/{n}" for n in WIN7_MANIFESTS],
                            "sources": [f"{oss.public}{version}/{WIN7_INSTALLER}", w7[WIN7_INSTALLER]["browser_download_url"]],
                        }]
                    installers[channel] = (prefix, mkey, version, local, size, sha256_hex(local), rel, asset, names, variants)

            for channel, (prefix, mkey, version, local, size, sha, rel, asset, names, variants) in installers.items():
                base = oss.public
                gh_dl = asset["browser_download_url"].rsplit("/", 1)[0] + "/"
                main_name = asset["name"]
                manifest = {
                    "format": 1, "version": version, "channel": channel,
                    "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "size": size, "sha256": sha, "installer": main_name,
                    "latest": f"{base}{prefix}/{main_name}",
                    "legacy_installer": LEGACY_INSTALLER, **TARGET,
                    "release_url": rel.get("html_url", f"https://github.com/{REPO}/releases/tag/v{version}"),
                    "asset_updated_at": asset.get("updated_at"),
                    # 每个名字一条（内容相同）；GitHub 地址只列 Release 里确实有的附件
                    "files": [{
                        "name": n, "path": f"{version}/{n}", "size": size, "sha256": sha, **TARGET,
                        "sources": [f"{base}{version}/{n}"] + ([gh_dl + n] if any(a["name"] == n for a in rel.get("assets", [])) else []),
                    } for n in names],
                    "branches": dict(shas),
                    # 其他系统的安装包（适用系统写在各条目的 os / min_os / max_os 里）；没有时为空列表
                    "variants": variants,
                    "win7_assets": win7_state(win7_assets(rel)),
                }
                # 内容没变化就不重写清单（generated 时间除外），减少无意义写入
                status, _, body = oss.request("GET", mkey)
                if status == 200:
                    try:
                        prev = json.loads(body)
                        if all(prev.get(k) == manifest[k] for k in manifest if k != "generated"):
                            log(f"⑤ 发布清单 {mkey} 无变化，跳过")
                            continue
                    except ValueError:
                        pass
                mpath = os.path.join(tmp, f"manifest-{channel}.json")
                with open(mpath, "w", encoding="utf-8", newline="\n") as f:
                    json.dump(manifest, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                log(f"⑤ 上传发布清单 {mkey}")
                oss.put(mkey, mpath, SHORT_CACHE)
                oss.verify(mkey, os.path.getsize(mpath))

            rc = audit(oss, shas, token, args)
            if rc == 0:
                log("镜像完成：OSS 与发布仓库一致。")
            return rc
    except Fatal as e:
        log(f"错误：{e}")
        return 1


def check_needed(oss, repo_dir, token, args):
    """不下载任何分支内容：比较远端各分支提交号与 OSS 清单记录、最新 Release 版本；一致 → 0（无需镜像），否则 → 10。"""
    try:
        out = git("ls-remote", "--heads", "origin", cwd=repo_dir)
        remote = {}
        for line in out.splitlines():
            sha, ref = line.split("\t")
            name = ref[len("refs/heads/"):]
            if name in BRANCHES:
                remote[name] = sha
        if args.releases_json:
            releases = json.load(open(args.releases_json, encoding="utf-8"))
        else:
            releases = gh_json(f"https://api.github.com/repos/{REPO}/releases?per_page=100", token)
        for channel, key in (("stable", "manifest/latest.json"), ("beta", "manifest/latest-beta.json")):
            rel = pick_release(releases, channel)
            if not rel:
                continue
            status, _, body = oss.request("GET", key)
            if status != 200:
                log(f"{key} 不存在或读取失败（HTTP {status}），需要镜像")
                return 10
            m = json.loads(body)
            asset = installer_asset(rel)
            if (m.get("version") != rel["tag_name"].lstrip("vV") or m.get("branches") != remote
                    or m.get("asset_updated_at") != asset.get("updated_at") or m.get("installer") != asset["name"]
                    or m.get("arch") != TARGET["arch"] or m.get("win7_assets") != win7_state(win7_assets(rel))):
                log(f"{key} 与发布仓库不一致，需要镜像")
                return 10
        log("OSS 与发布仓库一致，无需镜像。")
        return 0
    except Fatal:
        raise
    except Exception as e:
        log(f"轻量检查失败（{type(e).__name__}：{e}），按需要镜像处理")
        return 10


def audit(oss, shas, token, args):
    """兜底审计：OSS 清单必须与各分支当前提交、最新 Release 版本一致，不一致则报错（任务变红）。"""
    bad = []
    if args.skip_installer:
        return 0
    try:
        if args.releases_json:
            releases = json.load(open(args.releases_json, encoding="utf-8"))
        else:
            releases = gh_json(f"https://api.github.com/repos/{REPO}/releases?per_page=100", token)
    except Exception as e:
        raise Fatal(f"审计时读取 Release 失败：{e}")
    for channel, key in (("stable", "manifest/latest.json"), ("beta", "manifest/latest-beta.json")):
        rel = pick_release(releases, channel)
        if not rel:
            continue
        status, _, body = oss.request("GET", key)
        if status != 200:
            bad.append(f"{key} 读取失败（HTTP {status}）")
            continue
        m = json.loads(body)
        if m.get("version") != rel["tag_name"].lstrip("vV"):
            bad.append(f"{key} 版本 {m.get('version')} ≠ 最新 Release {rel['tag_name']}")
        for b, sha in shas.items():
            if m.get("branches", {}).get(b) != sha:
                bad.append(f"{key} 记录的分支 {b} 提交与当前不一致")
    if bad:
        for line in bad:
            log("审计不一致：" + line)
        return 1
    log("审计通过：清单与分支、最新 Release 一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
