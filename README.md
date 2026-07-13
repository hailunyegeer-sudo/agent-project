# Agent 智能体系统 — B1：Agent 运行与消息管理模块

> 作者：吴雨函 / 学号：20235765 / 项目：本地 Agent 智能体系统（B 方向）

---

## 1. 模块概述

### 1.1 模块名称

`B1 — Agent 运行与消息管理模块（Agent Runtime & Message Management）`

### 1.2 模块说明

B1 是整个 Agent 系统的**唯一编排者与消息管理者**。它负责接收用户输入，调用 Memory（B5）、Tool（B3）和 LLM（B4）模块的标准接口，维护完整的消息序列（SystemMessage、HumanMessage、AIMessage、ToolMessage），并控制一次 Agent 任务从用户输入到最终回答的完整运行流程。

B1 的核心原则是：**不执行 Skill，只把模型输出、工具结果和消息历史串起来**。它通过 B3 间接调用 B2 的 Skill 函数，通过 B5 获取和保存记忆，通过 B4 完成 LLM 推理决策。

### 1.3 完成情况概览

| 类型 | 完成情况 |
|---|---|
| 基础要求 | 1. 支持接收一个用户问题<br>2. 调用 B5 获取全局 memory 与用户所选 memory<br>3. 将 system prompt、memory context、用户输入构造成初始 messages<br>4. 调用 B3 获取当前 tools_schema，传给 B4 完成模型与工具绑定<br>5. 调用 B4 获得 AIMessage，判断是否包含 tool_calls<br>6. 支持调用 B3 执行工具，支持至少一次“LLM → Tool → LLM”闭环<br>7. 支持 max_turns，避免工具调用死循环<br>8. 能够说明 SystemMessage、HumanMessage、AIMessage、ToolMessage 的作用 |
| 进阶要求 | 1. **支持多轮用户输入**：实现 `--interactive` 交互式多轮对话模式，支持记忆继承<br>2. **支持批量任务运行**：实现 `--batch` 模式，读取 JSONL 批量任务文件<br>3. **支持 system prompt 模板切换**：交互模式下支持 `@creative`、`@concise`、`@default` 切换 |
| 可独立运行的演示 | `python b1_agent_runtime.py --input ../data/b1_fixtures/b1_fixture_input.json --outdir ../outputs/B1_fixture` |
| 与团队系统集成情况 | 通过函数调用 `run_agent()` 被 `run_full_demo.py` 调用；内部调用 B3、B4、B5 的标准接口 |

---

## 2. 环境、模型与数据依赖

### 2.1 运行环境

| 项目 | 要求 |
|---|---|
| Python 版本 | 3.10 |
| 必要依赖 | PyYAML；项目内 `common/` 工具模块（io_utils, logging_utils, path_utils, schemas） |
| 是否需要模型 | 是（集成模式需 Qwen3.5-4B） |
| 是否需要 GPU | 可选（CPU 可运行） |
| 是否需要外部数据集 | 否 |

### 2.2 依赖安装步骤

