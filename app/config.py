"""会话配置与凭据存取。

会话配置保存于 ~/.ssltool/sessions.json。
密码 / 私钥口令优先保存到系统钥匙串（keyring），
无可用 keyring 后端时回退到 ~/.ssltool/secrets.json。
回退存储默认用 Windows DPAPI（CryptProtectData，绑定当前用户账户）整体加密；
DPAPI 不可用（非 Windows 等）时退化为 base64 混淆格式（不推荐）。
"""
import base64
import json
import sys
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


# ---------------- Windows DPAPI（回退存储的静态加密） ----------------

def _setup_dpapi():
    """返回 (protect, unprotect) 两个可调用，或 (None, None) 表示不可用。"""
    if sys.platform != "win32":
        return None, None
    try:
        import ctypes
        from ctypes import wintypes

        class _DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.c_void_p)]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        def _blob_to_bytes(blob):
            try:
                if not blob.pbData:
                    return b""
                return ctypes.string_at(blob.pbData, blob.cbData)
            finally:
                if blob.pbData:
                    kernel32.LocalFree(blob.pbData)

        def protect(data: bytes) -> bytes:
            if not data:
                return b""
            src = ctypes.create_string_buffer(data)
            indata = _DATA_BLOB(len(data), ctypes.cast(src, ctypes.c_void_p))
            outdata = _DATA_BLOB()
            ok = crypt32.CryptProtectData(
                ctypes.byref(indata), None, None, None, None, 0,
                ctypes.byref(outdata))
            if not ok:
                raise OSError(ctypes.GetLastError())
            return _blob_to_bytes(outdata)

        def unprotect(blob: bytes) -> bytes:
            if not blob:
                return b""
            src = ctypes.create_string_buffer(blob)
            indata = _DATA_BLOB(len(blob), ctypes.cast(src, ctypes.c_void_p))
            outdata = _DATA_BLOB()
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(indata), None, None, None, None, 0,
                ctypes.byref(outdata))
            if not ok:
                raise OSError(ctypes.GetLastError())
            return _blob_to_bytes(outdata)

        # 冒烟自检：确保参数签名在当前系统上真正可用
        probe = protect(b"SSLTool dpapi probe")
        if unprotect(probe) != b"SSLTool dpapi probe":
            raise RuntimeError("DPAPI round-trip mismatch")
        return protect, unprotect
    except Exception:
        return None, None


_DPAPI_PROTECT, _DPAPI_UNPROTECT = _setup_dpapi()


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


def export_sessions(sessions: list, path) -> int:
    """把会话列表导出为 JSON 文件（仅会话配置，不含钥匙串中的密码）。

    返回导出的会话数。
    """
    with open(str(path), "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False, indent=2)
    return len(sessions)


def merge_sessions(current: list, incoming):
    """按 id 合并导入的会话：同 id 覆盖、新 id 追加、非法条目过滤。

    返回 (合并后的列表, 新增数, 更新数)。
    """
    if not isinstance(incoming, list):
        raise ValueError("会话文件格式不正确：应为会话列表")
    clean = [s for s in incoming
             if isinstance(s, dict) and (s.get("host") or s.get("name"))]
    by_id = {}
    for s in current:
        by_id[s.get("id")] = s
    added = updated = 0
    for s in clean:
        sid = s.get("id")
        if sid and sid in by_id:
            by_id[sid] = s
            updated += 1
        else:
            s = dict(s)
            s["id"] = sid or new_session_id()
            by_id[s["id"]] = s
            added += 1
    return list(by_id.values()), added, updated


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
        "group": "",                # 会话分组（空 = 未分组）
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
        "sftp_readonly": False,     # SFTP 只读模式（客户端禁用写操作）
        "jump_host": "",            # 跳板机（jump host / bastion）
        "jump_port": 22,
        "jump_username": "",
        "jump_auth": "password",    # password | key（跳板机密码不落盘，连接时询问）
        "jump_key_path": "",
    }
    s.update(kwargs)
    return s


# ---------------- 凭据存取 ----------------

def _fallback_store() -> dict:
    """读取回退凭据存储；兼容 DPAPI 加密格式与旧 base64 混淆格式。"""
    raw = _load_json(SECRETS_FILE, {})
    if not isinstance(raw, dict):
        return {}
    if raw.get("format") == "dpapi" and _DPAPI_UNPROTECT is not None:
        try:
            plain = _DPAPI_UNPROTECT(base64.b64decode(raw.get("blob", "")))
            data = json.loads(plain.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    if "format" not in raw and "blob" not in raw:
        # 旧格式：{key: base64(value)}，读取后由下次写入时迁移加密
        return raw
    return {}


def _save_fallback_store(data: dict) -> None:
    ensure_data_dir()
    if _DPAPI_PROTECT is not None:
        try:
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            blob = base64.b64encode(_DPAPI_PROTECT(payload)).decode("ascii")
            with open(SECRETS_FILE, "w", encoding="utf-8") as f:
                json.dump({"format": "dpapi", "blob": blob}, f)
            return
        except Exception:
            pass
    # 退化路径（非 Windows 或 DPAPI 失败）：旧 base64 混淆格式
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
