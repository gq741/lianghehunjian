"""Validated non-secret preferences and Windows current-user encrypted credentials."""
from pathlib import Path
import base64
import ctypes
from ctypes import wintypes
import json
import hashlib
import math
import os
import threading
import unicodedata
import uuid

DEFAULTS = {
    "output_dir": str(Path.home() / "Videos" / "京东混剪成片"),
    "download_dir": str(Path.home() / "Videos" / "京东混剪素材"),
    "asr_model_path": "", "asr_device": "cpu",
    "voice_id": "be40aff9ee7b4d0cba3bb35e924d85b2",
    "voice_provider": "fish", "minimax_model": "speech-2.8-hd",
    "minimax_voice_id": "male-qn-qingse",
    "deepseek_model": "deepseek-flash",
    "vision_provider": "glm",
    "vision_endpoint": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    "vision_model": "glm-5.3-flash",
    "bgm_path": "", "bgm_gain": .15, "target_seconds": 25, "output_count": 1,
    "render_concurrency": 1, "encoder": "cpu", "preset": "balanced",
    "width": 720, "height": 1280, "fps": 30,
    "transition": "fade", "transition_seconds": .2,
    "clean_mode": "none", "clean_top_pct": 75, "clean_height_pct": 18,
    "caption_plate": False, "subtitle_font_size": 42, "subtitle_margin_pct": 12,
}


def data_root():
    explicit = os.environ.get("JDMIX_DATA_DIR")
    return Path(explicit) if explicit else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "JDMixStudio" / "v1"


def _atomic(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _dpapi(data, decrypt=False):
    if os.name != "nt":
        raise ValueError("凭据保存需要 Windows 当前用户加密服务")
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    raw = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), "JDMixStudio", None, None, None, 1, ctypes.byref(result))
    if not ok:
        raise ValueError("Windows 凭据加密服务失败，请确认当前用户环境")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