```bash
conda create -n agent python=3.10 -y
conda activate agent
cd agent
pip install -r requirements.txt

### 2.3 模型依赖

| 项目 | 说明 |
| :--- | :--- |
| **模型名称** | `Qwen3.5-4B` |
| **下载地址** | [ModelScope - Qwen3.5-4B](https://www.modelscope.cn/models/Qwen/Qwen3.5-4B) |
| **本地路径** | `Qwen3.5-4B/` （由 `configs/model.yaml` 中的 `model_name_or_path` 指定） |
| **是否必须** | 集成模式（`--llm_mode integrated`）下必须 |

## 2.4 样例数据依赖

| 数据或文件来源 | 项目内相对路径 | 用途 |
| :--- | :--- | :--- |
| **runtime_input.json** | `data/runtime_input.json` | 用户提供，单任务输入配置 |
| **b1_fixture_input.json** | `data/b1_fixtures/b1_fixture_input.json` | 项目自带，B1 演示模式输入 |
| **local_tool_agent.txt** | `prompts/local_tool_agent.txt` | 项目自带，系统提示词 |
| **tools.yaml** | `configs/tools.yaml` | 项目自带，工具定义 |
| **memory.yaml** | `configs/memory.yaml` | 项目自带，记忆配置 |
| **model.yaml** | `configs/model.yaml` | 项目自带，模型配置 |

---

## 3. 文件结构与接口边界

### 3.1 文件结构

```text
agent/
├── code/
│   ├── b1_agent_runtime.py        # B1 核心代码（CLI入口+所有逻辑）
│   ├── b2_run_skill.py            # B2 Skill 工具函数模块
│   ├── b3_tool_layer.py           # B3 工具层模块
│   ├── b4_local_agent_llm.py      # B4 LLM 决策模块
│   ├── b5_memory.py               # B5 记忆模块
│   └── common/                    # 公共工具包
│       ├── io_utils.py            # JSON/YAML/JSONL 读写
│       ├── logging_utils.py       # 日志时间戳
│       ├── path_utils.py          # 路径解析
│       └── schemas.py             # 数据格式校验
├── configs/
│   ├── tools.yaml                 # 工具集配置
│   ├── memory.yaml                # 记忆配置
│   └── model.yaml                 # 模型配置
├── data/
│   ├── runtime_input.json         # 单任务输入
│   ├── b1_fixtures/               # B1 演示模式输入
│   ├── messages/                  # 预设消息样例
│   ├── tool_inputs/               # 工具输入样例
│   └── memory_inputs/             # 记忆输入样例
├── prompts/
│   └── local_tool_agent.txt       # 系统提示词
├── skills/                        # B2 Skill 实现
│   ├── calculator.py
│   ├── file_reader.py
│   ├── local_file_search.py
│   ├── table_analyzer.py
│   └── format_converter.py
├── memory/                        # 记忆文档存储（运行时生成）
│   ├── global/
│   ├── conversations/
│   └── memory_index.json
└── outputs/                       # 运行输出（运行时生成）
    ├── B1_fixture/
    ├── B1_runtime_mock/
    └── B1_runtime_real/

### 3.2 接口边界

| 类型 | 来源 / 去向 | 数据格式 | 说明 |
| :--- | :--- | :--- | :--- |
| **输入①** | 用户 / CLI | `runtime_input.json` | 任务配置（`conversation_id`, `user_input`, `system_prompt_path`, `toolset`, `max_turns`, `save_memory` 等） |
| **输入②** | B5（函数调用） | `dict` | `load_memory()` 返回的 `selected_memory` |
| **输入③** | B3（函数调用） | `list[dict]` | `get_tools_schema()` 返回的 `tools_schema` |
| **输入④** | B4（函数调用） | `dict` | `generate_ai_message()` / `generate_agent_decision()` 返回的 `AIMessage` |
| **输出①** | 用户 | `final_answer.md` | Agent 最终回答 |
| **输出②** | 日志 | `messages.json` | 完整消息序列 |
| **输出③** | 日志 | `trace.json` | 运行轨迹、状态、错误 |
| **输出④** | B5（函数调用） | `dict` | `save_memory()` 保存记忆 |

---

## 4. 基础要求实现与演示

### 4.1 核心概念讲解

* **Agent**: 能够接收目标、调用工具、利用记忆并生成最终结果的智能体。
* **Agent Loop**: 执行循环流程：`LLM 生成 AIMessage` $\rightarrow$ `若有 tool_calls 则执行工具并返回 ToolMessage` $\rightarrow$ `直到生成最终回答`。
* **SystemMessage**: 系统提示词，规定 Agent 的身份、行为边界和工具使用规则。
* **HumanMessage**: 用户输入消息，表示当前任务或问题。
* **AIMessage**: LLM 生成的消息，可能包含最终回答（`content`）或 `tool_calls`。
* **ToolMessage**: 工具执行后的返回消息，把工具结果写回 `messages` 供 LLM 继续使用。
* **messages**: 一次 Agent 运行中的完整消息序列，是 LLM 每轮决策的上下文。
* **tool_calls**: `AIMessage` 中的工具调用请求，包含工具名称、调用 ID 和参数。
* **max_turns**: 最大工具调用轮次，避免 Agent 陷入无限循环。
* **final_answer**: 没有新的 `tool_calls` 时，Agent 输出给用户的最终回答。

### 4.2 处理流程

