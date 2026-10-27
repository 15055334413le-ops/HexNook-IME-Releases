#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mirror_oss.py 的本机端到端测试：假 OSS（路径风格）+ 合成发布仓库，不访问网络、不使用任何真实密钥。
用法：python3 tools/test_mirror_oss.py
"""
import base64, hashlib, hmac, json, os, subprocess, sys, tempfile, threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import xml.sax.saxutils as sx

AK, SK, BUCKET = "TESTAK", "TESTSECRET", "corspii"
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "mirror_oss.py")

store = {}          # key -> bytes
puts = []           # PUT 顺序
truncate_key = [None]
dl_files = {}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def _key(self):
        u = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(u.path)
        return path, urllib.parse.parse_qs(u.query)

    def _auth_ok(self, method, key, ct="", md5=""):
        auth = self.headers.get("Authorization", "")
        sts = f"{method}\n{md5}\n{ct}\n{self.headers.get('Date')}\n/{BUCKET}/{key}"
        sig = base64.b64encode(hmac.new(SK.encode(), sts.encode(), hashlib.sha1).digest()).decode()
        return auth == f"OSS {AK}:{sig}"

    def _send(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)

    def do_PUT(self):
        path, _ = self._key()
        key = path[len(f"/{BUCKET}/"):]
        n = int(self.headers.get("Content-Length", 0)); data = self.rfile.read(n)
        if not self._auth_ok("PUT", key, self.headers.get("Content-Type", ""), self.headers.get("Content-MD5", "")):
            return self._send(403, b"bad signature")
        if base64.b64encode(hashlib.md5(data).digest()).decode() != self.headers.get("Content-MD5"):
            return self._send(400, b"md5 mismatch")
        puts.append(key)
        store[key] = data[:-1] if truncate_key[0] and truncate_key[0] in key else data   # 模拟存储端大小不符
        self._send(200)

    def do_HEAD(self):
        path, _ = self._key()
        key = path[len(f"/{BUCKET}/"):]
        if key not in store: return self._send(404)
        self.send_response(200); self.send_header("Content-Length", str(len(store[key]))); self.end_headers()

    def do_GET(self):
        path, q = self._key()
        if path.startswith("/dl/"):
            data = dl_files.get(path[4:])
            return self._send(200 if data is not None else 404, data or b"")
        key = path[len(f"/{BUCKET}/"):]
        if key == "" and "list-type" in q:
            if not self._auth_ok("GET", ""): return self._send(403)
            prefix = q.get("prefix", [""])[0]
            items = "".join(
                f"<Contents><Key>{sx.escape(k)}</Key><Size>{len(v)}</Size><ETag>&quot;{hashlib.md5(v).hexdigest().upper()}&quot;</ETag></Contents>"
                for k, v in sorted(store.items()) if k.startswith(prefix))
            body = f'<?xml version="1.0"?><ListBucketResult xmlns="http://doc.oss.aliyuncs.com"><IsTruncated>false</IsTruncated>{items}</ListBucketResult>'.encode()
            return self._send(200, body)
        if not self._auth_ok("GET", key): return self._send(403)
        if key not in store: return self._send(404, b"NoSuchKey")
        self._send(200, store[key])


def sh(*a, cwd=None, env=None):
    return subprocess.run(a, cwd=cwd, env=env, capture_output=True, text=True)


def make_repo(tmp):
    r = os.path.join(tmp, "repo"); os.makedirs(r)
    sh("git", "init", "-q", cwd=r); sh("git", "config", "user.email", "t@t", cwd=r); sh("git", "config", "user.name", "t", cwd=r)
    def branch(name, files):
        sh("git", "checkout", "-q", "--orphan", name, cwd=r); sh("git", "rm", "-rqf", ".", cwd=r)
        for rel, data in files.items():
            p = os.path.join(r, rel); os.makedirs(os.path.dirname(p) or r, exist_ok=True); open(p, "wb").write(data)
        sh("git", "add", "-A", cwd=r); sh("git", "commit", "-qm", name, cwd=r)
        sha = sh("git", "rev-parse", "HEAD", cwd=r).stdout.strip()
        sh("git", "update-ref", f"refs/remotes/origin/{name}", sha, cwd=r)
    branch("setup", {"v1/setup.txt": b"format 1\nversion 0.9.32\n", "v1/setup.sig": b"sig", "v1/notes.txt": "说明\n".encode(),
                     "v1/files/aa.0": os.urandom(5000), "v1/files/aa.1": os.urandom(100)})
    branch("data-pack", {"v3/manifest.txt": b"format 3\n", "v3/manifest.sig": b"s3", "v3/files/x.1.0": os.urandom(3000)})
    branch("cloud-dict", {"LICENSE": b"MIT", "v2/rime-ice/a/aa.txt": "啊 a\n".encode(), "v2/rime-ice/LICENSE": b"L"})
    return r


def run(repo, port, releases, extra=()):
    env = dict(os.environ, OSS_ACCESS_KEY_ID=AK, OSS_ACCESS_KEY_SECRET=SK, OSS_BUCKET=BUCKET,
               OSS_ENDPOINT=f"http://127.0.0.1:{port}", OSS_PATH_STYLE="1", GITHUB_REPOSITORY="o/r")
    return subprocess.run([sys.executable, "-I", SCRIPT, "--repo-dir", repo, "--releases-json", releases, *extra],
                          env=env, capture_output=True, text=True)


def check(name, cond, detail=""):
    print(("通过  " if cond else "失败  ") + name + (f"  [{detail}]" if detail and not cond else ""))
    return cond


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp)
        stable, beta = os.urandom(4000), os.urandom(4500)
        dl_files.update({"stable.exe": stable, "beta.exe": beta})
        base = f"http://127.0.0.1:{port}/dl/"
        rels = [
            {"tag_name": "v0.9.51", "prerelease": True, "draft": False, "published_at": "2026-10-27T10:00:00Z", "html_url": "u",
             "assets": [{"name": "HexNookIME-Setup.exe", "size": len(beta), "updated_at": "t2", "browser_download_url": base + "beta.exe"}]},
            {"tag_name": "v0.9.32", "prerelease": False, "draft": False, "published_at": "2026-10-11T09:17:47Z", "html_url": "u",
             "assets": [{"name": "HexNookIME-Setup.exe", "size": len(stable), "updated_at": "t1", "browser_download_url": base + "stable.exe"}]},
        ]
        rj = os.path.join(tmp, "releases.json"); json.dump(rels, open(rj, "w"))

        # 0 缺环境变量
        r0 = subprocess.run([sys.executable, "-I", SCRIPT, "--repo-dir", repo], env={k: v for k, v in os.environ.items() if not k.startswith("OSS_")}, capture_output=True, text=True)
        ok &= check("缺环境变量：中文报错、退出码 2", r0.returncode == 2 and "OSS_BUCKET" in r0.stdout, r0.stdout)

        # 1 首次全量
        r1 = run(repo, port, rj); print(r1.stdout.strip().splitlines()[-1] if r1.stdout else r1.stderr)
        ok &= check("首次全量上传成功", r1.returncode == 0, r1.stdout + r1.stderr)
        def blob(ref, path):
            return subprocess.run(["git", "show", f"{ref}:{path}"], cwd=repo, capture_output=True).stdout
        ok &= check("分支内容原样镜像（路径与字节逐个一致）",
                    store.get("setup/v1/files/aa.0") == blob("refs/remotes/origin/setup", "v1/files/aa.0")
                    and store.get("setup/v1/notes.txt") == blob("refs/remotes/origin/setup", "v1/notes.txt")
                    and store.get("cloud-dict/v2/rime-ice/a/aa.txt") == blob("refs/remotes/origin/cloud-dict", "v2/rime-ice/a/aa.txt"))
        idx = {k: i for i, k in enumerate(puts)}
        data_last = max(idx[k] for k in puts if k.endswith(("/aa.0", "/aa.1", "x.1.0", "aa.txt")))
        ok &= check("顺序：清单在数据之后", idx["setup/v1/setup.txt"] > data_last and idx["data-pack/v3/manifest.txt"] > data_last)
        ok &= check("顺序：latest.json 最后", puts[-1] in ("manifest/latest.json", "manifest/latest-beta.json") and "latest/HexNookIME-Setup.exe" in puts and idx["latest/HexNookIME-Setup.exe"] < idx["manifest/latest.json"])
        ok &= check("安装包进版本目录与 latest（正式/测试分开）",
                    store.get("0.9.32/HexNookIME-Setup.exe") == stable and store.get("latest/HexNookIME-Setup.exe") == stable
                    and store.get("latest-beta/HexNookIME-Setup.exe") == beta)
        m = json.loads(store["manifest/latest.json"])
        ok &= check("清单字段齐全", m["version"] == "0.9.32" and m["sha256"] == hashlib.sha256(stable).hexdigest() and set(m["branches"]) == {"setup", "data-pack", "cloud-dict"})

        # 2 二次运行零上传
        puts.clear(); r2 = run(repo, port, rj)
        ok &= check("二次运行：零上传（增量生效）", r2.returncode == 0 and puts == [], f"{puts} {r2.stdout}")

        # 2b 轻量检查：一致 → 0；分支前进 / 附件更新 → 10
        sh("git", "remote", "add", "origin", repo, cwd=repo)
        rc_same = run(repo, port, rj, ("--check-only",))
        ok &= check("轻量检查：一致时退出 0（无需镜像）", rc_same.returncode == 0, rc_same.stdout)
        rels2 = json.loads(json.dumps(rels)); rels2[1]["assets"][0]["updated_at"] = "t9"
        rj2 = os.path.join(tmp, "releases2.json"); json.dump(rels2, open(rj2, "w"))
        rc_asset = run(repo, port, rj2, ("--check-only",))
        ok &= check("轻量检查：附件更新后退出 10", rc_asset.returncode == 10, rc_asset.stdout)
        sh("git", "checkout", "-q", "data-pack", cwd=repo); open(os.path.join(repo, "v3", "files", "x.1.0"), "ab").write(b"!")
        sh("git", "commit", "-qam", "adv", cwd=repo)
        rc_branch = run(repo, port, rj, ("--check-only",))
        ok &= check("轻量检查：分支前进后退出 10", rc_branch.returncode == 10, rc_branch.stdout)
        sh("git", "reset", "-q", "--hard", "HEAD~1", cwd=repo)

        # 3 篡改一个文件只重传它
        store["setup/v1/files/aa.1"] = b"tampered"; puts.clear(); r3 = run(repo, port, rj)
        ok &= check("篡改后只重传被改的文件", r3.returncode == 0 and puts[:1] == ["setup/v1/files/aa.1"] and "setup/v1/files/aa.0" not in puts, str(puts))

        # 4 删除类：脚本绝不删除（OSS 上多出的对象保留）
        store["setup/v1/files/old.9"] = b"old"; run(repo, port, rj)
        ok &= check("绝不删除 OSS 上已有对象", "setup/v1/files/old.9" in store)

        # 5 存储端大小不符：报错且不更新清单
        store.clear(); puts.clear(); truncate_key[0] = "data-pack/v3/files/x.1.0"
        r5 = run(repo, port, rj); truncate_key[0] = None
        ok &= check("大小不符：报错退出且不上传清单与 latest",
                    r5.returncode == 1 and "回读校验失败" in r5.stdout and "data-pack/v3/manifest.txt" not in store and "latest/HexNookIME-Setup.exe" not in store and "manifest/latest.json" not in store, r5.stdout)

        # 6 审计：分支前进后 OSS 清单落后 → 变红
        run(repo, port, rj)
        sh("git", "update-ref", "refs/remotes/origin/setup", sh("git", "rev-parse", "refs/remotes/origin/data-pack", cwd=repo).stdout.strip(), cwd=repo)
        r6 = run(repo, port, rj, ("--audit-only",))
        ok &= check("审计：清单与分支不一致时报错", r6.returncode == 1 and "审计不一致" in r6.stdout, r6.stdout)

        # 7 密钥不出现在输出
        ok &= check("输出中不含密钥", SK not in (r1.stdout + r1.stderr + r5.stdout + r6.stdout))
    print("\n全部通过" if ok else "\n有失败项"); return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
