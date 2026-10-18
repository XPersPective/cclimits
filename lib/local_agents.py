"""Read local CLI configuration without exposing or changing credentials."""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import urllib.parse
from datetime import datetime, timezone


def read_json(path):
    try:
        data = Path(path).read_text(encoding="utf-8-sig")
        value = json.loads(data)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def mapping(value):
    return value if isinstance(value, dict) else {}


def opencode_key(provider):
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    entry = read_json(base / "opencode/auth.json").get(provider, {})
    if isinstance(entry, dict) and entry.get("type") == "api":
        key = entry.get("key")
        return key if isinstance(key, str) and key.strip() else None


def zai_key():
    """Use the enabled Z.AI coding-plan route, never another account's key."""
    config = read_json(Path.home() / ".zcode/v2/config.json")
    entry = mapping(config.get("provider")).get("builtin:zai-coding-plan", {})
    if isinstance(entry, dict) and entry.get("enabled") is True:
        options = mapping(entry.get("options"))
        if isinstance(options.get("baseURL"), str) and options["baseURL"].startswith("https://api.z.ai/"):
            key = options.get("apiKey")
            if isinstance(key, str) and key.strip():
                return key
    return opencode_key("zai-coding-plan") or opencode_key("zai")


def windows_antigravity_token():
    """Read agy's Windows Credential Manager entry using the native API."""
    if os.name != "nt":
        return None
    from ctypes import wintypes as W

    class Credential(ctypes.Structure):
        _fields_ = [("Flags", W.DWORD), ("Type", W.DWORD),
                    ("TargetName", W.LPWSTR), ("Comment", W.LPWSTR),
                    ("LastWritten", W.FILETIME), ("CredentialBlobSize", W.DWORD),
                    ("CredentialBlob", ctypes.c_void_p), ("Persist", W.DWORD),
                    ("AttributeCount", W.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", W.LPWSTR), ("UserName", W.LPWSTR)]

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.POINTER(Credential)()
    api.CredReadW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD,
                             ctypes.POINTER(ctypes.POINTER(Credential))]
    api.CredReadW.restype = W.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    if not api.CredReadW("gemini:antigravity", 1, 0, ctypes.byref(pointer)):
        return None
    try:
        raw = ctypes.string_at(pointer.contents.CredentialBlob,
                               pointer.contents.CredentialBlobSize)
        value = json.loads(raw.decode("utf-8"))
        token = value.get("token") if isinstance(value, dict) else None
        return dict(token, source="windows_credential_manager") if isinstance(token, dict) else None
    except (ValueError, UnicodeError):
        return None
    finally:
        api.CredFree(pointer)


def minimax_credentials():
    value = read_json(Path.home() / ".minimax/local-runtime.auth.json").get("auth", {})
    return value if isinstance(value, dict) and value.get("accessToken") else None


def kimi_code_credentials():
    """Honor the selected credential key; do not silently pick another account."""
    root = Path.home() / ".kimi-code"
    try:
        config = (root / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    # ponytail: only the CLI's generated quoted oauth/key form; use a TOML parser
    # if this provider starts supporting arbitrary TOML expressions here.
    section = re.search(r'^\[providers\."managed:kimi-code"\.oauth\]\s*\n([^\[]*)', config, re.M)
    if not section:
        return None
    key = re.search(r'^\s*key\s*=\s*"oauth/([A-Za-z0-9_-]+)"\s*$', section[1], re.M)
    if not key:
        return None
    value = read_json(root / "credentials" / (key[1] + ".json"))
    return value if isinstance(value.get("access_token"), str) else None


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def window(name, remaining, reset=None, minutes=None):
    pct = number(remaining)
    if pct is None or not 0 <= pct <= 100:
        raise ValueError("Invalid or missing quota percentage")
    result = {"name": name, "remaining_pct": pct}
    if reset is not None:
        if isinstance(reset, (int, float)) and not isinstance(reset, bool):
            reset = datetime.fromtimestamp(reset / 1000, timezone.utc).isoformat()
        if not isinstance(reset, str):
            raise ValueError("Invalid quota reset")
        parsed = datetime.fromisoformat(reset.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Quota reset needs timezone")
        result["resets_at"] = parsed.astimezone(timezone.utc).isoformat()
    if minutes is not None:
        duration = number(minutes)
        if duration is None or duration <= 0:
            raise ValueError("Invalid quota window duration")
        result["window_minutes"] = duration
    return result


def get_kimi_code_usage(http_get):
    creds = kimi_code_credentials()
    if not creds:
        return {"error": "No credentials found", "hint": "Run kimi login"}
    expiry = number(creds.get("expires_at"))
    if expiry is not None and expiry <= datetime.now(timezone.utc).timestamp():
        return {"error": "Token expired", "hint": "Run kimi login", "source": "kimi_code_oauth"}
    status, data = http_get("https://api.kimi.ai/coding/v1/usages",
                            {"Authorization": "Bearer " + creds["access_token"]})
    if status != 200 or not isinstance(data, dict):
        return {"error": "Kimi Code quota unavailable (HTTP %s)" % status}
    windows = []
    try:
        for name, entry in data.get("usages", {}).items():
            used = number(entry["used_ratio"])
            if used is None or not 0 <= used <= 1:
                raise ValueError("Invalid usage ratio")
            windows.append(window(name, 100 - 100 * used, entry.get("reset_time")))
        if not windows:
            entries = [("subscription", data["usage"])] if isinstance(data.get("usage"), dict) else []
            entries += [("limit_%d" % i, item["detail"]) for i, item in enumerate(data.get("limits", []))]
            for name, entry in entries:
                total, remaining = float(entry["limit"]), float(entry["remaining"])
                windows.append(window(name, remaining / total * 100, entry.get("resetTime")))
    except (ValueError, TypeError, KeyError, ZeroDivisionError):
        return {"error": "Invalid Kimi Code quota response"}
    return {"status": "ok" if windows else "unknown", "source": "kimi_code_oauth", "quota_windows": windows}


def minimax_group(creds, http_post, region):
    """Use the installed MiniMax client's read-only workspace lookup protocol."""
    now = datetime.now().astimezone()
    millis = str(int(now.timestamp() * 1000))
    seconds = str(int(now.timestamp()))
    lang = "zh" if region == "cn" else "en"
    query = urllib.parse.urlencode({"device_platform": "mcode", "biz_id": "3", "app_id": "3001",
        "version_code": "22201", "unix": millis, "timezone_offset": str(int(now.utcoffset().total_seconds())),
        "sys_language": lang, "lang": lang, "device_id": "0", "os_name": "win32" if os.name == "nt" else "linux",
        "browser_name": "mcode", "user_id": creds.get("realUserID") or "0", "client": "mcode"})
    path = "/matrix/api/v1/user/get_user_extra_info?" + query
    md5 = lambda value: hashlib.md5(value.encode()).hexdigest()
    headers = {"Authorization": "Bearer " + creds["accessToken"], "Content-Type": "application/json",
        "User-Agent": "MiniMaxCode", "x-timestamp": seconds,
        "yy": md5(urllib.parse.quote(path, safe="~()*!.'-") + "_{}" + md5(millis) + "ooui"),
        "x-signature": md5(seconds + "I*7Cf%WZ#S&%1RlZJ&C2" + "{}")}
    host = "https://agent.minimaxi.com" if region == "cn" else "https://agent.minimax.io"
    status, data = http_post(host + path, headers, {})
    if status != 200 or not isinstance(data, dict):
        return None
    workspaces = data.get("workspaces", data.get("data", {}).get("workspaces", []))
    for workspace in workspaces:
        if isinstance(workspace, dict) and workspace.get("workspace_type") == 0:
            return workspace.get("op_group_id")


def get_minimax_usage(http_get, http_post=None):
    creds = minimax_credentials()
    token = os.environ.get("MINIMAX_API_KEY") or (creds or {}).get("accessToken")
    if not token:
        return {"error": "No credentials found", "hint": "Run mcode login or configure MINIMAX_API_KEY"}
    region = os.environ.get("MINIMAX_REGION", "en")
    if region not in ("en", "cn"):
        return {"error": "MINIMAX_REGION must be en or cn"}
    headers = {"Authorization": "Bearer " + token}
    if creds and not os.environ.get("MINIMAX_API_KEY") and http_post:
        group = minimax_group(creds, http_post, region)
        if not group:
            return {"status": "detected", "source": "minimax_login", "quota_status": "unavailable",
                    "hint": "MiniMax login found; native personal-workspace billing lookup unavailable. Check mcode /usage or renew its login."}
        headers["X-Group-Id"] = str(group)
    host = "https://www.minimaxi.com" if region == "cn" else "https://platform.minimax.io"
    status, data = http_get(host + "/v1/api/openplatform/coding_plan/remains", headers)
    if status != 200 or not isinstance(data, dict):
        return {"error": "MiniMax quota unavailable (HTTP %s)" % status}
    code = data.get("base_resp", {}).get("status_code", 0)
    if code != 0:
        return {"status": "detected", "quota_status": "unavailable", "source": "minimax_login" if creds else "minimax_api_key",
                "hint": "MiniMax rejected the quota credential (code %s); account detected, remaining quota unknown" % code}
    windows = []
    try:
        for i, model in enumerate(data.get("model_remains", [])):
            name = model.get("model_name") or "model_%d" % i
            for prefix, label, reset, minutes in [("current_interval", "5h", "end_time", 300),
                                                   ("current_weekly", "7d", "weekly_end_time", 10080)]:
                if model.get(prefix + "_status") == 3:
                    continue  # Unlimited is not a measured percentage.
                pct = model.get(prefix + "_remaining_percent")
                if pct is None:
                    total = number(model.get(prefix + "_total_count"))
                    left = number(model.get(prefix + "_usage_count"))
                    if total and left is not None:
                        pct = left / total * 100  # Native client treats usage_count as remaining.
                if pct is not None:
                    windows.append(window(name + ":" + label, pct, model.get(reset), minutes))
    except (ValueError, TypeError, OverflowError, OSError):
        return {"error": "Invalid MiniMax quota response"}
    return {"status": "ok" if windows else "unknown", "quota_windows": windows, "source": "minimax_quota_api"}


CLI_NAMES = {
    "codex": ("codex",), "claude": ("claude",), "gemini": ("gemini",),
    "opencode": ("opencode",), "qwen": ("qwen",), "kimi-code": ("kimi",),
    "antigravity": ("agy", "antigravity"), "minimax": ("mcode",),
    "mmx": ("mmx",), "zcode": ("zcode",), "omniroute": ("omniroute",),
    "hermes": ("hermes",), "ollama": ("ollama",), "lmstudio": ("lms",),
    "aider": ("aider",), "kilocode": ("kilo", "kilocode"), "copilot": ("copilot",),
}


def inventory():
    """Presence and configured catalog only; never run model inference."""
    result = {}
    for key, names in CLI_NAMES.items():
        binary = next((p for name in names if (p := shutil.which(name))), None)
        if binary:
            result[key] = {"binary": binary, "quota_status": "unknown", "models": [],
                           "access_status": "unverified"}
    if "zcode" not in result and os.name == "nt":
        base = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        script = base / "ZCode/resources/glm/zcode.cjs"
        node = shutil.which("node")
        if node and script.is_file():
            result["zcode"] = {"binary": node, "argv_prefix": [node, str(script)],
                               "quota_status": "unknown", "models": [], "access_status": "unverified"}
    qwen = read_json(Path.home() / ".qwen/settings.json")
    if "codex" in result:
        cache = read_json(Path.home() / ".codex/models_cache.json")
        rows = cache.get("models")
        result["codex"]["models"] = [row["slug"] for row in (rows if isinstance(rows, list) else [])
                                        if isinstance(row, dict) and isinstance(row.get("slug"), str)]
        result["codex"]["catalog_status"] = "cached_unverified"
    for cli, filename, pattern in [
            ("minimax", ".minimax/config.yaml", r'^defaultModel:\s*([^\r\n]+)'),
            ("kimi-code", ".kimi-code/config.toml", r'^default_model\s*=\s*"([^"\r\n]+)"')]:
        if cli in result:
            try:
                content = (Path.home() / filename).read_text(encoding="utf-8")
                selected = re.search(pattern, content, re.M)
                if selected:
                    result[cli]["selected_model"] = selected[1].strip()
            except (OSError, UnicodeError):
                pass
    if "qwen" in result:
        selected = mapping(qwen.get("model"))
        result["qwen"]["selected_model"] = selected.get("name")
        result["qwen"]["endpoint"] = selected.get("baseUrl")
        result["qwen"]["models"] = [m["id"] for rows in mapping(qwen.get("modelProviders")).values()
                                       if isinstance(rows, list) for m in rows
                                       if isinstance(m, dict) and isinstance(m.get("id"), str)
                                       and m.get("baseUrl") == selected.get("baseUrl")]
        result["qwen"]["quota_note"] = "Quota belongs to selected provider/account, not the Qwen executable"
    if "opencode" in result:
        config = read_json(Path.home() / ".config/opencode/opencode.json")
        result["opencode"]["models"] = [p + "/" + m for p, entry in mapping(config.get("provider")).items()
                                           if isinstance(entry, dict) for m in mapping(entry.get("models"))]
        result["opencode"]["quota_note"] = "Each provider/account has its own pool; Zen is only one provider"
    if "zcode" in result:
        config = read_json(Path.home() / ".zcode/v2/config.json")
        result["zcode"]["models"] = [p + "/" + m for p, entry in mapping(config.get("provider")).items()
                                        if isinstance(entry, dict) and entry.get("enabled", True)
                                        for m in mapping(entry.get("models"))]
    return {"observed_at": datetime.now(timezone.utc).isoformat(), "cli_agents": result}


def brain_snapshot(provider, data):
    """Produce Project Brain's existing quota_argv contract; fail on unknown/stale."""
    if data.get("stale_fallback") or data.get("source_stale") or data.get("error"):
        raise ValueError("Quota unavailable or stale")
    windows = list(data.get("quota_windows", []))
    if provider == "antigravity":
        for group in data.get("quota_groups", []):
            for name, entry in group.get("buckets", {}).items():
                windows.append(window(group["name"] + ":" + name, entry.get("remaining_pct"),
                                      entry.get("reset_time"), 300 if name == "5h" else 10080))
    if not windows:
        fields = [("five_hour", 300), ("seven_day", 10080), ("opus", 10080),
                  ("primary_window", None), ("secondary_window", None)]
        for key, minutes in fields:
            entry = data.get(key)
            if not isinstance(entry, dict):
                continue
            pct = entry.get("remaining_percent", entry.get("remaining"))
            if isinstance(pct, str):
                pct = pct.rstrip("%")
            if pct is not None:
                windows.append(window(key, pct, entry.get("resets_at"), entry.get("window_duration_minutes", minutes)))
    if not windows:
        raise ValueError("No measured quota windows for " + provider)
    checked = [window(w["name"], w["remaining_pct"], w.get("resets_at"), w.get("window_minutes")) for w in windows]
    names = [w["name"] for w in checked]
    if any(not isinstance(n, str) or not n.strip() for n in names) or len(names) != len(set(names)):
        raise ValueError("Quota windows require unique names")
    observed = data.get("observed_at")
    if not observed:
        raise ValueError("Quota observation timestamp required")
    timestamp = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or not -30 <= (datetime.now(timezone.utc) - timestamp).total_seconds() <= 300:
        raise ValueError("Quota observation is stale or invalid")
    return {"remaining_pct": min(w["remaining_pct"] for w in checked), "updated_at": observed,
            "quota_windows": checked}
