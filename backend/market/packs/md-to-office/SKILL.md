---
name: md-to-office
description: 把 Markdown 或纯文本稿转成可交付的 Word、Excel、PPT 文件，并处理表格、标题层级和分页。用户说「转成 Word」「做成 PPT」「导出成表格」「给我个办公格式」时使用。转换前先报结构核对，转完说清文件路径。
compatibility: 读原稿用 read；出文件用 generate_document（按扩展名分派 docx/xlsx/pptx）；转换失败时说明是哪一处结构不支持，并给降级方案。
metadata:
  owner: Syncore
  version: "1.0"
  market_name: 转 Word / Excel / PPT
  market_category: 文档处理
  market_tags:
    - 转换
    - Word
    - PPT
  market_popularity: 81
  market_featured: true
allowed-tools:
  - read
  - ls
  - glob
  - generate_document
  - write
---

# 转办公格式怎么转

转换的坑在**结构丢失**：表格并成一段、层级拍平、PPT 一页塞八百字。所以先对结构，再谈转换。

## 步骤

1. **先读原稿并列结构清单**：几个一级标题、几张表（各自行列数）、有没有代码块/图片/脚注。这张清单就是转换后的验收标准。
2. **按目标格式做取舍**（这一步要告诉用户，不要默默做）：
   - **Word**：标题层级映射到样式；Markdown 表格转真表格；代码块保留等宽但注明"未做语法高亮"；
   - **Excel**：只有一张主表时才转 xlsx，多表就多 sheet；正文段落不要塞进单元格；
   - **PPT**：**一页一个要点**，正文超过 60 字就该拆页 —— 把长文原样搬进幻灯片是最常见的失败。需要时先压缩成提纲给用户看一眼再生成。
3. **生成**：`generate_document(path, content, title)`，扩展名决定格式。文件名用中文可读名，不放空格和斜杠。
4. **回读核对**：对照第 1 步的结构清单逐项确认（标题数对不对、表格行列有没有少）。核对结果如实写，**没核对不要说"已转换完成且无误"**。
5. 回复里给：文件绝对路径 + 结构清单核对结果 + 降级了什么。

## 常见降级与说法

- 图片外链：转不出来，说明"图片需手动插入，原链接在 X 处"；
- 复杂合并单元格：拆成规整表并标注"原表有合并，已拆开"；
- 公式：Markdown 公式在 docx 里多半变纯文本，提前说。

## 红线

- 不改内容：错别字、数字、引语原样带过去，发现问题只报告；
- 覆盖同名文件前先确认，别把用户上一版冲掉。
