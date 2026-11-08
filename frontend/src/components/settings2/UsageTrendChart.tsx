/**
 * 每日用量柱形图 —— 零依赖 SVG
 *
 * 为什么不引图表库：项目里已有的「图」全是手写的（用量分布条、连接状态卡），
 * 依赖策略是能零依赖就零依赖。一组柱子 + 一个 hover 提示，
 * 为它装 200KB 的图表库不划算，而且主题变量（绛红 / 暗色）还得再配一遍。
 *
 * ★ 为什么是柱子而不是曲线
 *
 * 横轴是**自然日**，一天一个桶 —— 中间那些 0 是「这天没开机」，不是「用量连续变化到 0」。
 * 曲线（哪怕补了零）会把两个真实峰值之间连出一段并不存在的斜坡；柱子之间天然断开，
 * 读出来的就是「哪几天花了、哪几天没花」。
 *
 * ★ 坐标怎么算的
 *
 * SVG 用 `viewBox="0 0 1000 H"` + `preserveAspectRatio="none"` 撑满容器，
 * 好处是不必测容器宽度（没有 ResizeObserver、没有一帧的布局抖动）。
 * 代价是横向被拉伸：矩形只有填充、没有描边，所以不受影响；
 * 而**提示框与 x 轴文字一律放到 HTML 层**按百分比定位 —— 放进 SVG 里字会被拉扁。
 */

import { useMemo, useRef, useState } from 'react';

import type { UsageDayPoint } from '@/types';

/** 内部坐标系高度（viewBox 单位，与实际渲染高度无关） */
const VB_H = 220;
/** 顶部留白：最高的柱子不该贴着边框 */
const PAD_TOP = 14;
/** 底部留白：柱子不压着 x 轴文字 */
const PAD_BOTTOM = 10;
/** 绘图区高度（viewBox 单位）—— 数据值与刻度文字都按它映射，只留一处定义 */
const PLOT_H = VB_H - PAD_TOP - PAD_BOTTOM;
/** 基线 y（柱子从这里往上长） */
const BASE_Y = VB_H - PAD_BOTTOM;
/** 非零但极小的日子至少露出这么多，否则「花了 1 个 token」看起来像没花 */
const MIN_BAR_H = 2;
/** 柱宽占槽位的比例 —— 剩下的是柱间缝 */
const BAR_RATIO = 0.62;

interface Props {
  points: UsageDayPoint[];
  /** 系列名（模型名或「全部」），出现在 hover 提示里 */
  series: string;
}

/**
 * 轴上限：先挑一个「好看」的步长（1/2/2.5/5 × 10ⁿ），再取它的 3 倍。
 *
 * 3 倍是因为纵轴固定分四档（三条网格线 + 0）—— 直接对最大值取整再除三，
 * 刻度会写成 16.5K / 8.3K 这种没人读得下去的数。
 */
function niceTop(max: number): number {
  if (max <= 0) return 100;
  const target = max / 3;
  const exp = Math.floor(Math.log10(target));
  const base = 10 ** exp;
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (target <= step * base) return step * base * 3;
  }
  return 10 * base * 3;
}

/** 轴刻度文字：3K / 1.5K / 500 这种量级写法 */
function fmtAxis(n: number): string {
  if (n >= 1000) {
    const k = n / 1000;
    return `${Number.isInteger(k) ? k : k.toFixed(1)}K`;
  }
  return String(Math.round(n));
}

/** 10月18日（x 轴与提示共用） */
function fmtDay(iso: string): string {
  const parts = iso.split('-');
  if (parts.length !== 3) return iso;
  return `${Number(parts[1])}月${Number(parts[2])}日`;
}

