import { useCallback, useEffect, useMemo, useState } from 'react';

import { Check, RefreshCw } from '@/components/icons';
import { MarkdownRenderer } from '@/components/MarkdownRenderer';
import { RulesSection } from '@/components/settings2/RulesSection';
import {
  createMcpServer,
  createPermissionRule,
  deletePermissionRule,
  fetchMcp,
  fetchPermissionRules,
  fetchSkillDetail,
  fetchTools,
  importMarketMcpConfig,
  reconnectMcpServer,
  updateMcpServer,
  updatePermissionRule,
} from '@/lib/api';
import { CAPABILITY_GROUPS, capabilityTitle } from '@/lib/extensions';
import type {
  McpServer,
  McpServerPayload,
  PermissionRulesResponse,
  RuleBehavior,
  ToolInfo,
  ToolListResponse,
} from '@/types';

function message(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

function pairs(text: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const line of text.split(/\r?\n/).filter((line) => line.trim())) {
    const index = line.indexOf('=');
    if (index < 1) throw new Error('请使用 名称=值 的格式');
    result[line.slice(0, index).trim()] = line.slice(index + 1);
  }
  return result;
}

/** 主助理拿不到这个工具的原因（与后端 `main_tool_availability` 的取值对齐） */
const MAIN_UNAVAILABLE: Record<string, string> = {
  danger: '危险操作，交给专员执行并审批',
  desktop: '电脑操控专属，受总开关控制',
  web_off: '联网开关已关闭',
  mcp_scope: '连接器限定了可用范围',
  hidden: '内部工具',
};

function sourceLabel(tool: ToolInfo, servers: McpServer[]): string {
  if (tool.source !== 'mcp') return '内置';
  const server = servers.find((item) => item.name === tool.server);
  return `连接器 · ${server?.display_name?.trim() || tool.server || '未命名'}`;
}

