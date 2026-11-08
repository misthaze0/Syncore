import { useEffect } from 'react';

import brandMark from '@/assets/brand/wordmark.png';
import { PageHead, Row, Rows, Section } from '@/components/settings2/kit';
import { useAgentStore } from '@/stores/agentStore';

const STATE_LABEL = {
  loading: '检查中',
  ok: '运行中',
  down: '未连接',
} as const;

const STATE_TONE = {
  loading: 'muted',
  ok: 'safe',
  down: 'danger',
} as const;

export function AboutPanel() {
  const info = useAgentStore((s) => s.serviceInfo);
  const status = useAgentStore((s) => s.serviceInfoStatus);
  const loadServiceInfo = useAgentStore((s) => s.loadServiceInfo);

  useEffect(() => {
    void loadServiceInfo();
  }, [loadServiceInfo]);

  const version = window.xjmsDesktop?.version || info?.version || '';

  return (
    <div className="tset tset-page">
      <PageHead title="关于" />

      <Section title="应用信息">
        <Rows>
          <Row
            label={
              <span className="about-brand">
                <img className="about-brand__logo" src={brandMark} alt="Syncore" />
              </span>
            }
            control={
              <span className="badge badge--muted">{version ? `v${version}` : '—'}</span>
            }
          />
          <Row label="产品定位" control={<span className="badge badge--muted">企业级 AI 工作台</span>} />
          <Row
            label="服务状态"
            control={
              <span className={`badge badge--${STATE_TONE[status]}`}>
                {STATE_LABEL[status]}
              </span>
            }
          />
        </Rows>
      </Section>
    </div>
  );
}

export default AboutPanel;
