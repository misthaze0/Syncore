"""
从 Python 函数签名自动推导工具参数 JSON Schema

★ 为什么要有这个模块

原先 13 个内置工具的 args_schema 是一份手写的 JSON 字典，和函数形参**平行存在**。
字典改了签名没改（或反过来），要到模型调用失败时才暴露，而且报错信息是
「参数错误: got unexpected keyword argument」这种对模型毫无用处的话。
签名本身就是唯一的真相：类型注解给出 JSON 类型，默认值给出可选性，
docstring 的 Args 段给出每个参数的描述。

推导出的 schema 仍然是一份普通 dict —— `registry._build_args_schema()` 和
MCP 适配器共用它，两条来源（代码推导 / 远端 server 上报）在注册中心里同构。
"""

from __future__ import annotations

import inspect
import re
import types
import typing
from dataclasses import dataclass, field
from typing import Any, get_args, get_origin

#: PEP 604 的 `X | Y` 在运行时是 types.UnionType（typing.UnionType 并非所有版本都有）
_UnionType = getattr(types, "UnionType", None)

# Python 类型 → JSON Schema 类型片段
_SIMPLE_TYPES: dict[Any, dict[str, Any]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    list: {"type": "array", "items": {"type": "string"}},
    dict: {"type": "object"},
    tuple: {"type": "array", "items": {"type": "string"}},
    set: {"type": "array", "items": {"type": "string"}},
}

# 参数描述的分节标题（中英都认，因为这个项目的 prompt 是中文的）
_SECTION_HEADS = ("args", "arguments", "parameter", "parameters", "参数", "入参", "参数说明")
_SECTION_STOPS = (
    "returns",
    "return",
    "raises",
    "examples",
    "example",
    "note",
    "notes",
    "yields",
    "see also",
    "注意",
    "说明",
    "返回",
    "示例",
    "用法",
)


@dataclass
class ParsedDoc:
    """docstring 拆解结果"""

    summary: str = ""
    param_docs: dict[str, str] = field(default_factory=dict)


def parse_docstring(doc: str | None) -> ParsedDoc:
    """
    拆 docstring：首段作为工具描述，`Args:` / `参数:` 小节里的
    `name (type): desc` 或 `name: desc` 作为参数描述。

    不追求解析所有 Sphinx/NumPy 风格 —— 写不出来的行会静默忽略，
    作者随时可以在 `@agent_tool(args_docs={...})` 里显式补齐。
    """
    if not doc:
        return ParsedDoc()

    lines = inspect.cleandoc(doc).splitlines()
    summary: list[str] = []
    params: dict[str, str] = []
    in_args = False

    for raw in lines:
        stripped = raw.strip()
        head = stripped.rstrip(":：").lower()
        if stripped and not raw.startswith(" ") and not raw.startswith("\t"):
            # 顶层小节标题才做切换判断，缩进内容一律当正文
            if head in _SECTION_HEADS:
                in_args = True
                continue
            if head in _SECTION_STOPS:
                in_args = False
                continue
        if in_args:
            if stripped:
                params.append(stripped)
        else:
            summary.append(raw)

    return ParsedDoc(
        summary="\n".join(summary).strip(),
        param_docs=_parse_param_lines(params),
    )


def _parse_param_lines(lines: list[str]) -> dict[str, str]:
    """把 `path (str): 文件路径` 这类行解析成 {参数名: 描述}"""
    out: dict[str, str] = {}
    current: str | None = None
    for line in lines:
        # name: desc  /  name(Type): desc  /  name (Type) : desc
        match = re.match(r"^([A-Za-z_][\w]*)\s*(?:\([^)]*\))?\s*[:：]\s*(.*)$", line.strip())
        if match:
            current = match.group(1)
            out[current] = match.group(2).strip()
        elif current:
            # 上一行参数描述的续行
            out[current] = f"{out[current]} {line.strip()}".strip()
    return {k: v for k, v in out.items() if v}


