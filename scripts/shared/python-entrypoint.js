/** Compatibility launcher only. All business logic executes in Python. */
const { spawn } = require('node:child_process');
const path = require('node:path');

function runPython(moduleName, argumentsPrefix = []) {
  const child = spawn('uv', [
    'run', '--directory', 'services/platform-api', '--locked',
    'python', '-m', moduleName, ...argumentsPrefix, ...process.argv.slice(2),
  ], { cwd: path.resolve(__dirname, '../..'), stdio: 'inherit', shell: false, env: process.env });
  child.once('error', error => { console.error(error.message); process.exitCode = 1; });
  child.once('exit', code => { process.exitCode = code ?? 1; });
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
  return child;
}

module.exports = { runPython };
