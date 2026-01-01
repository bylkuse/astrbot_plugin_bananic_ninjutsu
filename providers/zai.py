import asyncio
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
        d_int = int.from_bytes(base64.urlsafe_b64decode(self._pad_base64(jwk['d'])), 'big')
        x_int = int.from_bytes(base64.urlsafe_b64decode(self._pad_base64(jwk['x'])), 'big')
        y_int = int.from_bytes(base64.urlsafe_b64decode(self._pad_base64(jwk['y'])), 'big')

        public_numbers = ec.EllipticCurvePublicNumbers(x_int, y_int, ec.SECP256R1())
        return ec.EllipticCurvePrivateNumbers(d_int, public_numbers).private_key(default_backend())

    def generate_header(self) -> str:
        nonce = secrets.token_hex(32)
        ts = int(time.time() * 1000)

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

        canonical_json = json.dumps(base_payload, separators=(',', ':'), sort_keys=True)
        der_signature = self.private_key.sign(
            canonical_json.encode('utf-8'),
            ec.ECDSA(hashes.SHA256())
        )

        r, s = utils.decode_dss_signature(der_signature)
        raw_signature = r.to_bytes(32, 'big') + s.to_bytes(32, 'big')
        sig_b64 = base64.urlsafe_b64encode(raw_signature).decode().rstrip('=')

        final_payload = base_payload.copy()
        final_payload["sig"] = sig_b64

        return base64.urlsafe_b64encode(
            json.dumps(final_payload, separators=(',', ':'), sort_keys=True).encode()
        ).decode().rstrip('=')


