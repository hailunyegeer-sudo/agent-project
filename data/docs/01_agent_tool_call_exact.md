# Agent工具调用完整流程说明

## 文档目的

本文件用于说明一个本地 Agent 在处理“读取文件、分析表格、执行计算、转换格式”等任务时，如何完成一次完整的 Agent工具调用。这里故意连续使用“Agent工具调用”这一短语，便于验证本地检索器能否把“完整短语命中”的文档排在只分别包含“Agent”和“工具调用”的文档之前。

## 运行链路

一次 Agent工具调用通常从 B1 运行时开始。B1 读取用户输入、system prompt、memory 以及当前可用的 toolset，组织成 messages。随后 B1 向 B3 请求 tools_schema，再把 messages 与 tools_schema 交给 B4。B4 的职责不是执行工具，而是根据当前上下文决定是否需要调用工具；若需要，返回包含 name、args、id 的 tool_calls。

B3 接收到 tool_calls 后，先判断工具名称是否属于当前 toolset，再检查必填参数是否齐全、参数类型是否合理。校验通过后，B3 动态加载 B2 中对应的 Skill。B2 只执行确定性 Python 函数，例如 file_reader 读取本地文本，calculator 计算安全表达式，local_file_search 在指定目录中查找相关文档。B2 的返回结果会被统一包装为 SkillResult，其中包含输入、输出、状态、错误信息与耗时。

B3 再把 SkillResult 转换为 ToolMessage。ToolMessage 与刚才的 AIMessage 一起追加回 B1 的 messages。B1 再次调用 B4，使模型根据工具结果形成最终自然语言回答。只要 AIMessage 不再包含 tool_calls，当前 Agent工具调用闭环就结束，B1 将 content 保存为 final_answer。

## 检索验证点

本文件与测试集中的 `02_agent_tool_call_separated.md` 内容接近，但本文件在标题、第一段、流程说明中均连续出现“Agent工具调用”。当查询为 `Agent 工具调用` 或 `agent工具调用` 时，增强后的 local_file_search 应将本文件排在前面，并返回 `phrase_matched: true`。同时，由于正文中也包含 Agent、工具调用、B1、B2、B3、B4 等关键词，结果中的 `matched_terms` 应能够体现辅助词命中情况。

## 工程意义

完整的 Agent工具调用并不是“模型直接执行函数”。它是模型决策、参数校验、工具执行、结果标准化、消息回写之间的协同过程。B1 负责编排，B2 提供实际能力，B3 负责桥接和校验，B4 负责决策，B5 负责记忆读写。这样的分层使工具调用可追踪、可调试、可扩展，也便于后续加入缓存、重试、统计和权限控制。
