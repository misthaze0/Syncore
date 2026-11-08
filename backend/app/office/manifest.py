"""办公内置插件的唯一能力清单；不与第三方实现或 MCP 连接状态绑定。"""
PLUGIN_ID = "builtin-office"
PLUGIN_VERSION = "1.1.0"
WRITING_TOOLS = frozenset({
    "generate_document", "edit_document", "merge_document", "batch_merge_documents", "convert_document",
})
TOOLS = WRITING_TOOLS | {"read_document", "office_capabilities"}
SKILLS = frozenset({"office-documents"})


def plugin_manifest() -> dict:
    return {
        "id": PLUGIN_ID, "name": "办公文档", "version": PLUGIN_VERSION,
        "description": "读取和编辑 Word、Excel、PPT、PDF，生成文档、分析图表、填充模板及批量交付。",
        "tools": sorted(TOOLS), "skills": sorted(SKILLS),
        "implementation": "first-party", "contract_version": "1.0",
        "category": "文档处理",
    }
