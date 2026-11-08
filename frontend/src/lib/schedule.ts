import type { ScheduleFreq, ScheduledRunStatus } from '@/types';

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

/** ISO → Date。空串与坏值都给 null —— 调用方要区分「没有」和「读不出」 */
export function parseIso(iso?: string | null): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

function pad(n: number): string {
  return String(n).padStart(2, '0');
}

/** 「09:38」—— 时间线里每次运行的时刻 */
export function clockOf(iso?: string | null): string {
  const d = parseIso(iso);
  if (!d) return '';
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 「2026-10-06」—— 时间线按天分组的键；解析不出来就归到一个空键，不假造今天 */
export function dayKeyOf(iso?: string | null): string {
  const d = parseIso(iso);
  if (!d) return '';
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** 「周二 10月6日」—— 时间线的日期标题 */
export function dayLabelOf(iso?: string | null): string {
  const d = parseIso(iso);
  if (!d) return '';
  return `${WEEKDAYS[d.getDay()]} ${d.getMonth() + 1}月${d.getDate()}日`;
}

/** 相对今天的自然语言前缀（「今天 09:38」比「10月18日 09:38」更接近用户怎么想这件事） */
export function dayRelativeTo(iso?: string | null): string {
  const d = parseIso(iso);
  if (!d) return '';
  const today = new Date();
  const diffDays = Math.round(
    (new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() -
      new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime()) /
      86_400_000,
  );
  if (diffDays === 0) return '今天';
  if (diffDays === 1) return '明天';
  if (diffDays === -1) return '昨天';
  return '';
}

/** 「10月18日 周日 09:00」—— 下一次执行时间 */
export function formatLocalDateTime(iso?: string | null): string {
  const d = parseIso(iso);
  if (!d) return '';
  return `${d.getMonth() + 1}月${d.getDate()}日 ${WEEKDAYS[d.getDay()]} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 调度窗文案「2026-10-05 - 2026-10-12」；不限日期时返回空串 */
export function formatWindow(start?: string | null, end?: string | null): string {
  const s = (start ?? '').trim();
  const e = (end ?? '').trim();
  if (s && e) return `${s} - ${e}`;
  if (s) return `${s} 起`;
  if (e) return `截止 ${e}`;
  return '';
}

/** 运行状态 → 人话（与 `subtaskStatus` 同一口径，但少两维：那两维属于子任务自己） */
export function runStatusText(status: ScheduledRunStatus | string): string {
  switch (status) {
    case 'running':
      return '进行中';
    case 'succeeded':
      return '已完成';
    case 'failed':
      return '失败了';
    case 'cancelled':
      return '已取消';
    case 'orphaned':
      return '服务重启时中断了';
    default:
      return '';
  }
}

/** 「暂无后续执行」的原因文案 */
export function noNextRunHint(schedule: {
  enabled: boolean;
  start_date?: string;
  end_date?: string;
}): string {
  if (!schedule.enabled) return '已暂停，打开开关后会按排期继续';
  const today = new Date();
  const end = parseIso(schedule.end_date);
  if (end && end.getFullYear() === today.getFullYear() && end.getMonth() === today.getMonth() && end.getDate() === today.getDate()) {
    return '今天是窗口最后一天，错过这一班就不再执行';
  }
  if (end && end.getTime() < today.getTime()) return '日期窗已结束';
  return '暂无后续执行';
}

/** 星期选择（weekly 用）：0=周一 … 6=周日，与后端 `validate_freq` 同一口径 */
export const WEEK_OPTIONS = [
  { value: 0, label: '一' },
  { value: 1, label: '二' },
  { value: 2, label: '三' },
  { value: 3, label: '四' },
  { value: 4, label: '五' },
  { value: 5, label: '六' },
  { value: 6, label: '日' },
];

/** 从任务复制一份时挂在标题后面的后缀（同一屏里两份同名任务会让人认错） */
export function copyTitle(title: string): string {
  return `${title} 副本`.slice(0, 80);
}

/** 内置模版 ——「从模版新建」的起点清单 */
export interface ScheduleTemplate {
  key: string;
  title: string;
  desc: string;
  freq: ScheduleFreq;
  instruction: string;
}

export const SCHEDULE_TEMPLATES: ScheduleTemplate[] = [
  {
    key: 'morning-brief',
    title: '每日晨报',
    desc: '工作日早上把今天的安排与昨天没做完的事汇总一份',
    freq: { kind: 'daily', time: '09:00' },
    instruction:
      '生成今天的晨报：1) 列出今天日期与星期；2) 汇总知识库里昨天到今天的会议纪要、待办与未完成事项；3) 按「今天要办 / 等你确认 / 可以晚点」三类整理；4) 把结果写成一份 Markdown 文件保存到工作目录，文件名用日期。语气简洁，不要客套话。',
  },
  {
    key: 'weekly-report',
    title: '每周周报',
    desc: '每周五下班前整理本周做完的事和下周计划',
    freq: { kind: 'weekly', time: '17:00', days: [4] },
    instruction:
      '生成本周周报：1) 从本周的对话与知识库材料里提取做完了什么、卡在哪、下周要办什么；2) 写一份 Markdown 周报保存到工作目录，标题带上本周的日期区间；3) 每条一句话，重要事项放最前面。',
  },
  {
    key: 'pre-meeting',
    title: '会前准备提醒',
    desc: '每天早上列出今天的会议，并给每场会准备好要问的问题',
    freq: { kind: 'daily', time: '08:30' },
    instruction:
      '查看知识库里今天的会议安排：对每一场会议，整理「会议目的、需要我先看的材料、我应当提出的 3 个问题」，写成一份 Markdown 保存到工作目录。如果今天没有会议，就在文件里写一句「今天没有会议安排」。',
  },
  {
    key: 'news-watch',
    title: '定期行业速览',
    desc: '每隔几天联网查一次，只看新出现的动向',
    freq: { kind: 'interval', time: '10:00', every_days: 3 },
    instruction:
      '联网检索最近三天的行业动态：挑 5 条与量化投资、AI 助手相关的动向，每条写「发生了什么、为什么值得注意、来源」。最后把速览存成 Markdown 文件到工作目录。',
  },
];

/** 模版落到表单里时的默认排期 */
export const DEFAULT_FREQ: ScheduleFreq = { kind: 'daily', time: '09:00' };
