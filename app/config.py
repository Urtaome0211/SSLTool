"""会话配置与凭据存取。

会话配置保存于 ~/.ssltool/sessions.json。
密码 / 私钥口令优先保存到系统钥匙串（keyring），
无可用 keyring 后端时回退到 ~/.ssltool/secrets.json（仅 base64 混淆，不推荐）。
"""
import base64
import json
import uuid
from pathlib import Path

try:
    import keyring
    _KEYRING_OK = True
except Exception:
    _KEYRING_OK = False

DATA_DIR = Path.home() / ".ssltool"
SESSIONS_FILE = DATA_DIR / "sessions.json"
SECRETS_FILE = DATA_DIR / "secrets.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
SERVICE = "SSLTool"


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_sessions() -> list:
    ensure_data_dir()
    return _load_json(SESSIONS_FILE, [])


def save_sessions(sessions: list) -> None:
    ensure_data_dir()
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False, indent=2)


def new_session_id() -> str:
    return uuid.uuid4().hex


# ---------------- 应用设置（界面偏好等） ----------------

def load_settings() -> dict:
    ensure_data_dir()
    data = _load_json(SETTINGS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("dark", False)
    return data


def save_settings(settings: dict) -> None:
    ensure_data_dir()
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def make_session(**kwargs) -> dict:
    """返回带默认值的会话字典。"""
    s = {
        "id": new_session_id(),
        "name": "",
        "protocol": "ssh",          # ssh | ssl
        "host": "",
        "port": 22,
        "username": "",
        "auth": "password",         # password | key
        "save_password": False,
        "save_passphrase": False,
        "key_path": "",
        "ssl_verify": True,
        "client_cert": "",
        "client_key": "",
        "forwards": [],             # SSH 端口转发规则（OpenSSH 风格 L:/R:/D:）
    }
    s.update(kwargs)
    return s


# ---------------- 凭据存取 ----------------

def _fallback_store() -> dict:
    return _load_json(SECRETS_FILE, {})


def _save_fallback_store(data: dict) -> None:
    ensure_data_dir()
    with open(SECRETS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_secret(key: str):
    """读取凭据，keyring 优先，回退到混淆文件。"""
    if _KEYRING_OK:
        try:
            return keyring.get_password(SERVICE, key)
        except Exception:
            pass
    data = _fallback_store()
    val = data.get(key)
    if val is None:
        return None
    try:
        return base64.b64decode(val).decode("utf-8")
    except Exception:
        return None


def set_secret(key: str, value: str) -> None:
    if _KEYRING_OK:
        try:
            keyring.set_password(SERVICE, key, value)
            return
        except Exception:
            pass
    data = _fallback_store()
    data[key] = base64.b64encode(value.encode("utf-8")).decode("ascii")
    _save_fallback_store(data)


def delete_secret(key: str) -> None:
    if _KEYRING_OK:
        try:
            keyring.delete_password(SERVICE, key)
        except Exception:
            pass
    data = _fallback_store()
    data.pop(key, None)
    _save_fallback_store(data)
