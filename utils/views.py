import re
from typing import Any, Dict, List
from ..api_client import APIError, APIErrorType
from ..utils.serializer import ConfigSerializer

class ResponsePresenter:
    """视图层"""

    _ERROR_MESSAGES = {
        APIErrorType.INVALID_ARGUMENT: "💡请求无效 🔧请检查提示词、参数格式。",
        APIErrorType.AUTH_FAILED: "💡鉴权失败 🔧Key可能失效或无权限。",
        APIErrorType.QUOTA_EXHAUSTED: "💡额度耗尽 🔧账户余额不足。",
        APIErrorType.NOT_FOUND: "💡接入错误 🔧模型名或接口地址有误。",
        APIErrorType.RATE_LIMIT: "💡超额请求 🔧当前节点或账户受限。",
        APIErrorType.SERVER_ERROR: "💡网络异常 🔧上游服务波动。",
        APIErrorType.SAFETY_BLOCK: "❌ 安全拦截 🔧内容可能包含敏感信息，请调整提示词。",
        APIErrorType.UNKNOWN: "❌ 未知错误 🔧请检查日志详情。",
    }

    VAR_NAMES = {
        "un": "昵称",
        "uid": "QQ号",
        "age": "年龄",
        "bd": "生日",
        "g": "群名",
        "run": "随机群友",
        "r": "随机选项",
        "rn": "随机数",
        "rl": "随机字母",
        "rc": "随机颜色",
        "d": "日期",
        "t": "时间",
        "wd": "星期",
    }

    VAR_RULES = [
        (
            r"%p(\d*)(?::[^%]*)?%",
            "🔧 填空参数",
            lambda m: f"--p{m}" if m else "--p",  # 特殊处理: 转换成指令参数
            "请在指令后追加对应参数。",
        ),
        (
            r"%(un|uid|age|bd)%",
            "👤 用户信息",
            "VAR_NAMES",  # 使用通用映射
            "默认为发送者，可用 --q @某人 指定获取目标。",
        ),
        (r"%(g|run)%", "👯 群组互动", "VAR_NAMES", None),
        (
            r"%(r|rn|rl|rc)(?::[^%]*)?%",
            "🎲 随机变量",
            "VAR_NAMES",
            "每次生成结果不同。",
        ),
        (r"%(d|t|wd)%", "📅 时间日期", "VAR_NAMES", None),
    ]

    @staticmethod
    def api_error_message(error: APIError, is_master: bool, p: str = "#") -> str:
        hint = ResponsePresenter._ERROR_MESSAGES.get(
            error.error_type, error.raw_message
        )
        status_info = f" (HTTP {error.status_code})" if error.status_code else ""

        parts = [f"❌ 生成失败{status_info}", hint]

        if error.error_type == APIErrorType.UNKNOWN:
            parts.append(f"🔍 详情: {error.raw_message[:100]}...")

        if error.error_type != APIErrorType.SAFETY_BLOCK:
            parts.append(f"👉 如持续失败，请尝试 {p}lmc 切换连接")

        if not is_master:
            parts.append("(本次失败不扣除次数)")

        return "\n".join(parts)

    @staticmethod
    def generating(prompt: str) -> str:
        return f"🎨 正在生成 [{prompt}]..."

    @staticmethod
    def generation_success(
        elapsed: float,
        preset_name: str,
        enhancer_model: str | None = None,
        enhancer_preset: str | None = None,
    ) -> str:
        parts = [f"✅ 生成成功 ({elapsed:.2f}s)", f"连接: {preset_name}"]
        if enhancer_model:
            preset_suffix = f"({enhancer_preset})" if enhancer_preset else ""
            parts.append(f"✨{enhancer_model}{preset_suffix}")
        return " | ".join(parts)

    @staticmethod
    def unauthorized_admin() -> str:
        return "❌ 只有管理员可以执行此操作。"

    @staticmethod
    def item_not_found(item_name: str, key: str) -> str:
        return f"❌ {item_name} [{key}] 不存在。"

    @staticmethod
    def duplicate_item(item_name: str, key: str) -> str:
        return f"❌ {item_name} [{key}] 已存在。"

    @staticmethod
    def _get_rank_icon(index: int) -> str:
        """根据排名返回奖牌图标"""
        medals = ["🥇", "🥈", "🥉"]
        return medals[index] if index < len(medals) else f"NO.{index + 1}"

    @staticmethod
    def stats_dashboard(data: Any, group_id: str = None) -> str:
        msg_parts = []
        if data.checkin_result and data.checkin_result.message:
            msg_parts.append(data.checkin_result.message)

        quota_msg = f"💳 个人剩余: {data.user_count}次"
        if group_id:
            quota_msg += f" | 本群共享: {data.group_count}次"
        msg_parts.append(quota_msg)

        if data.leaderboard_date:
            msg_parts.append(f"\n📊 **今日榜单 ({data.leaderboard_date})**")
            has_data = False

            if data.top_groups:
                lines = ["👥 群组活跃 TOP3:"]
                for i, (gid, c) in enumerate(data.top_groups[:3]):
                    icon = ResponsePresenter._get_rank_icon(i)
                    lines.append(f"{icon} 群{gid}  —  {c}次")
                msg_parts.append("\n".join(lines))
                has_data = True

            if data.top_users:
                lines = ["👤 个人活跃 TOP5:"]
                for i, (uid, c) in enumerate(data.top_users[:5]):
                    icon = ResponsePresenter._get_rank_icon(i)
                    # 保护隐私
                    masked_uid = uid[:3] + "\\*\\*\\*\\*" + uid[-4:] if len(uid) > 7 else uid
                    lines.append(f"{icon} {masked_uid}  —  {c}次")
                msg_parts.append("\n".join(lines))
                has_data = True

            if not has_data:
                msg_parts.append("💤 暂无数据 (快来抢沙发)")

        return "\n\n".join(msg_parts)

    @staticmethod
    def admin_count_modification(
        target: str, count: int, new_total: int, is_group: bool = False
    ) -> str:
        type_str = "群组" if is_group else "用户"
        return f"✅ 已为{type_str} {target} 增加 {count} 次，当前剩余 {new_total} 次。"

    @staticmethod
    def admin_query_result(
        user_id: str, user_count: int, group_id: str = None, group_count: int = 0
    ) -> str:
        reply = f"用户 {user_id} 个人剩余次数为: {user_count}"
        if group_id:
            reply += f"\n本群共享剩余次数为: {group_count}"
        return reply

    @staticmethod
    def connection(is_admin: bool, p: str = "#") -> str:
        lines = [
            "💡 连接管理指令:",
            f"{p}lm连接 (显示列表)",
            f"{p}lm连接 <名称> (查看详情)",
            f"{p}lm连接 to <名称> (切换连接)",
        ]
        if is_admin:
            lines.extend(
                [
                    "🔧 管理员指令:",
                    f"{p}lm连接 add <name> <type> <url> <model> [keys] (添加)",
                    f"{p}lm连接 del <name> (删除)",
                    f"{p}lm连接 ren <旧名> <新名> (重命名)",
                    f"{p}lm连接 debug (调试模式)",
                ]
            )
        return "\n".join(lines)

    @staticmethod
    def format_connection_detail(name: str, data: Dict[str, Any], p: str = "#") -> str:
        keys = data.get("api_keys", [])
        count = len(keys)
        key_info = f"{count} 个" + (f" (请使用 {p}lmk 查看或管理)" if count > 0 else "")

        return (
            f"📝 连接预设 [{name}] 详情:\n"
            f"API 类型: {data.get('api_type')}\n"
            f"API URL: {data.get('api_url')}\n"
            f"模型: {data.get('model')}\n"
            f"Keys: {key_info}"
        )

    @staticmethod
    def format_connection_switch_success(name: str, data: Dict[str, Any]) -> str:
        key_count = len(data.get("api_keys", []))
        return (
            f"✅ 连接已成功切换为 **[{name}]** \n"
            f"API 类型: {data.get('api_type')}\n"
            f"API URL: {data.get('api_url', 'N/A')}\n"
            f"模型: {data.get('model')}\n"
            f"Key 数量: {key_count}"
        )

    @staticmethod
    def format_key_list(name: str, keys: List[str], p: str = "#") -> str:
        if not keys:
            return f"🔑 预设 [{name}] 暂无配置任何 Key。"
        lines = [f"🔑 预设 [{name}] 密钥列表 (共{len(keys)}个):"]
        for i, k in enumerate(keys):
            if len(k) > 12:
                masked_key = f"{k[:8]}......{k[-4:]}"
            else:
                masked_key = k

            lines.append(f"{i + 1}. {masked_key}")
        lines.append(f"\n💡 指令提示: {p}lmk del <预设名> <序号> 删除指定Key")
        return "\n".join(lines)

    @staticmethod
    def key_management(current_preset: str, p: str = "#") -> str:
        return (
            f"🔑 Key 管理指令 (管理员):\n"
            f"{p}lmk [预设名] - 查看指定预设的Key\n"
            f"{p}lmk add <预设名> <Key1> [Key2]... - 添加Key\n"
            f"{p}lmk del <预设名> <序号|all> - 删除Key\n"
            f"注: 当前连接预设为 [{current_preset}]"
        )

    @staticmethod
    def presets_common(item_name: str, cmd_prefix: str, is_admin: bool) -> str:
        lines = [
            f"💡 {item_name}指令格式:",
            f"{cmd_prefix} (显示列表)",
            f"{cmd_prefix} l (简略名录)",
            f"{cmd_prefix} <名称> (查看内容)",
            f"{cmd_prefix} <名称>:<内容> (添加/修改)",
        ]
        if is_admin:
            lines.extend(
                [
                    f"{cmd_prefix} del <名称> (管理员删除)",
                    f"{cmd_prefix} ren <旧名> <新名> (管理员重命名)",
                ]
            )
        return "\n".join(lines)

    @staticmethod
    def debug_info(data: Dict[str, Any], elapsed: float) -> str:
        model_display = data.get("model", "Unknown")

        enhancer = data.get("enhancer_model")
        preset = data.get("enhancer_preset")
        if enhancer:
            preset_info = f"📒{preset}" if preset else ""
            model_display += f"（✨{enhancer}{preset_info}）"

        prompt = data.get("prompt", "")

        return (
            f"【🛠️ 调试模式】\n"
            f"🔗 API: {data.get('api_type')}\n"
            f"🧠 模型: {model_display}\n"
            f"🖼️ 图数: {data.get('image_count', 0)}张\n"
            f"📝 提示词: {prompt}\n\n"
            f"(⏱️ 模拟耗时: {elapsed:.2f}s)"
        )

    @staticmethod
    def main_menu(bnn_cmd: str, p: str = "#") -> str:
        return f"""🍌 【香蕉忍法帖】
--- 🖼️ 生成 ---
● 文生图
  ▸ 指令: {p}lmt <预设名/提示词>
  ▸ 描述: 根据文字描述创作图片
● 图生图 (使用预设)
  ▸ 指令: (发送或引用图片) + {p}<预设名>
  ▸ 描述: 使用预设提示词处理图片
● 图生图 (自定义)
  ▸ 指令: (发送或引用图片) + {p}{bnn_cmd} <提示词>
  ▸ 描述: 根据你的提示词进行创作
‍👩‍👧‍👧<支持处理多图、多@>

--- 📁 预设 ---
● 预设预览/管理
  ▸ 格式:
    {p}lmp 或 {p}lm预设 ▸ 列表预览
    {p}lmo 或 {p}lm优化 ▸ 优化预设预览
  ▸ 通用操作:
    {p}lmp <名称>:<内容> ▸ 添加/覆盖
    {p}lmp del/ren ... ▸ 删除/重命名

--- 🔧 管理 ---
● 综合面板
  ▸ 指令: {p}lm 或 {p}lm次数
  ▸ 描述: 签到获取次数、查看剩余及今日排行
  ▸ 管理参数: 个人/群组次数管理
● 连接管理
  ▸ 指令: {p}lmc 或 {p}lm连接
  ▸ 描述: 查看所有可用的后端模型连接，并可按提示切换。
● 密钥管理 
  ▸ 指令: {p}lmk 或 {p}lm密钥

--- 📚 进阶 ---
发送以下指令查看详细说明👇
{p}lmh 参数 ▸ 查看 --ar, --up, --s, --q 等参数
{p}lmh 变量 ▸ 查看 %un%, %r%, %t% 等动态变量"""

    @staticmethod
    def help_params() -> str:
        return """🛠️ 【忍法·参数破魔】
🤔<在提示词后追加参数调整生成效果>
格式: --参数名 <值>
● 画面比例 (--ar)
  ▸ 示例: --ar 16:9
  ▸ 可选值: 1:1, 2:3, 3:2, 4:3, 3:4, 5:4, 4:5, 16:9, 9:16, 21:9
● 图像尺寸 (--r)
  ▸ 示例: --r 2K
  ▸ 可选值: 1K, 2K, 4K (尺寸越大，耗时越长)
● 联网搜索 (--s)
  ▸ 示例: --s
  ▸ 描述: 允许模型联网搜索以获取更精确的信息，可能会增加不稳定性
● 思维链 (--t)
  ▸ 示例: --t
  ▸ 描述: 开启 Thinking Chain (思维链)，让模型展示思考过程。(仅部分 Google 模型支持)
● 超时时间 (--to)
  ▸ 示例: --to 60
  ▸ 描述: 设置请求超时时间(秒)
● 补充描述 (--a)
  ▸ 示例: --a "拿着花"
  ▸ 描述: 在预设或提示词末尾追加额外描述（支持变量）
● 自定义内容 (--p)
  ▸ 示例: --p 小黎明
  ▸ 描述: 配合支持 %p% 变量的预设使用，可动态插入自定义内容
  ▸ 扩展: 支持 --p2, --p3... 对应预设中的 %p2%, %p3%...
● 指定对象 (--q)
  ▸ 示例: /生日 --q @某人
  ▸ 描述: 将 %un%, %uid%, %age%, %bd% 等变量的获取目标指定为 @ 的用户或特定QQ号
  ▸ 扩展: --q <QQ号>
● 提示词优化 (--up)
  --up ▸ 默认优化 (润色详情)
  --up <优化意见> ▸ 让AI根据你的意见优化提示词
  --up <优化预设名> ▸ 使用特定的提示词优化预设（default、审查等）"""

    @classmethod
    def format_preset_detail(cls, item_name: str, key: str, content: Any) -> str:
        content_str = ConfigSerializer.serialize_pretty(content)
        msg_parts = [f"📝 {item_name} [{key}] 内容:\n{content_str}"]

        if isinstance(content, str):
            hints = []

            for pattern, title, logic, extra_msg in cls.VAR_RULES:
                raw_matches = set(re.findall(pattern, content, re.IGNORECASE))
                if not raw_matches:
                    continue

                clean_items = []
                for m in raw_matches:
                    val = m if isinstance(m, str) else next((x for x in m if x), "")

                    display_text = ""
                    if callable(logic):
                        display_text = logic(val)
                    elif logic == "VAR_NAMES":
                        cn_name = cls.VAR_NAMES.get(val, val)
                        display_text = f"%{val}%({cn_name})"
                    else:
                        display_text = f"%{val}%"

                    clean_items.append(display_text)

                item_str = ", ".join(sorted(clean_items))
                full_hint = f"{title}: 包含 {item_str}。"
                if extra_msg:
                    full_hint += f" {extra_msg}"
                hints.append(full_hint)

            if hints:
                msg_parts.append("\n💡 **变量用法提示**:")
                msg_parts.extend([f"  ▸ {h}" for h in hints])

        return "\n".join(msg_parts)

    @classmethod
    def help_vars(cls) -> str:
        lines = ["🔁 【奥义•缭乱变量杀阵】", "🧙<在提示词、参数a和预设中使用>"]

        help_defs = [
            ("🔧 自定义填空", "配合 --p 使用 (如 %p%→--p, %p2%→--p2)"),
            ("👤 用户信息", "%un%(昵称), %uid%(QQ), %age%(年龄), %bd%(生日)"),
            ("👯 群组互动", "%g%(群名), %run%(随机群友)"),
            ("🎲 随机生成", "%r:A|B%(选项), %rn:1-10%(数字), %rc%(颜色)"),
            ("📅 时间日期", "%d%(日期), %t%(时间), %wd%(星期)"),
        ]

        for cat, desc in help_defs:
            lines.append(f"● {cat}")
            lines.append(f"  {desc}")

        return "\n".join(lines)
