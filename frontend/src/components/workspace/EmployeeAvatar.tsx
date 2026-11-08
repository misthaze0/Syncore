export function EmployeeAvatar({ name, size = 'normal' }: { name: string; size?: 'normal' | 'small' | 'large' }) {
  const tone = Array.from(name).reduce((sum, char) => sum + char.codePointAt(0)!, 0) % 5;
  return <span className={`team-avatar team-avatar--${size} team-avatar--${tone}`} aria-hidden="true">{Array.from(name)[0] || '员'}</span>;
}

