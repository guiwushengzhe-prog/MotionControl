import json

from motioncontrol import connection_code as connection_code_module
from motioncontrol.connection_code import connection_code
from motioncontrol.windows_firewall import WindowsFirewall


def test_code_contains_current_port_identity_and_only_the_pairing_key():
    data = connection_code("0123456789ab", "电脑", [
        {"host": "192.168.1.2", "port": 9876, "kind": "lan"},
        {"host": "192.168.1.2", "port": 9876, "kind": "lan"},
    ], 9876)
    payload = json.loads(data["text"])
    assert payload["instance"] == "0123456789ab"
    assert payload["candidates"] == [{"host": "127.0.0.1", "port": 9876, "kind": "usb"},
                                    {"host": "192.168.1.2", "port": 9876, "kind": "lan"}]
    assert set(payload) == {"type", "version", "instance", "name", "candidates"}
    assert "<svg" in data["svg"]
    # 唯一的凭据是配对钥匙：扫码就是配对（见 pairing.py）。别的什么都不放进去。
    paired = json.loads(connection_code("0123456789ab", "电脑", [], 9876, "k" * 22)["text"])
    assert set(paired) == {"type", "version", "instance", "name", "candidates", "key"}
    assert paired["key"] == "k" * 22


def test_missing_qr_library_keeps_payload_instead_of_breaking_startup(monkeypatch):
    # 程序更新只换 app/，旧便携包的 Python 里可能没有 qrcode：不能因此起不来。
    monkeypatch.setattr(connection_code_module, "qrcode", None)
    data = connection_code("0123456789ab", "电脑", [], 9876)
    assert data["svg"] is None
    assert json.loads(data["text"])["candidates"] == [{"host": "127.0.0.1", "port": 9876, "kind": "usb"}]


def test_firewall_reuses_existing_rules_without_permission_prompt(tmp_path, monkeypatch):
    firewall = WindowsFirewall(tmp_path / "firewall.json")
    monkeypatch.setattr(firewall, "_windows", True)
    calls = []
    def run(mode):
        calls.append(mode)
        return {"state": "ready", "message": ""}
    monkeypatch.setattr(firewall, "_run", run)
    firewall._ensure(False)
    assert calls == ["Check"]
    assert firewall.status()["state"] == "ready"
    assert not firewall.preference.exists()


def test_declined_firewall_prompt_does_not_repeat_until_manual_retry(tmp_path, monkeypatch):
    firewall = WindowsFirewall(tmp_path / "firewall.json")
    monkeypatch.setattr(firewall, "_windows", True)
    calls = []
    def run(mode):
        calls.append(mode)
        return {"state": "missing", "message": "未授权"}
    monkeypatch.setattr(firewall, "_run", run)
    firewall._ensure(False)
    firewall._ensure(False)
    assert calls == ["Check", "Ensure", "Check"]
    firewall._ensure(True)
    assert calls == ["Check", "Ensure", "Check", "Check", "Ensure"]


def test_configuration_failure_is_visible(tmp_path, monkeypatch):
    firewall = WindowsFirewall(tmp_path / "firewall.json")
    monkeypatch.setattr(firewall, "_windows", True)
    def broken(mode):
        raise RuntimeError("检查失败")
    monkeypatch.setattr(firewall, "_run", broken)
    firewall._ensure(False)
    assert firewall.status()["state"] == "error"