class Settings:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.path = self.root / "settings.json"
        self.secret_path = self.root / "secrets.dpapi"
        self.values = dict(DEFAULTS)
        self._recovery = {'needed': False, 'backup_saved': False,
                          'invalid_fields': [], 'message': ''}
        self._unbacked_preferences = None
        if self.path.exists():
            try:
                original = self.path.read_bytes()
            except OSError:
                self._unbacked_preferences = True
                self._recovery_notice(['配置文件'], False)
            else:
                try:
                    saved = json.loads(original.decode('utf-8-sig'))
                    if not isinstance(saved, dict):
                        raise ValueError
                except (ValueError, UnicodeError, RecursionError):
                    self._recover_preferences(original, ['配置文件'])
                else:
                    # Read-only migration: old Qwen preferences remain on disk
                    # until explicit save. Its encrypted key is never a GLM key.
                    old_model, old_endpoint = saved.get('vision_model'), saved.get('vision_endpoint')
                    if ((isinstance(old_model, str) and old_model.startswith('qwen')) or
                            (isinstance(old_endpoint, str) and old_endpoint.startswith((
                                'https://dashscope.aliyuncs.com/',
                                'https://dashscope-intl.aliyuncs.com/',
                                'https://dashscope-us.aliyuncs.com/')))):
                        saved = {**saved, **{key: DEFAULTS[key] for key in
                                 ('vision_provider', 'vision_endpoint', 'vision_model')}}
                    recovered, invalid = dict(DEFAULTS), []
                    for key, value in saved.items():
                        if key not in DEFAULTS:
                            continue
                        # Migrate valid V1 limits in memory, without rewriting.
                        if key == 'target_seconds' and type(value) in (int, float) and 60 < value <= 120:
                            value = 60
                        if key == 'output_count' and type(value) is int and 5 < value <= 20:
                            value = 5
                        try:
                            self._validate(key, value)
                        except (ValueError, TypeError, OverflowError):
                            invalid.append(key)
                        else:
                            recovered[key] = value
                    if invalid:
                        self._recover_preferences(original, invalid)
                    self.values = recovered

    def _recovery_notice(self, fields, backup_saved):
        self._recovery = {'needed': True, 'backup_saved': backup_saved,
            'invalid_fields': list(fields),
            'message': ('部分设置已恢复默认值，其他有效设置及凭据保持不变；原配置已备份。'
                        if backup_saved else
                        '部分设置已恢复默认值，原配置仍保留；备份未完成前不覆盖原配置。')}

    def _backup_preferences(self, original):
        """Preserve exact bytes durably before any explicit replacement."""
        identity = hashlib.sha256(original).hexdigest()
        archive = self.root / f'settings.invalid.{identity}.json'
        try:
            with archive.open('xb') as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if archive.is_symlink() or archive.read_bytes() != original:
                raise OSError('Configuration backup is not verifiable') from None
            # A prior flush may have failed after writing complete bytes.
            # Re-establish durability before allowing a later explicit save.
            with archive.open('r+b') as stream:
                stream.flush()
                os.fsync(stream.fileno())
        if archive.read_bytes() != original:
            raise OSError('Configuration backup is not verifiable')

    def _recover_preferences(self, original, fields):
        try:
            self._backup_preferences(original)
        except OSError:
            # Startup remains usable, but an explicit save must first succeed
            # in preserving the original. Credentials are never reset here.
            self._unbacked_preferences = original
            self._recovery_notice(fields, False)
        else:
            self._recovery_notice(fields, True)

    def _ensure_preferences_backup(self):
        if self._unbacked_preferences is None:
            return
        try:
            original = (self.path.read_bytes() if self._unbacked_preferences is True
                        else self._unbacked_preferences)
            self._backup_preferences(original)
        except OSError:
            raise ValueError('原配置备份尚未完成，请检查设置目录后重试；未覆盖原配置或凭据') from None
        self._unbacked_preferences = None
        self._recovery_notice(self._recovery['invalid_fields'], True)

    def _validate(self, key, value):
        enums = {"asr_device": ["cpu", "cuda"], "encoder": ["cpu", "nvenc", "qsv"],
                 "preset": ["fast", "balanced", "quality"], "transition": ["fade", "none"],
                 "clean_mode": ["none", "blur", "delogo"], "vision_provider": ["glm"],
                 "voice_provider": ["fish", "minimax"],
                 "minimax_model": ["speech-2.8-hd", "speech-2.8-turbo"]}
        ranges = {"bgm_gain": (0, 1), "target_seconds": (5, 60), "output_count": (1, 5),
                  "render_concurrency": (1, 3), "width": (240, 1920), "height": (240, 1920),
                  "fps": (15, 60), "transition_seconds": (0, .8), "clean_top_pct": (0, 95),
                  "clean_height_pct": (3, 45), "subtitle_font_size": (16, 96), "subtitle_margin_pct": (2, 40)}
        ints = {"output_count", "render_concurrency", "width", "height", "fps", "subtitle_font_size"}
        if key not in DEFAULTS:
            raise ValueError("未知设置字段：" + key)
        if key in enums:
            if value not in enums[key]:
                raise ValueError("设置选项无效：" + key)
        elif key in ranges:
            low, high = ranges[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("设置范围无效：" + key)
            if key in ints and (not isinstance(value, int) or (key in {"width", "height"} and value % 2)):
                raise ValueError("设置需要有效整数：" + key)
        elif key == "caption_plate":
            if type(value) is not bool:
                raise ValueError("字幕底板必须是开关")
        elif key == "minimax_voice_id":
            # Official system and custom voice IDs are not Fish hex identifiers.
            if (not isinstance(value, str) or not value.strip() or len(value) > 256
                    or any(unicodedata.category(c) == "Cc" for c in value)):
                raise ValueError("MiniMax 音色 ID 需要 1–256 个字符，且不含控制字符")
        else:
            if not isinstance(value, str) or len(value) > 2048 or any(c in value for c in "\n\r\0"):
                raise ValueError("设置文本无效：" + key)
            if key == "output_dir" and not value.strip():
                raise ValueError("请选择成片输出文件夹")
            if key in {"output_dir", "download_dir", "asr_model_path", "bgm_path"} and value and not Path(value).is_absolute():
                raise ValueError("请选择绝对路径：" + key)
            if key in {"voice_id", "deepseek_model"} and not value.strip():
                raise ValueError("音色和模型名称不能为空")
            if key == "vision_endpoint":
                from urllib.parse import urlsplit
                url = urlsplit(value)
                if (url.scheme != 'https' or url.hostname != 'open.bigmodel.cn'
                        or url.username or url.password or url.port not in (None, 443) or url.query or url.fragment
                        or url.path != '/api/paas/v4/chat/completions'):
                    raise ValueError("请选择智谱官方视觉模型接口地址")
            if key == "vision_model":
                if value != 'glm-5.3-flash':
                    raise ValueError("视觉模型请使用 glm-5.3-flash")

    def _secrets(self):
        if not self.secret_path.exists():
            return {}
        try:
            result = json.loads(_dpapi(base64.b64decode(self.secret_path.read_text("utf-8"), validate=True), True))
            if not isinstance(result, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                   for k, v in result.items()):
                raise ValueError('Invalid encrypted credential structure')
            return result
        except Exception:
            raise ValueError("保存的凭据属于其他用户或已损坏，请在设置页确认重置后重新填写") from None

    def secret(self, provider):
        with self.lock:
            if provider == 'vision':
                provider = 'glm'
            return self._secrets().get(provider, "")

    def public(self):
        with self.lock:
            result = dict(self.values)
            result['settings_recovery'] = {**self._recovery,
                'invalid_fields': list(self._recovery['invalid_fields'])}
            try:
                secrets = self._secrets()
                result.update(deepseek_key_set=bool(secrets.get("deepseek")), fish_key_set=bool(secrets.get("fish")),
                              minimax_key_set=bool(secrets.get("minimax")),
                              glm_key_set=bool(secrets.get("glm")), vision_key_set=bool(secrets.get("glm")),
                              credentials_need_reset=False)
            except ValueError:
                result.update(deepseek_key_set=False, fish_key_set=False, minimax_key_set=False, glm_key_set=False, vision_key_set=False,
                              credentials_need_reset=True)
            return result

    def reset_credentials(self, confirmed):
        """Explicit recovery only; preserve unreadable ciphertext before reset."""
        if confirmed is not True:
            raise ValueError('请明确确认备份并重置损坏凭据')
        with self.lock:
            try:
                self._secrets()
            except ValueError:
                pass
            else:
                raise ValueError('当前凭据未损坏，无需重置')
            identity = uuid.uuid4().hex
            archive = self.root / f'secrets.unreadable.{identity}.dpapi'
            temporary = self.root / f'secrets.reset-{identity}.tmp'
            try:
                # Build replacement first; encryption failures leave the old
                # file untouched. The archive is ciphertext, never plain keys.
                encrypted = base64.b64encode(_dpapi(b'{}'))
                original = self.secret_path.read_bytes()
                with archive.open('xb') as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
                with temporary.open('xb') as stream:
                    stream.write(encrypted)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.secret_path)
            except (OSError, ValueError):
                raise ValueError('凭据重置未完成，原凭据已保留；请检查本机加密服务和目录后重试') from None
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            return self.public()

    def update(self, patch):
        if not isinstance(patch, dict):
            raise ValueError("设置需要JSON对象")
        with self.lock:
            next_values = dict(self.values)
            secret_updates = {}
            for key, value in patch.items():
                if key == 'vision_api_key':
                    raise ValueError('请在智谱 GLM API Key 输入框保存新密钥')
                if key in {"deepseek_api_key", "fish_api_key", "minimax_api_key", "glm_api_key"}:
                    if not isinstance(value, str) or len(value) > 4096 or any(c in value for c in "\n\r\0"):
                        raise ValueError("API Key 格式无效")
                    if value.strip():
                        secret_updates[key.removesuffix("_api_key")] = value.strip()
                else:
                    self._validate(key, value)
                    next_values[key] = value
            self._ensure_preferences_backup()
            if secret_updates:
                secrets = self._secrets()
                secrets.update(secret_updates)
                encrypted = base64.b64encode(_dpapi(json.dumps(secrets).encode())).decode()
                temporary = self.secret_path.with_suffix(".tmp")
                temporary.write_text(encrypted, encoding="utf-8")
                os.replace(temporary, self.secret_path)
            _atomic(self.path, next_values)
            self.values = next_values
            self._recovery = {'needed': False, 'backup_saved': False,
                              'invalid_fields': [], 'message': ''}
            return self.public()
