import React, { CSSProperties, FormEvent, useEffect, useMemo, useRef, useState } from 'react';

import { Alert, Button, Spinner } from '@grafana/ui';

import { ChatHistoryItem, health, startChat, waitForJob } from './api';

type UiMessage = ChatHistoryItem & { id: string };

type PersistedChatState = {
  messages: UiMessage[];
  input: string;
  model: string;
  progress: string[];
  error: string;
  dashboardUrl: string;
  activeJobId: string;
};

const STORAGE_KEY = 'mkhco-ai-dashboard-app.chat.v1';

const welcomeMessage: UiMessage = {
  id: 'welcome',
  role: 'assistant',
  content:
    'Tell me what you want to learn from the live data or what dashboard you want created. Read-only questions use the fast local model; dashboard writes use the stronger local model.',
};

const starterPrompts = [
  'Inspect the live demo data and tell me what looks abnormal right now.',
  'Create a new Grafana dashboard called AI Demo Service Health using the live demo Prometheus metrics. Use useful modern panels, validate every PromQL query, create it through Grafana MCP, then verify it and give me the URL.',
  'Show me one actual raw CPU sample from Prometheus and tell me its unit.',
];

const pageStyle: CSSProperties = {
  minHeight: 'calc(100vh - 80px)',
  display: 'grid',
  gridTemplateColumns: 'minmax(0, 1fr) minmax(380px, 520px)',
  gap: 20,
  padding: 20,
};

const workspaceStyle: CSSProperties = {
  border: '1px solid rgba(128,128,128,0.25)',
  borderRadius: 8,
  padding: 24,
  minHeight: 620,
};

const sidebarStyle: CSSProperties = {
  border: '1px solid rgba(128,128,128,0.25)',
  borderRadius: 8,
  minHeight: 620,
  maxHeight: 'calc(100vh - 110px)',
  display: 'flex',
  flexDirection: 'column',
  overflow: 'hidden',
};

const messagesStyle: CSSProperties = {
  flex: 1,
  overflowY: 'auto',
  padding: 16,
  display: 'flex',
  flexDirection: 'column',
  gap: 12,
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
};

function emptyPersistedState(): PersistedChatState {
  return {
    messages: [welcomeMessage],
    input: '',
    model: '',
    progress: [],
    error: '',
    dashboardUrl: '',
    activeJobId: '',
  };
}

function loadPersistedState(): PersistedChatState {
  const fallback = emptyPersistedState();
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return fallback;
    }
    const parsed = JSON.parse(raw) as Partial<PersistedChatState>;
    const messages = Array.isArray(parsed.messages)
      ? parsed.messages
          .filter(
            (item): item is UiMessage =>
              Boolean(item) &&
              typeof item.id === 'string' &&
              (item.role === 'user' || item.role === 'assistant') &&
              typeof item.content === 'string'
          )
          .slice(-100)
      : [];
    return {
      messages: messages.length ? messages : fallback.messages,
      input: typeof parsed.input === 'string' ? parsed.input : '',
      model: typeof parsed.model === 'string' ? parsed.model : '',
      progress: Array.isArray(parsed.progress)
        ? parsed.progress.filter((item): item is string => typeof item === 'string').slice(-80)
        : [],
      error: typeof parsed.error === 'string' ? parsed.error : '',
      dashboardUrl: typeof parsed.dashboardUrl === 'string' ? parsed.dashboardUrl : '',
      activeJobId: typeof parsed.activeJobId === 'string' ? parsed.activeJobId : '',
    };
  } catch {
    return fallback;
  }
}

function MessageBubble({ message }: { message: UiMessage }) {
  const isUser = message.role === 'user';
  return (
    <div
      style={{
        alignSelf: isUser ? 'flex-end' : 'stretch',
        maxWidth: isUser ? '88%' : '100%',
        borderRadius: 8,
        padding: '10px 12px',
        background: isUser ? 'rgba(50,116,217,0.18)' : 'rgba(128,128,128,0.10)',
        whiteSpace: 'pre-wrap',
        lineHeight: 1.45,
      }}
    >
      {message.content}
    </div>
  );
}

