#!/usr/bin/env node

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

const grafanaUrl = (process.env.GRAFANA_URL || 'http://127.0.0.1:3000').replace(/\/$/, '');
const username = process.env.GRAFANA_ADMIN_USER || 'admin';
const password = process.env.GRAFANA_ADMIN_PASSWORD || 'admin';
const pluginId = 'mkhco-ai-dashboard-app';
const marker = 'ai-dashboard-builder-root';
const outputPath = process.argv[2] || '';
const appPath = process.env.AI_PLUGIN_APP_PATH || `/a/${pluginId}`;
const expectedText = process.env.AI_PLUGIN_EXPECT_TEXT || '';
const requireAssistant = process.env.AI_PLUGIN_REQUIRE_ASSISTANT === '1';

function findChrome() {
  const candidates = [process.env.CHROME_BIN, 'google-chrome', 'chromium', 'chromium-browser'].filter(Boolean);
  const pathEntries = (process.env.PATH || '').split(path.delimiter);
  for (const candidate of candidates) {
    if (candidate.includes('/') && fs.existsSync(candidate)) {
      return candidate;
    }
    for (const directory of pathEntries) {
      const resolved = path.join(directory, candidate);
      if (fs.existsSync(resolved)) {
        return resolved;
      }
    }
  }
  throw new Error('No Chrome/Chromium executable found (set CHROME_BIN to override)');
}

async function freePort() {
  return await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = typeof address === 'object' && address ? address.port : 0;
      server.close(() => resolve(port));
    });
  });
}

async function retry(fn, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      return await fn();
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 150));
    }
  }
  throw lastError || new Error('operation timed out');
}

class CdpClient {
  constructor(url) {
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    this.socket = new WebSocket(url);
  }

  async open() {
    await new Promise((resolve, reject) => {
      this.socket.addEventListener('open', resolve, { once: true });
      this.socket.addEventListener('error', () => reject(new Error('CDP websocket failed')), { once: true });
    });
    this.socket.addEventListener('message', (event) => {
      const message = JSON.parse(String(event.data));
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result);
        return;
      }
      for (const listener of this.listeners.get(message.method) || []) {
        listener(message.params || {});
      }
    });
  }

  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  on(method, listener) {
    const listeners = this.listeners.get(method) || [];
    listeners.push(listener);
    this.listeners.set(method, listeners);
  }

  close() {
    this.socket.close();
  }
}

async function evaluate(client, expression) {
  const result = await client.send('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text || 'browser evaluation failed');
  }
  return result.result?.value;
}

async function waitForReady(client, expectedUrl) {
  try {
    await retry(async () => {
      const ready = await evaluate(
        client,
        `document.readyState === "complete" && location.href.startsWith(${JSON.stringify(expectedUrl)})`
      );
      if (!ready) throw new Error('document is not ready');
      return true;
    });
  } catch (error) {
    const state = await evaluate(client, '({url: location.href, readyState: document.readyState})').catch(() => null);
    throw new Error(`${error instanceof Error ? error.message : error}; browser=${JSON.stringify(state)}`);
  }
}

const report = {
  url: `${grafanaUrl}${appPath.startsWith('/') ? appPath : `/${appPath}`}`,
  marker,
  markerVisible: false,
  appNotFoundVisible: false,
  pageNotFoundVisible: false,
  expectedTextVisible: !expectedText,
  assistantMessageCount: 0,
  moduleResponses: [],
  failedRequests: [],
  exceptions: [],
  consoleErrors: [],
};