```text
读取 runtime_input.json
└── 读取 system prompt
    └── 调用 B5.load_memory()，获得 selected_memory
        └── 构造初始 messages（system + user + memory_context）
            └── 调用 B3.get_tools_schema()，获得 tools_schema
                └── 调用 B4.generate_agent_decision() 或 generate_ai_message()
                    ├── 无 tool_calls：写入 final_answer
                    └── 有 tool_calls：
                        ├── 调用 B3.execute_tool_calls()
                        ├── 追加 AIMessage + ToolMessage 到 messages
                        └── 再次调用 B4（循环，直到无 tool_calls 或达到 max_turns）
                            ├── 保存 messages.json / trace.json / final_answer.md
                            └── 根据 save_memory 调用 B5.save_memory()
```

### 4.3 基础功能实现路径

* **`b1_agent_runtime.py` -> `run_agent()`**
  主入口函数，控制整个 Agent 运行流程。
* **`b1_agent_runtime.py` -> `_validate_runtime_input()`**
  校验 `runtime_input.json` 的格式和内容。
* **`b1_agent_runtime.py` -> `_memory_context()`**
  将 B5 返回的记忆重构为上下文文本。
* **`b1_agent_runtime.py` -> `_initial_user_content()`**
  构造初始 `HumanMessage`（支持 `text`/`audio` 模式）。
* **`b1_agent_runtime.py` -> `_interpret_agent_decision()`**
  解析 B4 的决策结果（`direct`/`plan`）。
* **`b1_agent_runtime.py` -> `_tool_failures()`**
  检测工具调用失败并生成失败报告。
* **`b1_agent_runtime.py` -> `_replan_instruction()`**
  生成重新计划的指令。
* **`common/schemas.py` -> `validate_ai_message()`**
  格式校验 `AIMessage`。

### 4.4 输入格式与样例

**`runtime_input.json` 示例：**
```json
{
  "conversation_id": "conv_001",
  "user_input": "帮我阅读 docs/agent_intro.txt，总结三条中文要点。",
  "system_prompt_path": "../prompts/local_tool_agent.txt",
  "selected_memory_ids": ["mem_conversation_conv_000"],
  "use_global_memory": true,
  "toolset": "basic_tools",
  "max_turns": 3,
  "save_memory": "conversation"
}
```

**字段说明：**

| 字段 | 类型 | 是否必要 | 说明 |
| :--- | :--- | :---: | :--- |
| **conversation_id** | string | 是 | 当前对话或任务编号 |
| **user_input** | string | 是 | （文本模式）用户问题或任务描述 |
| **system_prompt_path** | string | 是 | 系统提示词文件路径 |
| **selected_memory_ids** | list[string] | 否 | 用户显式选择加载的记忆文档 ID |
| **use_global_memory** | boolean | 否 | 是否加载全局记忆（默认 `false`） |
| **toolset** | string | 是 | 当前可用工具集合名称 |
| **max_turns** | integer | 是 | 最大允许的工具调用次数 |
| **save_memory** | string | 是 | 是否保存本轮记忆（`none`/ `conversation`/ `global`） |

### 4.5 基础功能演示命令

```bash
cd agent/code

# 1. 个人演示模式（Fixture）
python b1_agent_runtime.py \
  --input ../data/b1_fixtures/b1_fixture_input.json \
  --outdir ../outputs/B1_fixture
<img width="1918" height="998" alt="6d4c1210-0c2a-4a18-9f1d-922db473d023" src="https://github.com/user-attachments/assets/8f8128a2-1e0b-4ae4-99cb-fb35dea59390" />

# 2. 全系统集成模式（Mock 模式，不调用真实模型）
python b1_agent_runtime.py \
  --input ../data/runtime_input.json \
  --tools_config ../configs/tools.yaml \
  --memory_config ../configs/memory.yaml \
  --model_config ../configs/model.yaml \
  --llm_mode mock \
  --outdir ../outputs/B1_runtime_mock

# 3. 全系统集成模式（真实模型）
python b1_agent_runtime.py \
  --input ../data/runtime_input.json \
  --tools_config ../configs/tools.yaml \
  --memory_config ../configs/memory.yaml \
  --model_config ../configs/model.yaml \
  --llm_mode integrated \
  --outdir ../outputs/B1_runtime_real
```

