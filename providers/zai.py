import json
import base64
import time
import secrets
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple, List

try:
    from curl_cffi.requests import AsyncSession
except ImportError:
    raise ImportError("请安装 curl_cffi: pip install curl_cffi")

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    from cryptography.hazmat.backends import default_backend
except ImportError:
    raise ImportError("请安装加密库: pip install cryptography")

from astrbot.api import logger
from ..domain import ApiRequest, GenResult, PluginError, APIErrorType
from ..utils import Result, Ok, Err, ImageUtils
from .openai import OpenAIProvider


class DarkKnightSigner:
    """感谢来自WangYiHeng-47/zai.is-的实现"""
    def __init__(self, jwk_data: Dict, fingerprint: Dict):
        self.jwk = jwk_data
        self.fingerprint = fingerprint
        try:
            self.private_key = self._load_private_key(jwk_data)
        except Exception as e:
            raise ValueError(f"私钥还原失败: {e}")

    def _pad_base64(self, b64_str):
        return b64_str + '=' * (-len(b64_str) % 4)

    def _load_private_key(self, jwk):
        # 从 JWK 参数还原 EC 私钥
        d_int = int.from_bytes(base64.urlsafe_b64decode(self._pad_base64(jwk['d'])), 'big')
        x_int = int.from_bytes(base64.urlsafe_b64decode(self._pad_base64(jwk['x'])), 'big')
        y_int = int.from_bytes(base64.urlsafe_b64decode(self._pad_base64(jwk['y'])), 'big')

        public_numbers = ec.EllipticCurvePublicNumbers(x_int, y_int, ec.SECP256R1())
        return ec.EllipticCurvePrivateNumbers(d_int, public_numbers).private_key(default_backend())

    def generate_header(self) -> str:
        nonce = secrets.token_hex(32)
        ts = int(time.time() * 1000)

        # 构造 Payload
        base_payload = {
            "fp": self.fingerprint,
            "nonce": nonce,
            "pk": {
                "crv": self.jwk.get("crv", "P-256"),
                "kty": self.jwk.get("kty", "EC"),
                "x": self.jwk["x"],
                "y": self.jwk["y"]
            },
            "ts": ts,
            "v": 1
        }

        # 序列化 + 签名
        canonical_json = json.dumps(base_payload, separators=(',', ':'), sort_keys=True)
        der_signature = self.private_key.sign(
            canonical_json.encode('utf-8'),
            ec.ECDSA(hashes.SHA256())
        )

        # DER 转 R|S
        r, s = utils.decode_dss_signature(der_signature)
        raw_signature = r.to_bytes(32, 'big') + s.to_bytes(32, 'big')
        sig_b64 = base64.urlsafe_b64encode(raw_signature).decode().rstrip('=')

        final_payload = base_payload.copy()
        final_payload["sig"] = sig_b64

        # 最终编码 x-zai-darkknight
        return base64.urlsafe_b64encode(
            json.dumps(final_payload, separators=(',', ':'), sort_keys=True).encode()
        ).decode().rstrip('=')