let chrome;
let client;
let profile;
try {
  const port = await freePort();
  profile = fs.mkdtempSync(path.join(os.tmpdir(), 'grafana-ai-chrome-'));
  chrome = spawn(findChrome(), [
    '--headless=new',
    '--no-sandbox',
    '--disable-gpu',
    '--disable-dev-shm-usage',
    '--disable-background-networking',
    '--no-first-run',
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] });
  let chromeStderr = '';
  chrome.stderr.on('data', (chunk) => {
    chromeStderr = `${chromeStderr}${chunk}`.slice(-8_000);
  });

  const targets = await retry(async () => {
    const response = await fetch(`http://127.0.0.1:${port}/json/list`);
    if (!response.ok) throw new Error(`Chrome target API returned HTTP ${response.status}`);
    const value = await response.json();
    const pages = value.filter((target) => target.type === 'page' && !target.url.startsWith('chrome-extension://'));
    if (!pages.length) throw new Error('Chrome has no page target');
    return pages;
  });
  client = new CdpClient(targets[0].webSocketDebuggerUrl);
  await client.open();
  await Promise.all([
    client.send('Page.enable'),
    client.send('Runtime.enable'),
    client.send('Network.enable'),
    client.send('Log.enable'),
  ]);
  client.on('Runtime.exceptionThrown', ({ exceptionDetails }) => {
    report.exceptions.push({
      text: exceptionDetails?.exception?.description || exceptionDetails?.text || 'unknown exception',
      url: exceptionDetails?.url || '',
      line: exceptionDetails?.lineNumber ?? null,
      column: exceptionDetails?.columnNumber ?? null,
    });
  });
  client.on('Runtime.consoleAPICalled', ({ type, args }) => {
    if (type !== 'error') return;
    report.consoleErrors.push(args?.map((item) => item.value || item.description || '').join(' ') || 'console.error');
  });
  client.on('Log.entryAdded', ({ entry }) => {
    if (entry?.level === 'error') report.consoleErrors.push(entry.text || 'browser log error');
  });
  client.on('Network.responseReceived', ({ response }) => {
    if (response?.url?.includes(`/public/plugins/${pluginId}/module.js`)) {
      report.moduleResponses.push({ url: response.url, status: response.status, fromDiskCache: response.fromDiskCache || false });
    }
  });
  client.on('Network.loadingFailed', ({ errorText, blockedReason, type }) => {
    report.failedRequests.push({ error: errorText || 'request failed', blockedReason: blockedReason || '', type: type || '' });
  });

  report.loginNavigation = await client.send('Page.navigate', { url: `${grafanaUrl}/login` });
  await waitForReady(client, grafanaUrl);
  const loginResult = await evaluate(client, `fetch('/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({user: ${JSON.stringify(username)}, password: ${JSON.stringify(password)}})
  }).then(async response => ({status: response.status, body: await response.text()}))`);
  if (loginResult?.status !== 200) {
    throw new Error(`Grafana login returned HTTP ${loginResult?.status || 'unknown'}`);
  }

  await client.send('Network.clearBrowserCache');
  await client.send('Page.navigate', { url: `${report.url}?frontend-smoke=${Date.now()}` });
  await waitForReady(client, report.url);
  const deadline = Date.now() + 20_000;
  let state;
  do {
    state = await evaluate(client, `(() => {
      const marker = document.querySelector('[data-testid="${marker}"]');
      const text = document.body?.innerText || '';
      return {
        markerVisible: Boolean(marker),
        appNotFoundVisible: /App not found/i.test(text),
        pageNotFoundVisible: /Page not found/i.test(text),
        expectedTextVisible: !${JSON.stringify(expectedText)} || text.includes(${JSON.stringify(expectedText)}),
        assistantMessageCount: document.querySelectorAll('[data-testid="chat-message-assistant"]').length,
        loadingVisible: Boolean(document.querySelector('[aria-label="Loading"]')),
        aiLinks: Array.from(document.querySelectorAll('a'))
          .filter(link => /AI Dashboard Builder/i.test(link.textContent || ''))
          .map(link => ({text: (link.textContent || '').trim(), href: link.getAttribute('href') || ''})),
      };
    })()`);
    if (
      (state.markerVisible && state.expectedTextVisible && (!requireAssistant || state.assistantMessageCount > 0)) ||
      state.appNotFoundVisible ||
      state.pageNotFoundVisible ||
      (!state.loadingVisible && Date.now() + 1_000 > deadline)
    ) break;
    await new Promise((resolve) => setTimeout(resolve, 250));
  } while (Date.now() < deadline);
  Object.assign(report, state || {});
  report.ok =
    report.markerVisible &&
    !report.appNotFoundVisible &&
    !report.pageNotFoundVisible &&
    report.expectedTextVisible &&
    (!requireAssistant || report.assistantMessageCount > 0) &&
    report.moduleResponses.some((item) => item.status === 200) &&
    report.exceptions.length === 0;
} catch (error) {
  report.ok = false;
  report.runnerError = error instanceof Error ? error.message : String(error);
} finally {
  if (client) client.close();
  if (chrome && !chrome.killed) {
    chrome.kill('SIGTERM');
    await Promise.race([
      new Promise((resolve) => chrome.once('exit', resolve)),
      new Promise((resolve) => setTimeout(resolve, 2_000)),
    ]);
  }
  if (profile) {
    try {
      fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
    } catch {
      // The OS will clean its temporary directory; this must not hide the frontend result.
    }
  }
}

const serialized = `${JSON.stringify(report, null, 2)}\n`;
if (outputPath) fs.writeFileSync(outputPath, serialized, { mode: 0o600 });
process.stdout.write(serialized);
process.exit(report.ok ? 0 : 1);