export function UsageTrendChart({ points, series }: Props) {
  const [active, setActive] = useState<number | null>(null);
  const canvasRef = useRef<HTMLDivElement>(null);

  const { bars, ticks, xLabels } = useMemo(() => {
    const n = points.length;
    const top = niceTop(Math.max(...points.map((p) => p.total_tokens), 0));
    const slot = 1000 / n;
    // 30 天用宽柱，365 天只能收窄 —— 缝太密会糊成一条实心带
    const w = Math.max(1, Math.min(slot * BAR_RATIO, 26));
    const barList = points.map((p, i) => {
      const h = p.total_tokens <= 0 ? 0 : Math.max((p.total_tokens / top) * PLOT_H, MIN_BAR_H);
      const cx = i * slot + slot / 2;
      return { cxPct: (cx / 1000) * 100, x: cx - w / 2, w, y: BASE_Y - h, h };
    });
    const tickVals = [top, (top * 2) / 3, top / 3, 0];
    // 抽稀：**从最后一天往回数**，这样末位天然有标且不会和前一格贴在一起
    // （往前数再补末位，会出现「10月19日」「10月20日」叠成一片）。
    // 起点也要标 —— 用户得知道横轴从哪天开始，所以让离它最近的那一格腾位。
    const step = Math.max(1, Math.ceil(n / 8));
    const shown = new Set<number>();
    for (let i = n - 1; i >= 0; i -= step) shown.add(i);
    const firstShown = Math.min(...shown);
    if (firstShown !== 0) {
      shown.delete(firstShown);
      shown.add(0);
    }
    return {
      bars: barList,
      // 刻度文字与 SVG 里的网格线用**同一个**映射（含 PAD_TOP/PAD_BOTTOM），
      // 各算一套就会出现「3K 那行字不在 3K 那条线上」
      ticks: tickVals.map((v, i) => ({
        value: v,
        topPct: ((PAD_TOP + (i / (tickVals.length - 1)) * PLOT_H) / VB_H) * 100,
      })),
      xLabels: points.map((p, i) => ({
        date: p.date,
        cxPct: barList[i].cxPct,
        edge: i === 0 ? 'start' : i === n - 1 ? 'end' : 'mid',
        show: shown.has(i),
      })),
    };
  }, [points]);

  if (points.length === 0) return null;

  /** 指针横坐标 → 这一天的索引（槽位等宽，直接按比例反查） */
  const pickIndex = (clientX: number): number => {
    const box = canvasRef.current?.getBoundingClientRect();
    if (!box || box.width <= 0) return 0;
    const ratio = (clientX - box.left) / box.width;
    return Math.min(points.length - 1, Math.max(0, Math.floor(ratio * points.length)));
  };

  // 换时间档后点数会变少，旧索引可能落到区间外
  const at = active !== null && active < points.length ? active : null;
  const hover = at === null ? undefined : points[at];

  return (
    <div className="uc">
      <div className="uc__plot">
        <div className="uc__yticks" aria-hidden>
          {ticks.map((t, i) => (
            <span key={i} style={{ top: `${t.topPct}%` }}>
              {fmtAxis(t.value)}
            </span>
          ))}
        </div>

        <div className="uc__canvas" ref={canvasRef}>
          <svg
            className="uc__svg"
            viewBox={`0 0 1000 ${VB_H}`}
            preserveAspectRatio="none"
            aria-hidden
          >
            {ticks.map((t, i) => {
              const y = (t.topPct / 100) * VB_H;
              return <line key={i} className="uc__grid" x1="0" x2="1000" y1={y} y2={y} />;
            })}
            {bars.map((b, i) =>
              b.h > 0 ? (
                <rect
                  key={points[i].date}
                  className={at === i ? 'uc__bar uc__bar--on' : 'uc__bar'}
                  x={b.x}
                  y={b.y}
                  width={b.w}
                  height={b.h}
                />
              ) : null,
            )}
            {/* 基线：0 值的日子不留柱子，靠这条线说明「这里是有轴的，那天是 0」 */}
            <line className="uc__axis" x1="0" x2="1000" y1={BASE_Y} y2={BASE_Y} />
          </svg>

          {/* 一整块命中层，覆盖在 SVG 之上：索引由指针横坐标反查，不做「每天一个按钮」
              —— 曲线补零后「全部」档会有几百个点，几百个可 Tab 的空按钮既拖垮 DOM
              也毁掉键盘遍历。 */}
          <div
            className="uc__hits"
            role="application"
            aria-label={`每日用量柱形图，共 ${points.length} 天`}
            onPointerMove={(e) => setActive(pickIndex(e.clientX))}
            onPointerDown={(e) => setActive(pickIndex(e.clientX))}
            onPointerLeave={() => setActive(null)}
          />

          {hover && at !== null && (
            <div
              className={`uc__tip${bars[at].cxPct > 62 ? ' uc__tip--left' : ''}`}
              style={{ left: `${bars[at].cxPct}%` }}
            >
              <div className="uc__tip-date">{`${yearOf(hover.date)}年${fmtDay(hover.date)}`}</div>
              <div className="uc__tip-row">
                <span className="uc__tip-swatch" />
                <span className="uc__tip-series">{series}</span>
                <span className="uc__tip-value">{hover.total_tokens.toLocaleString('zh-CN')}</span>
              </div>
              <div className="uc__tip-sub">{`输入 ${hover.prompt_tokens.toLocaleString('zh-CN')} · 输出 ${hover.completion_tokens.toLocaleString('zh-CN')}`}</div>
              {hover.reasoning_tokens > 0 && (
                <div className="uc__tip-sub">{`其中思考 ${hover.reasoning_tokens.toLocaleString('zh-CN')}`}</div>
              )}
              <div className="uc__tip-sub">{`${hover.calls} 次调用`}</div>
            </div>
          )}
        </div>
      </div>

      <div className="uc__xticks">
        {xLabels
          .filter((l) => l.show)
          .map((l) => (
            <span
              key={l.date}
              className={[
                'uc__xtick',
                l.edge === 'start' ? 'uc__xtick--start' : l.edge === 'end' ? 'uc__xtick--end' : '',
                hover && hover.date === l.date ? 'uc__xtick--on' : '',
              ]
                .filter(Boolean)
                .join(' ')}
              style={{ left: `${l.cxPct}%` }}
            >
              {fmtDay(l.date)}
            </span>
          ))}
      </div>
    </div>
  );
}

function yearOf(iso: string): string {
  return iso.split('-')[0] ?? '';
}

export default UsageTrendChart;
