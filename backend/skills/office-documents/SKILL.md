---
name: office-documents
description: 读取或修改用户已有的 Word、Excel、PPT、PDF，按模板填数据，或把 Office 转 PDF。用户上传文件要求总结、改第几段、给表格加行、插趋势图、批量出合同时使用。
metadata:
  owner: Syncore
  version: "1.1"
  dependencies: 办公文档内置插件，原生读写依赖 python-docx、openpyxl、python-pptx、pypdf；转换引擎可选部署。
allowed-tools:
  - read_document
  - edit_document
  - merge_document
  - batch_merge_documents
  - convert_document
  - office_capabilities
  - generate_document
---

# 处理办公文件

用户给了原件时，先 `read_document`；附件原件路径在本轮上下文里。按 offset 继续读到相关段落、表格或工作表。附件摘要被截断不能证明原件没有后续内容。

修改已有文档用 `edit_document`，另存到工作目录的新路径。Word/PPT 用读取结果里的定位符和原文做精确替换，不能根据提取文本重新生成整份文件。段落定位符 `p:3` 是正文第三段，含空段落；表格是 `t:1:r:2:c:1:p:1`；PPT 是 `s:1:sh:2:p:1`。

需要新增文档才用 `generate_document`。Word 页眉、页脚和目录可以生成后再编辑，目录须在 Word 中更新域。用户要求保留修订时，用 `replace_text_tracked` 并指定 author；它在普通文本段落里写入可接受/拒绝的插入和删除修订，跨 run 保留原文。含已有修订、超链接或特殊域的段落不支持这项操作，不能假称已完成复杂文档无损修改。

所有工具返回同一 JSON 契约：summary 是执行摘要，content 是读取正文或详细说明，artifacts 是实际交付文件（路径、格式、大小、SHA-256）。读取返回的 source_sha256 可传给编辑的 expected_sha256，发现原件变化时先重读。输出默认不覆盖，只有用户要求时设 overwrite=true。错误中的稳定代码用于区分原件改变、输出已存在、依赖缺失与处理失败；不要把失败当作已交付。

Excel 定点写入必须明确 sheet、A1 坐标及 old_text。新行用 append_row；趋势图用 add_chart，data_range 例如 A1:C13，首列为分类、首行为系列名，target 为图表锚点（例如 E2）。图表是可编辑原生图表。openpyxl 不重算公式，读取到公式不能当作已计算数值，未缓存结果不能按零统计。

`merge_document` 用 JSON 的 values 填充 `{{字段名}}`，支持跨格式片段的占位符；缺字段先补齐。批量用 `batch_merge_documents`，提供 records 数组和 .zip 输出路径，一次最多 500 条；压缩包中文档按输入顺序编号。任一条失败不会交付不完整批次。不把收件人、金额、日期猜成事实。

Office/PDF 转换先 `office_capabilities` 查看引擎状态，再 `convert_document`。原生读取无需额外安装；MarkItDown 用于 Markdown 提取，Docling 用于复杂 PDF，需要额外依赖和模型。扫描页未提取到文字时明确说明需要 OCR，不能凭文件名猜内容。PDF 转 Word 的版式可能改变，转换后的文件应核对。

回复给出结果文件路径与实际完成的修改；工具失败时不声称已交付。只提供用户本次请求需要的操作。
