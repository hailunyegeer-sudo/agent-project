# B4 模型决策与 B5 记忆协作

## B4 决策模块

B4 接收 B1 提供的 messages 和 B3 提供的 tools_schema，调用本地 LLM 生成标准 AIMessage。AIMessage 要么包含自然语言 content，要么包含 tool_calls。调用工具时 content 往往为空；工具结果返回后，模型再基于 ToolMessage 生成最终答案。

## B5 记忆模块

B5 负责读取和保存 memory 文档。全局记忆适合保存稳定知识，对话记忆适合保存某轮任务中的 messages、trace 与 final_answer。B1 将 B5 返回的记忆文本注入初始 messages，使模型能够利用历史上下文。B5 不参与模型推理，也不直接执行工具。

## 与检索实验的关系

本文件适合测试较宽泛的查询，例如 `B4 模型决策`、`B5 记忆`、`AIMessage ToolMessage`。它可能包含 Agent、工具、调用等离散词语，但不应作为 `Agent 工具调用` 的首选结果。通过这类干扰文档可以观察增强检索是否真的偏向完整主题，而非仅靠常见词叠加。
