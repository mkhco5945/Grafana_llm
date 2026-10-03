import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, FormEvent } from 'react';

import { Alert, Button, Spinner } from '@grafana/ui';
import ModelSettings from './ModelSettings';
import type { ModelConnection } from './api';
import { ChatMessageBubble, ChatSurfaceStyles } from './ChatMessageBubble';

import {
  createSession,
  getJob,
  getSession,
  health,
  importLegacyChat,
  listSessions,
  retryJob,
  startChat,
} from './api';
import type { JobResponse, LegacyChatState, SessionDetail, SessionSummary } from './api';

const LEGACY_STORAGE_KEY = 'mkhco-ai-dashboard-app.chat.v1';
const MIGRATION_MARKER_KEY = 'mkhco-ai-dashboard-app.chat.v1.server-migrated';
const ACTIVE_SESSION_KEY = 'mkhco-ai-dashboard-app.active-session.v2';
const DRAFTS_KEY = 'mkhco-ai-dashboard-app.drafts.v2';

const starterPrompts = [
  'Inspect the live demo data and tell me what looks abnormal right now.',
  'Create a new Grafana dashboard called AI Demo Service Health using the live demo Prometheus metrics. Use useful modern panels, validate every PromQL query, create it through Grafana MCP, then verify it and give me the URL.',
  'Show me one actual raw CPU sample from Prometheus and tell me its unit.',
];

const pageStyle: CSSProperties = {
  minHeight: 'calc(100vh - 80px)',
  display: 'grid',
  gridTemplateColumns: 'minmax(0, 1fr) minmax(420px, 560px)',
  gap: 20,
  padding: 20,
};

const panelStyle: CSSProperties = {
  border: '1px solid rgba(128,128,128,0.25)',
  borderRadius: 8,
};

const messagesStyle: CSSProperties = {
  flex: 1,
  overflowY: 'auto',
  padding: 16,
  display: 'flex',
  flexDirection: 'column',
  gap: 14,
};

const inputStyle: CSSProperties = {
  width: '100%',
  resize: 'vertical',
  minHeight: 86,
  borderRadius: 6,
  border: '1px solid rgba(128,128,128,0.45)',
  background: 'transparent',
  color: 'inherit',
  padding: 10,
  font: 'inherit',
  lineHeight: 1.7,
};

function readDrafts(): Record<string, string> {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(DRAFTS_KEY) || '{}');
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function readLegacyState(): LegacyChatState | null {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(LEGACY_STORAGE_KEY) || 'null') as LegacyChatState | null;
    if (!parsed || !Array.isArray(parsed.messages)) {
      return null;
    }
    const usefulMessages = parsed.messages.filter(
      (item) =>
        item &&
        item.id !== 'welcome' &&
        (item.role === 'user' || item.role === 'assistant') &&
        typeof item.content === 'string' &&
        item.content.trim()
    );
    return usefulMessages.length ? { ...parsed, messages: usefulMessages } : null;
  } catch {
    return null;
  }
}

function statusColor(status: string) {
  if (status === 'completed') return '#56a64b';
  if (status === 'running' || status === 'queued') return '#5794f2';
  if (status === 'failed' || status === 'interrupted') return '#e02f44';
  return 'rgba(128,128,128,0.8)';
}

function statusLabel(status: string) {
  if (status === 'completed') return 'تکمیل‌شده';
  if (status === 'running') return 'در حال اجرا';
  if (status === 'queued') return 'در صف';
  if (status === 'failed') return 'ناموفق';
  if (status === 'interrupted') return 'متوقف‌شده';
  return status;
}

