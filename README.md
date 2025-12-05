# 🍌 AstrBot Plugin Bananic Ninjutsu | 香蕉忍法帖

<div align="center">

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-purple?style=flat-square)](https://github.com/Soulter/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10+-blue?style=flat-square)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](./LICENSE)
[![Version](https://img.shields.io/badge/Version-0.0.5-orange?style=flat-square)]()

**专为 AstrBot 设计的新一代 AI 绘图引擎**
<br>
*不仅仅是生图，更是集成了动态变量、智能密钥轮询与 LLM 提示词优化的完整创作系统。*

[安装教程](#-安装) • [核心优势](#-核心优势-why-us) • [指令大全](#-指令手册) • [变量系统](#-奥义动态变量)

</div>

---

## 📖 简介

**香蕉忍法帖 (Bananic Ninjutsu)** 是一个适配于 [AstrBot](https://github.com/Soulter/AstrBot) 的高级绘图插件。

不同于简单的 API 调用脚本，本插件构建了一套完整的**生成服务架构**。它内置了智能的 Token 桶限流算法、支持正则表达式的动态变量解析引擎、以及企业级的多 Key 负载均衡系统。无论是个人创作还是群组娱乐，它都能提供极其稳定且强大的支持。

## 🔥 核心优势 (Why Us?)

在众多绘图插件中，为什么选择“香蕉忍法帖”？基于源码实现的硬核特性：

### 1. 🛡️ 企业级连接管理与密钥轮询 (`Connection Manager`)
告别单纯的 Key 报错。内置智能 `APIClient`，支持**多 Key 自动负载均衡**：
- **智能重试&熔断&冷却**：自动识别 429 (Rate Limit)、402 (额度耗尽) 和 401 (鉴权失败)。
- **自动漂移**：当前 Key 报错时，毫秒级自动切换至下一个可用 Key，确保生成任务不中断。
- **多后端预设**：支持通过指令 `#lmc` 热切换不同的后端模型配置（如从 Google 切换至 OpenAI 兼容接口），无需重启 Bot。

### 2. ⚡ 统一且极简的指令风格 (`Unified & Minimalist`)
不仅功能强大，交互设计更追求直觉与效率，大幅降低记忆成本：
- **一致的交互逻辑**：所有管理系统均以 `#lm` 为前缀（如 `#lmc` 连接、`#lmp` 预设、`#lmk` 密钥），且全部遵循统一的 `list` / `add` / `del` / `ren` 标准化 CRUD 操作范式。学会管理预设，就等于学会了管理连接与密钥。
- **极速短参数**：精心设计的参数缩写（如 `--s` 搜索、`--ar` 比例、`--up` 优化、`--q` 指定对象），让复杂的专业生图参数输入变得如聊天般自然流畅。

### 3. 🧬 动态提示词变量引擎 (`Variable Engine`)
支持在提示词中使用动态逻辑，让每一次生成都独一无二：
- **随机化**：`%r:白丝|黑丝%`（随机选项）、`%rn:1-100%`（随机数）、`%rl%`（随机字母）。
- **用户信息注入**：`%un%`（用户昵称）、`%age%`（年龄）、`%bd%`（生日），实现“画一张我的二次元头像”这种上下文感知的指令。
- **自定义填空**：支持预设参数化（如预设中有 `%p1%`，用户调用时可用 `--p1 值` 动态填入）。

### 4. 🧠 LLM 提示词润色 (`Prompt Enhancer`)
不仅仅是拼接字符串。插件利用 AstrBot 的上下文 LLM 能力：
- 支持使用指令 `--up` 调用大模型对用户的简单描述进行扩写、优化和润色。
- 支持自定义优化策略（Prompt Engineering 预设）。

### 5. 💰 完善的经济与风控系统 (`Economy & Quota`)
内置 `StatsManager`，提供精细化的权限管理：
- **混合配额**：支持“个人每日配额”与“群组共享配额”双轨制。
- **防刷屏风控**：基于 Token Bucket 算法的群组速率限制（Rate Limiting），防止高频请求导致的风控。
- **签到与排行**：自带签到系统与每日活跃排行榜（个人榜/群组榜）。

---

## 💿 安装

1. 确保你已经安装并运行了 [AstrBot](https://github.com/Soulter/AstrBot)。
2. 在 AstrBot 的插件目录或通过 Web 管理面板安装本插件：
   ```bash
   # 推荐通过 AstrBot 管理面板安装
   # 或手动克隆至 data/plugins 目录
   git clone https://github.com/bylkuse/astrbot_plugin_bananic_ninjutsu.git
   ```
3. 重启 AstrBot，插件将自动加载。

---

## 🎮 指令手册

插件默认自定提示词图生图前缀为 `lmi`（可在配置中修改）。

### 🖼️ 基础绘图

| 指令 | 描述 | 示例 |
| :--- | :--- | :--- |
| `#lmt <描述>` | **文生图** (Text to Image) | `#lmt 一个在雨中哭泣的赛博朋克少女 --ar 16:9` |
| `#bnn <描述>` | **图生图** (Image to Image)<br>需附带图片或引用图片 | (发送图片) `#bnn 把头发变成银色 --up` |
| `#<预设名>` | 使用保存的预设直接生图 | `#二次元头像` (假设已保存该预设) |

### 🛠️ 参数详解 (支持混用)

在生图指令后追加以下参数，精确控制生成效果：

*   `--ar <比例>`: 设置画面比例 (如 `16:9`, `4:3`, `1:1`)。
*   `--r <尺寸>`: 设置分辨率质量 (`1K`, `2K`, `4K`)。
*   `--s`: **联网搜索** (Google Grounding)，让 AI 搜索最新信息辅助绘图。
*   `--t`: **思维链** (Thinking)，展示模型的思考过程 (仅支持部分 Gemini 模型)。
*   `--up [策略]`: **提示词优化**。不填则使用默认优化，也可指定策略或具体修改意见。
*   `--to <秒>`: 设置超时时间。
*   `--q <@用户>`: 指定变量获取的目标对象（如获取 @某人 的头像或昵称）。

### ⚙️ 管理与配置 (管理员/高级)

*   **连接管理**:
    *   `#lmc`: 查看当前模型连接状态。
    *   `#lmc to <预设名>`: 切换后端连接预设。
    *   `#lmk`: 管理 API Key（添加/删除）。
*   **预设管理**:
    *   `#lmp`: 查看所有生图预设。
    *   `#lmp <名>:<内容>`: 添加或修改预设。
*   **经济/统计**:
    *   `#lm` / `#lm次数`: 查看个人剩余次数、群组剩余次数及今日排行榜。
    *   `#lm签到`: 每日签到获取积分。

---

## 🥷 奥义·动态变量

在提示词、参数 `--a` 或预设中使用以下变量，插件将在运行时自动解析：

### 基础变量
*   `%un%`: 发送者的昵称。
*   `%uid%`: 发送者的 QQ 号。
*   `%g%`: 当前群聊名称。
*   `%run%`: 随机抽取群内一名幸运群友的昵称。

### 随机变量
*   `%r:A|B|C%`: 从 A、B、C 中随机选择一个。
    *   *例*: `画一个%r:红色|蓝色|金色%的头发`
*   `%rn:min-max%`: 生成指定范围内的随机整数。
*   `%rl%`: 生成随机字母串。
*   `%rc%`: 生成随机颜色 (Red/Blue/Green...)。

### 高级用法
*   **参数填空 (%p%)**:
    在预设中写入 `画一个%p%的猫`。
    调用时使用指令：`#猫预设 --p 飞翔` -> 最终解析为 `画一个飞翔的猫`。
    支持 `%p2%`, `%p3%` 等多个槽位。

---

## 🔧 配置说明

插件首次运行后，会在 `data/plugins/astrbot_plugin_bananic_ninjutsu/` 下生成配置文件。

建议直接通过聊天窗口指令进行配置，无需手动修改 JSON 文件：

1.  **添加连接预设 (Admin)**:
    ```
    #lmc add MyGemini google https://generativelanguage.googleapis.com gemini-2.5-flash-image-preview key1,key2...
    ```
2.  **添加 Key**:
    ```
    #lmk add MyGemini AIzaSyB...Key1 AIzaSyB...Key2
    ```
3.  **切换使用该连接**:
    ```
    #lmc to MyGemini
    ```

---

## 📝 常见问题

**Q: 为什么提示 "Safety Block"？**
A: Google Gemini 的安全过滤器较为严格。尝试调整提示词，或使用 `--up 审查` 让 LLM 帮你改写提示词以规避敏感词。

**Q: 图生图怎么用？**
A: 先发送图片，然后引用该图片回复 `#lmi 提示词`；或者直接发送图片加文字（如果客户端支持）；或者在发送指令时附带图片。

---

## ✔ 计划清单

**欢迎 ISSUES/PR 功能建议可能会看喜好加 初心是自用**
### 第一序列
* 撤回功能（生成后撤回等待词，定时撤回图片）
* 完成内置优化预设
* 完成内置变量生图预设
* 写一份正式的readme
### 第二序列
参考 piexian/astrbot_plugin_gemini_image_generation 引入先进处理：
* 添加gemini的openai方式作为fallback
* OpenAI 兼容接口下的传参
* 为头像获取引入备用方法
* 视觉裁切
### 第三序列（瞎想！不一定能做也不一定会做）
* 引入api支持，作为变量或输入图
* 占位符-上下文总结
* 占位符-预设的图片模板

## ⚖️ License

MIT License.

---

<div align="center">
Made with 🍌 by LilDawn
</div>