### 4.6 输出格式

| 输出文件 / 返回字段 | 格式 | 说明 |
| :--- | :--- | :--- |
| `outputs/B1_*/messages.json` | JSON 数组 | 完整消息序列 |
| `outputs/B1_*/trace.json` | JSON 对象 | 运行状况（turns、status、error、warnings） |
| `outputs/B1_*/final_answer.md` | Markdown 文本 | Agent 最终回答 |
| `outputs/B1_*/saved_memory.json` | JSON 对象 | 记忆保存结果（如果 `save_memory != "none"`） |
| `outputs/B1_*/runtime_log.jsonl` | JSONL | 运行日志（追加模式） |
| `outputs/B1_*/agent_decision.json` | JSON 对象 | B4 决策记录（仅集成模式生成） |

### 4.7 运行图例

**终端输出示例：**
```text
input_type: text
user_input: 帮我阅读 docs/agent_intro.txt，总结三条中文要点。
content: Agent 系统通常由模型、工具、记忆和执行循环组成...
```

**`messages.json` 示例：**
```json
[
  {"role": "system", "content": "You are a local tool-using agent..."},
  {"role": "user", "content": "帮我阅读 docs/agent_intro.txt..."},
  {"role": "assistant", "content": "", "tool_calls": [{"id": "call_001", "name": "file_reader", "args": {}}]},
  {"role": "tool", "tool_call_id": "call_001", "name": "file_reader", "content": "{...}"},
  {"role": "assistant", "content": "根据文件内容，总结如下：..."}
]
```

---

## 5. 进阶要求实现与演示

### 5.1 进阶功能完成度

| 进阶要求 | 是否完成 | 对应文件 / 函数 | 简要说明 |
| :--- | :---: | :--- | :--- |
| **支持多轮用户输入** | ✅ 是 | `b1_agent_runtime.py` -> `main()` | `--interactive` 分支实现交互式多轮对话，支持记忆继承 |
| **支持批量任务运行** | ✅ 是 | `b1_agent_runtime.py` -> `main()` | `--batch` 分支读取 JSONL 文件，批量执行多个 Agent 任务 |
| **支持系统提示模板切换** | ✅ 是 | `b1_agent_runtime.py` -> `PROMPT_SWITCH_MAP` | 交互模式下通过 `@creative`/ `@concise`/ `@default` 指令动态切换 |

### 5.2 进阶功能 1：交互式多轮对话（`--interactive`）

* **功能说明**: 在交互模式下，用户可以连续输入多轮问题。系统会自动保存每轮对话的记忆（`save_memory: "conversation"`），把上一轮的记忆 ID 传递给下一轮作为 `selected_memory_ids` 实现记忆继承。同时支持通过快捷指令切换系统提示模板。
* **演示命令**:
  ```bash
  cd agent/code
  python b1_agent_runtime.py \
    --input ../data/runtime_input.json \
    --tools_config ../configs/tools.yaml \
    --memory_config ../configs/memory.yaml \
    --model_config ../configs/model.yaml \
    --interactive \
    --outdir ../outputs/B1_fixture/interactive
  ```
* **交互示例**:
  ```text
  [Interactive] 多轮对话模式启动，输入 'exit' 或 'quit' 退出
  [Interactive] 提示：输入 @creative 切换创意模式，@concise 切换简洁模式
  你: 帮我总结一下 Agent 的核心概念
  Agent: Agent 是一个能够自主理解目标、调用工具并完成任务的智能体...
  你: @creative
  [Interactive] 已切换到 @creative 模式
  你: 用更有创意的方式重新解释
  Agent: 想象 Agent 是一个数字员工，它能像人一样思考...
  ```

### 5.3 进阶功能 2：批量任务运行（`--batch`）

* **功能说明**: 读取 JSONL 格式的批量任务文件，每行包含一个任务的 `input` 和 `outdir`，系统将依次执行所有任务并统计成功/失败数量。
* **输入格式 (`batch_tasks.jsonl`)**:
  ```json
  {"input": "../data/task1.json", "outdir": "../outputs/batch/task1"}
  {"input": "../data/task2.json", "outdir": "../outputs/batch/task2"}
  {"input": "../data/task3.json", "outdir": "../outputs/batch/task3"}
  ```
