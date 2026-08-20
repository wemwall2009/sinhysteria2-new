#!/usr/bin/env python3
"""Small, dependency-free management panel for sing-box nodes."""

import argparse
import base64
import copy
import contextlib
import hashlib
import hmac
import http.cookies
import http.server
import json
import os
import random
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.parse


APP_VERSION = "1.0.0"
DEFAULT_CONFIG = "/etc/sbox-panel/config.json"
MANAGED_PREFIX = "sbox-panel-"
CHAIN_IN = "SBOX_PANEL_IN"
CHAIN_OUT = "SBOX_PANEL_OUT"


class PanelError(Exception):
    pass


def now():
    return int(time.time())


def run(command, check=True, timeout=20):
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, timeout=timeout
    )
    if check and result.returncode != 0:
        raise PanelError(result.stderr.strip() or "command failed: " + " ".join(command))
    return result


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
    return base64.urlsafe_b64encode(salt).decode() + ":" + base64.urlsafe_b64encode(digest).decode()


def password_matches(password, encoded):
    try:
        salt_text, expected_text = encoded.split(":", 1)
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(expected_text.encode())
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def atomic_json(path, value, mode=0o600):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".sbox-panel-", dir=directory)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


class Store(object):
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._init()

    @contextlib.contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _init(self):
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS clients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    port INTEGER NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    quota_bytes INTEGER NOT NULL DEFAULT 0,
                    used_bytes INTEGER NOT NULL DEFAULT 0,
                    expires_at INTEGER NOT NULL DEFAULT 0,
                    last_seen_at INTEGER NOT NULL DEFAULT 0,
                    inbound_json TEXT NOT NULL DEFAULT '{}',
                    link TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS clients_status ON clients(enabled, expires_at);
            """)

    def list_clients(self):
        with self.lock, self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM clients ORDER BY id DESC")]

    def get_client(self, client_id):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
            return dict(row) if row else None

    def insert_client(self, values):
        timestamp = now()
        with self.lock, self.connect() as db:
            cursor = db.execute(
                "INSERT INTO clients(name,protocol,port,enabled,quota_bytes,expires_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (values["name"], values["protocol"], values["port"], 1,
                 values["quota_bytes"], values["expires_at"], timestamp, timestamp)
            )
            return cursor.lastrowid

    def restore_client(self, value):
        columns = (
            "id", "name", "protocol", "port", "enabled", "quota_bytes", "used_bytes",
            "expires_at", "last_seen_at", "inbound_json", "link", "created_at", "updated_at"
        )
        placeholders = ",".join("?" for _ in columns)
        with self.lock, self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO clients(%s) VALUES(%s)" % (",".join(columns), placeholders),
                tuple(value[column] for column in columns)
            )

    def update_generated(self, client_id, inbound, link):
        with self.lock, self.connect() as db:
            db.execute(
                "UPDATE clients SET inbound_json=?,link=?,updated_at=? WHERE id=?",
                (json.dumps(inbound, ensure_ascii=False), link, now(), client_id)
            )

    def update_client(self, client_id, values):
        allowed = ("name", "enabled", "quota_bytes", "expires_at")
        fields = []
        params = []
        for key in allowed:
            if key in values:
                fields.append(key + "=?")
                params.append(values[key])
        if not fields:
            return
        fields.append("updated_at=?")
        params.extend([now(), client_id])
        with self.lock, self.connect() as db:
            db.execute("UPDATE clients SET " + ",".join(fields) + " WHERE id=?", params)

    def delete_client(self, client_id):
        with self.lock, self.connect() as db:
            db.execute("DELETE FROM clients WHERE id=?", (client_id,))

    def add_usage(self, client_id, byte_count, seen_at):
        with self.lock, self.connect() as db:
            db.execute(
                "UPDATE clients SET used_bytes=used_bytes+?,last_seen_at=CASE WHEN ?>0 THEN ? ELSE last_seen_at END "
                "WHERE id=?", (byte_count, byte_count, seen_at, client_id)
            )

    def reset_usage(self, client_id):
        with self.lock, self.connect() as db:
            db.execute("UPDATE clients SET used_bytes=0,updated_at=? WHERE id=?", (now(), client_id))


class SingBoxManager(object):
    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.lock = threading.RLock()

    @property
    def config_path(self):
        return self.config["singbox_config"]

    def read_config(self):
        with open(self.config_path, "r") as handle:
            return json.load(handle)

    def eligible(self, client):
        timestamp = now()
        return bool(client["enabled"]) and not (
            client["expires_at"] and client["expires_at"] <= timestamp
        ) and not (
            client["quota_bytes"] and client["used_bytes"] >= client["quota_bytes"]
        )

    def allocate_port(self, protocol):
        used = {int(item.get("listen_port", 0)) for item in self.read_config().get("inbounds", [])}
        socket_type = socket.SOCK_DGRAM if protocol == "hysteria2" else socket.SOCK_STREAM
        for _ in range(200):
            port = random.randint(self.config.get("port_min", 20000), self.config.get("port_max", 50000))
            if port in used:
                continue
            probe = socket.socket(socket.AF_INET, socket_type)
            try:
                probe.bind(("0.0.0.0", port))
                return port
            except OSError:
                pass
            finally:
                probe.close()
        raise PanelError("没有可用端口，请调整面板端口范围")

    def find_template(self, protocol):
        for inbound in self.read_config().get("inbounds", []):
            if inbound.get("tag", "").startswith(MANAGED_PREFIX):
                continue
            if protocol == "reality" and inbound.get("type") == "vless":
                if inbound.get("tls", {}).get("reality", {}).get("enabled"):
                    return inbound
            if protocol == "hysteria2" and inbound.get("type") == "hysteria2":
                return inbound
        raise PanelError("当前 sing-box 配置中没有可复用的 %s 入站" % protocol)

    def _generate(self, *arguments):
        result = run([self.config["singbox_binary"], "generate"] + list(arguments))
        return result.stdout.strip()

    def build_node(self, client_id, name, protocol, port):
        template = self.find_template(protocol)
        stable_name = "panel-user-%d" % client_id
        tag = MANAGED_PREFIX + str(client_id)
        address = self.config["server_address"]
        uri_address = "[" + address + "]" if ":" in address and not address.startswith("[") else address
        label = urllib.parse.quote(name, safe="")

        if protocol == "reality":
            keypair = self._generate("reality-keypair")
            private_match = re.search(r"PrivateKey:\s*(\S+)", keypair)
            public_match = re.search(r"PublicKey:\s*(\S+)", keypair)
            if not private_match or not public_match:
                raise PanelError("无法解析 sing-box Reality 密钥")
            uuid_value = self._generate("uuid")
            short_id = self._generate("rand", "--hex", "8")
            tls = copy.deepcopy(template["tls"])
            tls["reality"]["private_key"] = private_match.group(1)
            tls["reality"]["short_id"] = [short_id]
            server_name = tls.get("server_name") or tls["reality"]["handshake"]["server"]
            inbound = {
                "type": "vless", "tag": tag, "listen": "::", "listen_port": port,
                "users": [{"name": stable_name, "uuid": uuid_value}], "tls": tls
            }
            link = (
                "vless://%s@%s:%d?encryption=none&security=reality&sni=%s&fp=chrome&pbk=%s&sid=%s&type=tcp#%s"
                % (uuid_value, uri_address, port, urllib.parse.quote(server_name, safe=""),
                   urllib.parse.quote(public_match.group(1), safe="-_"), short_id, label)
            )
            return inbound, link

        password = self._generate("rand", "--hex", "16")
        tls = copy.deepcopy(template["tls"])
        inbound = {
            "type": "hysteria2", "tag": tag, "listen": "::", "listen_port": port,
            "users": [{"name": stable_name, "password": password}], "tls": tls
        }
        server_name = tls.get("server_name") or self.config.get("hysteria_server_name", "bing.com")
        link = "hysteria2://%s@%s:%d?insecure=1&sni=%s#%s" % (
            password, uri_address, port, urllib.parse.quote(server_name, safe=""), label
        )
        return inbound, link

    def sync(self):
        with self.lock:
            original = self.read_config()
            updated = copy.deepcopy(original)
            unmanaged = [
                inbound for inbound in updated.get("inbounds", [])
                if not inbound.get("tag", "").startswith(MANAGED_PREFIX)
            ]
            managed = []
            for client in self.store.list_clients():
                if self.eligible(client):
                    managed.append(json.loads(client["inbound_json"]))
            updated["inbounds"] = unmanaged + managed
            self._apply(original, updated)

    def _apply(self, original, updated):
        directory = os.path.dirname(self.config_path)
        fd, candidate = tempfile.mkstemp(prefix=".panel-candidate-", suffix=".json", dir=directory)
        os.close(fd)
        backup = self.config_path + ".panel-backup"
        try:
            atomic_json(candidate, updated)
            run([self.config["singbox_binary"], "check", "-c", candidate])
            shutil.copy2(self.config_path, backup)
            os.replace(candidate, self.config_path)
            result = run(["systemctl", "reload", "sing-box"], check=False)
            if result.returncode != 0:
                result = run(["systemctl", "restart", "sing-box"], check=False)
            if result.returncode != 0:
                shutil.copy2(backup, self.config_path)
                run(["systemctl", "restart", "sing-box"], check=False)
                raise PanelError("sing-box 重载失败，已恢复原配置: " + result.stderr.strip())
        finally:
            if os.path.exists(candidate):
                os.unlink(candidate)


class Accounting(object):
    def __init__(self, store, manager):
        self.store = store
        self.manager = manager
        self.speeds = {}
        self.error = ""
        self.last_tick = time.time()
        self.lock = threading.RLock()

    def commands(self):
        available = []
        for command in ("iptables", "ip6tables"):
            if shutil.which(command):
                available.append(command)
        if not available:
            raise PanelError("未安装 iptables/ip6tables，无法统计流量")
        return available

    def command(self, binary, arguments, check=True):
        return run([binary, "-w", "2"] + arguments, check=check)

    def setup(self):
        with self.lock:
            self.sample()
            for binary in self.commands():
                for chain in (CHAIN_IN, CHAIN_OUT):
                    self.command(binary, ["-N", chain], check=False)
                    self.command(binary, ["-F", chain])
                for parent, chain in (("INPUT", CHAIN_IN), ("OUTPUT", CHAIN_OUT)):
                    if self.command(binary, ["-C", parent, "-j", chain], check=False).returncode != 0:
                        self.command(binary, ["-I", parent, "1", "-j", chain])
                for client in self.store.list_clients():
                    if not self.manager.eligible(client):
                        continue
                    protocol = "udp" if client["protocol"] == "hysteria2" else "tcp"
                    comment = "sbox-panel-%d" % client["id"]
                    self.command(binary, ["-A", CHAIN_IN, "-p", protocol, "--dport", str(client["port"]),
                                          "-m", "comment", "--comment", comment, "-j", "RETURN"])
                    self.command(binary, ["-A", CHAIN_OUT, "-p", protocol, "--sport", str(client["port"]),
                                          "-m", "comment", "--comment", comment, "-j", "RETURN"])
            self.error = ""

    def _read_chain(self, binary, chain):
        result = self.command(binary, ["-vnx", "-L", chain, "-Z"], check=False)
        if result.returncode != 0:
            return {}
        totals = {}
        for line in result.stdout.splitlines():
            match = re.search(r"^\s*\d+\s+(\d+)\s+RETURN.*?/\*\s+sbox-panel-(\d+)\s+\*/", line)
            if match:
                client_id = int(match.group(2))
                totals[client_id] = totals.get(client_id, 0) + int(match.group(1))
        return totals

    def sample(self):
        with self.lock:
            try:
                totals = {}
                for binary in self.commands():
                    for chain in (CHAIN_IN, CHAIN_OUT):
                        for client_id, value in self._read_chain(binary, chain).items():
                            totals[client_id] = totals.get(client_id, 0) + value
                timestamp = now()
                elapsed = max(time.time() - self.last_tick, 1)
                self.last_tick = time.time()
                self.speeds = {client_id: int(value / elapsed) for client_id, value in totals.items()}
                for client_id, value in totals.items():
                    if value:
                        self.store.add_usage(client_id, value, timestamp)
                self.error = ""
                return totals
            except Exception as error:
                self.error = str(error)
                return {}


class Panel(object):
    def __init__(self, config_path):
        self.config_path = config_path
        with open(config_path, "r") as handle:
            self.config = json.load(handle)
        self.store = Store(self.config["database"])
        self.manager = SingBoxManager(self.config, self.store)
        self.accounting = Accounting(self.store, self.manager)
        self.stop_event = threading.Event()
        self.sync_lock = threading.RLock()

    def start(self):
        self.accounting.setup()
        worker = threading.Thread(target=self._worker, name="accounting", daemon=True)
        worker.start()

    def _worker(self):
        while not self.stop_event.wait(self.config.get("accounting_interval", 10)):
            with self.sync_lock:
                before = {item["id"]: self.manager.eligible(item) for item in self.store.list_clients()}
                self.accounting.sample()
                after = {item["id"]: self.manager.eligible(item) for item in self.store.list_clients()}
                if before != after:
                    try:
                        self.manager.sync()
                        self.accounting.setup()
                    except Exception as error:
                        self.accounting.error = str(error)

    def state(self):
        clients = []
        timestamp = now()
        for item in self.store.list_clients():
            expired = bool(item["expires_at"] and item["expires_at"] <= timestamp)
            exhausted = bool(item["quota_bytes"] and item["used_bytes"] >= item["quota_bytes"])
            item["online"] = bool(item["last_seen_at"] >= timestamp - 60)
            item["speed"] = self.accounting.speeds.get(item["id"], 0)
            item["status"] = "expired" if expired else "exhausted" if exhausted else "disabled" if not item["enabled"] else "enabled"
            item.pop("inbound_json", None)
            item.pop("link", None)
            clients.append(item)
        return {
            "version": APP_VERSION, "clients": clients,
            "accounting_error": self.accounting.error,
            "protocols": self.available_protocols()
        }

    def available_protocols(self):
        protocols = []
        for protocol in ("reality", "hysteria2"):
            try:
                self.manager.find_template(protocol)
                protocols.append(protocol)
            except PanelError:
                pass
        return protocols

    def change_password(self, current_password, new_password):
        if not password_matches(current_password, self.config["admin_password_hash"]):
            raise PanelError("当前密码错误")
        if len(new_password) < 10:
            raise PanelError("新密码至少需要 10 个字符")
        self.config["admin_password_hash"] = password_hash(new_password)
        # Rotating the signing secret invalidates every existing session.
        self.config["session_secret"] = secrets.token_urlsafe(48)
        atomic_json(self.config_path, self.config)

    def create_client(self, payload):
        with self.sync_lock:
            return self._create_client(payload)

    def _create_client(self, payload):
        name = str(payload.get("name", "")).strip()
        protocol = str(payload.get("protocol", "reality"))
        if not name or len(name) > 64:
            raise PanelError("客户名称不能为空且不能超过 64 个字符")
        if protocol not in ("reality", "hysteria2"):
            raise PanelError("不支持的协议")
        quota_gb = max(float(payload.get("quota_gb") or 0), 0)
        duration_days = max(int(payload.get("duration_days") or 0), 0)
        port = self.manager.allocate_port(protocol)
        client_id = self.store.insert_client({
            "name": name, "protocol": protocol, "port": port,
            "quota_bytes": int(quota_gb * 1024 ** 3),
            "expires_at": now() + duration_days * 86400 if duration_days else 0
        })
        try:
            inbound, link = self.manager.build_node(client_id, name, protocol, port)
            self.store.update_generated(client_id, inbound, link)
            self.manager.sync()
            self.accounting.setup()
            return self.store.get_client(client_id)
        except Exception:
            self.store.delete_client(client_id)
            try:
                self.manager.sync()
                self.accounting.setup()
            except Exception as rollback_error:
                self.accounting.error = "创建失败且回滚不完整: " + str(rollback_error)
            raise

    def update_client(self, client_id, payload):
        with self.sync_lock:
            return self._update_client(client_id, payload)

    def _update_client(self, client_id, payload):
        existing = self.store.get_client(client_id)
        if not existing:
            raise PanelError("客户不存在")
        values = {}
        if "name" in payload:
            name = str(payload["name"]).strip()
            if not name or len(name) > 64:
                raise PanelError("客户名称不能为空且不能超过 64 个字符")
            values["name"] = name
        if "enabled" in payload:
            values["enabled"] = 1 if payload["enabled"] else 0
        if "quota_gb" in payload:
            values["quota_bytes"] = int(max(float(payload["quota_gb"] or 0), 0) * 1024 ** 3)
        if "expires_at" in payload:
            values["expires_at"] = max(int(payload["expires_at"] or 0), 0)
        self.accounting.sample()
        previous = self.store.get_client(client_id)
        try:
            self.store.update_client(client_id, values)
            self.manager.sync()
            self.accounting.setup()
        except Exception:
            self.store.restore_client(previous)
            try:
                self.manager.sync()
                self.accounting.setup()
            except Exception as rollback_error:
                self.accounting.error = "更新失败且回滚不完整: " + str(rollback_error)
            raise
        return self.store.get_client(client_id)

    def delete_client(self, client_id):
        with self.sync_lock:
            return self._delete_client(client_id)

    def _delete_client(self, client_id):
        if not self.store.get_client(client_id):
            raise PanelError("客户不存在")
        self.accounting.sample()
        previous = self.store.get_client(client_id)
        try:
            self.store.delete_client(client_id)
            self.manager.sync()
            self.accounting.setup()
        except Exception:
            self.store.restore_client(previous)
            try:
                self.manager.sync()
                self.accounting.setup()
            except Exception as rollback_error:
                self.accounting.error = "删除失败且回滚不完整: " + str(rollback_error)
            raise

    def reset_usage(self, client_id):
        with self.sync_lock:
            return self._reset_usage(client_id)

    def _reset_usage(self, client_id):
        if not self.store.get_client(client_id):
            raise PanelError("客户不存在")
        self.accounting.sample()
        previous = self.store.get_client(client_id)
        try:
            self.store.reset_usage(client_id)
            self.manager.sync()
            self.accounting.setup()
        except Exception:
            self.store.restore_client(previous)
            try:
                self.manager.sync()
                self.accounting.setup()
            except Exception as rollback_error:
                self.accounting.error = "重置失败且回滚不完整: " + str(rollback_error)
            raise


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "SBoxPanel/" + APP_VERSION

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    @property
    def panel(self):
        return self.server.panel

    def json_response(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:")

    def body_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024 * 1024:
            raise PanelError("请求过大")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise PanelError("JSON 格式错误")

    def cookie(self, name):
        jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return jar[name].value if name in jar else ""

    def session(self):
        token = self.cookie("sbox_session")
        if not token or "." not in token:
            return None
        payload, signature = token.rsplit(".", 1)
        expected = hmac.new(self.panel.config["session_secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            expires, nonce = payload.split(":", 1)
            if int(expires) < now():
                return None
            return {"token": token, "nonce": nonce}
        except ValueError:
            return None

    def csrf_valid(self, session):
        supplied = self.headers.get("X-CSRF-Token", "")
        expected = hmac.new(self.panel.config["session_secret"].encode(), (session["token"] + ":csrf").encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(supplied, expected)

    def require_auth(self, mutate=False):
        session = self.session()
        if not session:
            self.json_response(401, {"error": "请先登录"})
            return None
        if mutate and not self.csrf_valid(session):
            self.json_response(403, {"error": "CSRF 校验失败，请刷新页面"})
            return None
        return session

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            return self.static_file("index.html", "text/html; charset=utf-8")
        if path == "/app.js":
            return self.static_file("app.js", "application/javascript; charset=utf-8")
        if path == "/api/state":
            session = self.require_auth()
            if not session:
                return
            state = self.panel.state()
            state["csrf"] = hmac.new(
                self.panel.config["session_secret"].encode(),
                (session["token"] + ":csrf").encode(), hashlib.sha256
            ).hexdigest()
            return self.json_response(200, state)
        match = re.match(r"^/api/clients/(\d+)/(link|qr)$", path)
        if match:
            if not self.require_auth():
                return
            client = self.panel.store.get_client(int(match.group(1)))
            if not client:
                return self.json_response(404, {"error": "客户不存在"})
            if match.group(2) == "link":
                return self.json_response(200, {"link": client["link"]})
            return self.qr_response(client["link"])
        self.json_response(404, {"error": "Not found"})

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/api/login":
                payload = self.body_json()
                if payload.get("username") != self.panel.config["admin_username"] or not password_matches(
                    str(payload.get("password", "")), self.panel.config["admin_password_hash"]
                ):
                    return self.json_response(401, {"error": "用户名或密码错误"})
                session_payload = "%d:%s" % (now() + 86400, secrets.token_urlsafe(24))
                signature = hmac.new(self.panel.config["session_secret"].encode(), session_payload.encode(), hashlib.sha256).hexdigest()
                token = session_payload + "." + signature
                self.send_response(200)
                self.send_header("Set-Cookie", "sbox_session=%s; Path=/; Max-Age=86400; HttpOnly; Secure; SameSite=Strict" % token)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.security_headers()
                body = b'{"ok":true}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if path == "/api/logout":
                if not self.require_auth(mutate=True):
                    return
                self.send_response(200)
                self.send_header("Set-Cookie", "sbox_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict")
                self.security_headers()
                self.end_headers()
                return
            if path == "/api/change-password":
                if not self.require_auth(mutate=True):
                    return
                payload = self.body_json()
                self.panel.change_password(
                    str(payload.get("current_password", "")),
                    str(payload.get("new_password", "")),
                )
                self.send_response(200)
                self.send_header("Set-Cookie", "sbox_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.security_headers()
                body = b'{"ok":true}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if not self.require_auth(mutate=True):
                return
            if path == "/api/clients":
                result = self.panel.create_client(self.body_json())
                return self.json_response(201, {"id": result["id"]})
            match = re.match(r"^/api/clients/(\d+)/(reset)$", path)
            if match:
                self.panel.reset_usage(int(match.group(1)))
                return self.json_response(200, {"ok": True})
            return self.json_response(404, {"error": "Not found"})
        except PanelError as error:
            self.json_response(400, {"error": str(error)})
        except Exception as error:
            print("request error:", repr(error))
            self.json_response(500, {"error": "服务器内部错误"})

    def do_PUT(self):
        try:
            if not self.require_auth(mutate=True):
                return
            match = re.match(r"^/api/clients/(\d+)$", urllib.parse.urlsplit(self.path).path)
            if not match:
                return self.json_response(404, {"error": "Not found"})
            self.panel.update_client(int(match.group(1)), self.body_json())
            self.json_response(200, {"ok": True})
        except PanelError as error:
            self.json_response(400, {"error": str(error)})
        except Exception as error:
            print("request error:", repr(error))
            self.json_response(500, {"error": "服务器内部错误"})

    def do_DELETE(self):
        try:
            if not self.require_auth(mutate=True):
                return
            match = re.match(r"^/api/clients/(\d+)$", urllib.parse.urlsplit(self.path).path)
            if not match:
                return self.json_response(404, {"error": "Not found"})
            self.panel.delete_client(int(match.group(1)))
            self.json_response(200, {"ok": True})
        except PanelError as error:
            self.json_response(400, {"error": str(error)})
        except Exception as error:
            print("request error:", repr(error))
            self.json_response(500, {"error": "服务器内部错误"})

    def static_file(self, name, content_type):
        path = os.path.join(os.path.dirname(__file__), "static", name)
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            return self.json_response(404, {"error": "Not found"})
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    def qr_response(self, value):
        binary = subprocess.run(
            ["qrencode", "-t", "PNG", "-s", "7", "-m", "2", "-o", "-", value],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10
        )
        if binary.returncode != 0:
            raise PanelError("二维码生成失败")
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(binary.stdout)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(binary.stdout)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, panel):
        self.panel = panel
        super().__init__(address, Handler)


def init_config(arguments):
    if os.path.exists(arguments.config) and not arguments.force:
        raise PanelError("配置已存在；如需覆盖请使用 --force")
    value = {
        "admin_username": arguments.username,
        "admin_password_hash": password_hash(arguments.password),
        "session_secret": secrets.token_urlsafe(48),
        "listen": arguments.listen,
        "port": arguments.port,
        "cert_file": arguments.cert_file,
        "key_file": arguments.key_file,
        "database": arguments.database,
        "singbox_config": arguments.singbox_config,
        "singbox_binary": arguments.singbox_binary,
        "server_address": arguments.server_address,
        "hysteria_server_name": arguments.hysteria_server_name,
        "port_min": arguments.port_min,
        "port_max": arguments.port_max,
        "accounting_interval": 10
    }
    atomic_json(arguments.config, value)
    Store(arguments.database)
    print("Panel configuration initialized: " + arguments.config)


def serve(config_path):
    panel = Panel(config_path)
    panel.start()
    server = Server((panel.config.get("listen", "0.0.0.0"), int(panel.config.get("port", 2095))), panel)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(panel.config["cert_file"], panel.config["key_file"])
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print("sbox-panel %s listening on https://%s:%s" % (APP_VERSION, *server.server_address))
    try:
        server.serve_forever()
    finally:
        panel.stop_event.set()
        panel.accounting.sample()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="sing-box node management panel")
    subparsers = parser.add_subparsers(dest="command")
    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--config", default=DEFAULT_CONFIG)
    init_parser.add_argument("--username", default="admin")
    init_parser.add_argument("--password", required=True)
    init_parser.add_argument("--listen", default="0.0.0.0")
    init_parser.add_argument("--port", type=int, default=2095)
    init_parser.add_argument("--cert-file", required=True)
    init_parser.add_argument("--key-file", required=True)
    init_parser.add_argument("--database", default="/var/lib/sbox-panel/panel.db")
    init_parser.add_argument("--singbox-config", default="/root/sbox/sbconfig_server.json")
    init_parser.add_argument("--singbox-binary", default="/root/sbox/sing-box")
    init_parser.add_argument("--server-address", required=True)
    init_parser.add_argument("--hysteria-server-name", default="bing.com")
    init_parser.add_argument("--port-min", type=int, default=20000)
    init_parser.add_argument("--port-max", type=int, default=50000)
    init_parser.add_argument("--force", action="store_true")
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--config", default=DEFAULT_CONFIG)
    arguments = parser.parse_args()
    if arguments.command == "init":
        init_config(arguments)
    elif arguments.command == "serve":
        serve(arguments.config)
    else:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
