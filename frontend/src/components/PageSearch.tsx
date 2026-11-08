import { Search, X } from '@/components/icons';
import { useT } from '@/i18n';

export function PageSearch({ value, onChange, placeholder }: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
}) {
  const t = useT();
  return (
    <label className="page-search">
      <Search size={15} aria-hidden />
      <input type="search" value={value} onChange={event => onChange(event.target.value)} placeholder={placeholder} aria-label={placeholder} />
      {value && <button type="button" onClick={() => onChange('')} aria-label={t('common.clearSearch')}><X size={13} aria-hidden /></button>}
    </label>
  );
}
