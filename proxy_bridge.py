#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Локальный HTTP→HTTPS прокси-мост.

Зачем нужен
-----------
mpv/ffmpeg (и libavformat) понимают только plain-HTTP или SOCKS-прокси:

    --http-proxy=http://host:port
    --http-proxy=socks5://host:port

Прокси вида `https://92.242.41.77:443` — это HTTPS-прокси (TLS до самого
прокси). Его понимают requests/curl, но не mpv/ffmpeg. Если передать его
в mpv как `https://...`, mpv молча игнорирует прокси и идёт напрямую —
поэтому geo-blocked каналы показывают заглушку.

Мост поднимает локальный plain-HTTP прокси на 127.0.0.1, который принимает
CONNECT от mpv/ffmpeg, устанавливает TLS-соединение с upstream HTTPS-прокси
и пробрасывает трафик через него.

Использование
-------------
GUI поднимает мост автоматически, когда stream proxy начинается с https://.
Автономный запуск (например, для внешнего mpv/ffmpeg):

    python3 proxy_bridge.py --upstream https://92.242.41.77:443 --port 8888

Или в коде:

    from proxy_bridge import LocalProxyBridge
    bridge = LocalProxyBridge("https://92.242.41.77:443")
    print(bridge.local_url)   # http://127.0.0.1:PORT
    ...
    bridge.stop()
"""

from __future__ import annotations

import argparse
import base64
import select
import socket
import socketserver
import ssl
import threading
import time
import urllib.parse
from typing import Optional


def _read_headers(sock: socket.socket, limit: int = 65536) -> bytes:
    """Читает заголовки до \\r\\n\\r\\n (включая их)."""
    data = b""
    while b"\r\n\r\n" not in data:
        try:
            chunk = sock.recv(4096)
        except OSError:
            return b""
        if not chunk:
            return b""
        data += chunk
        if len(data) > limit:
            return b""
    return data


class _BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        bridge: "LocalProxyBridge" = self.server.bridge  # type: ignore[attr-defined]
        client = self.request
        try:
            client.settimeout(30)
            head = _read_headers(client)
            if not head:
                return
            first = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
            parts = first.split(" ")
            if len(parts) < 3:
                return
            method, target, _version = parts[0], parts[1], parts[2]

            upstream = bridge.connect_upstream()
            if upstream is None:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return

            try:
                if method.upper() == "CONNECT":
                    req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
                    if bridge.auth_header:
                        req += f"Proxy-Authorization: {bridge.auth_header}\r\n"
                    req += "Proxy-Connection: keep-alive\r\n\r\n"
                    upstream.sendall(req.encode("latin1"))
                    resp = _read_headers(upstream)
                    if not resp:
                        client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                        return
                    status_line = resp.split(b"\r\n", 1)[0]
                    if b" 200 " not in status_line and not status_line.endswith(b" 200"):
                        client.sendall(resp)
                        return
                    client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    self._relay(client, upstream)
                else:
                    # Обычный HTTP-запрос через прокси (для полноты).
                    upstream.sendall(head)
                    self._relay(client, upstream)
            finally:
                try:
                    upstream.close()
                except OSError:
                    pass
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    @staticmethod
    def _relay(a: socket.socket, b: socket.socket):
        sockets = [a, b]
        while True:
            try:
                readable, _, _ = select.select(sockets, [], [], 60)
            except OSError:
                return
            if not readable:
                return
            for src in readable:
                try:
                    data = src.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                dst = b if src is a else a
                try:
                    dst.sendall(data)
                except OSError:
                    return


class _BridgeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class LocalProxyBridge:
    """
    Локальный plain-HTTP прокси, который ходит через upstream HTTPS-прокси.

    local_url — то, что нужно отдать mpv/ffmpeg в --http-proxy.
    """

    def __init__(self, upstream_url: str, host: str = "127.0.0.1",
                 port: int = 0):
        p = urllib.parse.urlparse((upstream_url or "").strip())
        if p.scheme not in ("http", "https") or not p.hostname:
            raise ValueError(
                "upstream должен быть http://host:port или https://host:port"
            )
        self.upstream_url = upstream_url
        self.upstream_scheme = p.scheme
        self.upstream_host = p.hostname
        self.upstream_port = p.port or (443 if p.scheme == "https" else 80)
        self.username = urllib.parse.unquote(p.username or "")
        self.password = urllib.parse.unquote(p.password or "")
        self.auth_header = ""
        if self.username:
            token = base64.b64encode(
                f"{self.username}:{self.password}".encode()
            ).decode()
            self.auth_header = "Basic " + token

        self._server: Optional[_BridgeServer] = None
        self._thread: Optional[threading.Thread] = None
        self.local_url = ""

        server = _BridgeServer((host, port), _BridgeHandler)
        server.bridge = self  # type: ignore[attr-defined]
        self._server = server
        self.local_url = f"http://{host}:{server.server_address[1]}"
        self._thread = threading.Thread(
            target=server.serve_forever,
            name="limehd-proxy-bridge",
            daemon=True,
        )
        self._thread.start()

    def connect_upstream(self) -> Optional[socket.socket]:
        try:
            raw = socket.create_connection(
                (self.upstream_host, self.upstream_port), timeout=20
            )
            raw.settimeout(30)
            if self.upstream_scheme == "https":
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                return ctx.wrap_socket(
                    raw, server_hostname=self.upstream_host
                )
            return raw
        except OSError:
            return None

    def stop(self):
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Локальный HTTP-прокси для upstream HTTPS-прокси"
    )
    ap.add_argument(
        "--upstream", default="https://92.242.41.77:443",
        help="upstream-прокси (по умолчанию https://92.242.41.77:443)",
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8888)
    args = ap.parse_args()

    bridge = LocalProxyBridge(args.upstream, args.host, args.port)
    print(f"Локальный прокси: {bridge.local_url}", flush=True)
    print(
        f"mpv/ffmpeg: --http-proxy={bridge.local_url}", flush=True
    )
    print("Ctrl+C — остановить.", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
