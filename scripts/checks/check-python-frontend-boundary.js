#!/usr/bin/env node
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const root = path.resolve(__dirname, '../..');
const options = ts.readConfigFile(path.join(root, 'tsconfig.json'), ts.sys.readFile).config.compilerOptions;
const config = ts.convertCompilerOptionsFromJson(options, root).options;
const keep = new Set();
const external = new Set();
const violations = [];

function walk(folder) {
  if (!fs.existsSync(folder)) return [];
  return fs.readdirSync(folder, { withFileTypes: true }).flatMap(entry => entry.isDirectory()
    ? walk(path.join(folder, entry.name)) : /\.(?:ts|tsx)$/.test(entry.name) && !/\.test\./.test(entry.name) && !entry.name.endsWith('.d.ts') ? [path.join(folder, entry.name)] : []);
}

function visit(filename) {
  filename = path.resolve(filename);
  if (keep.has(filename)) return;
  keep.add(filename);
  if (filename.endsWith('.json')) return;
  if (filename.endsWith('.d.ts')) {
    violations.push({ file: path.relative(root, filename), reason: 'Runtime import resolves to a declaration-only backend contract' });
    return;
  }
  const source = fs.readFileSync(filename, 'utf8');
  const emitted = ts.transpileModule(source, { compilerOptions: { ...config, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.Preserve } }).outputText;
  const parsed = ts.createSourceFile(filename + '.js', emitted, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  function dependency(name) {
    if (name.startsWith('node:') || ['fs', 'path', 'child_process', 'pg', '@prisma/client', 'prisma', 'ws', 'better-auth/adapters/prisma'].includes(name)) {
      violations.push({ file: path.relative(root, filename), dependency: name });
    }
    const resolved = ts.resolveModuleName(name, filename, config, ts.sys).resolvedModule;
    if (resolved && !resolved.isExternalLibraryImport && !resolved.resolvedFileName.includes('node_modules')) visit(resolved.resolvedFileName);
    else external.add(name);
  }
  function scan(node) {
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) dependency(node.moduleSpecifier.text);
    if (ts.isCallExpression(node) && node.arguments.length && ts.isStringLiteral(node.arguments[0]) && (node.expression.kind === ts.SyntaxKind.ImportKeyword || node.expression.getText(parsed) === 'require')) dependency(node.arguments[0].text);
    ts.forEachChild(node, scan);
  }
  scan(parsed);
}

for (const filename of [...walk(path.join(root, 'src/app')), ...walk(path.join(root, 'src/pages')), path.join(root, 'src/proxy.ts')]) visit(filename);
const report = { frontendFiles: [...keep].map(file => path.relative(root, file).replaceAll('\\', '/')).sort(), external: [...external].sort(), violations };
if (process.argv.includes('--json')) console.log(JSON.stringify(report, null, 2));
else console.log(JSON.stringify({ frontendFiles: keep.size, externalPackages: external.size, violations }));
if (violations.length) process.exitCode = 1;
