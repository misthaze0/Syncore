/**
 * 设置页 TRAE 风格组件套件（2026-10 设置中心重写）
 *
 * 只做薄封装：输出 global.css 里「TRAE 风格设置组件」一节的全局类名。
 * 为什么不用 CSS 模块 —— 与全站约定一致（单 global.css + BEM 类名），
 * 套件的价值在于把重复结构（分区标题/行/单选卡/表格）收敛成类型安全的 JSX。
 */

import type { CSSProperties, ReactNode } from 'react';

/* ---------- 页头 ---------- */

export function PageHead({
  title,
  hint,
}: {
  title: string;
  desc?: string;
  /** 机制/范围说明：悬停在标题上才出现，不占正文版面 */
  hint?: string;
}) {
  return (
    <>
      <h2 className={`tset-page__title${hint ? ' tset-page__title--hint' : ''}`} title={hint}>
        {title}
      </h2>
    </>
  );
}

/* ---------- 分区：标题 + 说明 + 卡片行 ---------- */

export function Section({
  title,
  hint,
  children,
}: {
  title: string;
  desc?: string;
  /** 同 PageHead.hint：可见文字只留标题，读解释要悬停 */
  hint?: string;
  children: ReactNode;
}) {
  return (
    <section className="set-section">
      <h3 className={`set-section__title${hint ? ' set-section__title--hint' : ''}`} title={hint}>
        {title}
      </h3>
      {children}
    </section>
  );
}

/* ---------- 卡片行：左「标签 + 说明」右「控件」 ---------- */

export function Row({
  label,
  desc,
  control,
  stack,
  onClick,
  title,
  style,
}: {
  label: ReactNode;
  desc?: ReactNode;
  control?: ReactNode;
  /** 纵向排布（textarea / 长内容） */
  stack?: boolean;
  onClick?: () => void;
  title?: string;
  style?: CSSProperties;
}) {
  const cls = `set-row${stack ? ' set-row--stack' : ''}`;
  if (onClick) {
    return (
      <div className={cls} role="button" tabIndex={0} onClick={onClick} onKeyDown={
        (e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            onClick();
          }
        }
      } title={title} style={style}>
        <RowMain label={label} desc={desc} />
        {control && <div className="set-row__control">{control}</div>}
      </div>
    );
  }
  return (
    <div className={cls} title={title} style={style}>
      <RowMain label={label} desc={desc} />
      {control && <div className="set-row__control">{control}</div>}
    </div>
  );
}

function RowMain({ label, desc }: { label: ReactNode; desc?: ReactNode }) {
  return (
    <div className="set-row__main">
      <div className="set-row__label">{label}</div>
      {desc && <div className="set-row__desc">{desc}</div>}
    </div>
  );
}

/** 包住一组 Row，形成带边框的卡片 */
export function Rows({ children }: { children: ReactNode }) {
  return <div className="set-rows">{children}</div>;
}

/* ---------- 规范表格 ---------- */

export interface TableColumn {
  key: string;
  title: ReactNode;
  /** 数值列（等宽字体 + 右对齐的表头跟随左对齐，不做） */
  numeric?: boolean;
  muted?: boolean;
  width?: string | number;
}

export function Table<T>({
  columns,
  rows,
  rowKey,
  render,
  empty,
}: {
  columns: Array<TableColumn>;
  rows: T[];
  rowKey: (row: T, i: number) => string;
  render: (row: T, col: TableColumn, i: number) => ReactNode;
  /** 空态（表格为空时整块替换） */
  empty?: ReactNode;
}) {
  if (rows.length === 0 && empty) return <>{empty}</>;
  return (
    <div className="set-table__wrap">
      <table className="set-table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                style={{ width: c.width }}
                className={c.numeric ? 'set-table__num' : undefined}
              >
                {c.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={rowKey(row, i)}>
              {columns.map((c) => (
                <td
                  key={c.key}
                  className={
                    c.numeric
                      ? 'set-table__num'
                      : c.muted
                        ? 'set-table__muted'
                        : undefined
                  }
                >
                  {render(row, c, i)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ---------- 开关行 ---------- */

export function Switch({
  checked,
  onChange,
  disabled,
  ariaLabel,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  ariaLabel?: string;
}) {
  return (
    <label className="switch" aria-label={ariaLabel}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="switch__track">
        <span className="switch__knob" />
      </span>
    </label>
  );
}

/* ---------- 状态小圆点 ---------- */

export function Dot({ ok }: { ok: boolean }) {
  return <span className={ok ? 'dot-ok' : 'dot-bad'} />;
}
