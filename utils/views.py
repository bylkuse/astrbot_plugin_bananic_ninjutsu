from typing import Any, Dict, List
from astrbot.core.message.components import Image, Plain

class ResponsePresenter:
    """视图层：构建响应消息"""
    @staticmethod
    def generating(prompt: str) -> str:
        return f"🎨 正在生成 [{prompt}]..."

    @staticmethod
    def generation_success(elapsed: float, preset_name: str, enhancer_model: str = None, enhancer_preset: str = None) -> str:
        parts = [f"✅ 生成成功 ({elapsed:.2f}s)", f"连接: {preset_name}"]
        if enhancer_model:
            preset_suffix = f"({enhancer_preset})" if enhancer_preset else ""
            parts.append(f"✨{enhancer_model}{preset_suffix}")
        return " | ".join(parts)

    @staticmethod
    def generation_failed(reason: str, elapsed: float, is_master: bool) -> str:
        msg = f"❌ 生成失败 ({elapsed:.2f}s)\n原因: {reason}"
        if not is_master: 
            msg += "\n(本次失败不扣除次数)"
        return msg

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
    def connection(is_admin: bool) -> str:
        lines = [
            "💡 连接管理指令:",
            "#lm连接 (显示列表)",
            "#lm连接 <名称> (查看详情)",
            "#lm连接 to <名称> (切换连接)"
        ]
        if is_admin:
            lines.extend([
                "🔧 管理员指令:",
                "#lm连接 add <name> <type> <url> <model> [keys] (添加)",
                "#lm连接 del <name> (删除)",
                "#lm连接 ren <旧名> <新名> (重命名)",
                "#lm连接 debug (调试模式)"
            ])
        return "\n".join(lines)

    @staticmethod
    def format_connection_detail(name: str, data: Dict[str, Any]) -> str:
        keys = data.get('api_keys', [])
        key_list_str = "\n".join([f"- {k}" for k in keys]) if keys else "- (无)"
        return (
            f"📝 连接预设 [{name}] 详情:\n"
            f"API 类型: {data.get('api_type')}\n"
            f"API URL: {data.get('api_url')}\n"
            f"模型: {data.get('model')}\n"
            f"Keys ({len(keys)}):\n{key_list_str}"
        )

    @staticmethod
    def format_connection_switch_success(name: str, data: Dict[str, Any]) -> str:
        key_count = len(data.get('api_keys', []))
        return (
            f"✅ 连接已成功切换为 **[{name}]** \n"
            f"API 类型: {data.get('api_type')}\n"
            f"API URL: {data.get('api_url', 'N/A')}\n"
            f"模型: {data.get('model')}\n"
            f"Key 数量: {key_count}"
        )

    @staticmethod
    def key_management(current_preset: str) -> str:
        return (
            f'🔑 Key 管理指令 (管理员):\n'
            f'#lmk [预设名] - 查看指定预设的Key\n'
            f'#lmk add <预设名> <Key1> [Key2]... - 添加Key\n'
            f'#lmk del <预设名> <序号|all> - 删除Key\n'
            f'注: 当前连接预设为 [{current_preset}]'
        )

    @staticmethod
    def presets_common(item_name: str, cmd_prefix: str, is_admin: bool) -> str:
        lines = [
            f"💡 {item_name}指令格式:",
            f"{cmd_prefix} (显示列表)",
            f"{cmd_prefix} l (简略名录)",
            f"{cmd_prefix} <名称> (查看内容)",
            f"{cmd_prefix} <名称>:<内容> (添加/修改)"
        ]
        if is_admin:
            lines.extend([
                f"{cmd_prefix} del <名称> (管理员删除)",
                f"{cmd_prefix} ren <旧名> <新名> (管理员重命名)"
            ])
        return "\n".join(lines)

    @staticmethod
    def main_menu(bnn_cmd: str) -> str:
        return f"""🍌 【香蕉忍法帖】
💡<请用实际的唤醒词替换 '#' ,如 '/'>
--- 🖼️ 生成 ---
● 文生图
  ▸ 指令: #lmt <预设名/提示词>
  ▸ 描述: 根据文字描述创作图片
● 图生图 (使用预设)
  ▸ 指令: (发送或引用图片) + #<预设名>
  ▸ 描述: 使用预设提示词处理图片
● 图生图 (自定义)
  ▸ 指令: (发送或引用图片) + #{bnn_cmd} <提示词>
  ▸ 描述: 根据你的提示词进行创作
‍👩‍👧‍👧<支持处理多图、多@>

--- 📁 预设 ---
● 预设预览/管理
  ▸ 格式:
    #lmp 或 #lm预设 ▸ 列表预览
    #lmo 或 #lm优化 ▸ 优化预设预览
  ▸ 通用操作:
    #lmp <名称>:<内容> ▸ 添加/覆盖
    #lmp del/ren ... ▸ 删除/重命名

--- 🔧 管理 ---
● 综合面板
  ▸ 指令: #lm 或 #lm次数
  ▸ 描述: 签到获取次数、查看剩余及今日排行
  ▸ 管理参数: 个人/群组次数管理
● 连接管理
  ▸ 指令: #lmc 或 #lm连接
  ▸ 描述: 查看所有可用的后端模型连接，并可按提示切换。（供应商故障时的后备选项）
● 密钥管理 
  ▸ 指令: #lmk 或 #lm密钥

--- 📚 进阶 ---
发送以下指令查看详细说明👇
#lmh 参数 ▸ 查看 --ar, --up, --s, --q 等参数
#lmh 变量 ▸ 查看 %un%, %r%, %t% 等动态变量"""

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
  ▸ 描述: 允许模型联网搜索以获取更精确的信息，可能会增加不稳定性。
● 补充描述 (--a)
  ▸ 示例: --a "拿着花"
  ▸ 描述: 在预设或提示词末尾追加额外描述。
● 自定义内容 (--p)
  ▸ 示例: --p 小黎明
  ▸ 描述: 配合支持 %p% 变量的预设使用，可动态插入自定义内容。
  ▸ 扩展: 支持 --p2, --p3... 对应预设中的 %p2%, %p3%...
● 指定对象 (--q)
  ▸ 示例: /生日 --q @某人
  ▸ 描述: 将 %un%, %uid%, %age%, %bd% 等变量的获取目标指定为 @ 的用户或特定QQ号。
  ▸ 扩展: --q <QQ号>
● 提示词优化 (--up)
  --up ▸ 默认优化 (润色详情)
  --up <优化意见> ▸ 让AI根据你的意见优化提示词
  --up <优化预设名> ▸ 使用特定的提示词优化预设（default、审查等）"""

    @staticmethod
    def help_vars() -> str:
        return """🔁 【奥义•缭乱变量杀阵】
🧙<在提示词、参数a和预设中使用>
● 用户信息 (默认自己，可配合 --q 指定目标)
%un% : 用户昵称
%uid% : 用户QQ号
%age% : 用户年龄
%bd% : 用户生日
● 群组信息
%g% : 当前群名称
%run% : 随机群友昵称
● 时间日期
%d% : 日期 (如 11月30日)
%dd% : 完整日期 (如 2023年11月30日)
%t% : 当前时间 (HH:MM:SS)
%wd% : 星期几
● 随机生成
%r:A|B|C% : 从选项 A, B, C 中随机选择一个
%rc% : 随机颜色 (Red, Blue...)
%rn:1-100% : 指定范围内的随机整数
%rl:5% : 随机5个大小写字母"""