class ZaiProvider(OpenAIProvider):
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

        if request_api_key and request_api_key.strip().startswith("{") and '"private_key"' in request_api_key:
            try:
                creds_data = json.loads(request_api_key)
                source = "Config (Direct JSON)"
            except json.JSONDecodeError:
                pass

        if not creds_data:
            current_file = Path(__file__).resolve()
            paths_to_try = []

            try:
                data_root = current_file.parents[3] 
                path_std = data_root / "plugin_data" / self.PLUGIN_DIR_NAME / self.CREDS_FILENAME
                paths_to_try.append(path_std)
            except IndexError:
                pass

            path_local = current_file.parent / self.CREDS_FILENAME
            paths_to_try.append(path_local)

            for p in paths_to_try:
                if p.exists():
                    try:
                        content = p.read_text(encoding="utf-8")
                        creds_data = json.loads(content)
                        source = f"File ({p})"
                        break
                    except Exception as e:
                        logger.warning(f"[Zai] 尝试读取凭证 {p} 失败: {e}")

        if not creds_data:
            tried_str = "\n".join([str(p) for p in paths_to_try]) if 'paths_to_try' in locals() else "None"
            raise PluginError(
                APIErrorType.AUTH_FAILED, 
                f"未找到有效的 Zai 凭证文件，请在playwright环境下使用zai_creds.py生成zai_creds.json。\n已尝试路径:\n{tried_str}"
            )

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
            return "dynamic"
        valid_ratios = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9", "dynamic"]
        if ar_str in valid_ratios:
            return ar_str
        return "dynamic"

    async def generate(self, request: ApiRequest) -> Result[GenResult, PluginError]:
        try:
            signer, token = self._load_credentials(request.api_key)
            async with AsyncSession(impersonate="chrome120") as session:
                files_list = []
                if request.image_bytes_list:
                    for img_bytes in request.image_bytes_list:
                        file_url = await self._upload_image(session, signer, token, img_bytes, request.proxy_url)
                        files_list.append({
                            "type": "image",
                            "url": file_url
                        })

                chat_id, parent_id = await self._handshake(
                    session, signer, token, request, 
                    prompt=request.gen_config.prompt, 
                    files=files_list
                )

                return await self._chat(
                    session, signer, token, chat_id, 
                    parent_id=parent_id, 
                    request=request, 
                    files_list=files_list
                )
        except Exception as e:
            error, _ = self.convert_exception(e)
            return Err(error)

    async def _handshake(self, session: AsyncSession, signer: DarkKnightSigner, token: str, request: ApiRequest, prompt: str, files: List[Dict]) -> Tuple[str, str]:
        msg_id = str(uuid.uuid4())
        model = request.preset.model
        ts_ms = int(time.time() * 1000)
        ts_s = int(time.time())

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
            logger.error(f"[Zai Handshake Error] {resp.text}")
            raise PluginError(APIErrorType.SERVER_ERROR, f"Zai 建房失败 ({resp.status_code})")

        resp_data = resp.json()
        chat_id = resp_data.get("id")
        server_current_id = resp_data.get("chat", {}).get("history", {}).get("currentId", msg_id)

        return chat_id, server_current_id

    async def _upload_image(self, session: AsyncSession, signer: DarkKnightSigner, token: str, image_bytes: bytes, proxy: str = None) -> str:
        mime_type = ImageUtils.get_mime_type(image_bytes) or "image/png"
        ext = mime_type.split("/")[-1]
        filename = f"pasted-image-{int(time.time())}.{ext}"

        boundary_str = f"WebKitFormBoundary{secrets.token_hex(16)}"
        boundary = f"----{boundary_str}"

        meta_json = json.dumps({
            "public_access": True,
            "source": "base64_conversion"
        }, separators=(',', ':'))

        part_metadata = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="metadata"\r\n\r\n'
            f'{meta_json}\r\n'
        ).encode('utf-8')

        part_file_head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode('utf-8')

        part_file_tail = b"\r\n"
        part_closing = (f"--{boundary}--\r\n").encode('utf-8')

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

        resp = await session.post(self.ZAI_UPLOAD_URL, headers=headers, data=data_body, proxy=proxy, timeout=60)

        if resp.status_code == 200:
            data = resp.json()
            file_id = data.get("id")
            if not file_id:
                raise ValueError("响应缺失ID")
            return f"/api/v1/files/{file_id}/content/public"
        else:
            raise PluginError(APIErrorType.SERVER_ERROR, f"Zai 图片上传失败 ({resp.status_code})")

    async def _chat(self, session: AsyncSession, signer: DarkKnightSigner, token: str, chat_id: str, parent_id: str, request: ApiRequest, files_list: List[Dict]) -> Result[GenResult, PluginError]:
        message_content_parts = []
        if request.gen_config.prompt:
            message_content_parts.append({"type": "text", "text": request.gen_config.prompt})
        if files_list:
            for file_info in files_list:
                message_content_parts.append({"type": "image_url", "image_url": {"url": file_info.get("url")}})

        messages = [{
            "role": "user",
            "content": message_content_parts
        }]

        target_size = self._map_image_size(request.gen_config.image_size)
        target_ar = self._map_aspect_ratio(request.gen_config.aspect_ratio)

        payload = {}
        
        # 必须分离 Payload 构造
        if request.gen_config.enable_gif:
            # GIF 模式
            logger.info(f"[Zai] 使用浏览器模式 Payload (GIF=True)...")
            request_uuid = str(uuid.uuid4())
            session_id = secrets.token_urlsafe(16)
            
            payload = {
                "id": request_uuid,  
                "background_tasks": {
                    "title_generation": True, 
                    "tags_generation": True, 
                    "follow_up_generation": True
                },
                "features": {
                    "voice": False,
                    "image_generation": False, 
                    "code_interpreter": False,
                    "web_search": False
                },
                "chat_id": chat_id,
                "model": request.preset.model,
                "messages": messages,
                "parent_id": parent_id,
                "session_id": session_id,
                "stream": True,
                "params": {},
                "aspect_ratio": target_ar,
                "image_size": target_size,
                "tool_servers": [],
                "actions": [],
                "filters": [],
                "gifGeneration": True
            }
        else:
            # 普通模式
            logger.info(f"[Zai] 使用精简 API 模式 Payload (GIF=False)...")
            payload = {
                "chat_id": chat_id,
                "model": request.preset.model,
                "messages": messages,
                "stream": True,
                "params": {},
                "image_size": target_size,
                "aspect_ratio": target_ar
            }

        headers = {
            "Authorization": token,
            "x-zai-darkknight": signer.generate_header(),
            "x-zai-fp": json.dumps(signer.fingerprint),
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Origin": "https://zai.is",
            "Referer": "https://zai.is/"
        }

        # 发送请求
        resp = await session.post(
            self.ZAI_COMPLETION_URL, 
            json=payload, 
            headers=headers, 
            proxy=request.proxy_url, 
            stream=True,
            timeout=300
        )

        # GIF -> 轮询
        if request.gen_config.enable_gif:
            logger.info("[Zai] GIF 任务提交成功，进入轮询流程...")
            return await self._poll_chat_history(session, signer, token, chat_id, request.proxy_url, request.preset.model)

        # 普通 -> SSE 流解析
        if resp.status_code != 200:
            logger.error(f"[Zai Chat Error] Status: {resp.status_code} | Body: {resp.text}")
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
            if len(full_content) > 5:
                return Err(PluginError(APIErrorType.SERVER_ERROR, f"未检测到图片链接，Zai 回复: {full_content[:100]}..."))
            return Err(PluginError(APIErrorType.SERVER_ERROR, "Zai 响应为空"))

        image_bytes = await self._download_or_decode(image_url, request.proxy_url)

        return Ok(GenResult(
            images=[image_bytes],
            model_name=request.preset.model,
            finish_reason="success"
        ))

    async def _poll_chat_history(self, session: AsyncSession, signer: DarkKnightSigner, token: str, chat_id: str, proxy: str, model_name: str) -> Result[GenResult, PluginError]:
        poll_url = f"https://zai.is/api/v1/chats/{chat_id}?_t={int(time.time())}"

        logger.info(f"[Zai Poll] 开始轮询 (ChatID: {chat_id})...")

        max_retries = 60
        for i in range(max_retries):
            await asyncio.sleep(5)

            headers = {
                "Authorization": token,
                "x-zai-darkknight": signer.generate_header(),
                "x-zai-fp": json.dumps(signer.fingerprint),
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Origin": "https://zai.is",
                "Referer": "https://zai.is/",
                "Cache-Control": "no-cache"
            }

            try:
                resp = await session.get(poll_url, headers=headers, proxy=proxy, timeout=30)

                if resp.status_code != 200:
                    continue

                data = resp.json()
                chat_data = data.get("chat", {})

                history = chat_data.get("history", {})
                current_id = history.get("currentId")
                msgs_map = history.get("messages", {})

                if not current_id or current_id not in msgs_map:
                    msg_list = chat_data.get("messages", [])
                    last_msg = msg_list[-1] if msg_list else {}
                else:
                    last_msg = msgs_map[current_id]

                role = last_msg.get("role")
                content = last_msg.get("content", "")

                logger.info(f"[Zai Poll] #{i+1} Role: {role} | ContentLen: {len(str(content))}")

                if role == "user":
                    continue

                if last_msg.get("error"):
                    err_body = last_msg.get("error")
                    logger.warning(f"[Zai Poll] 任务报错: {err_body}")
                    continue

                if role == "assistant":
                    target_url = None

                    files = last_msg.get("files", [])
                    if files:
                        for f in files:
                            if "url" in f:
                                target_url = f["url"]
                                logger.info(f"[Zai Poll] 从 files 数组中发现文件: {target_url}")
                                break

                    if not target_url:
                        target_url = self._extract_image_url(content)

                    if not target_url and content.strip().startswith("http"):
                        target_url = content.strip()

                    if target_url:
                        logger.info(f"[Zai Poll] 下载媒体: {target_url}")
                        image_bytes = await self._download_or_decode(target_url, proxy)
                        return Ok(GenResult(images=[image_bytes], model_name=model_name, finish_reason="success"))

            except Exception as e:
                logger.warning(f"[Zai Poll] 异常: {e}")

        return Err(PluginError(APIErrorType.SERVER_ERROR, "GIF 生成超时 (指针未更新或无结果)"))

    async def get_models(self, request: ApiRequest) -> list[str]:
        return ["gemini-3-pro-image-preview"]