export function ExtensionConnection({
  name,
  required = [],
  onSaved,
  onBusyChange,
}: {
  name?: string;
  required?: string[];
  onSaved: (name?: string) => Promise<void>;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [editing, setEditing] = useState(!name);
  const [server, setServer] = useState<McpServer | null>(null);
  const [loading, setLoading] = useState(!!name);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [serverName, setName] = useState(name ?? '');
  const [label, setLabel] = useState('');
  const [transport, setTransport] = useState<McpServer['transport']>('streamable_http');
  const [danger, setDanger] = useState<McpServer['danger_level']>('danger');
  const [url, setUrl] = useState('');
  const [command, setCommand] = useState('');
  const [args, setArgs] = useState('');
  const [token, setToken] = useState('');
  const [env, setEnv] = useState<Record<string, string>>({});
  const [headers, setHeaders] = useState('');
  const [extraEnv, setExtraEnv] = useState('');
  const [json, setJson] = useState('');

  useEffect(() => {
    onBusyChange?.(busy);
    return () => onBusyChange?.(false);
  }, [busy, onBusyChange]);

  const requiredKey = required.join(',');
  const reload = useCallback(async () => {
    if (!name) return;
    setLoading(true);
    try {
      const data = await fetchMcp();
      const next = data.servers.find((item) => item.name === name);
      if (!next) throw new Error('找不到这个连接，请刷新列表');
      setServer(next);
      setEditing(!next.connected);
      setName(next.name);
      setLabel(next.display_name === next.name ? '' : next.display_name);
      setTransport(next.transport);
      setDanger(next.danger_level);
      setUrl(next.url);
      setCommand(next.command);
      setArgs(next.args.join('\n'));
      setHeaders(
        next.header_keys
          .filter((key) => key.toLowerCase() !== 'authorization')
          .map((key) => `${key}=********`)
          .join('\n'),
      );
      setExtraEnv(
        next.env_keys
          .filter((key) => !required.includes(key))
          .map((key) => `${key}=********`)
          .join('\n'),
      );
    } catch (e) {
      setError(message(e));
    } finally {
      setLoading(false);
    }
    // required 用 requiredKey 代表，数组字面量不该触发重读
  }, [name, requiredKey]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const save = async () => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const payload: McpServerPayload = {
        display_name: label.trim(),
        transport,
        danger_level: danger,
        ...(transport === 'stdio'
          ? { command: command.trim(), args: args.split(/\r?\n/).filter(Boolean) }
          : { url: url.trim() }),
      };
      const updatedHeaders = pairs(headers);
      if (server?.header_keys.some((key) => key.toLowerCase() === 'authorization')) {
        const key = server.header_keys.find((item) => item.toLowerCase() === 'authorization')!;
        updatedHeaders[key] = token
          ? token.startsWith('Bearer ')
            ? token
            : `Bearer ${token}`
          : '********';
      } else if (token) {
        updatedHeaders.Authorization = token.startsWith('Bearer ') ? token : `Bearer ${token}`;
      }
      payload.headers = updatedHeaders;

      const updatedEnv = pairs(extraEnv);
      for (const key of required) {
        if (env[key]) updatedEnv[key] = env[key];
        else if (server?.env_keys.includes(key)) updatedEnv[key] = '********';
      }
      payload.env = updatedEnv;

      if (name) await updateMcpServer(name, payload);
      else await createMcpServer({ ...payload, name: serverName.trim() });

      const live = await fetchMcp();
      const connected = live.servers.find((item) => item.name === (name ?? serverName.trim()));
      setNotice(connected?.connected ? '已连接' : '已保存，连接尚未成功');
      if (connected?.error) setError(connected.error);
      setToken('');
      setEnv({});
      await onSaved(name ?? serverName.trim());
      if (name) await reload();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <p role="status">正在读取连接…</p>;

  return (
    <section className="ext-section ext-connection">
      <h3>连接信息</h3>
      {server && (
        <p className={'ext-connection-status' + (server.connected ? ' is-on' : '')}>
          {server.connected ? <Check size={14} /> : <RefreshCw size={14} />}{' '}
          {server.connected ? '已连接' : server.enabled ? '未连接' : '已停用'}
        </p>
      )}
      <details
        className="ext-advanced"
        open={editing}
        onToggle={(e) => setEditing(e.currentTarget.open)}
      >
        <summary>连接设置</summary>
        {!name && (
          <label>
            名称
            <input
              className="ext-input"
              value={serverName}
              onChange={(e) => setName(e.target.value)}
              placeholder="例如 github"
              required
            />
          </label>
        )}
        <label>
          显示名
          <input
            className="ext-input"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="例如 网页抓取（留空显示连接标识）"
          />
        </label>
        {transport === 'stdio' ? (
          <>
            <label>
              启动命令
              <input
                className="ext-input"
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                placeholder="npx / uvx / python"
              />
            </label>
            <label>
              启动参数
              <textarea
                className="ext-input"
                value={args}
                onChange={(e) => setArgs(e.target.value)}
                placeholder="每行一个参数"
                rows={3}
              />
            </label>
          </>
        ) : (
          <>
            <label>
              服务地址
              <input
                className="ext-input"
                type="url"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://…/mcp"
              />
            </label>
            <label>
              访问密钥
              <input
                className="ext-input"
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder={
                  server?.header_keys.some((key) => key.toLowerCase() === 'authorization')
                    ? '已保存，留空保留'
                    : '可选'
                }
                autoComplete="new-password"
              />
            </label>
          </>
        )}
        {required.map((key) => (
          <label key={key}>
            {key}
            <input
              className="ext-input"
              type="password"
              value={env[key] ?? ''}
              onChange={(e) => setEnv((old) => ({ ...old, [key]: e.target.value }))}
              placeholder={server?.env_keys.includes(key) ? '已保存，留空保留' : '请输入密钥'}
              autoComplete="new-password"
            />
          </label>
        ))}
        <details className="ext-advanced">
          <summary>高级设置</summary>
          <label>
            连接方式
            <select
              className="ext-input"
              value={transport}
              onChange={(e) => setTransport(e.target.value as McpServer['transport'])}
            >
              <option value="streamable_http">远程服务</option>
              <option value="sse">远程服务（SSE）</option>
              <option value="stdio">本机命令</option>
            </select>
          </label>
          <label>
            调用方式
            <select
              className="ext-input"
              value={danger}
              onChange={(e) => setDanger(e.target.value as McpServer['danger_level'])}
            >
              <option value="danger">仅专员执行，需你批准</option>
              <option value="moderate">助理可直接调用（中风险）</option>
              <option value="safe">助理可直接调用（低风险）</option>
            </select>
          </label>
          <label>
            请求头
            <textarea
              className="ext-input"
              rows={3}
              value={headers}
              onChange={(e) => setHeaders(e.target.value)}
              placeholder="名称=值"
            />
          </label>
          <label>
            环境变量
            <textarea
              className="ext-input"
              rows={3}
              value={extraEnv}
              onChange={(e) => setExtraEnv(e.target.value)}
              placeholder="名称=值"
            />
          </label>
          {!name && (
            <details>
              <summary>导入 MCP 配置</summary>
              <textarea
                className="ext-input"
                aria-label="MCP 配置"
                rows={7}
                value={json}
                onChange={(e) => setJson(e.target.value)}
              />
              <button
                className="btn"
                disabled={busy || !json.trim()}
                onClick={async () => {
                  setBusy(true);
                  setError('');
                  try {
                    const result = await importMarketMcpConfig(json);
                    const failures = Object.entries(result.failed);
                    if (failures.length)
                      throw new Error(
                        (result.created.length ? `已添加 ${result.created.join('、')}。` : '') +
                          failures.map(([key, value]) => `${key}：${value}`).join('；'),
                      );
                    if (!result.created.length) throw new Error('配置中没有可添加的连接');
                    await onSaved();
                  } catch (e) {
                    setError(message(e));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                导入
              </button>
            </details>
          )}
        </details>
        {error && (
          <div className="ext-error" role="alert">
            {error}
          </div>
        )}
        {notice && <p role="status">{notice}</p>}
        <div className="ext-detail-actions">
          <button
            className="btn btn--primary"
            disabled={
              busy ||
              (!name && (!serverName.trim() || !label.trim())) ||
              (transport === 'stdio' ? !command.trim() : !url.trim())
            }
            onClick={() => void save()}
          >
            {busy ? '连接中…' : name ? '保存连接' : '添加连接'}
          </button>
          {name && (
            <button
              className="btn"
              disabled={busy || !server?.enabled}
              onClick={async () => {
                setBusy(true);
                setError('');
                try {
                  await reconnectMcpServer(name);
                  await reload();
                  await onSaved();
                } catch (e) {
                  setError(message(e));
                } finally {
                  setBusy(false);
                }
              }}
            >
              重新连接
            </button>
          )}
        </div>
      </details>
    </section>
  );
}

export function SkillContent({ name }: { name: string }) {
  const [content, setContent] = useState('');
  const [error, setError] = useState('');
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    fetchSkillDetail(name)
      .then((skill) => {
        if (alive) setContent(skill.instructions);
      })
      .catch((e) => {
        if (alive) setError(message(e));
      });
    return () => {
      alive = false;
    };
  }, [name, open]);

  return (
    <details className="ext-advanced" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>查看技能内容</summary>
      {error ? <p role="alert">{error}</p> : content ? <MarkdownRenderer content={content} /> : <p>正在读取…</p>}
    </details>
  );
}

export function ExtensionTools({ server }: { server?: string }) {
  const [permissions, setPermissions] = useState(false);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [servers, setServers] = useState<McpServer[]>([]);
  const [mainTools, setMainTools] = useState<string[] | null>(null);
  const [unavailable, setUnavailable] = useState<Record<string, string>>({});
  const [scopeWarnings, setScopeWarnings] = useState<
    NonNullable<ToolListResponse['scope_warnings']>
  >([]);
  const [rules, setRules] = useState<PermissionRulesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [query, setQuery] = useState('');

  const reloadRules = useCallback(async () => {
    setRules(await fetchPermissionRules());
  }, []);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([fetchTools(false), fetchPermissionRules(), fetchMcp().catch(() => null)])
      .then(([result, permissionData, mcp]) => {
        if (!alive) return;
        setTools(result.tools.filter((tool) => !tool.hidden && (server ? tool.server === server : true)));
        setMainTools(result.main_tools ?? null);
        setUnavailable(result.main_unavailable ?? {});
        setScopeWarnings(result.scope_warnings ?? []);
        setServers(mcp?.servers ?? []);
        setRules(permissionData);
      })
      .catch((e) => {
        if (alive) setError(message(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [server]);

  const mainSet = useMemo(() => new Set(mainTools ?? []), [mainTools]);
  const change = async (tool: string, value: string) => {
    setBusy(tool);
    setError('');
    try {
      const old = rules?.rules.find((rule) => rule.tool === tool && !rule.arg && !rule.pattern);
      if (!value) {
        if (old) await deletePermissionRule(old.rule_id);
      } else if (old) await updatePermissionRule(old.rule_id, { behavior: value as RuleBehavior, enabled: true });
      else await createPermissionRule({ tool, behavior: value as RuleBehavior, note: '扩展能力权限' });
      await reloadRules();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy('');
    }
  };

  const lowered = query.toLowerCase();
  const visible = tools.filter((tool) =>
    `${capabilityTitle(tool)} ${tool.description} ${tool.name} ${tool.server || ''}`
      .toLowerCase()
      .includes(lowered),
  );
  const groups = [...new Set(visible.map((tool) => tool.category || '其他'))];
  const warnings = scopeWarnings.filter((warning) => tools.some((tool) => tool.name === warning.tool));
  const mainCount = mainTools ? tools.filter((tool) => mainSet.has(tool.name)).length : null;

  return (
    <section className="ext-section">
      <div className="ext-section-heading">
        <h3>{server ? '提供的能力' : '全部能力'}</h3>
        <button className="btn btn--sm btn--ghost" onClick={() => setPermissions((value) => !value)}>
          {permissions ? '收起权限' : '管理权限'}
        </button>
      </div>
      {loading ? (
        <p role="status">正在读取…</p>
      ) : (
        <>
          <p className="ext-ledger-summary">
            共 {tools.length} 项能力
            {mainCount !== null ? `，主助理可直接调用 ${mainCount} 项` : ''}
          </p>
          {warnings.length > 0 && (
            <div className="ext-error" role="alert">
              作用域异常：
              {warnings
                .map(
                  (warning) =>
                    `${warning.display_name}（${warning.tool}）指向不存在的专员 ${warning.unknown_agents.join('、')}`,
                )
                .join('；')}
            </div>
          )}
          <input
            className="ext-input"
            aria-label="搜索能力"
            placeholder="搜索能力"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {groups.map((group) => (
            <details className="ext-tool-group" key={group} open>
              <summary>
                {CAPABILITY_GROUPS[group] || group}
                <span>{visible.filter((tool) => (tool.category || '其他') === group).length}</span>
              </summary>
              {visible
                .filter((tool) => (tool.category || '其他') === group)
                .map((tool) => {
                  const override = rules?.rules.find(
                    (rule) => rule.enabled && rule.tool === tool.name && !rule.arg && !rule.pattern,
                  );
                  const mainOk = mainSet.has(tool.name);
                  const reason = MAIN_UNAVAILABLE[unavailable[tool.name] || ''] || '当前不可用';
                  return (
                    <div className="ext-tool" key={tool.name}>
                      <div>
                        <b>{capabilityTitle(tool)}</b>
                        <div className="ext-tool__meta">
                          <span className="ext-chip">{sourceLabel(tool, servers)}</span>
                          <code>{tool.name}</code>
                          {mainTools && (
                            <span className={'ext-chip ' + (mainOk ? 'ext-chip--on' : 'ext-chip--off')}>
                              {mainOk ? '主助理可直接调用' : reason}
                            </span>
                          )}
                        </div>
                      </div>
                      {permissions && (
                        <select
                          className="ext-input"
                          aria-label={tool.name + ' 使用权限'}
                          disabled={busy === tool.name}
                          value={override?.behavior ?? ''}
                          onChange={(e) => void change(tool.name, e.target.value)}
                        >
                          <option value="">默认</option>
                          <option value="ask">每次询问</option>
                          <option value="deny">不允许</option>
                          <option value="allow">允许</option>
                        </select>
                      )}
                    </div>
                  );
                })}
            </details>
          ))}
          {!visible.length && <p>没有可显示的能力</p>}
        </>
      )}
      {error && (
        <p className="ext-error" role="alert">
          {error}
        </p>
      )}
      {!server && (
        <details className="ext-advanced">
          <summary>高级规则</summary>
          <RulesSection data={rules} error={null} onChanged={reloadRules} />
        </details>
      )}
    </section>
  );
}
