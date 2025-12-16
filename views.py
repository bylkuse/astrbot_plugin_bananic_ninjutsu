import re
from typing import Any, Dict, List, Optional, Tuple

from .domain.prompt import VariableDefinition
from .domain.model import (
    PluginError, APIErrorType, 
    GenResult, ApiRequest, 
    ConnectionPreset, UserQuota
)

class ResponsePresenter:
    _ERROR_MESSAGES = {
        APIErrorType.INVALID_ARGUMENT: "💡 请求无效\n🔧 检查提示词、参数格式。",
        APIErrorType.AUTH_FAILED: "💡 鉴权失败\n🔧 Key可能失效或无权限。",
        APIErrorType.QUOTA_EXHAUSTED: "💡 额度耗尽\n🔧 余额不足或Key冷却中。",
        APIErrorType.NOT_FOUND: "💡 接入错误\n🔧 模型名或接口有误。",
        APIErrorType.RATE_LIMIT: "💡 超额请求\n🔧 节点或账户暂时受限。",
        APIErrorType.SERVER_ERROR: "💡 网络异常\n🔧 上游服务波动。",
        APIErrorType.SAFETY_BLOCK: "❌ 安全拦截\n🔧 内容包含敏感信息。",
        APIErrorType.DEBUG_INFO: "🛠️ 调试信息",
        APIErrorType.UNKNOWN: "❌ 未知错误\n🔧 请检查日志详情。",
    }

    @staticmethod
    def make_preview(text: str, limit: int = 50, oneline: bool = False) -> str:
        if not text:
            return ""

        target = str(text)
        if oneline:
            target = target.replace("\n", " ").strip()

        if len(target) > limit:
            return target[:limit] + "..."
        return target

    @staticmethod
    def unauthorized_admin() -> str:
        return "❌ 只有管理员可以执行此操作。"

    @staticmethod
    def api_error_message(error: PluginError, is_admin: bool, p: str = "#") -> str:
        hint = ResponsePresenter._ERROR_MESSAGES.get(error.error_type, error.message)
        status_info = f" (HTTP {error.status_code})" if error.status_code else ""

        parts = [f"❌ 生成失败{status_info}", hint]

        detail = ResponsePresenter.make_preview(error.message, limit=100, oneline=True)
        if error.error_type == APIErrorType.UNKNOWN:
            parts.append(f"🔍 详情: {detail}")
        elif is_admin and error.error_type != APIErrorType.SAFETY_BLOCK:
            parts.append(f"🔍 详情: {detail}")

        if error.error_type != APIErrorType.SAFETY_BLOCK:
            parts.append(f"👉 如持续失败，请尝试 {p}lmc 切换连接")

        if not is_admin:
            parts.append("(本次失败不扣除次数)")

        return "\n".join(parts)

    @staticmethod
    def _get_stream_icon(val: Optional[bool]) -> str:
        if val is True: return "🌊"
        if val is False: return "🛑"
        return "🤖"

    @staticmethod
    def _fmt_stream(val: Optional[bool]) -> str:
        icon = ResponsePresenter._get_stream_icon(val)
        if val is True: return f"{icon} 开启 (强制流式)"
        if val is False: return f"{icon} 关闭 (完整响应)"
        return f"{icon} 自动 (默认策略)"

    @staticmethod
    def generating(prompt: str) -> str:
        display_prompt = ResponsePresenter.make_preview(prompt, limit=20, oneline=True)
        return f"🎨 正在生成 [{display_prompt}]..."

    @staticmethod
    def generation_success(
        result: GenResult,
        request: ApiRequest,
        cost: int,
        quota: Optional[UserQuota],
        group_balance: int = 0,
        preset_name: Optional[str] = None
    ) -> str:
        model_name = result.model_name
        clean_model = model_name.split("/")[-1] if "/" in model_name else model_name

        # 模型 & 优化器
        line1 = f"🚀 {clean_model}"
        line_enhancer = ""
        if result.enhancer_model:
            clean_em = result.enhancer_model.split("/")[-1] if "/" in result.enhancer_model else result.enhancer_model
            instr = result.enhancer_instruction or "Default"
            line_enhancer = f"✨ {clean_em} ({instr})"

        # 连接 & 策略 & 耗时
        conn_name = request.preset.name
        s_icon = ResponsePresenter._get_stream_icon(request.preset.stream)
        display_strategy = preset_name if preset_name else "自定义"
        line2 = f"🔗 [{conn_name}{s_icon}] · 🎨 {display_strategy} · ⏱️{result.cost_time:.1f}s"

        # Prompt 预览
        clean_prompt = request.gen_config.prompt.replace("\n", " ").strip()
        preview = ResponsePresenter.make_preview(request.gen_config.prompt, limit=25, oneline=True)
        line3 = f"📝 {preview}"

        # 规格 & 配额
        ar = request.gen_config.aspect_ratio
        ar_str = ar if ar != "default" else "自动"
        sz_str = request.gen_config.image_size
        specs = f"📐 {ar_str} · 📏 {sz_str}"

        user_rem = quota.remaining if quota else 0
        quota_str = f"-{cost} 👤 {user_rem}"
        if group_balance > 0:
            quota_str += f" · 👥 {group_balance}"
        line4 = f"{specs}\n💳 {quota_str}"

        # 组装
        parts = [line1]
        if line_enhancer:
            parts.append(line_enhancer)
        parts.extend([line2, line3, line4])
        return "\n".join(parts)

    @staticmethod
    def debug_info(error: PluginError) -> str:
        data = error.raw_data or {}

        # 1. API Type & Preset
        api_type = data.get("api_type", "Unknown")
        preset_name = data.get("preset_name", "Unknown")
        stream_val = data.get("stream") 
        s_icon = ResponsePresenter._get_stream_icon(stream_val)

        # 2. Model & Enhancer
        model = data.get("model", "Unknown")
        enhancer_model = data.get("enhancer_model")
        enhancer_preset = data.get("enhancer_preset")
        enhancer_info = ""
        if enhancer_model:
            e_info = f"[{enhancer_preset}]" if enhancer_preset else ""
            enhancer_info = f"\n✨  {enhancer_model} {e_info}"

        # 3. Prompt
        prompt = data.get("prompt", "")
        if not prompt:
            prompt = "(无提示词)"

        # 4. Images
        img_count = data.get("image_count", 0)

        return (
            f"【🛠️ 调试模式】\n"
            f"🚀  {model}{enhancer_info}\n"
            f"🔗  {api_type} [{preset_name}{s_icon}]\n"
            f"📝  {prompt}\n"
            f"🖼️  {img_count}\n"
            f"⛔  (未发送至服务器💳-0)"
        )

    @staticmethod
    def _get_rank_icon(index: int) -> str:
        medals = ["🥇", "🥈", "🥉"]
        return medals[index] if index < len(medals) else f"NO.{index + 1}"

    @staticmethod
    def stats_dashboard(
        user_quota: UserQuota,
        group_balance: int,
        checkin_result: Optional[Tuple[bool, int, str]],
        leaderboard: Dict[str, Any]
    ) -> str:
        msg_parts = []
        if checkin_result:
            _, _, msg = checkin_result
            msg_parts.append(msg)
        quota_msg = f"💳 个人剩余: {user_quota.remaining}次"
        if group_balance > 0:
            quota_msg += f" | 本群共享: {group_balance}次"
        msg_parts.append(quota_msg)
        date = leaderboard.get("date", "Unknown")
        msg_parts.append(f"\n📊 **今日榜单 ({date})**")
        has_data = False
        top_groups = leaderboard.get("groups", [])
        if top_groups:
            lines = ["👥 群组活跃 TOP10:"]
            for i, (gid, c) in enumerate(top_groups[:10]):
                icon = ResponsePresenter._get_rank_icon(i)
                lines.append(f"{icon} 群{gid}  —  {c}次")
            msg_parts.append("\n".join(lines))
            has_data = True
        top_users = leaderboard.get("users", [])
        if top_users:
            lines = ["👤 个人活跃 TOP10:"]
            for i, (uid, c) in enumerate(top_users[:10]):
                icon = ResponsePresenter._get_rank_icon(i)
                masked_uid = str(uid)
                if len(masked_uid) > 7:
                    masked_uid = masked_uid[:3] + "****" + masked_uid[-4:]
                lines.append(f"{icon} {masked_uid}  —  {c}次")
            msg_parts.append("\n".join(lines))
            has_data = True
        if not has_data:
            msg_parts.append("💤 暂无数据 (快来抢沙发)")
        return "\n\n".join(msg_parts)

    @staticmethod
    def connection_list_summary(presets: Dict[str, ConnectionPreset], active_name: str, p: str = "#") -> str:
        if not presets:
            return "🔗 连接预设列表为空。"
        msg = ["🔗 连接预设名录:"]
        for name, preset in presets.items():
            prefix = "➡️" if name == active_name else "▪️"
            key_count = len(preset.api_keys)
            s_icon = ResponsePresenter._get_stream_icon(preset.stream)
            msg.append(f"{prefix} {name} ({preset.api_type.value}, {s_icon}, {key_count} keys)")
        msg.append(f"\n💡 使用 {p}lmc <名称> 查看详情。")
        return "\n".join(msg)

    @staticmethod
    def connection_detail(
        preset: ConnectionPreset, 
        p: str = "#", 
        available_models: List[str] = None,
        simple_mode: bool = False
    ) -> str:
        count = len(preset.api_keys)
        key_info = f"{count} 个" + (f" (请使用 {p}lmk 查看或管理)" if count > 0 else "")
        stream_info = ResponsePresenter._fmt_stream(preset.stream)

        base_info = (
            f"🔗 连接预设 [{preset.name}] 详情:\n"
            f"🪧 {preset.api_type.value}\n"
            f"🔍 {preset.api_base}\n"
            f"🚀 {preset.model}\n"
            f"{stream_info}\n"
            f"🔑 {key_info}"
        )

        if simple_mode:
            return base_info

        model_list_str = ""
        if available_models:
            limit = 20
            top = available_models[:limit]
            content = "\n".join(top)
            model_list_str = f"\n\n📋 服务器可用生图模型 (Top {limit}):\n{content}"
            if len(available_models) > limit:
                model_list_str += f"\n... (剩余 {len(available_models) - limit} 个)"
            model_list_str += f"\n\n💡 切换指令: {p}lmc {preset.name} model <模型名>"
        elif available_models is not None:
            model_list_str = "\n\n⚠️ 无法获取可用模型列表 (网络超时或接口不支持)"
        return base_info + model_list_str

    @staticmethod
    def key_list(preset_name: str, keys: List[str], p: str = "#", status_map: Dict[str, str] = None) -> str:
        if not keys:
            return f"🔑 预设 [{preset_name}] 暂无配置任何 Key。"

        lines = [f"🔑 预设 [{preset_name}] 密钥列表 (共{len(keys)}个):"]

        for i, k in enumerate(keys):
            # 1. 掩码处理
            if len(k) > 12:
                masked = f"{k[:8]}......{k[-4:]}"
            else:
                masked = k

            # 2. 状态追加
            status_suffix = ""
            if status_map and k in status_map:
                status_suffix = f" {status_map[k]}"

            lines.append(f"{i + 1}. {masked}{status_suffix}")

        lines.append(f"\n💡 指令提示: {p}lmk del <预设名> [序号] 删除指定Key")
        return "\n".join(lines)

    @staticmethod
    def preset_list(data: Dict[str, str], item_name: str, p: str = "#", cmd: str = "lmp", simple_mode: bool = False) -> str:
        keys = sorted(data.keys())
        if not keys:
            return f"📒 {item_name}列表为空。"

        lines = []

        if simple_mode:
            lines.append(f"📒 {item_name}名录:")
            buffer = []
            current_len = 0
            CHAR_LIMIT = 500 

            for k in keys:
                delta = len(k) + 2
                if current_len + delta > CHAR_LIMIT:
                    lines.append(", ".join(buffer))
                    buffer = [k]
                    current_len = len(k)
                else:
                    buffer.append(k)
                    current_len += delta
            if buffer:
                lines.append(", ".join(buffer))
        else:
            lines.append(f"📒 {item_name}列表 (详细):")
            for k in keys:
                content = data.get(k, "")
                preview = ResponsePresenter.make_preview(content, limit=25, oneline=True)
                lines.append(f"▪️ [{k}]: {preview}")

        footer = f"\n💡 指令提示:\n{p}{cmd} <名> (查看)\n{p}{cmd} :<关键词> (搜索)\n{p}{cmd} <名>:[内容] (添加/修改)"
        return "\n".join(lines) + "\n" + footer

    @staticmethod
    def preset_detail(item_name: str, key: str, content: str, var_definitions: List[VariableDefinition] = None) -> str:
        if var_definitions is None:
            var_definitions = []

        msg_parts = [f"📝 {item_name} [{key}] 内容:\n{content}"]
        hints = []

        for var_def in var_definitions:
            matches = set(var_def.pattern.findall(content))
            if not matches:
                continue

            unique_displays = set()
            for m in var_def.pattern.finditer(content):
                full_match = m.group(0)
                if var_def.display_formatter:
                    unique_displays.add(var_def.display_formatter(full_match))
                else:
                    unique_displays.add(full_match)

            if unique_displays:
                item_str = ", ".join(sorted(unique_displays))
                hints.append(f"{var_def.name}: 包含 {item_str}")

        if hints:
            msg_parts.append("\n💡 **变量用法提示**:")
            msg_parts.extend([f"  ▸ {h}" for h in hints])

        return "\n".join(msg_parts)

    @staticmethod
    def search_result(keyword: str, found: List[Tuple[str, str]]) -> str:
        if not found:
            return f"🔍 未找到包含关键词 [{keyword}] 的条目。"
        lines = [f"🔍 搜索 [{keyword}] 结果 (共{len(found)}条):"]
        for k, v in found:
            preview = ResponsePresenter.make_preview(v, limit=50, oneline=True)
            lines.append(f"▪️ **{k}**: {preview}")
        return "\n".join(lines)

    @staticmethod
    def duplicate_item(item_name: str, key: str) -> str:
        return f"💡 相同的内容已存在于 {item_name} [{key}] 中。"

    @staticmethod
    def overwrite_confirmation(item_name: str, key: str, old_val: str, new_val: str) -> str:
        preview_old = ResponsePresenter.make_preview(old_val, limit=100, oneline=False)
        preview_new = ResponsePresenter.make_preview(new_val, limit=100, oneline=False)

        return (
            f"⚠ {item_name} [{key}] 已存在，是否覆盖？\n"
            f"(发送 '是/y' 确认，'否/n' 取消，30秒超时)\n\n"
            f"🔻 旧内容:\n{preview_old}\n\n"
            f"🔺 新内容:\n{preview_new}"
        )

    @staticmethod
    def overwrite_success(item_name: str, key: str, old_val: str, new_val: str) -> str:
        preview_old = ResponsePresenter.make_preview(old_val, limit=100, oneline=False)
        preview_new = ResponsePresenter.make_preview(new_val, limit=100, oneline=False)

        return (
            f"✅ 已更新 {item_name} [{key}]。\n\n"
            f"🔻 旧内容:\n{preview_old}\n\n"
            f"🔺 新内容:\n{preview_new}"
        )

    @staticmethod
    def main_menu(extra_prefix: str, p: str = "#") -> str:
        return f"""🍌 【香蕉忍法帖】
--- 🖼️ 生成 ---
● 文生图
  ▸ 指令: {p}lmt <预设名/提示词>
  ▸ 描述: 根据文字描述创作图片
● 图生图 (使用预设)
  ▸ 指令: (发送或引用图片) + {p}<预设名>
  ▸ 描述: 使用预设提示词处理图片
● 图生图 (自定义)
  ▸ 指令: (发送或引用图片) + {p}{extra_prefix} <提示词>
  ▸ 描述: 根据你的提示词进行创作
‍👩‍👧‍👧<支持处理多图、多@>

--- 📁 预设 ---
● 预设预览/管理
  ▸ 格式:
    {p}lmp 或 {p}lm预设 ▸ 列表预览
    {p}lmo 或 {p}lm优化 ▸ 优化预设预览
  ▸ 通用操作:
    {p}lmp <预设名> ▸ 查看提示词详情
    {p}lmp <预设名>:[提示词] ▸ 添加/覆盖
    {p}lmp :[关键词] ▸ 搜索功能
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

    @staticmethod
    def help_vars(var_definitions: List[VariableDefinition] = None) -> str:
        lines = ["🔁 【奥义•缭乱变量杀阵】", "🧙<在提示词、参数a和预设中使用>"]

        if not var_definitions:
            return "\n".join(lines + ["⚠️ 暂时无法获取变量定义，请检查 PromptResolver 配置。"])

        # 分组
        grouped = {}
        for var in var_definitions:
            if var.category not in grouped:
                grouped[var.category] = []
            grouped[var.category].append(var)

        for category, vars in grouped.items():
            lines.append(f"● {category}")
            for v in vars:
                lines.append(f"  ▸ {v.description}")

        return "\n".join(lines)