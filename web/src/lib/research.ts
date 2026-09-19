import { apiRequest, jsonBody, unwrap, type ApiEnvelope } from './api';
import type { ModelStatus } from './models';

export type Project = {
  id: string; name: string; description?: string | null; status?: string; previewUrl?: string | null;
  selectedModel?: string | null; initialPrompt?: string | null; createdAt?: string; updatedAt?: string;
  capabilityId?: string | null;
};
export type ChatMessage = { id: string; role: string; content: string; messageType?: string; createdAt?: string; requestId?: string; metadata?: Record<string, unknown> };
export type Generation = { status?: string; activeStep?: string; stepSummary?: string; requestId?: string; errorMessage?: string; previewUrl?: string | null; validationStatus?: string; previewStatus?: string; acceptedReceiptId?: string | null };
export type ActiveRequests = { activeCount: number; hasActiveRequests: boolean };

export async function projects(): Promise<Project[]> {
  return unwrap(await apiRequest<ApiEnvelope<Project[]>>('/api/projects')) || [];
}
export async function project(id: string): Promise<Project> {
  return unwrap(await apiRequest<ApiEnvelope<Project>>(`/api/projects/${encodeURIComponent(id)}`));
}
export async function modelStatus(): Promise<ModelStatus> {
  return apiRequest<ModelStatus>('/api/settings/cli-status');
}
export async function messages(id: string): Promise<ChatMessage[]> {
  const payload = await apiRequest<ApiEnvelope<ChatMessage[]>>(`/api/chat/${encodeURIComponent(id)}/messages?limit=200`);
  return unwrap(payload) || [];
}
export async function generation(id: string): Promise<Generation | null> {
  return unwrap(await apiRequest<ApiEnvelope<Generation | null>>(`/api/projects/${encodeURIComponent(id)}/generation/status`));
}
export async function activeRequests(id: string): Promise<ActiveRequests> {
  return apiRequest<ActiveRequests>(`/api/chat/${encodeURIComponent(id)}/requests/active`);
}
export async function createProject(question: string, selectedModel: string, capabilityId = 'stock_diagnosis'): Promise<Project> {
  const id = `project-${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;
  return unwrap(await apiRequest<ApiEnvelope<Project>>('/api/projects', jsonBody({
    projectId: id,
    name: question.slice(0, 50) + (question.length > 50 ? '...' : ''),
    initialPrompt: question,
    selectedModel,
    capabilityId,
    capabilitySelectionSource: 'manual',
  })));
}
export async function uploadImage(id: string, file: File): Promise<{ name: string; path: string }> {
  const form = new FormData();
  form.append('file', file);
  const result = await apiRequest<{ filename: string; path: string }>(`/api/assets/${encodeURIComponent(id)}/upload`, { method: 'POST', body: form });
  return { name: result.filename || file.name, path: result.path };
}
export async function submitResearch(id: string, question: string, selectedModel: string, mode: 'act' | 'chat', isInitialPrompt: boolean, capabilityId?: string, images: { name: string; path: string }[] = []): Promise<{ requestId: string }> {
  return apiRequest<{ requestId: string }>(`/api/chat/${encodeURIComponent(id)}/act`, jsonBody({
    instruction: question, displayInstruction: question, images, isInitialPrompt, mode,
    selectedModel, ...(isInitialPrompt && capabilityId ? { capabilityId, capabilitySelectionSource: 'manual' } : {}),
  }));
}
