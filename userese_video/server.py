"""Authenticated single-project HTTP workbench; no arbitrary filesystem routes."""

import hmac
import json
import mimetypes
import re
import secrets
import threading
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .media import build, history, preview
from .model import Conflict, identifier, read_json, relative_file

WEB = Path(__file__).parent / "web"
MAX_BODY = 2 * 1024 * 1024


class Workbench(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, project, token=None):
        self.project = project
        self.token = token or secrets.token_urlsafe(32)
        self.session = secrets.token_urlsafe(32)
        self.job = {"status": "idle"}
        self.job_lock = threading.Lock()
        super().__init__(address, Handler)

    def start_job(self, action, function):
        with self.job_lock:
            if self.job["status"] == "running":
                raise Conflict("已有任务正在处理，请等待完成")
            job = {"id": secrets.token_hex(8), "action": action, "status": "running"}
            self.job = job

        def worker():
            try:
                result = function()
                with self.job_lock:
                    self.job = {**job, "status": "complete", "result": result}
            except Exception as exc:
                with self.job_lock:
                    self.job = {**job, "status": "failed", "error": str(exc)}

        threading.Thread(target=worker, daemon=True).start()
        return job


class Handler(BaseHTTPRequestHandler):
    server_version = "UsereseVideo"

    def log_message(self, *_args):
        # Media filenames and session details are private project information.
        pass

    def send_headers(self, status, content_type, length, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self' blob:; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def json(self, data, status=200, extra=None):
        content = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_headers(status, "application/json; charset=utf-8", len(content), extra)
        if self.command != "HEAD":
            self.wfile.write(content)

    def file(self, path, download=False):
        if not path.is_file():
            raise ValueError("文件尚未生成")
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        extra = {"Accept-Ranges": "bytes"}
        value = self.headers_in.get("Range")
        if value:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
            if not match or not any(match.groups()) or not size:
                return self.json({"error": "无效的范围"}, 416, {"Content-Range": f"bytes */{size}"})
            left, right = match.groups()
            if left:
                start = int(left)
                end = min(int(right), end) if right else end
            else:
                start = max(0, size - int(right))
            if start > end or start >= size:
                return self.json({"error": "范围超出文件"}, 416, {"Content-Range": f"bytes */{size}"})
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
            status = 206
        if download:
            extra["Content-Disposition"] = f'attachment; filename="{path.name}"'
        self.send_headers(status, mimetypes.guess_type(path.name)[0] or "application/octet-stream", max(0, end-start+1), extra)
        if self.command == "HEAD":
            return
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def authenticated(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers_in.get("Cookie", ""))
            value = cookies.get("userese_session")
            return bool(value) and hmac.compare_digest(value.value, self.server.session)
        except Exception:
            return False

    def host_allowed(self):
        host = self.headers_in.get("Host", "")
        bound, port = self.server.server_address[:2]
        allowed = {f"{bound}:{port}"}
        if bound == "127.0.0.1":
            allowed.add(f"localhost:{port}")
        return host in allowed

    def body(self):
        length = int(self.headers_in.get("Content-Length", "0"))
        if not 0 < length <= MAX_BODY:
            raise ValueError("请求为空或超过 2 MiB")
        if self.headers_in.get_content_type() != "application/json":
            raise ValueError("需要 application/json")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("请求体必须为 JSON 对象")
        return data

    def dispatch(self, write=False):
        self.headers_in = self.headers
        try:
            if not self.host_allowed():
                return self.json({"error": "主机地址不匹配"}, 403)
            url = urlparse(self.path)
            path = unquote(url.path)
            if write:
                origin = self.headers_in.get("Origin")
                if origin and origin != "http://" + self.headers_in.get("Host", ""):
                    return self.json({"error": "跨站请求已拒绝"}, 403)
            if not write and path in ("/", "/index.html", "/app.js", "/style.css", "/favicon.svg"):
                return self.file(WEB / ("index.html" if path == "/" else path[1:]))
            if write and path == "/api/session":
                payload = self.body()
                token = payload.get("token", "")
                if not isinstance(token, str) or not hmac.compare_digest(token, self.server.token):
                    return self.json({"error": "访问口令不正确，请使用启动时显示的完整链接"}, 401)
                return self.json({"ok": True}, extra={"Set-Cookie": f"userese_session={self.server.session}; HttpOnly; SameSite=Strict; Path=/"})
            if not self.authenticated():
                return self.json({"error": "请使用启动时显示的完整链接打开工作台"}, 401)
            project = self.server.project
            if write:
                payload = self.body()
                if path == "/api/state":
                    return self.json(project.save(payload["state"], payload["expected_revision"]))
                if path == "/api/build":
                    if type(payload.get("draft", False)) is not bool:
                        raise ValueError("draft 必须为布尔值")
                    state = project.state()
                    if state["revision"] != payload["expected_revision"]:
                        raise Conflict("保存版本已更新，请刷新后重新生成")
                    draft = payload.get("draft", False)
                    project.selected(state, draft=draft)
                    return self.json(self.server.start_job("build", lambda: build(project, state, draft=draft)), 202)
                if path == "/api/preview":
                    take_id = payload["take"]
                    if take_id not in project.all_takes(project.state()):
                        raise ValueError("候选不存在")
                    if type(payload.get("context", False)) is not bool:
                        raise ValueError("context 必须为布尔值")
                    return self.json(self.server.start_job("preview", lambda: {"key": preview(project, take_id, payload.get("context", False))}), 202)
            else:
                if path == "/api/project":
                    state = project.state()
                    return self.json({"project": project.data, "catalog": project.catalog_with_proposals(state), "cues": project.cues, "state": state})
                if path == "/api/state":
                    return self.json(project.state())
                if path == "/api/history":
                    return self.json(history(project))
                if path == "/api/job":
                    with self.server.job_lock:
                        return self.json(self.server.job)
                if path == "/api/export":
                    return self.json(project.state(), extra={"Content-Disposition": 'attachment; filename="decisions.json"'})
                match = re.fullmatch(r"/media/preview/([a-f0-9]{24})", path)
                if match:
                    return self.file(relative_file(project.root, f"cache/{match[1]}/preview.mp4"))
                match = re.fullmatch(r"/media/source/([a-zA-Z0-9_-]+)", path)
                if match:
                    source = project.sources[match[1]]
                    return self.file(relative_file(project.root, source["file"]))
                match = re.fullmatch(r"/build/([a-zA-Z0-9_-]+)/([a-z0-9.]+)", path)
                if match:
                    build_id, name = identifier(match[1]), match[2]
                    if name not in ("video.mp4", "cover.jpg", "captions.srt", "decisions.json", "manifest.json", "evidence.json", "delivery.zip"):
                        raise ValueError("未公开的交付文件")
                    folder = project.root / "builds" / build_id
                    relative_file(project.root, f"builds/{build_id}/manifest.json")
                    if name == "delivery.zip":
                        with self.server.job_lock:
                            if not (folder / name).exists():
                                manifest = read_json(folder / "manifest.json")
                                with zipfile.ZipFile(folder / "delivery.tmp.zip", "w", zipfile.ZIP_DEFLATED) as archive:
                                    for filename in manifest["files"]:
                                        archive.write(relative_file(project.root, f"builds/{build_id}/{filename}"), filename)
                                (folder / "delivery.tmp.zip").replace(folder / name)
                    return self.file(relative_file(project.root, f"builds/{build_id}/{name}"), download=bool(parse_qs(url.query).get("download")))
            return self.json({"error": "没有这个地址"}, 404)
        except Conflict as exc:
            return self.json({"error": str(exc)}, 409)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return self.json({"error": str(exc)}, 400)

    def do_GET(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch(write=True)
