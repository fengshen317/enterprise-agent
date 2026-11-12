import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const CHECK_TIMEOUT_MS = 120000
const DATABASE_TEST_TIMEOUT_MS = 900000
const includeDatabase = process.argv.includes('--database')
const unknown = process.argv.slice(2).filter(arg => arg !== '--database')
if (unknown.length) { console.error('支持参数：--database'); process.exit(2) }
if (includeDatabase) {
  const validation = spawnSync('uv', ['run', '--frozen', 'python', '-c',
    'from app.core.config import Settings; Settings().test_sqlalchemy_url'], {
    cwd: path.join(root, 'backend'), encoding: 'utf8', windowsHide: true,
  })
  if (validation.error || validation.status !== 0) {
    console.error('请在 backend/.env 配置 EA_TEST_DATABASE_NAME，必须以 _test 结尾且不同于开发库')
    process.exit(2)
  }
}

// Windows 的 npm.cmd 不能直接作为可执行文件启动，优先使用同目录 npm CLI。
const npmCli = path.join(path.dirname(process.execPath), 'node_modules', 'npm', 'bin', 'npm-cli.js')
const steps = [
  ['backend', 'uv', ['run', '--frozen', 'ruff', 'check', '.']],
  ['backend', 'uv', ['run', '--frozen', 'ruff', 'format', '--check', '.']],
  ['backend', 'uv', ['run', '--frozen', 'mypy', 'app/']],
  ['backend', 'uv', ['run', '--frozen', 'pytest', ...(includeDatabase ? [] : ['-m', 'not integration', '--no-cov'])]],
  ...['lint', 'typecheck', 'test', 'build'].map(name => ['frontend', 'npm', ['run', name]]),
]

for (const [folder, executable, args] of steps) {
  console.log('\n检查：' + folder + ' / ' + executable + ' ' + args.join(' '))
  const npm = executable === 'npm' && existsSync(npmCli)
  const command = npm ? process.execPath : executable
  const result = spawnSync(command, npm ? [npmCli, ...args] : args, {
    cwd: path.join(root, folder), encoding: 'utf8', windowsHide: true,
    timeout: includeDatabase && args.includes('pytest') ? DATABASE_TEST_TIMEOUT_MS : CHECK_TIMEOUT_MS,
    shell: executable === 'npm' && !npm && process.platform === 'win32',
  })
  if (result.stdout) process.stdout.write(result.stdout)
  if (result.stderr) process.stderr.write(result.stderr)
  if (result.error || result.status !== 0) {
    console.error(result.error?.message ?? '检查失败，退出码：' + result.status)
    process.exit(result.status || 1)
  }
}
console.log('\n全部本地检查通过。' + (includeDatabase ? '包含真实数据库集成测试。' : '仅运行快速检查，未运行数据库集成测试及总体覆盖率门禁；完整验收请加 --database。'))
