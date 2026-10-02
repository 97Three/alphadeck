import fs from 'node:fs';
import { checkModelAvailability } from '../../src/lib/platform/model-availability';
import { PI_AGENT_DEFAULT_MODEL, getPiAgentModelDisplayName } from '../../src/lib/constants/models';

async function main() {
  for (const file of ['src/lib/agent/pi/run-engine.ts', 'src/lib/agent/providers/deepseek.ts',
    'src/lib/agent/providers/openai-compatible.ts', 'src/lib/agent/tools/index.ts', 'src/lib/services/cli/pi-agent.ts']) {
    if (!fs.existsSync(file)) throw new Error('PI Agent runtime files are incomplete.');
  }
  const result = await checkModelAvailability();
  for (const model of result.modelChecks ?? []) {
    console.log(`[${model.status}] ${getPiAgentModelDisplayName(model.id)}: ${model.message}`);
  }
  if (!result.models?.includes(PI_AGENT_DEFAULT_MODEL) && result.available) {
    console.log('默认 Qwen 未就绪。请在首页或项目中显式选择上方通过检查的模型。');
  }
  console.log('本检查只读取模型目录，不生成内容、不验证推理质量。');
  if (!result.available) process.exitCode = 1;
}

main().catch(() => {
  console.error('模型访问检查失败，请检查运行配置。');
  process.exitCode = 1;
});
