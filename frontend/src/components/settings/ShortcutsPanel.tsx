/**
 * 快捷键说明页（2026-10 新增）—— Linear「Settings → Shortcuts」范式
 *
 * 纯静态展示：本应用当前支持的全部键位。可发现性配套 ——
 * 键位只有在「我看得见它在哪」时才值得存在。
 * 页面语言与助理页一致：大标题 + 分区 + 卡片行，不写说明段落。
 */

import { PageHead, Row, Rows, Section } from '@/components/settings2/kit';

interface Shortcut {
  keys: string[];
  label: string;
  scope: string;
}

const SHORTCUTS: Shortcut[] = [
  { keys: ['Ctrl', 'K'], label: '命令面板：搜会话、设置、功能', scope: '全局' },
  { keys: ['Ctrl', 'N'], label: '新建会话', scope: '全局' },
  { keys: ['Esc'], label: '关闭弹窗 / 面板 / 清空搜索', scope: '弹窗内' },
  { keys: ['Enter'], label: '发送消息', scope: '输入框' },
  { keys: ['Shift', 'Enter'], label: '换行', scope: '输入框' },
  { keys: ['/'], label: '唤起快捷指令', scope: '输入框' },
  { keys: ['↑', '↓'], label: '在结果间移动', scope: '命令面板' },
  { keys: ['Ctrl', 'L'], label: '聚焦网页地址栏', scope: '浏览器' },
  { keys: ['Ctrl', 'Shift', 'C'], label: '复制终端选中内容', scope: '终端' },
  { keys: ['Ctrl', 'Shift', 'V'], label: '粘贴到终端', scope: '终端' },
  { keys: ['Ctrl', 'C'], label: '中断当前命令', scope: '终端' },
  { keys: ['Esc'], label: '展开工作台恢复并排', scope: '工作台' },
];

export function ShortcutsPanel() {
  const groups = [...new Set(SHORTCUTS.map((s) => s.scope))];

  return (
    <div className="tset tset-page">
      <PageHead title="快捷键" />

      {groups.map((scope) => (
        <Section title={scope} key={scope}>
          <Rows>
            {SHORTCUTS.filter((s) => s.scope === scope).map((s) => (
              <Row
                key={s.label}
                label={s.label}
                control={s.keys.map((k) => (
                  <kbd key={k} className="kbd-key">
                    {k}
                  </kbd>
                ))}
              />
            ))}
          </Rows>
        </Section>
      ))}
    </div>
  );
}

export default ShortcutsPanel;
