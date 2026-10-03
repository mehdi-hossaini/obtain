"""VM-only HTTPS GitHub fixture. Never imported by the production CLI."""

import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ssl
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(sys.argv[1])
STATE = Path("/var/lib/obtain-evals")
PAYLOADS = {
    "archive",
    "zip",
    "binary",
    "traversal",
    "link",
    "script",
    "foreign-payload",
    "graphics-helper",
    "graphics-precedence",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        state = json.loads((STATE / "control.json").read_text())
        path = urlsplit(self.path).path
        parts = path.strip("/").split("/")
        repo = parts[2] if len(parts) > 2 and parts[0] == "repos" else ""
        mode = state.get("modes", {}).get(repo)
        if state.get("offline") or mode == "offline":
            return self.reply(503, {"message": "fixture offline"})
        if mode == "rate-limit":
            return self.reply(
                429, {"message": "fixture rate limit"}, {"Retry-After": "60"}
            )
        if mode == "missing" or repo == "missing":
            return self.reply(404, {"message": "not found"})
        if mode == "redirect":
            return self.reply(
                302, {}, {"Location": "https://elsewhere.invalid/metadata"}
            )
        if mode == "delay":
            time.sleep(5)
        if mode == "null":
            return self.reply(200, None)
        if mode == "scalar":
            return self.reply(200, 17)
        if mode == "list":
            return self.reply(200, [])
        if mode == "asset-null" and parts[-1] == "assets":
            return self.reply(200, [None])
        if mode == "asset-name-number" and parts[-1] == "assets":
            return self.reply(200, [{"name": 17}])
        if mode == "malformed":
            return self.reply(200, b"{invalid", raw=True)
        version = state.get("versions", {}).get(repo, 1)
        if path == "/health":
            return self.reply(200, {"ok": True})
        if (
            len(parts) >= 6
            and parts[0] == "eval"
            and parts[2:4] == ["releases", "download"]
        ):
            version = int(parts[4].removeprefix("v"))
            return self.reply(200, self.asset_bytes(parts[1], version), raw=True)
        if (
            len(parts) == 4
            and parts[:2] == ["NixOS", "nixpkgs"]
            and parts[2] == "archive"
        ):
            return self.file(ROOT / "nixpkgs.tar.gz")
        if path.startswith("/repos/NixOS/nixpkgs/tarball/"):
            return self.file(ROOT / "nixpkgs.tar.gz")
        if path.startswith("/repos/eval/") and len(parts) > 3:
            if parts[3] == "releases":
                if repo not in PAYLOADS and repo not in (
                    "image",
                    "base",
                    "ambiguous-image",
                    "foreign",
                    "unlabelled",
                    "digest",
                    "prerelease",
                    "unsupported",
                    "invalid-image",
                    "truncated-image",
                    "missing-apprun",
                    "nonexec-apprun",
                    "directory-apprun",
                ):
                    return self.reply(404, {"message": "no releases"})
                if parts[-1] == "assets":
                    return self.reply(200, self.assets(repo, int(parts[-2]), mode))

                def release(v, pre=False):
                    return {
                        "id": v,
                        "tag_name": f"v{v}",
                        "draft": False,
                        "prerelease": pre,
                        "published_at": f"2026-01-0{v}T00:00:00Z",
                    }

                if parts[-1] == "latest":
                    return self.reply(200, release(version))
                return self.reply(200, [release(2, True), release(1)])
        return self.reply(404, {"message": "unknown fixture route: " + self.path})

    @staticmethod
    def asset_bytes(repo, version):
        if repo in PAYLOADS:
            filename = {
                "archive": f"archive-{version}.tar.gz",
                "zip": f"zip-{version}.zip",
                "binary": f"binary-{version}",
                "foreign-payload": "foreign.tar.gz",
                "graphics-helper": f"graphics-helper-{version}.tar.gz",
                "graphics-precedence": f"graphics-precedence-{version}.tar.gz",
            }.get(repo, f"{repo}.tar.gz")
            return (ROOT / "payloads" / filename).read_bytes()
        if repo == "invalid-image" or (repo == "base" and version == 3):
            return b"This is not an AppImage.\n"
        if repo in ("missing-apprun", "nonexec-apprun", "directory-apprun"):
            return (ROOT / f"{repo}.AppImage").read_bytes()
        data = (ROOT / f"image-{version}.AppImage").read_bytes()
        return data[:128] if repo == "truncated-image" else data

    def assets(self, repo, version, mode):
        data = self.asset_bytes(repo, version)
        names = {
            "ambiguous-image": ["one-x86_64.AppImage", "two-x86_64.AppImage"],
            "foreign": ["image-aarch64.AppImage"],
            "unlabelled": ["Image.AppImage"],
        }.get(repo, ["image-x86_64.AppImage"])
        if repo == "unsupported":
            names = [
                "app.deb",
                "app.rpm",
                "app.tar.gz",
                "app.zip",
                "app.exe",
                "app.dmg",
                "app",
                "Dockerfile",
                "source.tar.gz",
            ]
        if repo in PAYLOADS:
            suffix = (
                "" if repo == "binary" else (".zip" if repo == "zip" else ".tar.gz")
            )
            names = [f"app-linux-x86_64{suffix}"]
        result = [
            {
                "id": version * 10 + i,
                "name": name,
                "state": "uploaded",
                "browser_download_url": f"https://github.com/eval/{repo}/releases/download/v{version}/{name}",
                "updated_at": "2026-01-01T00:00:00Z",
                "size": len(data),
                "digest": "sha256:"
                + ("0" * 64 if repo == "digest" else hashlib.sha256(data).hexdigest()),
            }
            for i, name in enumerate(names)
        ]
        if mode == "asset-url":
            result[0]["browser_download_url"] = (
                "https://elsewhere.invalid/evil.AppImage"
            )
        if mode == "asset-digest":
            result[0]["digest"] = {"bad": "digest"}
        return result

    def file(self, path):
        return self.reply(
            200, path.read_bytes(), {"Content-Type": "application/gzip"}, raw=True
        )

    def reply(self, status, data, headers=None, raw=False):
        body = data if raw else json.dumps(data).encode()
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        if status == 200 and self.headers.get("If-None-Match") == etag:
            status = 304
            body = b""
        with (STATE / "requests.jsonl").open("a") as f:
            f.write(
                json.dumps(
                    {
                        "path": self.path,
                        "status": status,
                        "authorization": bool(self.headers.get("Authorization")),
                    }
                )
                + "\n"
            )
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError):
            # Expected when the SIGINT/SIGKILL scenarios disconnect a client.
            pass


context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(sys.argv[2], sys.argv[3])
server = ThreadingHTTPServer(("0.0.0.0", 443), Handler)
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