export default function App() {
  const [restored] = useState<PersistedChatState>(() => loadPersistedState());
  const [connected, setConnected] = useState<boolean | null>(null);
  const [messages, setMessages] = useState<UiMessage[]>(restored.messages);
  const [input, setInput] = useState(restored.input);
  const [submitting, setSubmitting] = useState(false);
  const [activeJobId, setActiveJobId] = useState(restored.activeJobId);
  const [model, setModel] = useState(restored.model);
  const [progress, setProgress] = useState<string[]>(restored.progress);
  const [error, setError] = useState(restored.error);
  const [dashboardUrl, setDashboardUrl] = useState(restored.dashboardUrl);
  const messageEndRef = useRef<HTMLDivElement | null>(null);
  const working = submitting || Boolean(activeJobId);

  useEffect(() => {
    health()
      .then(() => setConnected(true))
      .catch(() => setConnected(false));
  }, []);

  useEffect(() => {
    const state: PersistedChatState = {
      messages: messages.slice(-100),
      input,
      model,
      progress: progress.slice(-80),
      error,
      dashboardUrl,
      activeJobId,
    };
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch {
      // Chat persistence is best-effort. The app remains usable if browser storage is unavailable.
    }
  }, [messages, input, model, progress, error, dashboardUrl, activeJobId]);

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, progress]);

  useEffect(() => {
    if (!activeJobId) {
      return;
    }

    let cancelled = false;
    const resume = async () => {
      try {
        const result = await waitForJob(activeJobId, (job) => {
          if (cancelled) {
            return;
          }
          setModel(job.model);
          setProgress(job.progress ?? []);
        });
        if (cancelled) {
          return;
        }
        if (result.status === 'failed') {
          throw new Error(result.error || 'AI job failed');
        }
        const assistantId = `job-${result.id}`;
        setMessages((items) => {
          if (items.some((item) => item.id === assistantId)) {
            return items;
          }
          return [
            ...items,
            {
              id: assistantId,
              role: 'assistant',
              content: result.answer || 'The agent completed without a text answer.',
            },
          ];
        });
        setDashboardUrl(result.dashboard_url || '');
        setError('');
        setActiveJobId('');
      } catch (cause) {
        if (cancelled) {
          return;
        }
        setError(cause instanceof Error ? cause.message : String(cause));
        setActiveJobId('');
      }
    };

    void resume();
    return () => {
      cancelled = true;
    };
  }, [activeJobId]);

  const history = useMemo<ChatHistoryItem[]>(
    () =>
      messages
        .filter((item) => item.id !== 'welcome')
        .map(({ role, content }) => ({ role, content }))
        .slice(-10),
    [messages]
  );

  const send = async (text?: string) => {
    const message = (text ?? input).trim();
    if (!message || working) {
      return;
    }

    setError('');
    setDashboardUrl('');
    setProgress([]);
    setInput('');
    setSubmitting(true);
    const userMessage: UiMessage = { id: `u-${Date.now()}`, role: 'user', content: message };
    setMessages((items) => [...items, userMessage]);

    try {
      const started = await startChat(message, history);
      setModel(started.model);
      setActiveJobId(started.job_id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSubmitting(false);
    }
  };

  const clearChat = () => {
    if (working) {
      return;
    }
    const fresh = emptyPersistedState();
    setMessages(fresh.messages);
    setInput('');
    setModel('');
    setProgress([]);
    setError('');
    setDashboardUrl('');
    setActiveJobId('');
    try {
      window.localStorage.removeItem(STORAGE_KEY);
    } catch {
      // Ignore unavailable browser storage.
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void send();
  };

  return (
    <div style={pageStyle}>
      <main style={workspaceStyle}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
          <div>
            <h1 style={{ marginTop: 0 }}>AI Dashboard Builder</h1>
            <p style={{ maxWidth: 760, opacity: 0.8 }}>
              This app talks to the same local Ollama + Grafana MCP agent used by the CLI. The model must discover real metrics,
              validate PromQL, write dashboards through MCP, and verify saved queries before claiming success.
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

        <h3>Try it</h3>
        <div style={{ display: 'grid', gap: 10, maxWidth: 900 }}>
          {starterPrompts.map((prompt) => (
            <button
              key={prompt}
              type="button"
              onClick={() => void send(prompt)}
              disabled={working}
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
          <h3>How model routing works</h3>
          <p style={{ opacity: 0.8 }}>
            Questions and data inspection use <code>qwen3:4b</code>. Creating or modifying a dashboard automatically uses{' '}
            <code>qwen3:8b</code>. Both use the same host-side grounding and dashboard verification guards.
          </p>
          <p style={{ opacity: 0.8 }}>
            Grafana 12.1 does not expose a public extension point inside the native “New dashboard” chooser, so “Create dashboard with AI”
            is available from this app page and Grafana’s command palette. The right-hand chat is the first-party builder workspace without
            maintaining a fork of Grafana.
          </p>
          <p style={{ opacity: 0.8 }}>
            Chat history and the current AI job are saved in this browser. You can navigate to another Grafana page or switch tabs and come
            back; an in-progress job will keep running on the local AI bridge and this page will reconnect to it automatically.
          </p>
        </div>

        {dashboardUrl && (
          <div style={{ marginTop: 24 }}>
            <Button onClick={() => (window.location.href = dashboardUrl)}>Open created dashboard</Button>
          </div>
        )}
      </main>

      <aside style={sidebarStyle}>
        <div
          style={{
            padding: '14px 16px',
            borderBottom: '1px solid rgba(128,128,128,0.25)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 12,
          }}
        >
          <div>
            <strong>Local Grafana AI</strong>
            <div style={{ fontSize: 12, opacity: 0.7, marginTop: 4 }}>
              {working
                ? `Working with ${model || 'local model'}… safe to leave this page`
                : model
                  ? `Last model: ${model}`
                  : 'Ready'}
            </div>
          </div>
          <button
            type="button"
            onClick={clearChat}
            disabled={working}
            style={{
              border: '1px solid rgba(128,128,128,0.35)',
              borderRadius: 6,
              background: 'transparent',
              color: 'inherit',
              padding: '6px 9px',
              cursor: working ? 'default' : 'pointer',
              opacity: working ? 0.5 : 0.8,
            }}
          >
            New chat
          </button>
        </div>

        <div style={messagesStyle}>
          {messages.map((message) => (
            <MessageBubble key={message.id} message={message} />
          ))}
          {working && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, opacity: 0.8 }}>
              <Spinner size={16} />
              <span>{progress.length ? progress[progress.length - 1] : 'Starting local agent…'}</span>
            </div>
          )}
          <div ref={messageEndRef} />
        </div>

        {progress.length > 0 && working && (
          <details style={{ padding: '0 16px 10px' }}>
            <summary style={{ cursor: 'pointer' }}>Agent progress ({progress.length})</summary>
            <pre style={{ maxHeight: 170, overflow: 'auto', fontSize: 11, whiteSpace: 'pre-wrap' }}>
              {progress.slice(-15).join('\n')}
            </pre>
          </details>
        )}

        {error && (
          <div style={{ padding: '0 16px 10px' }}>
            <Alert title="AI request failed" severity="error">
              {error}
            </Alert>
          </div>
        )}

        <form onSubmit={submit} style={{ padding: 16, borderTop: '1px solid rgba(128,128,128,0.25)' }}>
          <textarea
            aria-label="Ask the Grafana AI"
            value={input}
            onChange={(event) => setInput(event.currentTarget.value)}
            placeholder="e.g. Create a new dashboard for request rate, errors, p95 latency, CPU and memory…"
            disabled={working || connected === false}
            style={inputStyle}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginTop: 10 }}>
            <span style={{ fontSize: 11, opacity: 0.65 }}>
              {working ? 'This job will keep running if you navigate away.' : 'Chat is saved in this browser.'}
            </span>
            <Button type="submit" disabled={working || !input.trim() || connected === false}>
              {working ? 'Working…' : 'Send'}
            </Button>
          </div>
        </form>
      </aside>
    </div>
  );
}
