import { Users } from '@/components/icons';
import { MarkdownRenderer } from '@/components/MarkdownRenderer';
import { EmployeeAvatar } from './EmployeeAvatar';
import type { GroupMessage } from '@/types';

export function GroupMessageItem({ message, onEmployee, onReply }: { message: GroupMessage; onEmployee: (id: string) => void; onReply?: (message: GroupMessage) => void }) {
  const user = message.sender_kind === 'user' || message.kind === 'goal';
  const employee = !!message.employee_id;
  const sender = user ? '我' : message.employee_name || (message.kind === 'summary' ? '团队总结' : '团队协调');
  const time = new Date(message.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
  return <article className={`team-message${user ? ' team-message--self' : ''}${!user && !employee ? ' team-message--coordinator' : ''}`}>
    <button type="button" className="team-message__avatar" onClick={() => { if (message.employee_id) onEmployee(message.employee_id); }} disabled={!employee} aria-label={employee ? `打开${sender}的私聊` : sender}>
      {employee ? <EmployeeAvatar name={sender} /> : <span className={`team-avatar${user ? ' team-avatar--self' : ' team-avatar--coordinator'}`}>{user ? '我' : <Users size={16} />}</span>}
    </button>
    <div className="team-message__body"><div className="team-message__meta"><button type="button" disabled={!employee} onClick={() => { if (message.employee_id) onEmployee(message.employee_id); }}>{sender}</button>{message.employee_tagline && <span className="team-message__role">{message.employee_tagline}</span>}<time dateTime={message.created_at}>{time}</time></div>
      <div className="team-message__bubble">{message.recipient_name && <span className="team-message__mention">@{message.recipient_name} </span>}<MarkdownRenderer content={message.content} /></div>
      <div className="team-message__delivery">{message.delivery_state && <span title={message.delivery_error}>{({pending: '待处理', read: '已读待处理', processing: '处理中', answered: '已处理', failed: '处理失败', cancelled: '已结束'})[message.delivery_state]}</span>}{employee && onReply && <button type="button" onClick={() => onReply(message)}>回复</button>}</div>
    </div>
  </article>;
}
