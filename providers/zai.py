import asyncio
import json
import base64
import time
import secrets
import uuid
import re
from pathlib import Path
from typing import Dict, Optional, Tuple, List, Any

# 依赖检查
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

try:
    from playwright.async_api import async_playwright
except ImportError:
    raise ImportError("请安装 playwright: pip install playwright && playwright install chromium")

from astrbot.api import logger
from ..domain import ApiRequest, GenResult, PluginError, APIErrorType
from ..utils import Result, Ok, Err, ImageUtils
from .openai import OpenAIProvider

# ==========================================
# 🔐 核心组件：暗夜骑士签名器
# ==========================================
class DarkKnightSigner:
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
            "fp": self.fingerprint, "nonce": nonce,
            "pk": { "crv": self.jwk.get("crv", "P-256"), "kty": self.jwk.get("kty", "EC"), "x": self.jwk["x"], "y": self.jwk["y"] },
            "ts": ts, "v": 1
        }
        canonical_json = json.dumps(base_payload, separators=(',', ':'), sort_keys=True)
        der_signature = self.private_key.sign(canonical_json.encode('utf-8'), ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der_signature)
        raw_signature = r.to_bytes(32, 'big') + s.to_bytes(32, 'big')
        sig_b64 = base64.urlsafe_b64encode(raw_signature).decode().rstrip('=')
        final_payload = base_payload.copy()
        final_payload["sig"] = sig_b64
        return base64.urlsafe_b64encode(json.dumps(final_payload, separators=(',', ':'), sort_keys=True).encode()).decode().rstrip('=')

