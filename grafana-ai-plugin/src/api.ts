import { getBackendSrv } from '@grafana/runtime';
import { lastValueFrom } from 'rxjs';

const BASE = '/api/plugin-proxy/mkhco-ai-dashboard-app/ai';

export type ChatHistoryItem = {
  role: 'user' | 'assistant';
  content: string;
};

export type StartJobResponse = {
  job_id: string;
  status: string;
  model: string;
};

export type JobResponse = {
  id: string;
  status: 'queued' | 'running' | 'completed' | 'failed';
  model: string;
  progress: string[];
  answer: string;
  error: string;
  dashboard_url: string;
};

async function request<T>(options: { url: string; method?: 'GET' | 'POST'; data?: unknown }): Promise<T> {
  const response = await lastValueFrom(
    getBackendSrv().fetch<T>({
      url: options.url,
      method: options.method ?? 'GET',
      data: options.data,
    })
  );
  return response.data;
}

export async function health(): Promise<{ ok: boolean; service: string }> {
  return request({ url: `${BASE}/health` });
}

export async function startChat(message: string, history: ChatHistoryItem[]): Promise<StartJobResponse> {
  return request({
    url: `${BASE}/chat`,
    method: 'POST',
    data: { message, history },
  });
}

export async function getJob(jobId: string): Promise<JobResponse> {
  return request({ url: `${BASE}/jobs/${jobId}` });
}

export async function waitForJob(
  jobId: string,
  onProgress: (job: JobResponse) => void,
  timeoutMs = 20 * 60 * 1000
): Promise<JobResponse> {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const job = await getJob(jobId);
    onProgress(job);
    if (job.status === 'completed' || job.status === 'failed') {
      return job;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }
  throw new Error('AI request timed out. Check the local AI API log.');
}
