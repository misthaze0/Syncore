import { memo, useState } from 'react';
import { Brain, ChevronDown, CircleCheck, Hourglass, Loader2, TriangleAlert } from '@/components/icons';
import { ToolCallView } from '@/components/ToolCallView';
import { toolActivity, toolStatus } from '@/lib/messagePresentation';
import type { ToolCallInfo } from '@/types';

interface Props {
  tools?: Array<Omit<ToolCallInfo, 'dangerLevel'> & { dangerLevel?: ToolCallInfo['dangerLevel'] }>;
  reasoning?: string;
  streaming?: boolean;
  /**
   * 本轮用量（「1.2s · 843 tokens」）。
   *
   * 助理会话里它挂在**展开后的过程**里，而不是回答下方：闲聊场景下那是一行
   * 与对话无关的账单，默认看不见；想核对成本的人展开就能看到。
   */
  usageText?: string;
  usageTitle?: string;
}

export const MessageProcess = memo(function MessageProcess({ tools = [], reasoning, streaming = false, usageText, usageTitle }: Props) {
  const [open, setOpen] = useState(false);
  if (!tools.length && !reasoning) return null;
  const waiting = streaming ? tools.find(tool => tool.status === 'awaiting_approval') : undefined;
  const active = tools.filter(tool => tool.status === 'pending');
  const problems = tools.filter(tool => ['failed', 'rejected', 'interrupted'].includes(toolStatus(tool, streaming)));
  const current = waiting ?? (streaming ? active[active.length - 1] : undefined);
  const Icon = waiting ? Hourglass : current || streaming ? Loader2 : problems.length ? TriangleAlert : CircleCheck;
  const label = waiting ? '等待你确认' : current ? toolActivity(current, 'pending') : streaming ? (!tools.length && reasoning ? '正在思考' : '正在整理回答') : problems.length ? '工作过程有未完成的步骤' : '工作过程';
  return <section className={'message-process' + (problems.length ? ' message-process--attention' : '')} aria-label="工作过程">
    <button className="message-process__toggle" aria-expanded={open} onClick={() => setOpen(value => !value)}>
      <Icon size={14} className={streaming && !waiting ? 'spin' : undefined}/>
      <span className="message-process__label" aria-live="polite">{label}</span>
      {tools.length > 0 && <span className="message-process__count">{tools.length} 步</span>}
      <ChevronDown size={13} className={open ? 'is-open' : ''}/>
    </button>
    {open ? <div className="message-process__steps">
      {reasoning && <details className="message-process__reasoning"><summary><Brain size={12}/>思考记录</summary><div>{reasoning}</div></details>}
      {tools.map(tool => <ToolCallView key={tool.callId} {...tool} settled={!streaming}/>)}
      {usageText && <div className="message-process__usage" title={usageTitle}>{usageText}</div>}
    </div> : problems.length > 0 && <div className="message-process__issues">{problems.map(tool =>
      <div key={tool.callId}><span>{toolActivity(tool, toolStatus(tool, streaming))}{tool.reason && <small>{tool.reason}</small>}</span><b>{tool.status === 'failed' ? '未成功' : tool.status === 'rejected' ? '未执行' : '已中断'}</b></div>
    )}</div>}
  </section>;
});