def _annotation_to_schema(annotation: Any) -> dict[str, Any]:
    """类型注解 → JSON Schema 片段（未知类型降级为 string，不抛异常）"""
    if annotation is inspect.Parameter.empty or annotation is Any or annotation is None:
        return {"type": "string"}

    origin = get_origin(annotation)

    # Annotated[X, "描述"] —— 先剥元数据再看底层类型（get_origin 对 Annotated
    # 返回的是底层的 origin，放在后面判会漏）
    if hasattr(annotation, "__metadata__"):
        return _annotation_to_schema(get_args(annotation)[0])

    # Optional[X] / X | None —— 只在真的拿到 union origin 时才走这条路，
    # 否则 `origin is None`（普通类型的 get_origin 就是 None）会被误判成联合类型
    if origin is not None and (origin is typing.Union or origin is _UnionType):
        candidates = [a for a in get_args(annotation) if a is not type(None)]
        if len(candidates) == 1:
            return _annotation_to_schema(candidates[0])
        if not candidates:
            return {"type": "string"}
        return {"anyOf": [_annotation_to_schema(a) for a in candidates]}

    # Literal["a", "b"] → enum
    if origin is typing.Literal:
        values = list(get_args(annotation))
        kinds = {type(v) for v in values}
        base = _SIMPLE_TYPES.get(next(iter(kinds), str), {"type": "string"})
        return {**base, "enum": values}

    if origin in (list, set, frozenset, tuple):
        args = [a for a in get_args(annotation) if a is not Ellipsis]
        items = _annotation_to_schema(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": items}

    if origin is dict:
        args = get_args(annotation)
        schema: dict[str, Any] = {"type": "object"}
        if len(args) == 2:
            schema["additionalProperties"] = _annotation_to_schema(args[1])
        return schema

    if isinstance(annotation, type):
        if issubclass(annotation, bool):
            return {"type": "boolean"}
        if issubclass(annotation, int):
            return {"type": "integer"}
        if issubclass(annotation, float):
            return {"type": "number"}
        if issubclass(annotation, str):
            return {"type": "string"}
        if issubclass(annotation, (list, tuple, set)):
            return {"type": "array", "items": {"type": "string"}}
        if issubclass(annotation, dict):
            return {"type": "object"}
        # Pydantic 模型 / dataclass：让模型传对象
        return {"type": "object"}

    return {"type": "string"}


def _annotated_description(annotation: Any) -> str:
    """从 Annotated[X, "..."] 的字符串元数据里取参数描述"""
    metadata = getattr(annotation, "__metadata__", None)
    if not metadata:
        return ""
    for item in metadata:
        if isinstance(item, str) and item:
            return item
    return ""


def is_tool_context_annotation(annotation: Any) -> bool:
    """
    是否为 `ToolContext` 标注（含 `ToolContext | None` 这类联合形式）。

    工具模块普遍 `from __future__ import annotations`，注解可能是类对象也可能是
    字符串，所以两条都要认。
    """
    if annotation is inspect.Parameter.empty or annotation is None:
        return False
    if getattr(annotation, "__name__", "") == "ToolContext":
        return True
    text = str(annotation)
    if ".ToolContext" in text or text.startswith("ToolContext"):
        return True
    for arg in get_args(annotation):
        if is_tool_context_annotation(arg):
            return True
    return False


def derive_args_schema(fn: Any) -> dict[str, Any]:
    """
    生成工具参数 JSON Schema。

    规则：
    - 无默认值的形参 → required
    - `*args` / `**kwargs` 与 `self` / `cls` → 忽略（无法用 JSON 表达）
    - 标注为 `ToolContext` 的形参 → 运行时注入，模型看不到
    """
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except Exception:  # noqa: BLE001
        hints = dict(getattr(fn, "__annotations__", {}) or {})

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in inspect.signature(fn).parameters.items():
        if name in ("self", "cls") or param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        annotation = hints.get(name, param.annotation)
        if is_tool_context_annotation(annotation):
            continue

        schema = _annotation_to_schema(annotation)
        desc = _annotated_description(annotation)
        if desc:
            schema["description"] = desc
        if param.default is inspect.Parameter.empty:
            required.append(name)
        elif isinstance(param.default, (str, int, float, bool, list, dict, tuple)):
            # None 不算「有默认值」：写进 schema 会诱导模型显式传 null
            schema["default"] = list(param.default) if isinstance(param.default, tuple) else param.default
        properties[name] = schema

    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def enrich_schema_descriptions(schema: dict[str, Any], param_docs: dict[str, str]) -> dict[str, Any]:
    """把 docstring 里解析出的参数描述补进 schema（Annotated 显式描述优先）"""
    props = schema.get("properties") or {}
    for name, prop in props.items():
        if not prop.get("description") and param_docs.get(name):
            prop["description"] = param_docs[name]
    return schema


__all__ = [
    "ParsedDoc",
    "derive_args_schema",
    "enrich_schema_descriptions",
    "is_tool_context_annotation",
    "parse_docstring",
]
