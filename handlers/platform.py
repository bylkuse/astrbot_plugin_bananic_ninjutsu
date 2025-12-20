import asyncio
import base64
import re
import math
import pathlib
from typing import Any, List, Optional, Union, Set
from urllib.parse import urlparse, parse_qs

from astrbot.api import logger
from astrbot.api.platform import At, Image, Reply, Plain, Node, Nodes
from astrbot.api.event import AstrMessageEvent, MessageChain

class PlatformAdapter:
    _recall_tasks: Set[asyncio.Task] = set()
    def __init__(self, event: AstrMessageEvent):
        self.event = event
        self.bot = event.bot

    @property
    def message_str(self) -> str:
        return self.event.message_str

    @property
    def sender_id(self) -> str:
        return self.event.get_sender_id()

    @property
    def group_id(self) -> str:
        return self.event.get_group_id()

    async def send_text(self, text: str) -> Optional[Union[int, str]]:
        is_long_text = len(text) > 200 or text.count('\n') > 5
        if is_long_text:
            sent_ids = await self.send_text_as_nodes([text])
            if sent_ids:
                return sent_ids[0]
            return None
        return await self.send_payload(self.event.plain_result(text))

    def get_sender_avatar_url(self) -> Optional[str]:
        def _pick(data: Any) -> Optional[str]:
            if not isinstance(data, dict): return None
            for key in ["avatar", "avatar_url", "user_avatar", "head_image", "url"]:
                url = data.get(key)
                if isinstance(url, str) and url.startswith(("http", "https")):
                    return url
            if isinstance(data.get("data"), dict):
                if res := _pick(data["data"]): return res
            if isinstance(data.get("sender"), dict):
                if res := _pick(data["sender"]): return res
            return None

        if hasattr(self.event, "message_obj"):
            sender = getattr(self.event.message_obj, "sender", None)
            if sender:
                sender_data = sender.__dict__ if hasattr(sender, "__dict__") else sender
                if url := _pick(sender_data): return url

            raw = getattr(self.event.message_obj, "raw_message", None)
            if isinstance(raw, dict):
                if url := _pick(raw): return url

        uid = self.sender_id
        if uid and uid.isdigit():
            return f"https://q1.qlogo.cn/g?b=qq&nk={uid}&s=640"

        return None

    def get_image_sources(self) -> List[str]:
        sources = []
        if not hasattr(self.event.message_obj, "message"): 
            return sources

        def _extract_from_seg(seg):
            # 图片组件
            if isinstance(seg, Image):
                if seg.url: sources.append(seg.url)
                elif seg.file: sources.append(seg.file)

            # 回复
            elif isinstance(seg, Reply) and seg.chain:
                for sub_seg in seg.chain:
                    _extract_from_seg(sub_seg)

            # 头像
            elif isinstance(seg, At):
                if str(seg.qq).isdigit():
                    sources.append(f"https://q1.qlogo.cn/g?b=qq&nk={seg.qq}&s=640")

        for seg in self.event.message_obj.message:
            _extract_from_seg(seg)

        return sources

    async def send_payload(self, payload: Any) -> Optional[Union[int, str]]:
        if hasattr(self.event, "_parse_onebot_json") and hasattr(self.bot, "call_action"):
            try:
                chain = payload.chain if hasattr(payload, "chain") else payload
                if not isinstance(chain, list):
                    chain = [chain]

                nodes_component = next((x for x in chain if isinstance(x, Nodes)), None)
                obmsg = None

                if nodes_component:
                    obmsg = []
                    for node in nodes_component.nodes:
                        inner_chain = MessageChain(chain=node.content)
                        inner_ob_msg = await self.event._parse_onebot_json(inner_chain)

                        obmsg.append({
                            "type": "node",
                            "data": {
                                "name": node.name,
                                "uin": str(node.uin),
                                "content": inner_ob_msg
                            }
                        })
                else:
                    msg_chain = MessageChain(chain=chain)
                    obmsg = await self.event._parse_onebot_json(msg_chain)

                params = {"message": obmsg}
                if gid := self.event.get_group_id():
                    params["group_id"] = int(gid)
                    action = "send_group_msg"
                elif uid := self.event.get_sender_id():
                    params["user_id"] = int(uid)
                    action = "send_private_msg"
                else:
                    raise ValueError("无法确定发送目标")

                resp = await self.bot.call_action(action, **params)
                return self._extract_message_id(resp)

            except Exception as e:
                if not nodes_component:
                    pass
                else:
                    logger.error(f"[PlatformAdapter] OneBot 合并转发发送失败: {e}")

        try:
            resp = await self.event.send(payload)
            return self._extract_message_id(resp)
        except Exception as e:
            logger.error(f"[PlatformAdapter] 发送消息失败: {e}")
            return None

    async def fetch_user_name(self, user_id: str) -> str:
        # 本人
        if user_id == self.sender_id:
            if name := self.event.get_sender_name():
                return name

        # 群名片
        if self.group_id and hasattr(self.bot, "get_group_member_info"):
            try:
                info = await self.bot.get_group_member_info(
                    group_id=int(self.group_id), 
                    user_id=int(user_id), 
                    no_cache=True
                )
                if name := (info.get("card") or info.get("nickname")):
                    return name
            except Exception:
                pass

        # 陌生人
        if hasattr(self.bot, "get_stranger_info"):
            try:
                info = await self.bot.get_stranger_info(user_id=int(user_id), no_cache=True)
                if name := info.get("nickname"):
                    return name
            except Exception:
                pass

        return user_id

    async def send_text_as_nodes(self, lines: List[str], header: str = "") -> List[Union[int, str]]:
        if not lines: return []

        bot_uin = "10000"
        if hasattr(self.event, "message_obj") and hasattr(self.event.message_obj, "self_id"):
            bot_uin = self.event.message_obj.self_id
        if not bot_uin:
            bot_uin = "10000"

        bot_name = "AstrBot"
        try:
            fetched_name = await self.fetch_user_name(bot_uin)
            if fetched_name != bot_uin:
                bot_name = fetched_name
        except Exception as e:
            logger.debug(f"[PlatformAdapter] 获取 Bot 昵称失败: {e}")

        CHUNK_LIMIT = 2500 
        all_nodes = []
        current_chunk_lines = []
        current_length = 0

        for line in lines:
            line_len = len(line) + 1
            if current_length + line_len > CHUNK_LIMIT:
                if current_chunk_lines:
                    text_content = "\n".join(current_chunk_lines)
                    all_nodes.append(Node(uin=bot_uin, name=bot_name, content=[Plain(text_content)]))
                current_chunk_lines = [line]
                current_length = line_len
            else:
                current_chunk_lines.append(line)
                current_length += line_len

        if current_chunk_lines:
            text_content = "\n".join(current_chunk_lines)
            all_nodes.append(Node(uin=bot_uin, name=bot_name, content=[Plain(text_content)]))

        if not all_nodes: return []

        BATCH_SIZE = 4
        total_batches = math.ceil(len(all_nodes) / BATCH_SIZE)
        sent_msg_ids = []

        for i in range(total_batches):
            start_idx = i * BATCH_SIZE
            end_idx = start_idx + BATCH_SIZE
            batch_nodes = all_nodes[start_idx:end_idx]

            current_header = header
            if total_batches > 1 and header:
                current_header = f"{header} ({i+1}/{total_batches})"

            if current_header:
                batch_nodes.insert(0, Node(uin=bot_uin, name=bot_name, content=[Plain(current_header)]))

            try:
                nodes_container = Nodes(nodes=batch_nodes)
                payload = self.event.chain_result([nodes_container])

                msg_id = await self.send_payload(payload)
                if msg_id:
                    sent_msg_ids.append(msg_id)

                if i < total_batches - 1:
                    await asyncio.sleep(1.0)
            except Exception as e:
                logger.error(f"[PlatformAdapter] 发送第 {i+1} 批合并转发失败: {e}")
                fallback_id = await self.send_text(f"【{current_header}】(发送失败转文本)\n...")
                if fallback_id:
                    sent_msg_ids.append(fallback_id)

        return sent_msg_ids

    async def fetch_group_name(self) -> str:
        gid = self.group_id
        if not gid: 
            return "私聊"
        try:
            if hasattr(self.bot, "get_group_info"):
                info = await self.bot.get_group_info(group_id=int(gid))
                return info.get("group_name", str(gid))
        except Exception:
            pass
        return str(gid)

    def resolve_target_user_id(self, parsed_params: dict) -> Optional[str]:
        # 窗口扫描
        chain_target = self._scan_chain_for_target()
        if chain_target:
            return chain_target

        # Parser 提取
        raw_q = parsed_params.get("target_user_id")
        if isinstance(raw_q, str):
            if match := re.search(r"(\d+)", raw_q):
                return match.group(1)

        # 兜底
        if raw_q is True:
            if hasattr(self.event, "message_obj") and hasattr(self.event.message_obj, "message"):
                for seg in self.event.message_obj.message:
                    if isinstance(seg, At):
                        return str(seg.qq)

        return None

    def _scan_chain_for_target(self) -> Optional[str]:
        if not hasattr(self.event, "message_obj") or not hasattr(self.event.message_obj, "message"):
            return None

        segments = list(self.event.message_obj.message)

        # 定位 --q
        start_index = -1
        start_offset = 0

        for i, seg in enumerate(segments):
            if isinstance(seg, Plain):
                matches = list(re.finditer(r"(?<!\w)--q(?!\w)", seg.text))
                if matches:
                    match = matches[-1]
                    start_index = i
                    start_offset = match.end()

        if start_index == -1:
            return None

        # 向后扫描
        for i in range(start_index, len(segments)):
            seg = segments[i]

            # At 组件
            if isinstance(seg, At):
                return str(seg.qq)

            if isinstance(seg, Plain):
                text = seg.text
                scan_text = text[start_offset:] if i == start_index else text

                # 检查 Flag 边界
                flag_match = re.search(r"(?<!\w)--[a-zA-Z]", scan_text)
                limit_index = flag_match.start() if flag_match else len(scan_text)

                scope_text = scan_text[:limit_index]

                # CQ:at
                if match := re.search(r"\[CQ:at,.*?qq=(\d+)", scope_text):
                    return match.group(1)
                # 数字
                if match := re.search(r"\b(\d{5,})\b", scope_text):
                    return match.group(1)
                if flag_match:
                    return None

        return None

    def schedule_recall(self, message_id: Union[int, str, None], delay: int = 120):
        if not message_id: return
        task = asyncio.create_task(self._recall_task(message_id, delay))
        PlatformAdapter._recall_tasks.add(task)
        task.add_done_callback(PlatformAdapter._recall_tasks.discard)

    async def _recall_task(self, message_id, delay):
        try:
            await asyncio.sleep(delay)
            await self.recall_message(message_id)
        except Exception as e:
            logger.debug(f"[PlatformAdapter] 自动撤回异常: {e}")

    async def recall_message(self, message_id: Union[int, str, None]):
        if not message_id: return

        try:
            if hasattr(self.bot, "delete_msg"):
                await self.bot.delete_msg(message_id=message_id)
            elif hasattr(self.bot, "recall_message"):
                try:
                    await self.bot.recall_message(int(message_id))
                except (ValueError, TypeError):
                    logger.debug(f"[PlatformAdapter] recall_message 不支持 ID: {message_id}")
            else:
                logger.debug(f"[PlatformAdapter] 未找到撤回方法")
        except Exception as e:
            logger.debug(f"[PlatformAdapter] 撤回消息 {message_id} 失败: {e}")

    @staticmethod
    def _extract_message_id(resp: Any) -> Optional[Union[int, str]]:
        if not resp: return None

        if isinstance(resp, (int, str)):
            return resp

        if isinstance(resp, dict):
            data = resp.get("data")
            if isinstance(data, dict):
                if "message_id" in data: return data["message_id"]
                if "res_id" in data: return data["res_id"]
                if "forward_id" in data: return data["forward_id"]

            if "message_id" in resp: return resp["message_id"]

        if hasattr(resp, "message_id"):
            return resp.message_id

        return None

    async def fetch_onebot_image(self, file_param: str) -> Optional[bytes]:
        if not hasattr(self.bot, "call_action"): return None

        payloads = []

        try:
            parsed = urlparse(file_param)
            qs = parse_qs(parsed.query or "")
            if "fileid" in qs and qs["fileid"]: 
                payloads.append({"file": qs["fileid"][0]})
            elif "file" in qs and qs["file"]:
                payloads.append({"file": qs["file"][0]})
        except: pass

        payloads.append({"file": file_param})

        for payload in payloads:
            try:
                resp = await self.bot.call_action("get_image", **payload)
                if isinstance(resp, dict):
                    # Base64
                    if base64_str := resp.get("base64"): 
                        return base64.b64decode(base64_str)

                    # 本地路径
                    if file_path := resp.get("file"):
                        path_obj = pathlib.Path(file_path)
                        if path_obj.exists() and path_obj.is_file(): 
                            return await asyncio.to_thread(path_obj.read_bytes)

                    # URL
                    if url := resp.get("url"):
                        if url != file_param and url.startswith("http"):
                            return None

            except Exception as e:
                continue
        return None