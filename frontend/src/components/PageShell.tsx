import type { ReactNode } from 'react';
import type { LucideIcon } from '@/components/icons';

interface PageHeaderProps {
  title?: string;
  icon?: LucideIcon;
  head?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
}

/** Shared heading for sidebar pages; detail pages can supply a custom head. */
export function PageHeader({ title, icon: Icon, head, meta, actions }: PageHeaderProps) {
  return (
    <header className="main__topbar page-header">
      <div className="page-header__inner">
        <div className="page-header__identity">
          {Icon && <span className="page-header__icon"><Icon size={21} aria-hidden /></span>}
          <div className="page-header__copy">
            <div className="page-header__heading">
              {title ? <h1 className="page-header__title">{title}</h1> : <div className="main__title">{head}</div>}
              {meta && <div className="page-header__meta">{meta}</div>}
            </div>
          </div>
        </div>
        {actions && <div className="page-shell__actions">{actions}</div>}
      </div>
    </header>
  );
}

export function PageShell({
  head,
  title,
  icon,
  meta,
  actions,
  toolbar,
  className = '',
  children,
}: PageHeaderProps & {
  toolbar?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  return (
    <main className={`main page-shell ${className}`}>
      <PageHeader {...{ head, title, icon, meta, actions }} />
      <div className="main__scroll">
        <div className="main__inner main__inner--wide">
          {toolbar && <div className="page-shell__toolbar">{toolbar}</div>}
          <div className="page-shell__body tset">{children}</div>
        </div>
      </div>
    </main>
  );
}

export default PageShell;
