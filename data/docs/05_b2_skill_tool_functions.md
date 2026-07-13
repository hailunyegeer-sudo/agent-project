# B2 Skill工具函数模块说明

## 模块职责

B2 是 Agent 系统中的具体能力层。它只实现可以被调用的工具函数，不负责工具绑定，不负责 tools_schema 生成，也不决定模型是否需要工具。基础 Skill 可以包括 calculator、file_reader、local_file_search、table_analyzer 与 format_converter。每个 Skill 都需要有明确输入参数、可 JSON 序列化的返回值，以及可独立运行的命令行测试方式。

## SkillResult

为了让 B3 能够稳定处理 B2 返回内容，Skill 的业务结果需要包装为统一结构。一个 SkillResult 通常包含：skill_name、status、input、output、error、latency_ms。成功时 status 为 success，失败时 status 为 error，并保留错误类型与错误消息。这样单个工具异常不会使 Agent 主流程直接崩溃。

## local_file_search 的作用

local_file_search 用于在本地 docs 或其他允许目录内查找相关文本文件。基础实现可按关键词出现次数排序；增强实现可同时使用完整查询短语、辅助词、文件名命中和稳定排序规则。该工具的目标不是替代向量数据库，而是在不额外部署模型和索引的情况下，提高课程项目中的本地文本查找效果。

## 与 B3 的关系

B3 读取 tools.yaml，根据 toolset 生成工具说明，接收 B4 返回的 tool_calls，校验工具名和参数后调用 B2。B2 只需专注于函数正确执行。这样 B2 的 Skill 可独立演示，也可以被 B3 在完整 Agent 中复用。

## 检索提示

对于查询 `B2 Skill工具函数`，本文件应该具有较高相关度。对于查询 `Agent 工具调用`，本文也可能被召回，因为包含 Agent、工具、调用等词，但通常不应超过精确描述 Agent工具调用链路的文档。
