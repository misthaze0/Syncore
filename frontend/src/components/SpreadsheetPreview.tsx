import { useMemo, useState } from 'react';
import type { PreviewCell, SpreadsheetFilePreview } from '@/types';

function columnName(index: number): string {
  let name = '';
  for (let n = index + 1; n > 0; n = Math.floor((n - 1) / 26)) name = String.fromCharCode(65 + (n - 1) % 26) + name;
  return name;
}

export function csvCell(text: string): PreviewCell {
  const value = text.trim();
  // Identifiers with leading zeros remain text. Do not infer currencies or units.
  const numeric = /^[+-]?(?:0|[1-9]\d*)(?:\.\d+)?%?$/.test(value);
  const number = Number(value.replace(/%$/, '')) / (value.endsWith('%') ? 100 : 1);
  return { text, value: numeric && Number.isFinite(number) ? number : null };
}

export function SpreadsheetPreview({ data }: { data: SpreadsheetFilePreview }) {
  const [sheetIndex, setSheetIndex] = useState(0);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<{ row: number; col: number } | null>(null);
  const sheet = data.sheets[sheetIndex] ?? data.sheets[0];
  const rows = sheet?.rows ?? [];
  const columns = Math.max(0, ...rows.map(row => row.length));
  const visible = useMemo(() => rows.map((cells, index) => ({ cells, index })).filter(row => !query || row.cells.some(cell => cell.text.toLocaleLowerCase().includes(query.toLocaleLowerCase()))), [rows, query]);
  const active = selected ? rows[selected.row]?.[selected.col] : null;
  const chooseSheet = (index: number) => { setSheetIndex(index); setQuery(''); setSelected(null); };
  if (!sheet) return <div className="artifact-empty">工作簿中没有工作表</div>;
  return <div className="sheet-preview">
    <div className="sheet-preview__toolbar">
      {data.sheets.length > 1 && <label>工作表 <select aria-label="切换工作表" value={sheetIndex} onChange={event => chooseSheet(Number(event.target.value))}>{data.sheets.map((item, i) => <option key={item.name} value={i}>{i + 1}. {item.name}</option>)}</select><span>共 {data.sheets.length} 张</span></label>}
      <input className="sheet-preview__search" aria-label="搜索工作表" placeholder="搜索当前工作表…" value={query} onChange={event => setQuery(event.target.value)} />
    </div>
    <div className="sheet-preview__info">{sheet.name} · {sheet.total_rows} 行 × {sheet.total_columns} 列{query && ` · 匹配 ${visible.length} 行`}{sheet.truncated && ' · 当前仅显示部分内容，请下载查看完整工作表'}</div>
    <>
      <div className="sheet-preview__formula" title={active?.formula || active?.text}>{selected ? `${columnName(selected.col)}${selected.row + 1}` : '单元格'}<span>{active?.formula ? `${active.formula}  →  ${active.text}` : active?.text || '点击单元格查看完整内容'}</span></div>
      <div className="artifact-table__scroll sheet-preview__grid" tabIndex={0} aria-label={`${sheet.name}表格，可横向滚动`}><table className="artifact-table"><thead><tr><th aria-label="行号" />{Array.from({ length: columns }, (_, i) => <th key={i} scope="col">{columnName(i)}</th>)}</tr></thead><tbody>
        {visible.map(({ cells, index }) => <tr key={index}><th scope="row">{index + 1}</th>{Array.from({ length: columns }, (_, col) => <td key={col} className={`${index === 0 ? 'sheet-cell--header' : ''}${selected?.row === index && selected.col === col ? ' sheet-cell--selected' : ''}`}><button type="button" title={cells[col]?.formula || cells[col]?.text} onClick={() => setSelected({ row: index, col })}>{cells[col]?.text || '\u00a0'}</button></td>)}</tr>)}
      </tbody></table>{visible.length === 0 && <div className="artifact-empty">{query ? '没有匹配的内容' : '此工作表为空'}</div>}</div>
    </>
    <div className="sheet-preview__tabs" role="tablist" aria-label="工作表">{data.sheets.map((item, i) => <button key={item.name} type="button" role="tab" title={item.name} aria-selected={i === sheetIndex} onClick={() => chooseSheet(i)}>{item.name}</button>)}</div>
    {data.notice && <div className="sheet-preview__hint">{data.notice}</div>}
  </div>;
}
