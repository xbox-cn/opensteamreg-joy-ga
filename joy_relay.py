"""Minimal authenticated CONNECT relay for GA runners.

Reads GH_JOY_RESIDENTIAL (http://user:pass@host:port), listens on
127.0.0.1:<port> (argv[1], default 18080) and forwards every CONNECT
through the authenticated upstream gate. Runners are overseas so the
gate is reachable directly — no SSH forward hop needed.

Usage: python joy_relay.py [listen_port]
"""
import base64
import os
import socket
import sys
import threading
import urllib.parse

LISTEN_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
LISTEN = ("127.0.0.1", LISTEN_PORT)

URL = os.environ.get("JOY_PROXY_URL", "").strip()
if not URL:
    print("JOY_PROXY_URL not set", flush=True)
    sys.exit(1)
u = urllib.parse.urlparse(URL)
EP_HOST, EP_PORT = u.hostname, u.port or 80
AUTH = base64.b64encode(f"{u.username}:{u.password}".encode()).decode()


def _read_headers(sock, limit=65536):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            return None, None
        data += chunk
        if len(data) > limit:
            return None, None
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def _send_connect(sock, host, port, auth=None):
    req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
    if auth:
        req += f"Proxy-Authorization: Basic {auth}\r\n"
    req += "\r\n"
    sock.sendall(req.encode())
    head, rest = _read_headers(sock)
    if head is None:
        return None, None
    return head.split(b"\r\n", 1)[0], rest


def _pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client):
    up = None
    try:
        client.settimeout(30)
        head, _ = _read_headers(client)
        if head is None:
            return
        first = head.split(b"\r\n", 1)[0].split()
        if len(first) < 3 or first[0] != b"CONNECT":
            client.sendall(b"HTTP/1.1 501 Only CONNECT supported\r\n\r\n")
            return
        target = first[1].decode("ascii", "replace")
        host, port = target.rsplit(":", 1)

        up = socket.create_connection((EP_HOST, EP_PORT), timeout=30)
        up.settimeout(30)
        status, _ = _send_connect(up, host, int(port), auth=AUTH)
        if status is None or b" 200 " not in status:
            client.sendall(b"HTTP/1.1 502 gate-refused\r\n\r\n")
            print("[relay] gate refused:", (status or b"<none>").decode("latin1"), flush=True)
            return
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        threading.Thread(target=_pipe, args=(up, client), daemon=True).start()
        _pipe(client, up)
    except Exception as exc:
        print("[relay] error:", type(exc).__name__, exc, flush=True)
    finally:
        for s in (client, up):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


if __name__ == "__main__":
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN)
    srv.listen(64)
    print(f"[relay] listening on {LISTEN[0]}:{LISTEN_PORT} -> {EP_HOST}:{EP_PORT}", flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()
