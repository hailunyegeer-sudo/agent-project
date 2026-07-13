# B3 工具说明与调用桥接层

## B3 的位置

B3 位于 LLM 决策和 B2 Skill 之间。B4 负责根据 messages 与 tools_schema 输出 AIMessage；当 AIMessage 内存在 tool_calls 时，B3 才开始工作。它不决定要不要调用工具，也不代替 B2 处理实际业务。

## tools_schema 生成

B3 会读取 tools.yaml。配置中记录工具模块、函数名称、功能描述、parameters、required 与 returns。B3 根据当前 toolset 选择工具，将 YAML 配置转换为模型可识别的 tools_schema。模型通过这份说明了解 calculator 的 expression 参数、file_reader 的 path 与 max_chars 参数、local_file_search 的 query 与 root_dir 参数等信息。

## 工具调用执行

一次工具调用请求通常包含 id、name 与 args。B3 会先检查 name 是否存在于当前 toolset，再检查 required 参数是否缺失，并进行基础类型校验。随后 B3 动态导入 B2 对应 Skill，执行后把 SkillResult 转换为 ToolMessage。ToolMessage 中保留 tool_call_id、name、status 和 JSON 字符串形式的 content，B1 会将其写回 messages。

## 测试价值

查询 `tools_schema`、`B3 工具调用` 或 `参数校验` 时，本文应该有较高相关性。本文虽然也会提到 Agent、工具调用等通用词，但不以“Agent工具调用”连续主题为主，因此对精确短语查询应处于中等位置。
