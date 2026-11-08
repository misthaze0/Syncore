import type { OfficeKind } from '@/types/office';

export interface OfficeTemplate {
  id: string;
  title: string;
  description: string;
  category: string;
  kind: OfficeKind;
  color: string;
  content: string;
}

export const OFFICE_TEMPLATES: OfficeTemplate[] = [
  { id: 'blank', title: '空白文档', description: '从一个想法开始，自由书写', category: '自由创作', kind: 'document', color: 'rose', content: '<p></p>' },
  { id: 'weekly', title: '工作周报', description: '记录进展，让每一份努力被看见', category: '工作汇报', kind: 'document', color: 'blue', content: '<p>记录本周的关键进展，以及下一步的方向。</p><h2>本周工作回顾</h2><p>概括本周最重要的工作成果，优先呈现结果和价值。</p><ul><li><p>重点事项一：目标、行动与结果</p></li><li><p>重点事项二：阶段性进展</p></li></ul><h2>关键数据与成果</h2><table><tr><th><p>事项</p></th><th><p>本周进展</p></th><th><p>完成情况</p></th></tr><tr><td><p>核心项目</p></td><td><p>填写具体进展</p></td><td><p>进行中</p></td></tr></table><h2>问题与支持</h2><p>需要协助的事项、当前的阻碍与建议方案。</p><h2>下周工作计划</h2><ul data-type="taskList"><li data-type="taskItem" data-checked="false"><p>确定下周优先事项</p></li><li data-type="taskItem" data-checked="false"><p>同步关键节点与交付时间</p></li></ul>' },
  { id: 'meeting', title: '会议纪要', description: '从讨论到行动，留住关键共识', category: '团队协作', kind: 'document', color: 'violet', content: '<p><strong>会议时间：</strong>待填写　<strong>参会人员：</strong>待填写</p><h2>会议目标</h2><p>这次会议需要解决什么问题？</p><h2>议题与讨论</h2><h3>议题一</h3><p>记录讨论要点、不同意见与关键结论。</p><h2>达成的共识</h2><blockquote><p>用一句话总结最重要的决定。</p></blockquote><h2>待办与负责人</h2><table><tr><th><p>行动事项</p></th><th><p>负责人</p></th><th><p>截止时间</p></th></tr><tr><td><p>跟进关键事项</p></td><td><p>待分配</p></td><td><p>待确认</p></td></tr></table><h2>下次跟进</h2><p>需要在下一次会议前确认的信息。</p>' },
  { id: 'proposal', title: '项目方案', description: '把好的想法，变成清晰的行动', category: '项目策划', kind: 'document', color: 'amber', content: '<p>一份清晰、可执行的项目计划。</p><h2>项目背景</h2><p>描述现状、机会与需要解决的核心问题。</p><h2>目标与成功标准</h2><ul><li><p>主要目标：希望达成的结果</p></li><li><p>衡量方式：可验证的指标</p></li></ul><h2>实施方案</h2><h3>第一阶段 · 调研与准备</h3><p>明确范围，收集信息，确认关键假设。</p><h3>第二阶段 · 执行与交付</h3><p>说明关键任务、协作方式与交付物。</p><h2>时间安排</h2><table><tr><th><p>阶段</p></th><th><p>主要任务</p></th><th><p>时间节点</p></th></tr><tr><td><p>准备</p></td><td><p>需求确认</p></td><td><p>待确认</p></td></tr><tr><td><p>执行</p></td><td><p>开发与验证</p></td><td><p>待确认</p></td></tr></table><h2>资源与风险</h2><p>列出所需资源、潜在风险与应对措施。</p><h2>下一步行动</h2><p>从最重要、最可执行的一件事开始。</p>' },
  { id: 'prd', title: '产品需求文档', description: '聚焦用户问题，梳理产品细节', category: '产品设计', kind: 'document', color: 'blue', content: '<p><strong>文档状态：</strong>草稿　<strong>版本：</strong>v1.0</p><h2>需求背景</h2><p>用户是谁，在什么场景下遇到了什么问题？</p><h2>用户故事</h2><blockquote><p>作为一名用户，我希望……，以便……</p></blockquote><h2>功能范围</h2><table><tr><th><p>功能</p></th><th><p>说明</p></th><th><p>优先级</p></th></tr><tr><td><p>核心功能</p></td><td><p>填写行为与规则</p></td><td><p>P0</p></td></tr></table><h2>交互与异常</h2><p>描述正常流程、空状态、加载状态与错误处理。</p><h2>验收标准</h2><ul data-type="taskList"><li data-type="taskItem" data-checked="false"><p>主要场景可以完整走通</p></li><li data-type="taskItem" data-checked="false"><p>异常情况有明确反馈</p></li></ul>' },
  { id: 'email', title: '商务邮件', description: '表达得体，沟通更进一步', category: '日常沟通', kind: 'document', color: 'rose', content: '<p><strong>主题：</strong>填写邮件主题</p><p>您好，</p><p>简要说明写信的背景与目的。</p><p>清晰说明需要沟通的事项、相关信息与预期时间。</p><p>期待您的回复。如需补充信息，请随时联系。</p><p>祝好！</p><p>署名</p>' },
  { id: 'data', title: '数据分析表', description: '整理数据，发现值得关注的变化', category: '数据表格', kind: 'spreadsheet', color: 'green', content: '<p>在下方表格中整理数据，也可以让 Syncore 帮你分析、补充和解释。</p><h2>数据概览</h2><table><tr><th><p>指标</p></th><th><p>上期</p></th><th><p>本期</p></th><th><p>变化与说明</p></th></tr><tr><td><p>指标一</p></td><td><p>待填写</p></td><td><p>待填写</p></td><td><p>待分析</p></td></tr><tr><td><p>指标二</p></td><td><p>待填写</p></td><td><p>待填写</p></td><td><p>待分析</p></td></tr><tr><td><p>指标三</p></td><td><p>待填写</p></td><td><p>待填写</p></td><td><p>待分析</p></td></tr></table><h2>分析与洞察</h2><p>结合数据说明主要变化、可能原因与下一步行动。</p>' },
  { id: 'slides', title: '汇报演示大纲', description: '理清叙事，讲好你的故事', category: '演示汇报', kind: 'presentation', color: 'amber', content: '<h1>开场 · 核心观点</h1><p>用一句话告诉听众，这次汇报最值得记住的是什么。</p><hr><h1>背景 · 为什么重要</h1><ul><li><p>当前面临的问题</p></li><li><p>这件事的价值与机会</p></li></ul><hr><h1>进展 · 我们做了什么</h1><ul><li><p>关键行动与里程碑</p></li><li><p>可验证的成果与数据</p></li></ul><hr><h1>计划 · 接下来怎么做</h1><ul><li><p>下一阶段目标</p></li><li><p>资源需求与时间安排</p></li></ul><hr><h1>结尾 · 行动与共识</h1><p>明确希望听众支持的决定或行动。</p>' },
];

export const OFFICE_KIND_LABELS: Record<OfficeKind, string> = { document: '文档', spreadsheet: '表格', presentation: '演示' };

export function officeDate(value: string): string {
  const date = new Date(value);
  const now = new Date();
  const minutes = Math.max(0, Math.floor((now.getTime() - date.getTime()) / 60000));
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  if (date.toDateString() === now.toDateString()) return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  return date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
}