# ==========================================
# 🤖 核心组件：凭证自动化管理器
# ==========================================
class ZaiCredentialManager:
    CREDS_FILENAME = "zai_creds.json"

    # 毒药
    POISON_SCRIPT = """
    (() => {
        const target = (typeof window !== 'undefined' ? window.crypto : self.crypto);
        if (!target || !target.subtle) return;
        const originalGenerate = target.subtle.generateKey;
        target.subtle.generateKey = async function(algo, extractable, usages) {
            return originalGenerate.call(this, algo, true, usages);
        };
    })();
    """

    # 提取
    DB_EXTRACT_SCRIPT = """
    async () => {
        return new Promise((resolve) => {
            const req = indexedDB.open("darkknight");
            req.onerror = () => resolve({status: "error", msg: "无法打开数据库"});
            req.onsuccess = (e) => {
                const db = e.target.result;
                if (!db.objectStoreNames.contains("keys")) { resolve({status: "pending", msg: "等待 keys 表..."}); return; }
                const tx = db.transaction(["keys"], "readonly");
                tx.objectStore("keys").get("current_keypair").onsuccess = (evt) => {
                    const res = evt.target.result;
                    if (!res) { resolve({status: "pending", msg: "等待 Key 生成..."}); return; }
                    let targetKey = res.privateKey || (res.keyPair && res.keyPair.privateKey);
                    if (targetKey && targetKey.extractable) {
                        window.crypto.subtle.exportKey("jwk", targetKey).then(jwk => {
                            resolve({status: "success", jwk: jwk});
                        });
                    } else { resolve({status: "pending", msg: "Key 不可导出"}); }
                };
            };
        });
    }
    """

    def __init__(self, data_dir: str):
        self._lock = asyncio.Lock()
        self._mem_cache: Optional[Dict] = None
        self._bg_task: Optional[asyncio.Task] = None

        base_path = Path(data_dir)
        base_path.mkdir(parents=True, exist_ok=True)
        self.creds_path = base_path / self.CREDS_FILENAME
        logger.info(f"[ZaiAuth] 凭证文件路径: {self.creds_path}")

    def _get_restore_script(self, jwk_data: Dict) -> str:
        """[Step 2] 还原脚本"""
        return f"""
        (async () => {{
            const keyData = {json.dumps(jwk_data)};
            try {{
                const privateKey = await crypto.subtle.importKey("jwk", keyData, {{ name: "ECDSA", namedCurve: "P-256" }}, true, ["sign"]);
                const pubData = {{ ...keyData }};
                delete pubData.d; delete pubData.key_ops;
                const publicKey = await crypto.subtle.importKey("jwk", pubData, {{ name: "ECDSA", namedCurve: "P-256" }}, true, ["verify"]);

                const req = indexedDB.open("darkknight");
                req.onsuccess = (e) => {{
                    const db = e.target.result;
                    if (!db.objectStoreNames.contains("keys")) return;
                    const tx = db.transaction(["keys"], "readwrite");
                    tx.objectStore("keys").put({{
                        id: "current_keypair",
                        keyPair: {{ privateKey, publicKey }},
                        publicKeyJwk: pubData
                    }});
                    window.INJECTION_STATUS = "SUCCESS";
                }};
            }} catch(e) {{}}
        }})();
        """

    async def get_credentials(self, discord_token: str, force_refresh: bool = False) -> Tuple[DarkKnightSigner, str]:
        # 1. 后台保活
        if discord_token and not self._bg_task:
            self._bg_task = asyncio.create_task(self._auto_refresh_loop(discord_token))
            logger.info("[ZaiAuth] 已启动 12小时自动保活任务")

        # 2. 获取流程
        if not force_refresh:
            if self._mem_cache: return self._parse_creds(self._mem_cache)
            if self.creds_path.exists():
                try:
                    data = json.loads(self.creds_path.read_text(encoding="utf-8"))
                    self._mem_cache = data
                    return self._parse_creds(data)
                except: pass

        if not discord_token: raise PluginError(APIErrorType.AUTH_FAILED, "未配置 Discord Token")

        logger.info(f"[ZaiAuth] 正在启动浏览器自动化流程 (Step 1+2)...")

        async with self._lock:
            # 双重检查缓存
            if not force_refresh and self._mem_cache: return self._parse_creds(self._mem_cache)

            jwk = await self._step_1_extract_key()
            final_creds = await self._step_2_login_clean(jwk, discord_token)

            self.creds_path.write_text(json.dumps(final_creds), encoding="utf-8")
            self._mem_cache = final_creds
            logger.info(f"[ZaiAuth] 凭证更新成功")

            return self._parse_creds(final_creds)

    # 自动刷新
    async def _auto_refresh_loop(self, discord_token: str):
        try:
            while True:
                await asyncio.sleep(12 * 3600)

                logger.info("[ZaiAuth] 触发定时保活任务 (12h)...")
                try:
                    await self.get_credentials(discord_token, force_refresh=True)
                    logger.info("[ZaiAuth] 定时保活成功")
                except Exception as e:
                    logger.error(f"[ZaiAuth] 定时保活失败: {e}")
                    # 失败等待下个周期或请求
        except asyncio.CancelledError:
            logger.info("[ZaiAuth] 自动保活任务已停止")

    # 资源回收
    async def shutdown(self):
        if self._bg_task:
            logger.info("[ZaiAuth] 正在取消后台任务...")
            self._bg_task.cancel()
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass
            self._bg_task = None

    def _parse_creds(self, data: Dict) -> Tuple[DarkKnightSigner, str]:
        signer = DarkKnightSigner(data["private_key"], data["fingerprint"])
        return signer, data["token"]

    async def _step_1_extract_key(self) -> Dict:
        """步骤一：提取私钥 (复刻 zai_creds.py Step 1)"""
        logger.info("[ZaiAuth] Step 1: 提取私钥...")
        jwk = None

        async with async_playwright() as p:
            # 这里可 headless
            browser = await p.chromium.launch(
                headless=True, 
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
            )

            context = await browser.new_context()
            await context.add_init_script(self.POISON_SCRIPT)

            page = await context.new_page()
            try:
                await page.goto("https://zai.is", timeout=45000)
            except: pass

            await page.evaluate("""try { indexedDB.deleteDatabase("darkknight"); localStorage.clear(); } catch(e){}""")
            await page.reload()

            for i in range(20):
                res = await page.evaluate(self.DB_EXTRACT_SCRIPT)
                if res.get("status") == "success":
                    jwk = res.get("jwk")
                    break
                await asyncio.sleep(1.5)

            await browser.close()

        if not jwk:
            raise PluginError(APIErrorType.AUTH_FAILED, "Step 1 Failed: 私钥提取失败")
        return jwk

    async def _step_2_login_clean(self, jwk: Dict, discord_token: str) -> Dict:
        """步骤二：登录认证 (已增加 Discord 授权页自动滚动逻辑)"""
        logger.info("[ZaiAuth] Step 2: 登录认证 (启动窗口)...")
        creds = {"token": None, "fp": None}
        clean_token = discord_token.replace("Bearer ", "").strip()

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True, 
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
            )
            context = await browser.new_context()

            # 监听
            async def on_request(req):
                if "zai.is" not in req.url: return
                auth = req.headers.get("authorization")
                if auth and "Bearer ey" in auth and not creds["token"]:
                    creds["token"] = auth
                dk = req.headers.get("x-zai-darkknight")
                if dk and not creds["fp"]:
                    try:
                        payload = dk.split('.')[0] + '=' * (-len(dk.split('.')[0]) % 4)
                        decoded = json.loads(base64.urlsafe_b64decode(payload).decode())
                        creds["fp"] = decoded.get("fp")
                    except: pass
            context.on("request", on_request)

            page = await context.new_page()

            try:
                # 1. 打开首页
                await page.goto("https://zai.is", timeout=60000)
                await asyncio.sleep(3)

                # 2. 注入 Key
                restore_js = self._get_restore_script(jwk)
                for _ in range(3):
                    await page.evaluate(restore_js)
                    status = await page.evaluate("window.INJECTION_STATUS")
                    if status == "SUCCESS": break
                    await asyncio.sleep(1)

                logger.info("[ZaiAuth] 私钥注入完成")

                # 3. 点击登录
                if "login" not in page.url:
                    try:
                        btns = page.locator("button", has_text=re.compile(r"(Discord|Log in|Login)"))
                        if await btns.count() > 0:
                            logger.info("[ZaiAuth] 点击登录按钮")
                            await btns.first.click()
                            await asyncio.sleep(2)
                        else:
                            await page.goto("https://zai.is/login")
                    except:
                        await page.goto("https://zai.is/login")

                # 4. 自动化 Discord 登录 & 授权
                logger.info("[ZaiAuth] 等待 Discord 跳转...")
                for i in range(40):
                    try:
                        url = page.url

                        if "discord.com" in url:
                            # === A. 注入 Token ===
                            if "login" in url:
                                js = f"""
                                (() => {{
                                    const token = "{clean_token}";
                                    const iframe = document.createElement('iframe');
                                    document.body.appendChild(iframe);
                                    iframe.contentWindow.localStorage.token = `"${{token}}"`;
                                    setTimeout(() => {{ window.location.reload(); }}, 300);
                                }})();
                                """
                                try: await page.evaluate(js)
                                except: pass
                                await asyncio.sleep(4) # 等待刷新

                            # === B. 授权页 ===
                            if "authorize" in url or "oauth2" in url:
                                logger.info("[ZaiAuth] 检测到授权页，尝试滚动...")
                                try:
                                    # 1. 滚动
                                    await page.click("body") # 获取焦点
                                    for _ in range(5):
                                        await page.keyboard.press("PageDown")
                                        await asyncio.sleep(0.1)
                                    await page.keyboard.press("End") # 滚到底部
                                    await asyncio.sleep(1)

                                    # 2. 寻找授权按钮
                                    auth_btn = page.locator("button", has_text=re.compile(r"(Authorize|授权)"))

                                    if await auth_btn.count() > 0:
                                        btn = auth_btn.last # 通常是最后一个按钮
                                        # 等待按钮变绿/可点击
                                        if await btn.is_enabled():
                                            logger.info("[ZaiAuth] 点击授权按钮")
                                            await btn.click()
                                        else:
                                            # 如果还是不可点，再次尝试强制JS滚动
                                            logger.info("[ZaiAuth] 按钮仍未激活，尝试强制滚动...")
                                            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                                            await asyncio.sleep(1)
                                            await btn.click(force=True)
                                except Exception as e:
                                    logger.debug(f"[ZaiAuth] 授权页操作微调: {e}")

                        elif "zai.is" in url:
                            # 补救读取
                            if not creds["token"]:
                                t = await page.evaluate("""
                                    (() => {
                                        try {
                                            for(let i=0; i<localStorage.length; i++) {
                                                let k = localStorage.key(i);
                                                let v = localStorage.getItem(k);
                                                if(v.includes('access_token')) {
                                                    let j = JSON.parse(v);
                                                    if(j.access_token) return 'Bearer ' + j.access_token;
                                                }
                                                if(v.startsWith('eyJ')) return 'Bearer ' + v;
                                            }
                                        } catch(e){}
                                        return null;
                                    })()
                                """)
                                if t: creds["token"] = t

                    except Exception as e:
                        # 忽略过程报错，继续重试
                        pass

                    if creds["token"] and creds["fp"]:
                        break
                    await asyncio.sleep(2)

            except Exception as e:
                logger.error(f"[ZaiAuth] Step 2 Error: {e}")
            finally:
                await browser.close()

        if not creds["token"]:
            raise PluginError(APIErrorType.AUTH_FAILED, "Step 2 Failed: Token 获取失败")

        return {
            "private_key": jwk,
            "token": creds["token"],
            "fingerprint": creds["fp"] or {"c": "default", "wgl": "default"}
        }