* **演示命令**:
  ```bash
  cd agent/code
  python b1_agent_runtime.py --batch ../data/batch_tasks.jsonl
  ```
* **输出示例**:
  ```text
  [Batch] 开始批量运行，共 3 个任务
  [Batch] 正在执行任务 1/3: ../data/task1.json -> ../outputs/batch/task1
  [Batch] 任务 1 完成 ✓ (status: success)
  [Batch] 正在执行任务 2/3: ../data/task2.json -> ../outputs/batch/task2
  [Batch] 任务 2 完成 ✓ (status: success)
  [Batch] 正在执行任务 3/3: ../data/task3.json -> ../outputs/batch/task3
  [Batch] 任务 3 完成 ✓ (status: success)
  [Batch] 全部任务执行完毕。成功: 3, 失败: 0
  ```
<img width="1911" height="1022" alt="468fa7fd-31bb-4761-a6eb-5376dc3d3fd1" src="https://github.com/user-attachments/assets/99760013-a28b-468c-9274-dab5790e5a3b" />

### 5.4 进阶功能 3：系统提示模板切换

* **功能说明**: 在交互模式下，用户可以通过在输入框中输入特殊命令来实时切换系统提示。
* **命令关系映射**:
  ```python
  PROMPT_SWITCH_MAP = {
      "@creative": "../prompts/creative_system.txt",  # 切换到创意模式提示词
      "@concise": "../prompts/concise_system.txt",    # 切换到简洁模式提示词
      "@default": None,                               # 切回原始默认提示词（prompts/local_tool_agent.txt）
  }
  ```

---

## 6. 与团队系统的集成说明

B1 作为整个 Agent 系统的主要运行模块，与 B3、B4、B5 模块采用 Python 函数调用的方式进行紧密集成：

1. **Schema 生成**: B1 调用 `b3_tool_layer.get_tools_schema(config_path, toolset, outdir)` -> 返回 `list[dict]`（tools_schema 数组）。B1 将结果传给 B4 注入提示上下文。
2. **工具调用执行**: B1 调用 `b3_tool_layer.execute_tool_calls(tool_calls, config_path, toolset, outdir)` -> 返回 `list[dict]`（ToolMessage 数组）。B1 将 ToolMessage 追加到 `messages` 中继续驱动代理循环。
3. **记忆管理**: B1 调用 `b5_memory.load_memory()` 和 `b5_memory.save_memory()` 实现状态的持久化与读取。
4. **LLM 决策**: B1 调用 `b4_local_agent_llm.generate_agent_decision()` 并 `generate_ai_message()` 来获取下一步行动。

> 💡 **全系统入口调用方式（由 `run_full_demo.py` 调用）：**
> ```bash
> cd agent/code
> python run_full_demo.py \
>   --input ../data/runtime_input.json \
>   --tools_config ../configs/tools.yaml \
>   --memory_config ../configs/memory.yaml \
>   --model_config ../configs/model.yaml \
>   --llm_mode prompt_json \
>   --outdir ../outputs/full_demo
> ```

---

## 7. 已知问题与后续改进

| 当前已知问题 | 关键原因 | 后续改进计划 |
| :--- | :--- | :--- |
| **音频输入模式未充分测试** | 依赖外部 ASR 工具（`speech_to_text`） | 音频输入完善测试用例，集成更稳定的 ASR 服务 |
| **断点续跑能力设定有限** | 目前仅支持 `memory_id` 继承，未实现完整状态序列化 | 实现完整的系统 `checkpoint`/`restore` 恢复机制 |
| **`max_turns` 达到时直接强行中断** | 设计使然，旨在严格避免代理无限工具循环 | 可增加平滑退出机制，引导 B4 生成当前进度的总结性回答 |
| **多轮对话中记忆积累可能过长** | 未实现历史消息的摘要压缩 | 引入历史消息摘要与动态窗口滑动压缩功能 |
| **批量模式下某任务错误会导致后续中断** | 默认采用异常抛出设计 | 计划增加 `--stop_on_error` 选项来控制是否跳过错误任务 |
