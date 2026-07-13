from __future__ import annotations

# B2 Skill 运行阶段的统一错误码。
INVALID_ARGUMENT = "B2-1001"
PATH_OUTSIDE_DATA_ROOT = "B2-1002"
FILE_NOT_FOUND = "B2-1003"
UNSUPPORTED_FILE_TYPE = "B2-1004"
FILE_READ_FAILED = "B2-1005"
SKILL_EXECUTION_FAILED = "B2-1099"

'''
B2-1001  参数非法
B2-1002  路径越出 data_root
B2-1003  文件不存在
B2-1004  文件类型不支持
B2-1005  文件读取失败
B2-1099  Skill 执行错误
'''

class SkillError(Exception):
    """携带统一错误码的 Skill 业务异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def error_to_dict(exc: Exception) -> dict:
    """
    将任意异常统一转换为可 JSON 序列化的错误结构。
    原有 type 和 message 保留，新增 code。
    """
    if isinstance(exc, SkillError):
        return {
            "code": exc.code,
            "type": type(exc).__name__,
            "message": exc.message,
        }

    if isinstance(exc, FileNotFoundError):
        code = FILE_NOT_FOUND
    elif isinstance(exc, (UnicodeDecodeError, PermissionError, OSError)):
        code = FILE_READ_FAILED
    elif isinstance(exc, (ValueError, TypeError)):
        code = INVALID_ARGUMENT
    else:
        code = SKILL_EXECUTION_FAILED

    return {
        "code": code,
        "type": type(exc).__name__,
        "message": str(exc),
    }