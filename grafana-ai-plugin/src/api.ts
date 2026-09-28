import { getBackendSrv } from '@grafana/runtime';
import { lastValueFrom } from 'rxjs';

const BASE = '/api/plugin-proxy/mkhco-ai-dashboard-app/ai';

export type JobStatus = 'queued' | 'running' | 'completed' | 'failed' | 'interrupted';

export type ChatMessage = {
  id: string;
  session_id: string;
  role: 'user' | 'assistant';
  content: string;
  created_at: number;
  job_id: string;
};

export type JobResponse = {
  id: string;
  session_id: string;
  user_message_id: string;
  status: JobStatus;
  model: string;
  progress: string[];
  answer: string;
  error: string;
  dashboard_url: string;
  created_at: number;
  updated_at: number;
};

export type SessionSummary = {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  status: JobStatus | 'idle';
  model: string;
  active_job_id: string;
};

export type SessionDetail = {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  messages: ChatMessage[];
  jobs: JobResponse[];
  active_job: JobResponse | null;
};

export type StartJobResponse = {
  job_id: string;
  status: JobStatus;
  model: string;
};

export type LegacyChatState = {
  messages: Array<{ id?: string; role?: string; content?: string }>;
  model?: string;
  progress?: string[];
  error?: string;
  dashboardUrl?: string;
  activeJobId?: string;
};

async function request<T>(options: { url: string; method?: 'GET' | 'POST' | 'DELETE'; data?: unknown }): Promise<T> {
  const response = await lastValueFrom(
    getBackendSrv().fetch<T>({
      url: options.url,
      method: options.method ?? 'GET',
      data: options.data,
    })
  );
  return response.data;
}

export async function health(): Promise<{ ok: boolean; service: string; database: string }> {
  return request({ url: `${BASE}/health` });
}

export async function listSessions(): Promise<SessionSummary[]> {
  const result = await request<{ sessions: SessionSummary[] }>({ url: `${BASE}/sessions` });
  return result.sessions;
}

export async function createSession(title?: string): Promise<SessionDetail> {
  return request({ url: `${BASE}/sessions`, method: 'POST', data: title ? { title } : {} });
}

export async function getSession(sessionId: string): Promise<SessionDetail> {
  return request({ url: `${BASE}/sessions/${sessionId}` });
}

export async function startChat(sessionId: string, message: string): Promise<StartJobResponse> {
  return request({
    url: `${BASE}/sessions/${sessionId}/chat`,
    method: 'POST',
    data: { message },
  });
}

export async function getJob(jobId: string): Promise<JobResponse> {
  return request({ url: `${BASE}/jobs/${jobId}` });
}

export async function retryJob(jobId: string): Promise<StartJobResponse> {
  return request({ url: `${BASE}/jobs/${jobId}/retry`, method: 'POST', data: {} });
}

export async function importLegacyChat(state: LegacyChatState): Promise<{ session: SessionDetail; already_imported: boolean }> {
  return request({
    url: `${BASE}/sessions/import`,
    method: 'POST',
    data: { source: 'mkhco-ai-dashboard-app.chat.v1', ...state },
  });
}