export default function App() {
  const [connection, setConnection] = useState<ModelConnection>({ provider: 'ollama', base_url: '', model: '', api_key: '' });
  const [connected, setConnected] = useState<boolean | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState('');
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [input, setInput] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [migrationNotice, setMigrationNotice] = useState('');
  const messageEndRef = useRef<HTMLDivElement | null>(null);

  const refreshSessionList = useCallback(async () => {
    const values = await listSessions();
    setSessions(values);
    return values;
  }, []);

  const reloadSelectedSession = useCallback(async () => {
    if (!selectedSessionId) return null;
    const value = await getSession(selectedSessionId);
    setSession(value);
    return value;
  }, [selectedSessionId]);

  useEffect(() => {
    let cancelled = false;
    const initialize = async () => {
      try {
        await health();
        if (cancelled) return;
        setConnected(true);

        let migratedSessionId = '';
        let legacyDraft = '';
        if (!window.localStorage.getItem(MIGRATION_MARKER_KEY)) {
          const legacy = readLegacyState();
          const rawLegacy = (() => {
            try {
              return JSON.parse(window.localStorage.getItem(LEGACY_STORAGE_KEY) || 'null') as { input?: unknown } | null;
            } catch {
              return null;
            }
          })();
          legacyDraft = typeof rawLegacy?.input === 'string' ? rawLegacy.input : '';
          if (legacy) {
            const migrated = await importLegacyChat(legacy);
            migratedSessionId = migrated.session.id;
            setMigrationNotice(
              migrated.already_imported ? 'Your browser chat was already imported.' : 'Your previous browser chat was imported to SQLite.'
            );
          }
          // Preserve the old key for manual recovery; this marker only prevents duplicate imports.
          window.localStorage.setItem(MIGRATION_MARKER_KEY, 'done');
        }

        let values = await refreshSessionList();
        if (!values.length) {
          const created = await createSession();
          values = await refreshSessionList();
          migratedSessionId = created.id;
        }
        const remembered = window.localStorage.getItem(ACTIVE_SESSION_KEY) || '';
        const preferred =
          migratedSessionId || (values.some((item) => item.id === remembered) ? remembered : '') || values[0]?.id || '';
        if (legacyDraft && preferred) {
          const drafts = readDrafts();
          drafts[preferred] = legacyDraft;
          window.localStorage.setItem(DRAFTS_KEY, JSON.stringify(drafts));
        }
        if (!cancelled) setSelectedSessionId(preferred);
      } catch (cause) {
        if (cancelled) return;
        setConnected(false);
        setError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void initialize();
    return () => {
      cancelled = true;
    };
  }, [refreshSessionList]);

  useEffect(() => {
    if (!selectedSessionId) {
      setSession(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    window.localStorage.setItem(ACTIVE_SESSION_KEY, selectedSessionId);
    setInput(readDrafts()[selectedSessionId] || '');
    getSession(selectedSessionId)
      .then((value) => {
        if (!cancelled) {
          setSession(value);
          setError('');
        }
      })
      .catch((cause) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedSessionId]);

  useEffect(() => {
    if (!selectedSessionId) return;
    const drafts = readDrafts();
    if (input) drafts[selectedSessionId] = input;
    else delete drafts[selectedSessionId];
    try {
      window.localStorage.setItem(DRAFTS_KEY, JSON.stringify(drafts));
    } catch {
      // Draft persistence is optional; server messages remain authoritative.
    }
  }, [input, selectedSessionId]);

  const activeJobId = session?.active_job?.id || '';
  useEffect(() => {
    if (!activeJobId) return;
    let cancelled = false;
    let polling = false;
    const poll = async () => {
      if (polling || cancelled) return;
      polling = true;
      try {
        const job = await getJob(activeJobId);
        if (cancelled) return;
        if (job.status === 'queued' || job.status === 'running') {
          setSession((current) => (current ? { ...current, active_job: job } : current));
        } else {
          await reloadSelectedSession();
          await refreshSessionList();
        }
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        polling = false;
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeJobId, refreshSessionList, reloadSelectedSession]);

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [session?.messages, session?.active_job?.progress]);

  const latestJob = useMemo<JobResponse | null>(() => {
    const jobs = session?.jobs || [];
    return session?.active_job || jobs[jobs.length - 1] || null;
  }, [session]);
  const working = submitting || latestJob?.status === 'queued' || latestJob?.status === 'running';
  const messages = session?.messages || [];

  const send = async (text?: string) => {
    const message = (text ?? input).trim();
    if (!message || !selectedSessionId || working) return;
    setSubmitting(true);
    setError('');
    setInput('');
    try {
      await startChat(selectedSessionId, message, connection);
      await reloadSelectedSession();
      await refreshSessionList();
    } catch (cause) {
      setInput(message);
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSubmitting(false);
    }
  };

  const newChat = async () => {
    try {
      const created = await createSession();
      await refreshSessionList();
      setSelectedSessionId(created.id);
      setError('');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  const retry = async () => {
    if (!latestJob || !['failed', 'interrupted'].includes(latestJob.status)) return;
    setSubmitting(true);
    setError('');
    try {
      await retryJob(latestJob.id, connection);
      await reloadSelectedSession();
      await refreshSessionList();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSubmitting(false);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void send();
  };

  return (
    <div data-testid="ai-dashboard-builder-root" style={pageStyle}>
      <main style={{ ...panelStyle, padding: 24, minHeight: 620 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
          <div>
            <h1 style={{ marginTop: 0 }}>AI Dashboard Builder</h1>
            <p style={{ maxWidth: 760, opacity: 0.8 }}>
              Use local Ollama or an OpenAI-compatible API with the Grafana MCP agent. The model discovers real metrics,
              validates PromQL, writes dashboards through MCP, and verifies saved queries before claiming success.
            </p>
          </div>
          <div style={{ fontSize: 13, opacity: 0.8 }}>
            AI bridge: {connected === null ? 'checking…' : connected ? 'connected' : 'offline'}
          </div>
        </div>

        {connected === false && (
          <Alert title="Local AI bridge is offline" severity="error">
            Run ./start.sh and check .run/ai-api.log.
          </Alert>
        )}
        {migrationNotice && <Alert title="Chat migration complete" severity="success">{migrationNotice}</Alert>}

        <ModelSettings value={connection} onChange={setConnection} disabled={working} />
        <h3>Try it</h3>
        <div style={{ display: 'grid', gap: 10, maxWidth: 900 }}>
          {starterPrompts.map((prompt) => (
            <button
              key={prompt}
              type="button"
              onClick={() => void send(prompt)}
              disabled={working || !selectedSessionId}
              style={{
                textAlign: 'left',
                border: '1px solid rgba(128,128,128,0.28)',
                borderRadius: 8,
                background: 'transparent',
                color: 'inherit',
                padding: 14,
                cursor: working ? 'default' : 'pointer',
              }}
            >
              {prompt}
            </button>
          ))}
        </div>

        <div style={{ marginTop: 28 }}>
          <h3>Persistent conversations</h3>
          <p style={{ opacity: 0.8 }}>
            Messages, progress, model selection, results, and dashboard links are stored by the local AI bridge in SQLite. You can
            navigate away, close the browser, or restart Grafana and reopen any chat from the history list.
          </p>
          <p style={{ opacity: 0.8 }}>
            A running Python worker continues when you leave this page. If the AI bridge itself restarts, its saved request becomes
            interrupted and can be retried safely from this conversation.
          </p>
        </div>

        {latestJob?.dashboard_url && (
          <div style={{ marginTop: 24 }}>
            <Button onClick={() => (window.location.href = latestJob.dashboard_url)}>Open created dashboard</Button>
          </div>
        )}
      </main>

      <aside dir="rtl" style={{ ...panelStyle, minHeight: 620, maxHeight: 'calc(100vh - 110px)', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <ChatSurfaceStyles />
        <div style={{ padding: 14, borderBottom: '1px solid rgba(128,128,128,0.25)' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
            <strong style={{ fontSize: 16 }}>گفت‌وگوها</strong>
            <button
              type="button"
              data-testid="new-chat"
              onClick={() => void newChat()}
              style={{ border: '1px solid rgba(128,128,128,0.35)', borderRadius: 6, background: 'transparent', color: 'inherit', padding: '6px 9px', cursor: 'pointer' }}
            >
              + گفت‌وگوی جدید
            </button>
          </div>
          <div style={{ display: 'grid', gap: 6, marginTop: 10, maxHeight: 150, overflowY: 'auto' }}>
            {sessions.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setSelectedSessionId(item.id)}
                style={{
                  display: 'grid',
                  gridTemplateColumns: '1fr auto',
                  gap: 8,
                  textAlign: 'start',
                  border: item.id === selectedSessionId ? '1px solid #5794f2' : '1px solid rgba(128,128,128,0.25)',
                  borderRadius: 6,
                  background: item.id === selectedSessionId ? 'rgba(87,148,242,0.12)' : 'transparent',
                  color: 'inherit',
                  padding: '8px 10px',
                  cursor: 'pointer',
                }}
              >
                <span dir="auto" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.title}</span>
                <span style={{ color: statusColor(item.status), fontSize: 11 }}>{statusLabel(item.status)}</span>
              </button>
            ))}
          </div>
        </div>

        <div style={{ padding: '12px 16px', borderBottom: '1px solid rgba(128,128,128,0.25)' }}>
          <strong dir="auto">{session?.title || 'در حال بارگذاری گفت‌وگو…'}</strong>
          <div style={{ fontSize: 12, opacity: 0.7, marginTop: 4 }}>
            {working
              ? `در حال پردازش با ${latestJob?.model || 'مدل انتخاب‌شده'}…`
              : latestJob
                ? `${statusLabel(latestJob.status)} · ${latestJob.model || 'مدل انتخاب‌شده'}`
                : 'آماده'}
          </div>
        </div>

        <div style={messagesStyle} data-testid="chat-messages">
          {!messages.length && !loading && (
            <div style={{ opacity: 0.75 }}>
              بنویسید چه چیزی از داده‌های زنده می‌خواهید بدانید یا چه داشبوردی باید ساخته شود.
            </div>
          )}
          {messages.map((message) => <ChatMessageBubble key={message.id} message={message} />)}
          {working && (
            <div dir="auto" style={{ display: 'flex', alignItems: 'center', gap: 8, opacity: 0.8 }}>
              <Spinner size={16} />
              <span>{latestJob?.progress?.length ? latestJob.progress[latestJob.progress.length - 1] : 'در حال شروع عامل…'}</span>
            </div>
          )}
          <div ref={messageEndRef} />
        </div>

        {latestJob?.progress?.length ? (
          <details style={{ padding: '0 16px 10px' }} open={working}>
            <summary style={{ cursor: 'pointer' }}>روند اجرای عامل ({latestJob.progress.length})</summary>
            <pre dir="ltr" style={{ maxHeight: 170, overflow: 'auto', fontSize: 11, whiteSpace: 'pre-wrap', textAlign: 'left' }}>
              {latestJob.progress.slice(-15).join('\n')}
            </pre>
          </details>
        ) : null}

        {(error || latestJob?.error) && (
          <div style={{ padding: '0 16px 10px' }}>
            <Alert title={latestJob?.status === 'interrupted' ? 'اجرای هوش مصنوعی متوقف شد' : 'درخواست هوش مصنوعی ناموفق بود'} severity="error">
              {error || latestJob?.error}
            </Alert>
            {latestJob && ['failed', 'interrupted'].includes(latestJob.status) && (
              <div style={{ marginTop: 8 }}><Button onClick={() => void retry()} disabled={submitting}>تلاش دوباره</Button></div>
            )}
          </div>
        )}

        <form onSubmit={submit} style={{ padding: 16, borderTop: '1px solid rgba(128,128,128,0.25)' }}>
          <textarea
            aria-label="پیام به دستیار Grafana"
            dir="auto"
            value={input}
            onChange={(event) => setInput(event.currentTarget.value)}
            placeholder="مثلاً یک داشبورد برای نرخ درخواست، خطاها، تأخیر p95، CPU و حافظه بساز…"
            disabled={working || connected === false || !selectedSessionId}
            style={inputStyle}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginTop: 10 }}>
            <span style={{ fontSize: 11, opacity: 0.65 }}>پیام‌ها روی سرور ذخیره می‌شوند.</span>
            <Button type="submit" disabled={working || !input.trim() || connected === false || !selectedSessionId}>
              {working ? 'در حال پردازش…' : 'ارسال'}
            </Button>
          </div>
        </form>
      </aside>
    </div>
  );
}
