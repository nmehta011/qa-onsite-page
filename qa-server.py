#!/usr/bin/env python3
"""
qa-server.py — local development server for the Onsite Validation Hub.

Serves the sandbox exactly the way Vercel does (every unknown path rewritten to the app, so
/page1 and /page2 deep links behave identically to production) and exposes a tiny local API
that lets the dashboard launch a mobile-user-agent Chrome instance without dropping to a
terminal.

A browser cannot start a process on its own; that is what this helper is for. It only ever
runs the bundled launch-mobile-qa.sh with an allow-listed device key, never a caller-supplied
command, and binds to the loopback interface so nothing outside this machine can reach it.

    python3 qa-server.py              # http://localhost:8080/
    python3 qa-server.py 9000         # custom port

On Vercel this file is irrelevant: the static app is served directly and the launch button
falls back to showing the command, because no server-side process could open a window on the
tester's own machine anyway.
"""
import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import time
import urllib.parse

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
APP_DOCUMENT = "niraj-onsite.html"
LAUNCH_SCRIPT = os.path.join(REPO_ROOT, "launch-mobile-qa.py")
ALLOWED_DEVICES = ("ios", "android", "tablet")
MAX_REQUEST_BYTES = 4096
# The launcher stays alive for as long as the emulated browser window is open, so it is spawned
# detached and only watched briefly for an immediate failure (missing Playwright, bad profile).
LAUNCH_STARTUP_GRACE_SECONDS = 5.0


class ValidationHubHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=REPO_ROOT, **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    # --- Vercel-equivalent routing -------------------------------------------------
    def translate_path(self, path):
        # Delegate to the stdlib, which normalises the URL and strips ".." segments. Joining
        # REPO_ROOT with the raw request path by hand (as an earlier revision did) let
        # "/../../../../etc/passwd" escape the document root and serve arbitrary files.
        resolved = os.path.realpath(super().translate_path(path))
        document_root = os.path.realpath(REPO_ROOT)

        # Defence in depth: even with normalisation, only paths that genuinely resolve inside
        # the repository are ever served.
        if resolved != document_root and not resolved.startswith(document_root + os.sep):
            return os.path.join(REPO_ROOT, APP_DOCUMENT)

        if os.path.isfile(resolved):
            return resolved

        # Platform-injected assets are served by Vercel itself, ahead of any rewrite. Rewriting
        # them here would hand back HTML for a .js request, which the browser reports as a
        # syntax error and which would then show up as phantom noise in the QA error console.
        if urllib.parse.urlparse(path).path.startswith("/_vercel/"):
            return resolved

        return os.path.join(REPO_ROOT, APP_DOCUMENT)

    # --- host validation -----------------------------------------------------------
    def _has_local_host_header(self):
        """Guards against DNS rebinding, where an attacker domain resolves to 127.0.0.1 and a
        browser is coaxed into reaching this helper. Requests must address it by loopback name."""
        host_header = (self.headers.get("Host") or "").strip()
        if not host_header:
            return True  # HTTP/1.0 clients may omit Host entirely
        hostname = host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header
        return hostname.strip("[]") in ("localhost", "127.0.0.1", "::1")

    # --- request handling ----------------------------------------------------------
    def do_GET(self):
        if not self._has_local_host_header():
            self._send_json(403, {"ok": False, "error": "requests must address this helper as localhost"})
            return
        if urllib.parse.urlparse(self.path).path == "/api/health":
            self._send_json(200, {
                "ok": True,
                "helper": "qa-server",
                "devices": list(ALLOWED_DEVICES),
                "canLaunch": os.path.isfile(LAUNCH_SCRIPT),
            })
            return
        super().do_GET()

    def do_POST(self):
        if not self._has_local_host_header():
            self._send_json(403, {"ok": False, "error": "requests must address this helper as localhost"})
            return
        if urllib.parse.urlparse(self.path).path != "/api/launch-mobile":
            self._send_json(404, {"ok": False, "error": "unknown endpoint"})
            return

        # Only same-machine callers may trigger a launch. The browser always sends Origin on
        # a cross-origin POST, so rejecting foreign origins keeps another site from using this
        # endpoint just because the tester happens to have the helper running.
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).hostname not in ("localhost", "127.0.0.1"):
            self._send_json(403, {"ok": False, "error": "cross-origin launch requests are refused"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send_json(400, {"ok": False, "error": "missing or oversized request body"})
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"ok": False, "error": "request body must be JSON"})
            return

        device = payload.get("device")
        if device not in ALLOWED_DEVICES:
            self._send_json(400, {"ok": False, "error": "device must be one of %s" % (", ".join(ALLOWED_DEVICES))})
            return

        target_url = payload.get("url") or "http://localhost:%d/%s" % (self.server.server_address[1], APP_DOCUMENT)
        parsed_target = urllib.parse.urlparse(target_url)
        if parsed_target.scheme not in ("http", "https") or not parsed_target.netloc:
            self._send_json(400, {"ok": False, "error": "url must be an absolute http(s) URL"})
            return

        if not os.path.isfile(LAUNCH_SCRIPT):
            self._send_json(500, {"ok": False, "error": "launch-mobile-qa.py is missing from the repository"})
            return

        output_capture = tempfile.TemporaryFile(mode="w+")
        try:
            # Argument list, never a shell string: nothing from the request is interpreted.
            process = subprocess.Popen(
                # -u keeps the launcher's output unbuffered; it blocks after printing, so a
                # buffered stream would leave the diagnostics stranded in memory.
                [sys.executable, "-u", LAUNCH_SCRIPT, device, target_url],
                cwd=REPO_ROOT, stdout=output_capture, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as err:
            output_capture.close()
            self._send_json(500, {"ok": False, "error": "could not start launcher: %s" % err})
            return

        # The launcher blocks for the lifetime of the emulated window, so "still running after
        # the grace period" is the success signal; an early exit means it failed to start.
        deadline = time.monotonic() + LAUNCH_STARTUP_GRACE_SECONDS
        while time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.25)

        output_capture.seek(0)
        launcher_output = output_capture.read().strip()

        if process.poll() is not None and process.returncode != 0:
            output_capture.close()
            self._send_json(500, {
                "ok": False,
                "error": (launcher_output or "launcher exited with code %d" % process.returncode)[-600:],
            })
            return

        output_capture.close()
        self._send_json(200, {"ok": True, "device": device, "url": target_url,
                              "detail": launcher_output[:800]})

    def _send_json(self, status, body):
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


class ReusableServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = 8080
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print("error: port must be a number", file=sys.stderr)
            return 1

    # Loopback only: this helper can start a browser process, so it must never be reachable
    # from the network.
    with ReusableServer(("127.0.0.1", port), ValidationHubHandler) as httpd:
        print("Onsite Validation Hub  →  http://localhost:%d/" % port)
        print("  serving      : %s" % REPO_ROOT)
        print("  routing      : unknown paths rewritten to /%s (matches vercel.json)" % APP_DOCUMENT)
        print("  launch API   : POST /api/launch-mobile  (devices: %s)" % ", ".join(ALLOWED_DEVICES))
        print("  bound to     : 127.0.0.1 only")
        print("\nPress Ctrl+C to stop.\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
