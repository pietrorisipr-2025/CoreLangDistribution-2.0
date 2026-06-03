from __future__ import annotations

import functools
import hashlib
import os
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class RangeRequestHandler(SimpleHTTPRequestHandler):
    """Static file server with byte-range, ETag and test fault-injection support."""

    fail_every: int = 0
    fail_status: int = 503
    truncate_every: int = 0
    delay_ms: int = 0
    request_counter: int = 0

    @classmethod
    def _next_request_id(cls) -> int:
        cls.request_counter += 1
        return cls.request_counter

    @staticmethod
    def _etag_for(path: str, size: int, mtime_ns: int) -> str:
        payload = f"{Path(path).name}:{size}:{mtime_ns}".encode("utf-8", "surrogatepass")
        return '"cld2-' + hashlib.sha256(payload).hexdigest()[:32] + '"'

    def end_headers(self):  # noqa: N802 - stdlib override
        self.send_header("X-CLD2-Test-Server", "alpha8")
        return super().end_headers()

    def send_head(self):  # noqa: N802 - stdlib override
        rid = self._next_request_id()
        if self.delay_ms > 0:
            time.sleep(self.delay_ms / 1000.0)
        if self.fail_every > 0 and rid % self.fail_every == 0:
            self.send_error(self.fail_status, "Injected alpha8 test failure")
            return None

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        ctype = self.guess_type(path)
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        fs = os.fstat(f.fileno())
        size = fs.st_size
        etag = self._etag_for(path, size, fs.st_mtime_ns)
        range_header = self.headers.get("Range")
        if_range = self.headers.get("If-Range")

        # If-Range mismatch: return full 200 per HTTP semantics.
        range_allowed = bool(range_header and range_header.startswith("bytes=") and (not if_range or if_range == etag))
        if range_allowed:
            spec = range_header.split("=", 1)[1]
            start_s, end_s = spec.split("-", 1)
            try:
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else size - 1
            except ValueError:
                self.send_error(416, "Invalid Range")
                f.close()
                return None
            if start < 0 or end < start or start >= size:
                self.send_error(416, "Requested Range Not Satisfiable")
                f.close()
                return None
            end = min(end, size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
            self.end_headers()
            f.seek(start)
            self.range = (start, end, length, rid)
            return f

        self.send_response(200)
        self.send_header("Content-type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
        self.end_headers()
        return f

    def copyfile(self, source, outputfile):  # noqa: N802 - stdlib override
        rng = getattr(self, "range", None)
        if not rng:
            return super().copyfile(source, outputfile)
        _start, _end, length, rid = rng
        if self.truncate_every > 0 and rid % self.truncate_every == 0:
            length = max(0, length // 2)
        remaining = length
        while remaining > 0:
            data = source.read(min(64 * 1024, remaining))
            if not data:
                break
            outputfile.write(data)
            remaining -= len(data)
        self.range = None


def serve(
    directory: str | Path,
    port: int = 9387,
    bind: str = "127.0.0.1",
    *,
    fail_every: int = 0,
    fail_status: int = 503,
    truncate_every: int = 0,
    delay_ms: int = 0,
) -> None:
    RangeRequestHandler.fail_every = int(fail_every or 0)
    RangeRequestHandler.fail_status = int(fail_status or 503)
    RangeRequestHandler.truncate_every = int(truncate_every or 0)
    RangeRequestHandler.delay_ms = int(delay_ms or 0)
    RangeRequestHandler.request_counter = 0
    handler = functools.partial(RangeRequestHandler, directory=str(directory))
    httpd = ThreadingHTTPServer((bind, port), handler)
    print(f"Serving {directory} at http://{bind}:{port}/")
    print(f"alpha8 fault injection: fail_every={fail_every} fail_status={fail_status} truncate_every={truncate_every} delay_ms={delay_ms}")
    httpd.serve_forever()
