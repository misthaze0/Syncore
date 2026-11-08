#!/usr/bin/env node
/**
 * 构建后校验：安装包里不允许出现开发凭据与运行数据。
 *
 * 两道检查：
 * 1. 文件名 —— .env / .env.local / *.log / 非空 data/
 * 2. 值 —— 拿开发态 backend/.env 里真正的密钥值扫打包产物，
 *    防止密钥被复制进别的配置或示例文件。
 *
 * 只打印键名与文件路径，不打印任何值。
 */
const fs = require('node:fs');
const path = require('node:path');

const DESKTOP_DIR = path.join(__dirname, '..');
const TARGET = process.argv[2]
  ? path.resolve(process.argv[2])
  : path.join(DESKTOP_DIR, 'release', 'win-unpacked', 'resources', 'backend');
const DEV_ENV = path.join(DESKTOP_DIR, '..', 'backend', '.env');

const SKIP_DIRS = new Set(['node_modules', '__pycache__', '.git']);
const SKIP_EXT = new Set([
  '.pyc', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.zip', '.whl',
  '.exe', '.dll', '.pyd', '.db', '.woff', '.woff2', '.ttf', '.pdf',
]);
const SECRET_KEY = /(KEY|TOKEN|SECRET|PASSWORD)$/i;
const NON_SECRET_VALUES = new Set(['true', 'false', 'none', 'null', 'changeme', 'sk-placeholder']);
const MAX_SCAN_BYTES = 2 * 1024 * 1024;

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name)) continue;
      walk(path.join(dir, entry.name), out);
    } else if (entry.isFile()) {
      out.push(path.join(dir, entry.name));
    }
  }
  return out;
}

function isForbiddenName(file) {
  const base = path.basename(file).toLowerCase();
  if (base === '.env' || base === '.env.local') return true;
  if (base.startsWith('.env.') && base !== '.env.example') return true;
  return base.endsWith('.log');
}

function loadSecrets(file) {
  const secrets = [];
  if (!fs.existsSync(file)) return secrets;
  for (const raw of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    if (!SECRET_KEY.test(key)) continue;
    if (value.length < 8 || NON_SECRET_VALUES.has(value.toLowerCase())) continue;
    secrets.push({ key, value });
  }
  return secrets;
}

function main() {
  if (!fs.existsSync(TARGET)) {
    console.error(`✗ 找不到打包产物目录：${TARGET}`);
    process.exit(1);
  }

  const issues = [];
  const files = walk(TARGET);

  for (const file of files) {
    const rel = path.relative(TARGET, file);
    if (isForbiddenName(file)) issues.push(`${rel} —— 不允许打包的文件`);
  }

  const dataDir = path.join(TARGET, 'data');
  if (fs.existsSync(dataDir) && walk(dataDir).length > 0) {
    issues.push('data/ —— 运行数据不应进安装包');
  }

  const secrets = loadSecrets(DEV_ENV);
  for (const file of files) {
    if (SKIP_EXT.has(path.extname(file).toLowerCase())) continue;
    let stat;
    try {
      stat = fs.statSync(file);
    } catch {
      continue;
    }
    if (!stat.isFile() || stat.size > MAX_SCAN_BYTES) continue;
    let text;
    try {
      text = fs.readFileSync(file, 'utf8');
    } catch {
      continue;
    }
    for (const secret of secrets) {
      if (text.includes(secret.value)) {
        issues.push(`${path.relative(TARGET, file)} —— 含有 ${secret.key} 的值`);
      }
    }
  }

  if (issues.length > 0) {
    console.error(`✗ 安装包校验失败（${issues.length} 项）`);
    for (const issue of issues) console.error(`  · ${issue}`);
    process.exit(1);
  }

  console.log(`✓ 安装包校验通过：${files.length} 个文件，未发现凭据与运行数据`);
}

main();
