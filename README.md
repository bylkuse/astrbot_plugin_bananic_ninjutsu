# 🍌 AstrBot Plugin Bananic Ninjutsu | 香蕉忍法帖

<div align="center">

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-purple?style=flat-square)](https://github.com/Soulter/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10+-blue?style=flat-square)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](./LICENSE)
[![Version](https://img.shields.io/badge/Version-0.2.5-orange?style=flat-square)]()

**专为 AstrBot 设计的新一代 AI 绘图工具**
<br>
*极客  优雅  更多🍌。*

[功能预览](#-功能预览) • [指令大全](#-指令大全) • [配置详解](#-配置详解) • [核心优势](#-核心优势) • [计划清单](#-计划清单) • [常见问题](#-常见问题) • [友情鸣谢](#-友情鸣谢)

</div>

---

## 📖 简介

**香蕉忍法帖 (Bananic Ninjutsu)** 是一个适配于 [AstrBot](https://github.com/Soulter/AstrBot) 和 aiocqhttp 的高级绘图插件。
<br>**设计理念** 
* 统一高效的指令逻辑
* 简洁友好的UI
* 代码&性能优化
* 自定义&扩展性<br>

⚠小心刷屏：√生成后撤回等待词 √定时撤回生成结果，强烈建议配合AstrBot的 **长文本转发功能** 使用

## 🖼️ 功能预览
**不想看长篇大论？几张图带你速览插件特色**
| `签到看板` | `密钥管理捷径` |
| :---: | :---: |
| <img src="./preview/lm.jpg" width="400"> | <img src="./preview/lmk.jpg" width="400"> |

### 像预设一样管理&切换供应商
| `供应商面板` | `对话中完成管理` |
| :---: | :---: |
| <img src="./preview/lmc1.jpg" width="400"> | <img src="./preview/lmc3.jpg" width="400"> |
| `失效？灵活切换` | `填错？便捷修改` |
| <img src="./preview/lmc2.jpg" width="400"> | <img src="./preview/lmc4.jpg" width="400"> |

### 精心打磨的提示词管理系统
| `带排序&预览的详细列表` | `太长不看？来个省流版` |
| :---: | :---: |
| <img src="./preview/lmp1.jpg" width="400"> | <img src="./preview/lmp2.jpg" width="400"> |
| `搜索功能，大量预设不再头痛` | `智能查重，守护你的预设数据` |
| <img src="./preview/lmp4.jpg" width="400"> | <img src="./preview/lmp3.jpg" width="400"> |

### 先进的变量&参数系统
| `配套的提示词调试模式` | `在预设里使用聊天环境的变量` |
| :---: | :---: |
| <img src="./preview/p1.jpg" width="400"> | <img src="./preview/p2.jpg" width="400"> |
| `参数开关的高灵活性` | `说明书` |
| <img src="./preview/p3.jpg" width="400"> | <img src="./preview/p4.jpg" width="400"> |

### 提示词修改苦手？
| `调用LLM润色提示词` | `预设/参数值选择你的优化策略` |
| :---: | :---: |
| <img src="./preview/lmo1.jpg" width="400"> | <img src="./preview/lmo2.jpg" width="400"> |

## 🎮 指令大全

插件默认自定提示词图生图前缀为 `lmi`（可在配置中修改）
<br>'#'指代你实际的AstrBot唤醒词，在插件提示信息中已作自动获取处理

| 指令 | 描述 |
| :--- | :--- |
| `#lmh` | 基本帮助菜单 |
| `#lmh 参数` | 参数使用帮助 |
| `#lmh 变量` | 变量使用帮助 |

### 🖼️ 基础绘图

| 指令 | 描述 | 示例 |
| :--- | :--- | :--- |
| `#lmt <生图预设名>/[描述]` | **文生图**  | `#lmt 一个在雨中哭泣的少女 --ar 16:9` |
| `#lmi <生图预设名>/[描述]` | **图生图** <br>若未 带图/引图/@ 则默认使用用户头像 | (图) `#lmi 把头发变成银色 --up` |
| `#<生图预设名>` | 使用预设快速图生图 | `#生日` |

### ⚙️ 管理&配置

*   **供应商**:

| 指令 | 描述 |
| :--- | :--- |
| `#lmc` | 查看当前模型连接状态 |
| `#lmc to <连接预设名>` | 切换后端连接预设 |
| `#lmc ren <旧名> <新名>` | 重命名连接预设（仅管理） |
| `#lmc add <连接预设格式>` | 添加新的连接预设（仅管理） |
| `#lmc del <连接预设格式>` | 删除连接预设（仅管理） |
| `#lmc <连接预设名> <配置项> <值>` | 修改连接预设配置（仅管理） |

<连接预设格式>:<连接预设名> <供应商类型(google/openai)> <接入点> <模型名> [密钥1],[密钥2]
<配置项>:api_type(供应商类型)、api_url(接入点)、model(模型名)

*   **提示词**:

| 指令 | 描述 |
| :--- | :--- |
| `#lmp` | 带预览的详情列表 |
| `#lmp l` | 紧凑视图的列表 |
| `#lmp <生图预设名>` | 查看提示词详情 |
| `#lmp <生图预设名>:[提示词]` | 添加/覆盖生图预设 |
| `#lmp :[关键词]` | 在预设中搜索 |
| `#lmp ren <旧名> <新名>` | 重命名生图预设（仅管理） |
| `#lmp del <生图预设名>` | 删除生图预设（仅管理） |

*   **提示词优化**:

| 指令 | 描述 |
| :--- | :--- |
| `#lmo` | 带预览的优化预设列表 |
| `#lmo l` | 紧凑视图的列表 |
| `#lmo <优化预设名>` | 查看提示词详情 |
| `#lmo <优化预设名>:[提示词]` | 添加/覆盖优化预设 |
| `#lmo :[关键词]` | 在预设中搜索 |
| `#lmo ren <旧名> <新名>` | 重命名优化预设（仅管理） |
| `#lmo del <优化预设名>` | 删除优化预设（仅管理） |

*   **数据**:

| 指令 | 描述 |
| :--- | :--- |
| `#lm` | 聚合面板（签到&查次&排行） |
| `#lm @` | 查询指定用户次数（仅管理） |
| `#lm [次数] ` | 为当前群组增加次数（仅管理） |
| `#lm @ [次数]` | 为指定用户增加次数（仅管理） |

*   **密钥管理捷径（仅管理）**:

| 指令 | 描述 |
| :--- | :--- |
| `#lmk` | 查询当前预设key列表 |
| `#lmk <连接预设名>` | 查询指令预设key列表 |
| `#lmk [key1] [key2]...` | 为当前预设增加key |
| `#lmk <连接预设名> [key1] [key2]...` | 为指令预设增加key |
| `#lmk del [序号n]...` | 删除当前预设的第n个key |
| `#lmk del <连接预设名> [序号n]...` | 删除指令预设的第n个key |

### 🛠️ 参数详解

在生图指令后追加以下参数，精确控制生成效果
格式：<基础绘图指令> --参数A 参数A的值 --参数B 参数B的值 ...
部分参数存在缺省值，可省略参数值

| 参数名 | 可用参数值 | 描述 |
| :--- | :--- | :--- |
| `--a [提示词]` | 文字，变量 | 在生图提示词末尾追加提示词，智能处理符号 |
| `--ar [比例]` | 1:1, 2:3, 3:2, 4:3, 3:4, <br>5:4, 4:5, 16:9, 9:16, 21:9 | 设置画面比例，默认自动 |
| `--r [尺寸]` | 1K, 2K, 4K | 设置分辨率质量（耗次↑耗时↑），默认1K |
| `--s [布尔值]` | true, false | **联网搜索** (Google Grounding)，让 AI 搜索最新信息辅助绘图，默认true |
| `--t [布尔值]` | true, false | **思维链** (Thinking)，展示模型的思考过程（若兼容），默认true |
| `--up [策略]` | <优化预设名>, "你的修改意见" | **提示词优化**，默认使用default优化 |
| `--to [秒]` | 数字 | 设置超时时间，默认300秒 |
| `--q <用户>` | @用户, QQ号 | 指定用户信息变量获取的目标对象，默认自己 |

### 🥷 动态变量

在 提示词/参数 `--a` /预设 中使用以下变量，进行生成前会自动解析和替换

### 聊天

在不使用--q进行指令时，默认使用发送者的用户信息

| 变量 | 描述 |
| :--- | :--- |
| `%un%` | 昵称 |
| `%run%` | 当前群聊内随机一名幸运群友的昵称 |
| `%uid%` | QQ 号 |
| `%age%` | 年龄 |
| `%bd%` | 生日 |
| `%g%` | 当前群聊名称 |

### 随机

| 变量 | 描述 |
| :--- | :--- |
| `%r:A\|B\|C%` | 从 A、B、C 中随机选择一个 |
| `%rn:整数下限-整数上限%` | 指定范围内的随机整数 |
| `%rl:[长度n]%` | n位随机大小写字母 |
| `%rc%` | 随机颜色 (Red/Blue/Green...) |

### 日期

| 变量 | 描述 |
| :--- | :--- |
| `%d%` | 当前日期，MM月DD日 |
| `%t%` | 当前时间，hh:mm:ss |
| `%wd%` | 星期x |

### 预设填空(%p%)

在预设中写入 `画一个%p%的猫` -> 
调用时使用指令：`#猫预设 --p 飞翔` -> 
最终解析为 `画一个飞翔的猫`
（支持 `%p2%`, `%p3%` 等多个槽位）

## 🔧 配置详解

### 基础设置 (Basic Config)
插件的基础触发方式与调试选项。

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `prefix` | boolean | `true` | **指令前缀校验**。<br>开启后，指令必须以 `#` 或 `@机器人` 开头才能触发，有效防止日常聊天误触。 |
| `extra_prefix` | string | `"lmi"` | **自定义生图前缀**。<br>用于触发自定义 Prompt 模式。例如设置为 `lmi`，则用户发送 `lmi <描述>` 即可直接调用。 |
| `debug_prompt` | boolean | `false` | **调试模式**。<br>开启后仅输出构建好的最终提示词，**不进行**实际生图请求，用于排查提示词构建问题。 |

### 连接&网络 (Connection Config)
配置绘图 API 的连接方式、代理及超时设置。

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `current_preset_name` | string | `"GoogleDefault"` | **当前使用的连接预设**。<br>需填写下方 `connection_presets` 中已存在的 `name` 字段值。 |
| `connection_presets` | list | `[...]` | **连接预设列表**。<br>存储 API 配置信息。建议使用 `#lmc` 指令进行添加或切换。包含 API URL、Model、Key 等信息。 |
| `timeout` | number | `300` | **请求超时时间(秒)**。<br>API 请求的最大等待时间。复杂生图模型建议设置在 120 秒以上。 |
| `use_proxy` | boolean | `false` | **启用网络代理**。<br>如果服务器无法直接访问国外 API，请开启此项。 |
| `proxy_url` | string | `""` | **代理服务器地址**。<br>格式示例: `http://127.0.0.1:7890` 或 `http://user:pass@host:port`。 |

### 生图&优化 (Generation Config)
管理生图触发词、提示词优化模型及预设。

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `prompt_list` | list | `["生日..."]` | **触发词映射列表**。<br>格式为 `触发词:提示词`。建议使用 `#lm添加` 指令动态管理。 |
| `prompt_enhance_provider_id` | string | `""` | **提示词优化模型供应商**。<br>选择一个 LLM 用于扩写和优化用户的简单提示词。留空则使用当前会话默认模型。 |
| `prompt_enhanced_model` | string | `""` | **指定优化模型名称**。<br>可选。强制指定上述供应商的具体模型名（不填则使用该供应商的默认模型）。 |
| `optimizer_presets` | list | `[...]` | **优化系统预设**。<br>用于 `--up` 参数。格式为 `预设名:SystemPrompt`。建议使用 `#lm优化` 指令管理。 |

### 权限&限额 (Permission Config)
设置黑白名单及使用频率限制。

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `enable_rate_limit` | boolean | `true` | **启用限流窗口**。<br>开启后，将限制单会话在周期内的最多请求数（默认 3次/min）。 |
| `rate_limit_period` | number | `60` | **限流窗口期**。<br>计算限流次数的每个周期的时长（秒）。 |
| `max_requests_per_group` | number | `3` | **窗口期请求上限**。<br>每个周期内允许的最大请求数。 |
| `enable_user_limit` | boolean | `false` | **启用个人次数限制**。<br>开启后，将独立计算每个 QQ 号的使用次数。 |
| `enable_group_limit` | boolean | `false` | **启用群组次数限制**。<br>开启后，将计算群内所有成员的总使用次数。 |
| `user_whitelist` | list | `[]` | **用户白名单**。<br>【只允许】列表内的 QQ 号使用。留空则对所有人开放（黑名单优先级更高）。 |
| `user_blacklist` | list | `[]` | **用户黑名单**。<br>【禁止】列表内的 QQ 号使用。优先级最高。 |
| `group_whitelist` | list | `[]` | **群聊白名单**。<br>【只允许】列表内的群号使用。留空则所有群可用。 |
| `group_blacklist` | list | `[]` | **群聊黑名单**。<br>【禁止】列表内的群号使用。 |

### 签到&次数 (Checkin Config)
配置每日签到奖励机制。

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `enable_checkin` | boolean | `false` | **开启每日签到**。<br>开启后，用户可使用 `#lm签到` 获取生图次数。 |
| `enable_random_checkin` | boolean | `false` | **开启随机奖励**。<br>开启：奖励为 `1` ~ `最大值` 的随机数；关闭：使用下方固定奖励。 |
| `checkin_fixed_reward` | number | `3` | **固定奖励次数**。<br>关闭随机奖励时的固定赠送次数。 |
| `checkin_random_reward_max` | number | `5` | **随机奖励最大值**。<br>开启随机奖励时，用户可能获得的最高次数。 |

## 🔥 核心优势

在众多绘图插件中，为什么选择“香蕉忍法帖”？基于源码实现的硬核特性：

### 🛡️ 企业级连接管理与密钥轮询 (`Connection Manager`)
告别单纯的 Key 报错。内置智能 `APIClient`，支持**多 Key 自动负载均衡**：
- **智能重试&熔断&冷却**：自动识别 429 (Rate Limit)、402 (额度耗尽) 和 401 (鉴权失败)。
- **自动漂移**：当前 Key 报错时，毫秒级自动切换至下一个可用 Key，确保生成任务不中断。
- **多后端预设**：支持通过指令 `#lmc` 热切换不同的后端模型配置（如从 Google 切换至 OpenAI 兼容接口），无需重启 Bot。

### ⚡ 统一且极简的指令风格 (`Unified & Minimalist`)
不仅功能强大，交互设计更追求直觉与效率，大幅降低记忆成本：
- **一致的交互逻辑**：所有管理系统均以 `#lm` 为前缀（如 `#lmc` 连接、`#lmp` 预设、`#lmk` 密钥），且全部遵循统一的 `list` / `add` / `del` / `ren` 标准化 CRUD 操作范式。学会管理预设，就等于学会了管理连接与密钥。
- **极速短参数**：精心设计的参数缩写（如 `--s` 搜索、`--ar` 比例、`--up` 优化、`--q` 指定对象），让复杂的专业生图参数输入变得如聊天般自然流畅。

### 🧬 动态提示词变量引擎 (`Variable Engine`)
支持在提示词中使用动态逻辑，让每一次生成都独一无二：
- **随机化**：`%r:白丝|黑丝%`（随机选项）、`%rn:1-100%`（随机数）、`%rl%`（随机字母）。
- **用户信息注入**：`%un%`（用户昵称）、`%age%`（年龄）、`%bd%`（生日），实现对象信息感知的指令。
- **自定义填空**：支持预设参数化（如预设中有 `%p1%`，用户调用时可用 `--p1 值` 动态填入）。

### 🧠 LLM 提示词润色 (`Prompt Enhancer`)
不仅仅是拼接字符串。插件利用 AstrBot 的上下文 LLM 能力：
- 支持使用指令 `--up` 调用大模型对用户的简单描述进行扩写、优化和润色。
- 支持自定义优化策略（Prompt Engineering 预设）。

### 💰 完善的经济与风控系统 (`Economy & Quota`)
内置 `StatsManager`，提供精细化的权限管理：
- **混合配额**：支持“个人每日配额”与“群组共享配额”双轨制。
- **防刷屏风控**：基于 Token Bucket 算法的群组速率限制（Rate Limiting），防止高频请求导致的风控。
- **签到与排行**：自带签到系统与每日活跃排行榜（个人榜/群组榜）。

## ✔ 计划清单

**欢迎 ISSUES/PR 功能建议可能会看喜好加 初心是自用**

### 第一序列
* ~~撤回功能（生成后撤回等待词，定时撤回图片）~~
* ~~关键词搜索预设~~
* 反推提示词
* 会话式采集，作为移动端多图的一种fallback
* 给压缩加个开关
### 第二序列
参考 piexian/astrbot_plugin_gemini_image_generation 引入先进处理：
* 添加gemini的openai方式作为fallback
* OpenAI 兼容接口下的传参
* ~~为头像获取引入备用方法~~
* 视觉裁切
### 第三序列（瞎想！不一定能做也不一定会做）
* 引入api支持，作为变量或输入图
* 占位符-上下文总结
* 占位符-预设的图片模板

🫸🏻 暂不考虑做工具调用支持，本身该插件的灵活&强大在使用自然语言时几无发挥余地
🔧 且过多画图插件都喜欢塞个工具调用，这在使用同类插件时引入了【冗余&冲突】，和我的插件宗旨相悖

## ❓︎ 常见问题

### 关于zAI
zAI类型的连接配置的key兼容Discord Token（较长有效期，会自动进行zAI Token的交换&更新）或zAI Token（貌似3个小时有效期？），均通过F12获取
<br>Discord Token在Discord登录后，携带 /api 请求的 Authorization 字段中找到
<br>zAI Token只需要在 https://zai.is/ 中，找到存储的 token 字段

## 🤝 友情鸣谢

Gemini Image https://github.com/railgun19457/astrbot_plugin_gemini_image/
<br>Gemini Image Generation https://github.com/piexian/astrbot_plugin_gemini_image_generation
<br>zai.is2api https://github.com/Futureppo/zai.is2api
<br>他们的项目给予了我很多灵感和先进实现作为参考

<br>AstrBot https://github.com/AstrBotDevs/AstrBot 我的Bot启蒙导师
<br>Gemini(cli) https://github.com/google-gemini/gemini-cli 赛博大善人，代码审阅极大降低了我的工作量

## 🌳 目录结构
```
astrbot_plugin_bananic_ninjutsu/
├── core/                 # 核心层
│   ├── config_mgr.py         # 各类预设的CRUD操作
│   ├── images.py             # 图片预处理
│   ├── prompt.py             # 提示词&变量解析
│   └── stats.py              # 统计&限流&限额
├── services/             # 服务层
│   └── generation.py         # 生图工作流
├── utils/                # 工具层
│   ├── parser.py             # 参数系统
│   ├── serializer.py         # 序列化&反序列化
│   ├── result.py             # Result容器
│   └── views.py              # 视图&提示信息
├── _conf_schema.json     # 配置文件模板
├── metadata.yaml         # 注册信息
├── requirements.txt      # 依赖声明
├── logo.png              # logo
├── api_client.py         # 通信层
└── main.py               # 指令入口&逻辑
```

---

<div align="center">
Made with 🍌 by LilDawn
</div>