import React, { FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import * as ReactDOM from 'react-dom';

import { config, locationService } from '@grafana/runtime';
import { Alert, Button, Spinner } from '@grafana/ui';

import {
  createSession,
  getConnection,
  getJob,
  getSession,
  health,
  listSessions,
  startChat,
} from './api';
import type { ModelConnection, SessionDetail, SessionSummary } from './api';
import { ChatMessageBubble, ChatSurfaceStyles } from './ChatMessageBubble';

const PLUGIN_PATH = '/a/mkhco-ai-dashboard-app';
const ACTIVE_SESSION_KEY = 'mkhco-ai-dashboard-app.active-session.v2';

function shouldHideGlobalChat(pathname: string) {
  return pathname.includes('mkhco-ai-dashboard-app') || /^\/(d|d-solo)\//.test(pathname) || /^\/dashboard(?:\/|$)/.test(pathname);
}

function GlobalChatSidebar() {
  const theme = config.theme2;
  const user = config.bootData.user;
  const canUse = user.isSignedIn && (user.isGrafanaAdmin || user.orgRole === 'Admin' || user.orgRole === 'Editor');
  const [hiddenOnRoute, setHiddenOnRoute] = useState(() => shouldHideGlobalChat(window.location.pathname));
  const [open, setOpen] = useState(false);
  const [initialized, setInitialized] = useState(false);
  const [connected, setConnected] = useState(false);
  const [connection, setConnection] = useState<ModelConnection>({ provider: 'ollama', base_url: '', model: '' });
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const subscription = locationService.getLocationObservable().subscribe(location => {
      const hidden = shouldHideGlobalChat(location.pathname);
      setHiddenOnRoute(hidden);
      if (hidden) setOpen(false);
    });
    return () => subscription.unsubscribe();
  }, []);

  useEffect(() => {
    if (!open || initialized || !canUse) return;
    let active = true;
    Promise.all([health(), getConnection(), listSessions()]).then(async ([, defaults, existing]) => {
      let values = existing;
      if (!values.length) {
        await createSession();
        values = await listSessions();
      }
      if (!active) return;
      setConnected(true);
      setConnection({ provider: defaults.provider, base_url: defaults.base_url, model: defaults.model });
      setSessions(values);
      const remembered = window.localStorage.getItem(ACTIVE_SESSION_KEY) || '';
      setSelectedId(values.some(item => item.id === remembered) ? remembered : values[0]?.id || '');
      setInitialized(true);
    }).catch(cause => {
      if (active) setError(cause instanceof Error ? cause.message : String(cause));
    });
    return () => { active = false; };
  }, [canUse, initialized, open]);

  useEffect(() => {
    if (!selectedId) return;
    let active = true;
    window.localStorage.setItem(ACTIVE_SESSION_KEY, selectedId);
    getSession(selectedId).then(value => {
      if (active) {
        setSession(value);
        setError('');
      }
    }).catch(cause => { if (active) setError(cause instanceof Error ? cause.message : String(cause)); });
    return () => { active = false; };
  }, [selectedId]);

  const activeJobId = session?.active_job?.id || '';
  const activeSessionId = session?.id || '';
  useEffect(() => {
    if (!activeJobId || !activeSessionId) return;
    let active = true;
    const poll = async () => {
      try {
        const job = await getJob(activeJobId);
        if (!active) return;
        if (job.status === 'queued' || job.status === 'running') {
          setSession(current => current ? { ...current, active_job: job } : current);
        } else {
          const [detail, values] = await Promise.all([getSession(activeSessionId), listSessions()]);
          if (active) {
            setSession(detail);
            setSessions(values);
          }
        }
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : String(cause));
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1000);
    return () => { active = false; window.clearInterval(timer); };
  }, [activeJobId, activeSessionId]);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [session?.messages, session?.active_job?.progress]);

  const working = sending || session?.active_job?.status === 'queued' || session?.active_job?.status === 'running';
  const lastProgress = useMemo(() => session?.active_job?.progress?.slice(-1)[0] || '', [session?.active_job?.progress]);

  const send = async (event: FormEvent) => {
    event.preventDefault();
    const message = input.trim();
    if (!message || !selectedId || working) return;
    setSending(true);
    setError('');
    try {
      await startChat(selectedId, message, connection);
      setInput('');
      setSession(await getSession(selectedId));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSending(false);
    }
  };

  const newChat = async () => {
    try {
      const created = await createSession();
      setSessions(await listSessions());
      setSelectedId(created.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  if (!canUse || hiddenOnRoute) return null;
  const colors = theme.colors;
  return <>
    {!open && <button
      type="button"
      aria-label="Open Grafana AI chat"
      data-testid="global-ai-chat-open"
      onClick={() => setOpen(true)}
      style={{ position: 'fixed', right: 22, bottom: 22, zIndex: 10000, border: 0, borderRadius: 24,
        padding: '12px 17px', color: colors.primary.contrastText, background: colors.primary.main,
        boxShadow: '0 6px 24px rgba(0,0,0,.35)', cursor: 'pointer', fontWeight: 600 }}
    >✦ گفت‌وگوی هوشمند</button>}
    {open && <aside
      dir="rtl"
      aria-label="Grafana AI chat sidebar"
      data-testid="global-ai-chat-sidebar"
      style={{ position: 'fixed', right: 0, top: 0, bottom: 0, width: 'min(440px, 100vw)', zIndex: 10000,
        display: 'flex', flexDirection: 'column', background: colors.background.primary, color: colors.text.primary,
        borderLeft: `1px solid ${colors.border.weak}`, boxShadow: '-8px 0 30px rgba(0,0,0,.30)' }}
    >
      <header style={{ padding: 14, borderBottom: `1px solid ${colors.border.weak}` }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
          <strong style={{ fontSize: 16 }}>دستیار هوشمند Grafana</strong>
          <div style={{ display: 'flex', gap: 8 }}>
            <Button size="sm" variant="secondary" onClick={() => locationService.push(PLUGIN_PATH)}>تنظیمات</Button>
            <Button size="sm" variant="secondary" onClick={() => setOpen(false)}>بستن</Button>
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
          <select aria-label="گفت‌وگو" dir="auto" value={selectedId} onChange={event => setSelectedId(event.target.value)}
            style={{ flex: 1, minWidth: 0, height: 38, minHeight: 38, boxSizing: 'border-box', padding: '0 10px',
              lineHeight: 'normal', fontSize: 14, fontFamily: 'inherit', appearance: 'auto', color: 'inherit',
              background: colors.background.secondary, border: `1px solid ${colors.border.medium}`, borderRadius: 6 }}>
            {sessions.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}
          </select>
          <Button size="sm" onClick={() => void newChat()}>گفت‌وگوی جدید</Button>
        </div>
      </header>
      <div style={{ flex: 1, overflowY: 'auto', padding: 14, display: 'flex', flexDirection: 'column', gap: 13 }}>
        <ChatSurfaceStyles />
        {!initialized && !error && <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}><Spinner size={18} /> در حال اتصال…</div>}
        {session?.messages.map(message => <ChatMessageBubble key={message.id} message={message} compact />)}
        {working && <div dir="auto" style={{ display: 'flex', gap: 8, alignItems: 'center', opacity: .8 }}>
          <Spinner size={16} /> {lastProgress || 'در حال بررسی…'}
        </div>}
        {error && <Alert title="درخواست هوش مصنوعی ناموفق بود" severity="error">{error}</Alert>}
        <div ref={endRef} />
      </div>
      <form onSubmit={send} style={{ padding: 14, borderTop: `1px solid ${colors.border.weak}`, background: colors.background.primary }}>
        <textarea aria-label="پیام به دستیار Grafana" dir="auto" value={input} onChange={event => setInput(event.currentTarget.value)}
          placeholder="درباره متریک‌ها، خطاها یا ساخت داشبورد بپرسید…" disabled={!connected || working}
          onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}
          style={{ width: '100%', minHeight: 92, resize: 'vertical', boxSizing: 'border-box', padding: 11,
            lineHeight: 1.7, fontFamily: 'inherit', color: 'inherit', background: colors.background.secondary,
            border: `1px solid ${colors.border.medium}`, borderRadius: 8 }} />
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 8, gap: 8 }}>
          <small dir="ltr" style={{ opacity: .7 }}>{connection.model || 'اتصال تنظیم نشده'}</small>
          <Button type="submit" disabled={!connected || working || !input.trim()}>ارسال</Button>
        </div>
      </form>
    </aside>}
  </>;
}

export function mountGlobalChatSidebar() {
  if (typeof document === 'undefined' || document.getElementById('mkhco-global-ai-chat-root')) return;
  const mount = () => {
    if (document.getElementById('mkhco-global-ai-chat-root')) return;
    const root = document.createElement('div');
    root.id = 'mkhco-global-ai-chat-root';
    document.body.appendChild(root);
    ReactDOM.render(<GlobalChatSidebar />, root);
  };
  if (document.body) mount();
  else window.addEventListener('DOMContentLoaded', mount, { once: true });
}
