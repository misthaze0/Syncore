import { fetchMarket, type FetchMarketOptions } from './api';
import type { MarketEntry, ToolInfo } from '@/types';

export function extensionStatus(entry: MarketEntry): string {
  if (!entry.installed) return '';
  if (entry.builtin && !entry.attention) return '已内置';
  if (entry.configuration_required) return '待连接';
  if (entry.attention && !entry.update_available) return '需要处理';
  if (!entry.enabled) return '已停用';
  if (entry.update_available) return '可更新';
  return '已添加';
}

export function extensionSummary(entry: MarketEntry): string {
  return entry.description.split(/[。！？\n]/)[0] || { skill: '技能', mcp: '连接器', plugin: '组合包' }[entry.kind];
}

// Keep each request within the API limit, including when loading a large catalog.
export async function fetchExtensionPages(options: FetchMarketOptions, count: number) {
  const pages = await Promise.all(Array.from({ length: Math.max(1, Math.ceil(count / 24)) }, (_, page) =>
    fetchMarket({ ...options, page: page + 1, pageSize: 24 })));
  return {
    ...pages[0], items: pages.flatMap(page => page.items),
    failures: pages.flatMap(page => page.failures), has_more: pages[pages.length - 1].has_more
  };
}

const CAPABILITY_NAMES: Record<string, string> = {
  batch_merge_documents: '批量填充文档',
  generate_document: '生成文档', read_document: '读取办公文档', edit_document: '编辑办公文档', merge_document: '填充文档模板', convert_document: '转换办公文档', office_capabilities: '办公能力状态', read: '读取文件', ls: '浏览文件夹', glob: '查找文件', grep: '搜索文件内容', write: '创建与写入文件', edit: '修改文件内容', delete: '删除文件',
  kb_search: '搜索知识库', memory_list: '查看记忆', memory_remember: '记住偏好', memory_forget: '忘记偏好', bash: '执行本机命令', bash_output: '查看命令运行结果', bash_kill: '停止运行命令',
  use_skill: '使用技能', skills_list: '查看已有技能', tasks_list: '查看后台任务', task_status: '查看任务进度', task_dispatch: '交给专员处理', task_cancel: '取消后台任务', team_work: '查看团队工作记录',
  web_search: '联网搜索', web_fetch: '读取网页', list_workflows: '查看工作流', run_workflow: '运行工作流',
};
export const CAPABILITY_GROUPS: Record<string, string> = { 文档: '文档处理', 文件: '文件操作', 知识: '知识检索', 记忆: '记忆管理', 终端: '本机命令', 技能: '技能使用', 编排: '任务协作', 团队协作: '团队协作', 网络: '网页与搜索', 连接器: '连接器提供的能力', MCP: '连接器提供的能力', 桌面助手: '桌面操作', 其他: '其他能力' };
export const DANGER_LABELS: Record<string, string> = { safe: '安全', moderate: '谨慎', danger: '危险' };
export function capabilityTitle(tool: ToolInfo): string {
  return tool.display_name?.trim() || CAPABILITY_NAMES[tool.name] || tool.description.split(/[。\n]/)[0] || tool.name;
}

export async function fetchInstalledInventory() {
  const first = await fetchMarket({ view: 'installed', page: 1, pageSize: 100 });
  if (!first.has_more) return first;
  const rest = await Promise.all(Array.from({ length: Math.ceil(first.total / 100) - 1 }, (_, index) =>
    fetchMarket({ view: 'installed', page: index + 2, pageSize: 100 })));
  return { ...first, items: [...first.items, ...rest.flatMap(page => page.items)] };
}