class ZaiProvider(OpenAIProvider):
    """基于DarkKnightSigner构造的简易2api，并补上了图生图接口"""
    # API 接口
    ZAI_NEW_CHAT_URL = "https://zai.is/api/v1/chats/new"
    ZAI_COMPLETION_URL = "https://zai.is/api/chat/completions"
    ZAI_UPLOAD_URL = "https://zai.is/api/v1/files/"

    PLUGIN_DIR_NAME = "astrbot_plugin_bananic_ninjutsu"
    CREDS_FILENAME = "zai_creds.json"

    def __init__(self, session):
        super().__init__(session)
        self._cached_signer: Optional[DarkKnightSigner] = None
        self._cached_token: Optional[str] = None
        self._last_creds_hash: str = ""

    def _load_credentials(self, request_api_key: str) -> Tuple[DarkKnightSigner, str]:
        creds_data = None
        source = "Unknown"

        # 1. 连接配置 (转义过的JSON 字符串)
        if request_api_key and request_api_key.strip().startswith("{") and '"private_key"' in request_api_key:
            try:
                creds_data = json.loads(request_api_key)
                source = "Config (Direct JSON)"
            except json.JSONDecodeError:
                pass

        # 2. 凭证文件 (推荐)
        if not creds_data:
            current_file = Path(__file__).resolve()
            paths_to_try = []

            # 标准数据目录
            try:
                data_root = current_file.parents[3] 
                path_std = data_root / "plugin_data" / self.PLUGIN_DIR_NAME / self.CREDS_FILENAME
                paths_to_try.append(path_std)
            except IndexError:
                pass

            # 同级目录 (Fallback)
            path_local = current_file.parent / self.CREDS_FILENAME
            paths_to_try.append(path_local)

            # 遍历寻找
            for p in paths_to_try:
                if p.exists():
                    try:
                        content = p.read_text(encoding="utf-8")
                        creds_data = json.loads(content)
                        source = f"File ({p})"
                        break
                    except Exception as e:
                        logger.warning(f"[Zai] 尝试读取凭证 {p} 失败: {e}")

        # 3. 最终校验
        if not creds_data:
            tried_str = "\n".join([str(p) for p in paths_to_try]) if 'paths_to_try' in locals() else "None"
            raise PluginError(
                APIErrorType.AUTH_FAILED, 
                f"未找到有效的 Zai 凭证文件，请在playwright环境下使用zai_creds.py生成zai_creds.json。\n已尝试路径:\n{tried_str}"
            )

        # 4. 缓存校验
        current_hash = str(hash(json.dumps(creds_data, sort_keys=True)))
        if self._cached_signer and self._last_creds_hash == current_hash:
            return self._cached_signer, self._cached_token

        try:
            signer = DarkKnightSigner(creds_data["private_key"], creds_data["fingerprint"])
            token = creds_data["token"]
            self._cached_signer = signer
            self._cached_token = token
            self._last_creds_hash = current_hash
            logger.info(f"[Zai] 成功加载凭证。来源: {source}")
            return signer, token
        except Exception as e:
            raise PluginError(APIErrorType.AUTH_FAILED, f"凭证初始化失败: {e}")

    def _map_image_size(self, size_str: str) -> str:
        s = size_str.upper()
        if "4K" in s or "4k" in s: return "4K"
        if "2K" in s or "2k" in s: return "2K"
        return "1K"

    def _map_aspect_ratio(self, ar_str: str) -> str:
        if not ar_str or ar_str == "default":
            return "dynamic" # Zai 的默认值

        valid_ratios = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9", "dynamic"]

        if ar_str in valid_ratios:
            return ar_str

        return "dynamic"

    async def generate(self, request: ApiRequest) -> Result[GenResult, PluginError]:
        try:
            signer, token = self._load_credentials(request.api_key)
            async with AsyncSession(impersonate="chrome120") as session:
                # 1. 上传
                files_list = []
                if request.image_bytes_list:
                    for img_bytes in request.image_bytes_list:
                        file_url = await self._upload_image(session, signer, token, img_bytes, request.proxy_url)
                        files_list.append({
                            "type": "image",
                            "url": file_url
                        })

                # 2. 握手
                chat_id = await self._handshake(
                    session, signer, token, request, 
                    prompt=request.gen_config.prompt, 
                    files=files_list
                )

                # 3. 生成
                return await self._chat(
                    session, signer, token, chat_id, request, 
                    files_list=files_list
                )
        except Exception as e:
            error, _ = self.convert_exception(e)
            return Err(error)

    async def _handshake(self, session: AsyncSession, signer: DarkKnightSigner, token: str, request: ApiRequest, prompt: str, files: List[Dict]) -> str:
        msg_id = str(uuid.uuid4())
        model = request.preset.model
        ts_ms = int(time.time() * 1000)
        ts_s = int(time.time())

        # 构造消息内容
        message_content = {
            "id": msg_id,
            "parentId": None,
            "childrenIds": [],
            "role": "user",
            "content": prompt,
            "timestamp": ts_s,
            "models": [model]
        }

        if files:
            message_content["files"] = files

        # 构造完整 Payload
        payload = {
            "chat": {
                "id": "", 
                "title": "New Chat", 
                "models": [model], 
                "params": {},
                "history": { 
                    "messages": { msg_id: message_content }, 
                    "currentId": msg_id 
                },
                "messages": [message_content],
                "tags": [], 
                "timestamp": ts_ms
            }, 
            "folder_id": None
        }

        # 探针：查看握手 Payload
        logger.debug(f"[Zai Probe] Handshake Payload Files: {files}")
        # logger.debug(f"[Zai Probe] Full Payload: {json.dumps(payload, ensure_ascii=False)}")

        headers = {
            "Authorization": token,
            "x-zai-darkknight": signer.generate_header(),
            "x-zai-fp": json.dumps(signer.fingerprint),
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Origin": "https://zai.is",
            "Referer": "https://zai.is/"
        }

        resp = await session.post(self.ZAI_NEW_CHAT_URL, json=payload, headers=headers, proxy=request.proxy_url, timeout=15)

        if resp.status_code != 200:
            logger.error(f"[Zai Probe] Handshake Error: {resp.text}")
            raise PluginError(APIErrorType.SERVER_ERROR, f"Zai 建房失败 ({resp.status_code})")

        return resp.json().get("id")

    async def _upload_image(self, session: AsyncSession, signer: DarkKnightSigner, token: str, image_bytes: bytes, proxy: str = None) -> str:
        # 1. 准备基础信息
        mime_type = ImageUtils.get_mime_type(image_bytes) or "image/png"
        ext = mime_type.split("/")[-1]
        filename = f"pasted-image-{int(time.time())}.{ext}"

        boundary_str = f"WebKitFormBoundary{secrets.token_hex(16)}"
        boundary = f"----{boundary_str}"

        # 2. 构造 Metadata
        meta_json = json.dumps({
            "public_access": True,
            "source": "base64_conversion"
        }, separators=(',', ':'))

        part_metadata = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="metadata"\r\n\r\n'
            f'{meta_json}\r\n'
        ).encode('utf-8')

        # 3. 构造 File
        part_file_head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode('utf-8')

        part_file_tail = b"\r\n"

        # 4. 结束符
        part_closing = (
            f"--{boundary}--\r\n"
        ).encode('utf-8')

        # 拼装
        data_body = part_metadata + part_file_head + image_bytes + part_file_tail + part_closing

        headers = {
            "Authorization": token,
            "x-zai-darkknight": signer.generate_header(),
            "x-zai-fp": json.dumps(signer.fingerprint),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(data_body)),
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Origin": "https://zai.is",
            "Referer": "https://zai.is/"
        }

        logger.debug(f"[Zai Probe] 上传中(修正字段名为metadata)... Size: {len(data_body)}")

        resp = await session.post(
            self.ZAI_UPLOAD_URL, 
            headers=headers, 
            data=data_body, 
            proxy=proxy, 
            timeout=60
        )

        if resp.status_code == 200:
            try:
                data = resp.json()
                file_id = data.get("id")
                meta_data = data.get("meta", {}).get("data", {})
                logger.debug(f"[Zai Probe] 上传成功. Meta Data: {meta_data}")

                if not file_id:
                    raise ValueError("响应缺失ID")
                    
                return f"/api/v1/files/{file_id}/content/public"
            except Exception as e:
                logger.error(f"[Zai] 解析失败: {e} Resp: {resp.text[:200]}")
                raise PluginError(APIErrorType.SERVER_ERROR, "Zai 图片上传解析失败")
        else:
            logger.error(f"[Zai] 上传失败 {resp.status_code}: {resp.text}")
            raise PluginError(APIErrorType.SERVER_ERROR, f"Zai 图片上传失败 ({resp.status_code})")

    async def _chat(self, session: AsyncSession, signer: DarkKnightSigner, token: str, chat_id: str, request: ApiRequest, files_list: List[Dict]) -> Result[GenResult, PluginError]:
        # 1. 构造消息体
        message_content_parts = []

        # 文本提示词
        if request.gen_config.prompt:
            message_content_parts.append({
                "type": "text",
                "text": request.gen_config.prompt
            })

        # 图片引用
        if files_list:
            for file_info in files_list:
                message_content_parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": file_info.get("url")
                    }
                })

        # 2. 构造 messages 列表
        messages = [{
            "role": "user",
            "content": message_content_parts,
            # 单次调用不需要，实现多轮交互可能需要从 History 中提取
            # "parentId": None,
            # "childrenIds": [],
            # "timestamp": int(time.time()),
            # "models": [request.preset.model]
        }]

        target_size = self._map_image_size(request.gen_config.image_size)
        target_ar = self._map_aspect_ratio(request.gen_config.aspect_ratio)

        # 3. 构造 Payload
        payload = {
            "chat_id": chat_id,
            "model": request.preset.model,
            "messages": messages, # 包含图文混合内容
            "stream": True,
            "params": {},
            "image_size": target_size,
            "aspect_ratio": target_ar
        }

        # 探针：打印 Chat 完整载荷
        logger.debug("="*20 + " ZAI CHAT PAYLOAD " + "="*20)
        logger.debug(json.dumps(payload, ensure_ascii=False, indent=2))
        logger.debug("="*60)

        headers = {
            "Authorization": token,
            "x-zai-darkknight": signer.generate_header(),
            "x-zai-fp": json.dumps(signer.fingerprint),
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Origin": "https://zai.is"
        }

        resp = await session.post(
            self.ZAI_COMPLETION_URL, 
            json=payload, 
            headers=headers, 
            proxy=request.proxy_url, 
            stream=True, 
            timeout=120
        )

        if resp.status_code != 200:
            logger.error(f"[Zai Chat Error] Status: {resp.status_code}\nBody: {resp.text}")
            raise PluginError(APIErrorType.SERVER_ERROR, f"Zai 生成失败 ({resp.status_code})")

        full_content = ""
        buffer = ""

        async for chunk in resp.aiter_content():
            if not chunk: continue
            chunk_str = chunk.decode('utf-8', errors='ignore')
            buffer += chunk_str

            while True:
                start_index = buffer.find("data: ")
                if start_index == -1:
                    if len(buffer) > 100: buffer = buffer[-20:]
                    break

                next_start_index = buffer.find("data: ", start_index + 6)
                json_str = None

                if next_start_index != -1:
                    segment = buffer[start_index:next_start_index]
                    buffer = buffer[next_start_index:] 
                    json_str = segment[6:].strip() 
                else:
                    if "[DONE]" in buffer[start_index:]:
                        json_str = "[DONE]"
                        buffer = "" 
                    elif buffer.strip().endswith("}"): 
                        temp_segment = buffer[start_index:]
                        try:
                            check_json = temp_segment[6:].strip()
                            json.loads(check_json)
                            json_str = check_json
                            buffer = "" 
                        except:
                            break
                    else:
                        break

                if not json_str: 
                    continue

                if json_str == "[DONE]":
                    break

                try:
                    data = json.loads(json_str)
                    choices = data.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content_piece = delta.get("content", "")
                        if content_piece:
                            full_content += content_piece
                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    pass

        image_url = self._extract_image_url(full_content)
        if not image_url:
            logger.error("="*20 + " ZAI DEBUG PROBE " + "="*20)
            logger.error(f"[Parse Result] Length: {len(full_content)}")
            logger.error(f"[Parse Content] {full_content}")
            logger.error("="*57)

            if len(full_content) > 5:
                return Err(PluginError(APIErrorType.SERVER_ERROR, f"未检测到图片链接，Zai 回复: {full_content[:100]}..."))
            return Err(PluginError(APIErrorType.SERVER_ERROR, "Zai 响应为空"))

        image_bytes = await self._download_or_decode(image_url, request.proxy_url)

        return Ok(GenResult(
            images=[image_bytes],
            model_name=request.preset.model,
            finish_reason="success"
        ))

    async def get_models(self, request: ApiRequest) -> list[str]:
        # 逆向接口都不知道能活多久，懒得维护获取模型列表，感兴趣的可以自己去抓
        return ["gemini-3-pro-image-preview"]