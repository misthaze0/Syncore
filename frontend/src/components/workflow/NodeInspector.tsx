import { useEffect, useMemo, useState } from 'react';

import { Copy, Trash, TriangleAlert } from '@/components/icons';
import {
  NODE_TYPE_META,
  OPERATORS,
  OPERATOR_LABELS,
  referencedVariables,
} from '@/lib/workflow';
import type {
  WorkflowConditionOperator,
  WorkflowMetaResponse,
  WorkflowNodeConfig,
  WorkflowNodeDTO,
} from '@/types';

interface NodeInspectorProps {
  node: WorkflowNodeDTO;
  nodes: WorkflowNodeDTO[];
  meta: WorkflowMetaResponse | null;
  onChange: (node: WorkflowNodeDTO) => void;
  onDelete: () => void;
  onDuplicate: () => void;
}

type FieldKey = keyof WorkflowNodeConfig & string;

const DEFAULT_FIELD: Record<WorkflowNodeDTO['type'], FieldKey> = {
  start: 'prompt',
  llm: 'prompt',
  knowledge: 'query',
  tool: 'args',
  condition: 'left',
  answer: 'template',
};

export function NodeInspector({ node, nodes, meta, onChange, onDelete, onDuplicate }: NodeInspectorProps) {
  const [lastField, setLastField] = useState<FieldKey>(DEFAULT_FIELD[node.type]);

  useEffect(() => {
    setLastField(DEFAULT_FIELD[node.type]);
  }, [node.id, node.type]);

  const config = node.config ?? {};
  const setConfig = (key: string, value: unknown) => {
    onChange({ ...node, config: { ...config, [key]: value } });
  };

  const operators = meta?.operators?.length
    ? meta.operators
    : OPERATORS.map((value) => ({ value, label: OPERATOR_LABELS[value] }));
  const operator = (config.operator as WorkflowConditionOperator | undefined) ?? 'contains';
  const noRight = operator === 'is_empty' || operator === 'not_empty';

  const variables = useMemo(
    () => [
      { label: '输入', value: '{{input}}' },
      { label: '当前时间', value: '{{now}}' },
      ...nodes
        .filter((item) => item.id !== node.id)
        .map((item) => ({ label: item.title || item.id, value: `{{${item.id}.output}}` })),
    ],
    [nodes, node.id],
  );

  const missingVariables = useMemo(() => {
    const known = new Set(['input', 'now']);
    for (const item of nodes) {
      known.add(item.id);
      known.add(`${item.id}.output`);
    }
    const checks = ['prompt', 'template', 'query', 'left', 'right', 'system']
      .map((key) => String(config[key] ?? ''))
      .join('\n');
    return referencedVariables(checks).filter((name) => !known.has(name));
  }, [config, nodes]);

  const insertVariable = (snippet: string) => {
    const field = lastField || DEFAULT_FIELD[node.type];
    const current = String(config[field] ?? '');
    setConfig(field, current + snippet);
  };

  const tools = meta?.tools ?? [];
  const selectedTool = tools.find((item) => item.name === config.tool);
  const argsInvalid = useMemo(() => {
    const raw = String(config.args ?? '').trim();
    if (!raw) return false;
    try {
      const parsed = JSON.parse(raw);
      return typeof parsed !== 'object' || parsed === null || Array.isArray(parsed);
    } catch {
      return true;
    }
  }, [config.args]);

  const field = (key: FieldKey) => ({
    onFocus: () => setLastField(key),
  });

  return (
    <div className="wf-inspector">
      <div className="wf-inspector__head">
        <span className={`wf-type-badge wf-type-badge--${node.type}`}>
          {NODE_TYPE_META[node.type]?.label ?? node.type}
        </span>
        <span className="wf-inspector__id" title={node.id}>
          {node.id}
        </span>
      </div>

      <label className="wf-field">
        <span className="wf-field__label">名称</span>
        <input
          className="wf-input"
          value={node.title}
          maxLength={60}
          onChange={(e) => onChange({ ...node, title: e.target.value })}
        />
      </label>

      {node.type === 'llm' && (
        <>
          <label className="wf-field">
            <span className="wf-field__label">系统提示词</span>
            <textarea
              className="wf-textarea"
              rows={2}
              value={String(config.system ?? '')}
              placeholder="可留空"
              {...field('system')}
              onChange={(e) => setConfig('system', e.target.value)}
            />
          </label>
          <label className="wf-field">
            <span className="wf-field__label">用户提示词</span>
            <textarea
              className="wf-textarea"
              rows={6}
              value={String(config.prompt ?? '')}
              {...field('prompt')}
              onChange={(e) => setConfig('prompt', e.target.value)}
            />
          </label>
          <label className="wf-field">
            <span className="wf-field__label">模型</span>
            <input
              className="wf-input"
              value={String(config.model ?? '')}
              placeholder="跟随全局默认模型"
              onChange={(e) => setConfig('model', e.target.value)}
            />
          </label>
        </>
      )}

      {node.type === 'knowledge' && (
        <>
          <label className="wf-field">
            <span className="wf-field__label">检索内容</span>
            <textarea
              className="wf-textarea"
              rows={4}
              value={String(config.query ?? '')}
              {...field('query')}
              onChange={(e) => setConfig('query', e.target.value)}
            />
          </label>
          <label className="wf-field wf-field--compact">
            <span className="wf-field__label">召回条数</span>
            <input
              className="wf-input"
              type="number"
              min={1}
              max={20}
              value={Number(config.top_k) || 5}
              onChange={(e) => setConfig('top_k', Number(e.target.value))}
            />
          </label>
        </>
      )}

      {node.type === 'tool' && (
        <>
          <label className="wf-field">
            <span className="wf-field__label">工具</span>
            <select
              className="wf-select"
              value={String(config.tool ?? '')}
              onChange={(e) => setConfig('tool', e.target.value)}
            >
              <option value="">选择工具</option>
              {tools.map((item) => (
                <option key={item.name} value={item.name}>
                  {item.display_name && item.display_name !== item.name
                    ? `${item.display_name}（${item.name}）`
                    : item.name}
                  {item.category ? ` · ${item.category}` : ''}
                  {item.requires_approval ? '（需批准）' : ''}
                </option>
              ))}
              {config.tool && !tools.some((item) => item.name === config.tool) && (
                <option value={String(config.tool)}>{String(config.tool)}（当前清单里没有）</option>
              )}
            </select>
          </label>
          {selectedTool?.requires_approval && (
            <div className="wf-warn">
              <TriangleAlert size={12} aria-hidden />
              这个工具需要人工批准，在工作流里运行会被拒绝
            </div>
          )}
          <label className="wf-field">
            <span className="wf-field__label">参数（JSON）</span>
            <textarea
              className="wf-textarea wf-textarea--mono"
              rows={5}
              spellCheck={false}
              value={String(config.args ?? '')}
              {...field('args')}
              onChange={(e) => setConfig('args', e.target.value)}
            />
          </label>
          {argsInvalid && <div className="wf-warn">参数需要是 JSON 对象</div>}
        </>
      )}

      {node.type === 'condition' && (
        <>
          <label className="wf-field">
            <span className="wf-field__label">左值</span>
            <input
              className="wf-input"
              value={String(config.left ?? '')}
              {...field('left')}
              onChange={(e) => setConfig('left', e.target.value)}
            />
          </label>
          <label className="wf-field">
            <span className="wf-field__label">判断</span>
            <select
              className="wf-select"
              value={operator}
              onChange={(e) => setConfig('operator', e.target.value)}
            >
              {operators.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          {!noRight && (
            <label className="wf-field">
              <span className="wf-field__label">右值</span>
              <input
                className="wf-input"
                value={String(config.right ?? '')}
                {...field('right')}
                onChange={(e) => setConfig('right', e.target.value)}
              />
            </label>
          )}
        </>
      )}

      {node.type === 'answer' && (
        <label className="wf-field">
          <span className="wf-field__label">回答内容</span>
          <textarea
            className="wf-textarea"
            rows={8}
            value={String(config.template ?? '')}
            {...field('template')}
            onChange={(e) => setConfig('template', e.target.value)}
          />
        </label>
      )}

      {node.type !== 'start' && (
        <>
          <div className="wf-vars">
            <span className="wf-vars__label">插入变量</span>
            <div className="wf-vars__chips">
              {variables.map((item) => (
                <button
                  key={item.value}
                  type="button"
                  className="wf-var"
                  title={item.value}
                  onClick={() => insertVariable(item.value)}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </div>

          {missingVariables.length > 0 && (
            <div className="wf-warn">
              <TriangleAlert size={12} aria-hidden />
              引用了不存在的节点：{missingVariables.join('、')}
            </div>
          )}
        </>
      )}

      <div className="wf-inspector__foot">
        <button
          type="button"
          className="btn btn--sm"
          onClick={onDuplicate}
          disabled={node.type === 'start'}
        >
          <Copy size={12} />
          复制
        </button>
        <button
          type="button"
          className="btn btn--sm btn--danger"
          onClick={onDelete}
          disabled={node.type === 'start'}
        >
          <Trash size={12} />
          删除
        </button>
      </div>
    </div>
  );
}
