import React, { CSSProperties, FormEvent, useEffect, useMemo, useRef, useState } from 'react';

import { Alert, Button, Spinner } from '@grafana/ui';

import { ChatHistoryItem, health, startChat, waitForJob } from './api';

type UiMessage = ChatHistoryItem & { id: string };

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
  const [connected, setConnected] = useState<boolean | null>(null);
  const [messages, setMessages] = useState<UiMessage[]>([
    {
      id: 'welcome',
      role: 'assistant',
      content:
        'Tell me what you want to learn from the live data or what dashboard you want created. Read-only questions use the fast local model; dashboard writes use the stronger local model.',
    },
  ]);
  const [input, setInput] = useState('');
  const [working, setWorking] = useState(false);
  const [model, setModel] = useState('');
  const [progress, setProgress] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [dashboardUrl, setDashboardUrl] = useState('');
  const messageEndRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    health()
      .then(() => setConnected(true))
      .catch(() => setConnected(false));
  }, []);

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, progress]);

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
    setWorking(true);
    const userMessage: UiMessage = { id: `u-${Date.now()}`, role: 'user', content: message };
    setMessages((items) => [...items, userMessage]);

    try {
      const started = await startChat(message, history);
      setModel(started.model);
      const result = await waitForJob(started.job_id, (job) => {
        setModel(job.model);
        setProgress(job.progress ?? []);
      });
      if (result.status === 'failed') {
        throw new Error(result.error || 'AI job failed');
      }
      setMessages((items) => [
        ...items,
        {
          id: `a-${Date.now()}`,
          role: 'assistant',
          content: result.answer || 'The agent completed without a text answer.',
        },
      ]);
      setDashboardUrl(result.dashboard_url || '');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setWorking(false);
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
        </div>

        {dashboardUrl && (
          <div style={{ marginTop: 24 }}>
            <Button onClick={() => (window.location.href = dashboardUrl)}>Open created dashboard</Button>
          </div>
        )}
      </main>

      <aside style={sidebarStyle}>
        <div style={{ padding: '14px 16px', borderBottom: '1px solid rgba(128,128,128,0.25)' }}>
          <strong>Local Grafana AI</strong>
          <div style={{ fontSize: 12, opacity: 0.7, marginTop: 4 }}>
            {working ? `Working with ${model || 'local model'}…` : model ? `Last model: ${model}` : 'Ready'}
          </div>
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
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 10 }}>
            <Button type="submit" disabled={working || !input.trim() || connected === false}>
              {working ? 'Working…' : 'Send'}
            </Button>
          </div>
        </form>
      </aside>
    </div>
  );
}
