export const MODELS = [
  { id: 'local_qwen:qwen3.5-9b-q5km', label: 'Qwen 3.5 9B (Local Q5_K_M)' },
  { id: 'deepseek:deepseek-v4-flash', label: 'DeepSeek V4 Flash (ModelPort)' },
  { id: 'deepseek-v4-flash', label: 'DeepSeek V4 Flash (Official Direct)' },
] as const;

export type ModelId = typeof MODELS[number]['id'];
export type ModelCheck = { id: string; status: string; message: string };
export type ModelStatus = { pi?: { available?: boolean; configured?: boolean; models?: string[]; modelChecks?: ModelCheck[]; error?: string } };

export function modelReadiness(status: ModelStatus | null, id: string): string | null {
  if (!status) return '正在检查模型状态。';
  const entry = status.pi;
  const check = entry?.modelChecks?.find(item => item.id === id);
  if (check) return check.status === 'available' ? null : check.message;
  if (entry?.available && entry.models?.includes(id)) return null;
  return entry?.error || '当前模型尚未通过连接检查。';
}

export function modelLabel(id?: string | null): string {
  return MODELS.find(model => model.id === id)?.label || id || '未选择模型';
}
