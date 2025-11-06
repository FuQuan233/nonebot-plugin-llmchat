# 私聊功能实现总结

## 📝 概览

已成功为 nonebot-plugin-llmchat 项目添加了完整的私聊功能支持。用户现在可以在私聊中与机器人进行对话，同时保持群聊功能完全不变。

---

## 🔧 主要改动

### 1. **config.py** - 配置模块

#### 新增配置项：
- `LLMCHAT__ENABLE_PRIVATE_CHAT` (bool, 默认值: False)
  - 是否启用私聊功能
  
- `LLMCHAT__PRIVATE_CHAT_PRESET` (str, 默认值: "off")
  - 私聊默认使用的预设名称

### 2. **__init__.py** - 主程序模块

#### 新增导入：
```python
from typing import Union
from nonebot.adapters.onebot.v11 import PrivateMessageEvent
```

#### 新增数据结构：

**PrivateChatState 类**
- 用于管理每个用户的私聊状态
- 结构与 GroupState 类似，但针对单个用户独立管理
- 包含：preset_name、history、queue、processing 等属性

**private_chat_states 字典**
- 类型：`dict[int, PrivateChatState]`
- 按用户ID存储私聊状态

#### 修改的函数：

1. **format_message()**
   - 参数改为：`event: Union[GroupMessageEvent, PrivateMessageEvent]`
   - 支持两种消息事件类型的格式化

2. **is_triggered()**
   - 参数改为：`event: Union[GroupMessageEvent, PrivateMessageEvent]`
   - 新增私聊事件检测逻辑
   - 私聊消息在启用且预设不为"off"时自动触发

3. **get_preset()**
   - 新增参数：`is_group: bool = True`
   - 支持从群组或私聊状态获取预设配置

4. **process_messages()**
   - 新增参数：`context_id: int, is_group: bool = True`
   - 支持处理群组和私聊消息
   - 私聊时跳过OneBot群操作工具（ob__开头的工具）

5. **handle_message()**
   - 参数改为：`event: Union[GroupMessageEvent, PrivateMessageEvent]`
   - 支持路由到不同的处理逻辑

6. **save_state()** / **load_state()**
   - 新增私聊状态的持久化
   - 私聊状态保存到单独的文件：`llmchat_private_state.json`

#### 新增命令处理器（私聊相关）：

所有私聊命令需要主人权限，且仅在启用私聊功能时可用：

1. **私聊API预设**
   - 查看或修改私聊使用的API预设
   - 用法：`私聊API预设 [预设名]`

2. **私聊修改设定**
   - 修改私聊机器人的性格设定
   - 用法：`私聊修改设定 [新设定]`

3. **私聊记忆清除**
   - 清除私聊的对话历史记录
   - 用法：`私聊记忆清除`

4. **私聊切换思维输出**
   - 切换是否输出AI的思维过程
   - 用法：`私聊切换思维输出`

### 3. **README.md** - 文档更新

#### 更新的章节：

1. **项目介绍**
   - 更新标题为"群聊&私聊的AI对话插件"
   - 添加"群聊和私聊支持"功能说明

2. **配置表格**
   - 添加两个新配置项的说明

3. **使用指南**
   - 将原"指令表"改名为"群聊指令表"
   - 新增"私聊指令表"
   - 添加"私聊功能启用示例"部分

---

## 🚀 使用指南

### 启用私聊功能

在 `.env` 文件中添加：

```bash
# 启用私聊功能
LLMCHAT__ENABLE_PRIVATE_CHAT=true

# 设置私聊默认预设
LLMCHAT__PRIVATE_CHAT_PRESET="deepseek-v1"
```

### 私聊命令示例

```
# 主人私聊机器人

私聊API预设                    # 查看当前预设
私聊API预设 aliyun-deepseek-v3 # 切换预设

私聊修改设定 你是一个有趣的AI   # 修改性格设定

私聊记忆清除                   # 清除对话记忆

私聊切换思维输出               # 开关思维过程输出
```

---

## 🔑 关键特性

✅ **独立管理** - 群聊和私聊拥有完全独立的对话记忆和配置

✅ **灵活控制** - 可单独启用/禁用私聊功能，无需影响群聊

✅ **自动触发** - 私聊消息自动触发回复，无需@机器人

✅ **权限隔离** - 私聊命令仅主人可用

✅ **工具适配** - 私聊时自动跳过不适用的群操作工具

✅ **状态持久化** - 私聊状态独立保存和恢复

---

## 📊 文件对比

| 文件 | 变更类型 | 主要改动 |
|------|--------|--------|
| config.py | 修改 | 新增2个配置项 |
| __init__.py | 修改 | 新增私聊类、处理器、命令 |
| README.md | 修改 | 更新文档说明 |

---

## ⚠️ 注意事项

1. **默认禁用** - 私聊功能默认为禁用状态，需要在配置文件中显式启用

2. **群操作工具** - OneBot群操作工具（禁言、撤回等）在私聊中不可用

3. **状态文件** - 私聊状态存储在 `llmchat_private_state.json` 文件中

4. **权限限制** - 所有私聊命令都需要主人权限

5. **独立预设** - 私聊和群聊可以使用不同的API预设


