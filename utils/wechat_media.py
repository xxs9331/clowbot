"""微信 CDN 图片下载工具（参考 OpenCode Bridge weixin-media.ts）

WeChat iLink API 收到的图片消息包含加密的 CDN 信息，
需要通过 CDN URL 下载并用 AES 解密。
"""

import base64
import hashlib
import io
import os
import tempfile
from pathlib import Path
from urllib.parse import quote

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


# CDN 基础 URL
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"

# encodeURIComponent 安全字符（与 JS encodeURIComponent 一致）
_ENCODE_URI_COMPONENT_SAFE_CHARS = "-_.!~*'()"


def _hex_to_bytes(hex_str: str) -> bytes:
    """hex 字符串转 bytes"""
    if not hex_str:
        return b""
    try:
        return bytes.fromhex(hex_str)
    except ValueError:
        return b""


def _aes_decrypt_ecb(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB 解密（微信 CDN 图片加密方式）

    微信 CDN 使用 AES-128-ECB 模式（无 IV），PKCS7 padding。
    """
    if len(data) == 0:
        return data
    cipher = Cipher(algorithms.AES(key[:16]), modes.ECB())
    decryptor = cipher.decryptor()
    plaintext = decryptor.update(data) + decryptor.finalize()
    # 去除 PKCS7 padding
    pad_len = plaintext[-1]
    if 0 < pad_len <= 16:
        plaintext = plaintext[:-pad_len]
    return plaintext


def _parse_aes_key(aes_key_b64: str = "", aes_key_hex: str = "") -> bytes:
    """解析 AES key

    两种编码格式（来自 pic-decrypt.ts parseAesKey）：
    - base64(raw 16 bytes) → 图片
    - base64(hex string of 32 chars) → 文件/语音/视频

    优先使用 aes_key_hex（image_item.aeskey，hex 字符串直接 → 16字节）
    """
    if aes_key_hex:
        return _hex_to_bytes(aes_key_hex)

    if not aes_key_b64:
        return b""

    decoded = base64.b64decode(aes_key_b64)
    # 如果解码后是 16 字节，就是原始密钥
    if len(decoded) == 16:
        return decoded
    # 如果解码后是 32 字节的 hex 字符串，需要再做 hex 解码
    try:
        hex_str = decoded.decode("ascii")
        if len(hex_str) == 32:
            return bytes.fromhex(hex_str)
    except (UnicodeDecodeError, ValueError):
        pass
    # fallback: 截取前 16 字节
    return decoded[:16]


async def download_image(
    image_item: dict,
    session: aiohttp.ClientSession = None,
) -> bytes | None:
    """下载微信图片消息中的图片
    
    iLink 消息中的 image_item 结构:
    {
        "image_item": {
            "media": {
                "encrypt_query_param": "...",  // URL 参数（含加密 key）
                "aes_key": "...",              // AES 密钥 (base64)
                "encrypt_type": 1,             // 加密类型
            },
            "aeskey": "...",                    // AES 密钥 (hex, 备用)
            "mid_size": 38824,                  // 中等尺寸图片大小
        }
    }
    
    Returns: 图片二进制数据，失败返回 None
    """
    img = image_item.get("image_item", image_item)
    if not img:
        print("[Media] 无 image_item 数据")
        return None

    # 打印完整结构便于调试
    print(f"[Media] image_item keys: {list(img.keys())}")
    media = img.get("media", {})
    if media:
        print(f"[Media] media keys: {list(media.keys())}")
        for k, v in media.items():
            if isinstance(v, str):
                print(f"[Media]   {k}: {v[:60]}...")

    # 构造下载 URL（需要 URL 编码 encrypt_query_param）
    encrypt_param = media.get("encrypt_query_param", "")
    if not encrypt_param:
        print("[Media] 缺少 encrypt_query_param")
        encrypt_param = img.get("encrypt_query_param", "")
        if not encrypt_param:
            print("[Media] img 中也没有 encrypt_query_param")
            return None

    url = media.get("full_url", "")
    if not url:
        url = f"{CDN_BASE}/download?encrypted_query_param={quote(encrypt_param, safe=_ENCODE_URI_COMPONENT_SAFE_CHARS)}"
    print(f"[Media] 下载 URL: {url[:120]}...")

    # 解析 AES 密钥
    aes_key = _parse_aes_key(
        aes_key_b64=media.get("aes_key", ""),
        aes_key_hex=img.get("aeskey", ""),
    )

    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        headers = {
            "User-Agent": "MicroMessenger Client",
            "Accept": "image/webp,image/*",
        }
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            print(f"[Media] HTTP {resp.status}, content-type={resp.headers.get('Content-Type','?')}, size={resp.headers.get('Content-Length','?')}")
            if resp.status != 200:
                return None
            raw = await resp.read()

        # 使用 AES-128-ECB 解密（微信 CDN 标准加密方式）
        if aes_key and len(aes_key) >= 16:
            print(f"[Media] AES-ECB 解密, key_len={len(aes_key)}, data_len={len(raw)}")
            raw = _aes_decrypt_ecb(raw, aes_key)
            print(f"[Media] 解密后大小: {len(raw)} bytes")
            # 验证解密后是否是有效图片
            if raw[:4] == b"\x89PNG":
                print(f"[Media] 解密后格式: PNG ✓")
            elif raw[:2] == b"\xff\xd8":
                print(f"[Media] 解密后格式: JPEG ✓")
            elif raw[:4] == b"RIFF":
                print(f"[Media] 解密后格式: WebP ✓")
            elif raw[:4] == b"GIF8":
                print(f"[Media] 解密后格式: GIF ✓")
            else:
                print(f"[Media] ⚠ 解密后格式未知! 首字节: {raw[:16].hex()}")
        else:
            print(f"[Media] 无 AES key 或太短 ({len(aes_key) if aes_key else 0} bytes)")

        return raw
    except Exception as e:
        print(f"[Media] 下载图片失败: {e}")
        return None
    finally:
        if close_session:
            await session.close()


def save_temp_image(data: bytes, prefix: str = "wx_img") -> str:
    """保存图片到临时文件，返回路径"""
    # 检测格式
    ext = ".jpg"
    if data[:4] == b"\x89PNG":
        ext = ".png"
    elif data[:2] == b"\xff\xd8":
        ext = ".jpg"
    elif data[:4] == b"RIFF":
        ext = ".webp"
    elif data[:4] == b"GIF8":
        ext = ".gif"
    
    fd, path = tempfile.mkstemp(suffix=ext, prefix=f"{prefix}_")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def extract_image_items(msg: dict) -> list[dict]:
    """从 iLink 消息中提取所有图片类型的 item"""
    images = []
    for item in msg.get("item_list", []):
        if item.get("type") == 2:  # MessageItemType.IMAGE
            images.append(item)
    return images
