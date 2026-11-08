import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import Pet from '@/pages/Pet';
import '@/hooks/useTheme';
import '@/styles/pet.css';

const container = document.getElementById('root');
if (!container) throw new Error('找不到 #root 挂载点');

createRoot(container).render(
  <StrictMode>
    <Pet />
  </StrictMode>,
);