# ==========================================
# ⚡ Zai Provider 实现
# ==========================================
class ZaiProvider(OpenAIProvider):
    # API 接口
    ZAI_NEW_CHAT_URL = "https://zai.is/api/v1/chats/new"
    ZAI_COMPLETION_URL = "https://zai.is/api/chat/completions"
    ZAI_UPLOAD_URL = "https://zai.is/api/v1/files/"

    def __init__(self, session, data_dir: str):
        super().__init__(session)
        self.cred_manager = ZaiCredentialManager(data_dir)

    def _map_image_size(self, size_str: str) -> str:
        s = size_str.upper()
        if "4K" in s: return "4K"
        if "2K" in s: return "2K"
        return "1K"

    def _map_aspect_ratio(self, ar_str: str) -> str:
        if not ar_str or ar_str == "default": return "dynamic"
        valid_ratios = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9", "dynamic"]
        return ar_str if ar_str in valid_ratios else "dynamic"

    async def generate(self, request: ApiRequest) -> Result[GenResult, PluginError]:
        # 获取 Discord Token
        discord_token = request.api_key

        # 核心生成逻辑
        async def _do_generate(signer: DarkKnightSigner, token: str):
            async with AsyncSession(impersonate="chrome120") as session:
                files_list = []
                if request.image_bytes_list:
                    for img_bytes in request.image_bytes_list:
                        file_url = await self._upload_image(session, signer, token, img_bytes, request.proxy_url)
                        files_list.append({"type": "image", "url": file_url})

                chat_id, parent_id = await self._handshake(
                    session, signer, token, request, 
                    prompt=request.gen_config.prompt, files=files_list
                )
                return await self._chat(
                    session, signer, token, chat_id, 
                    parent_id=parent_id, request=request, files_list=files_list
                )

        current_signer = None
        current_token = None

        for attempt in range(2):
            try:
                # 1. 获取凭证
                if current_signer is None:
                    # 首次获取
                    current_signer, current_token = await self.cred_manager.get_credentials(discord_token, force_refresh=False)

                # 2. 执行生成
                return await _do_generate(current_signer, current_token)

            except Exception as e:
                error, is_retryable = self.convert_exception(e)

                if attempt == 1:
                    return Err(error)

                # --- 错误 & 重试 ---

                # 认证失效 (401/403) -> 强制刷新凭证并重试
                if error.error_type == APIErrorType.AUTH_FAILED and discord_token:
                    logger.warning(f"[Zai] 认证失效 ({error.message})，正在自动刷新并重试...")
                    try:
                        current_signer, current_token = await self.cred_manager.get_credentials(discord_token, force_refresh=True)
                        await asyncio.sleep(2) 
                        continue
                    except Exception as refresh_e:
                        return Err(self.convert_exception(refresh_e)[0])

                # 服务器错误
                # 刚获取完凭证时容易遇到"响应为空"，此时凭证其实是有效的，只是连接不稳
                if error.error_type == APIErrorType.SERVER_ERROR:
                    logger.warning(f"[Zai] 服务器连接不稳定 ({error.message})，正在立即重试...")
                    await asyncio.sleep(2)
                    continue

                return Err(error)

        return Err(PluginError(APIErrorType.UNKNOWN, "重试次数超限"))

    # === 生图逻辑 ===

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

        # 分离 Payload 构造逻辑
        if request.gen_config.enable_gif:
            # GIF (全量)
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
            # 普通 (精简)
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
            stream=True,  # <--- 必须开启
            timeout=300
        )

        # GIF -> 强制轮询
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

        logger.warning(f"[Zai Poll] 开始轮询 (ChatID: {chat_id})...")

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

    async def terminate(self):
        if self.cred_manager:
            await self.cred_manager.shutdown()
        logger.info("[ZaiProvider] 资源已释放")