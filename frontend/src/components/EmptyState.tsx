import type { LucideIcon } from '@/components/icons';
import type { ReactNode } from 'react';

export function EmptyState({
  icon: Icon,
  title,
  fill = false,
  action,
}: {
  icon: LucideIcon;
  title: string;
  fill?: boolean;
  action?: ReactNode;
}) {
  return (
    <div className={`empty-state${fill ? ' empty-state--fill' : ''}`}>
      <div className="empty-state__icon">
        <Icon size={26} aria-hidden />
      </div>
      <div className="empty-state__title">{title}</div>
      {action && <div className="empty-state__actions">{action}</div>}
    </div>
  );
}
