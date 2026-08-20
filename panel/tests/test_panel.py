import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock


MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "sbox_panel.py")
SPEC = importlib.util.spec_from_file_location("sbox_panel", MODULE_PATH)
panel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(panel)


BASE_CONFIG = {
    "log": {"level": "info"},
    "inbounds": [
        {
            "type": "vless", "tag": "vless-in", "listen": "::", "listen_port": 443,
            "users": [{"uuid": "original", "flow": "xtls-rprx-vision"}],
            "tls": {
                "enabled": True, "server_name": "itunes.apple.com",
                "reality": {
                    "enabled": True,
                    "handshake": {"server": "itunes.apple.com", "server_port": 443},
                    "private_key": "private", "short_id": ["1234567890abcdef"]
                }
            }
        },
        {
            "type": "hysteria2", "tag": "hy2-in", "listen": "::", "listen_port": 8443,
            "users": [{"password": "original"}],
            "tls": {"enabled": True, "certificate_path": "/tmp/cert", "key_path": "/tmp/key"}
        }
    ],
    "outbounds": [{"type": "direct", "tag": "direct"}]
}


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.temp.name, "sing-box.json")
        self.db_path = os.path.join(self.temp.name, "panel.db")
        with open(self.config_path, "w") as handle:
            json.dump(BASE_CONFIG, handle)
        self.store = panel.Store(self.db_path)
        self.manager = panel.SingBoxManager({
            "singbox_config": self.config_path,
            "singbox_binary": "/mock/sing-box",
            "server_address": "203.0.113.10",
            "hysteria_server_name": "example.com",
            "port_min": 20000, "port_max": 50000
        }, self.store)

    def tearDown(self):
        self.temp.cleanup()

    def create_record(self, protocol="reality", quota=0, expires=0):
        client_id = self.store.insert_client({
            "name": "Alice", "protocol": protocol, "port": 23456,
            "quota_bytes": quota, "expires_at": expires
        })
        inbound = {"type": "vless", "tag": "sbox-panel-%d" % client_id, "listen_port": 23456}
        self.store.update_generated(client_id, inbound, "vless://example")
        return client_id

    @mock.patch.object(panel.SingBoxManager, "_apply")
    def test_sync_keeps_unmanaged_and_active_nodes(self, apply_mock):
        client_id = self.create_record()
        self.manager.sync()
        updated = apply_mock.call_args.args[1]
        tags = [item["tag"] for item in updated["inbounds"]]
        self.assertEqual(tags, ["vless-in", "hy2-in", "sbox-panel-%d" % client_id])

    @mock.patch.object(panel.SingBoxManager, "_apply")
    def test_quota_exhausted_node_is_removed(self, apply_mock):
        client_id = self.create_record(quota=100)
        self.store.add_usage(client_id, 100, panel.now())
        self.manager.sync()
        updated = apply_mock.call_args.args[1]
        self.assertFalse(any(item["tag"].startswith("sbox-panel-") for item in updated["inbounds"]))

    def test_build_reality_uses_unique_credentials(self):
        outputs = iter([
            "PrivateKey: private-new\nPublicKey: public-new\n", "uuid-new", "abcdef0123456789"
        ])
        with mock.patch.object(self.manager, "_generate", side_effect=lambda *args: next(outputs)):
            inbound, link = self.manager.build_node(7, "测试用户", "reality", 24443)
        self.assertEqual(inbound["tag"], "sbox-panel-7")
        self.assertEqual(inbound["users"][0]["name"], "panel-user-7")
        self.assertEqual(inbound["tls"]["reality"]["private_key"], "private-new")
        self.assertIn("pbk=public-new", link)
        self.assertIn("uuid-new@203.0.113.10:24443", link)

    def test_build_hysteria_uses_template_certificate(self):
        with mock.patch.object(self.manager, "_generate", return_value="password-new"):
            inbound, link = self.manager.build_node(8, "Bob", "hysteria2", 24444)
        self.assertEqual(inbound["tls"]["certificate_path"], "/tmp/cert")
        self.assertEqual(inbound["users"][0]["name"], "panel-user-8")
        self.assertIn("sni=example.com", link)

    def test_password_hash_round_trip(self):
        encoded = panel.password_hash("correct horse battery staple")
        self.assertTrue(panel.password_matches("correct horse battery staple", encoded))
        self.assertFalse(panel.password_matches("wrong", encoded))

    def test_password_change_requires_current_password(self):
        config = {"admin_password_hash": panel.password_hash("old-password-123"), "session_secret": "secret"}
        store = object.__new__(panel.Panel)
        store.config = config
        store.config_path = os.path.join(self.temp.name, "panel-config.json")
        with mock.patch.object(panel, "atomic_json") as write_mock:
            with self.assertRaises(panel.PanelError):
                store.change_password("wrong-password", "new-password-123")
            store.change_password("old-password-123", "new-password-123")
        self.assertTrue(panel.password_matches("new-password-123", config["admin_password_hash"]))
        write_mock.assert_called_once()

    def test_panel_loads_config_path_for_password_persistence(self):
        config_path = os.path.join(self.temp.name, "panel-config.json")
        with open(config_path, "w") as handle:
            json.dump({"database": self.db_path, "singbox_config": self.config_path}, handle)
        with mock.patch.object(panel.Store, "__init__", return_value=None):
            instance = object.__new__(panel.Panel)
            panel.Panel.__init__(instance, config_path)
        self.assertEqual(instance.config_path, config_path)


if __name__ == "__main__":
    unittest.main()
