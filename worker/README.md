# Cloudflare Workers 版 — Bruno License Server

基于 Bruno v4.0.0 license.js 逆向源码实现的 Cloudflare Workers 版本。

**完全无状态设计**：激活数据编码在 activationId 中，无需服务端存储，充分利用 Workers 的边缘计算特性。

## 快速部署

```bash
cd worker
npm install
npx wrangler deploy
```

部署后获得 `https://bruno-license-server.<你的子域>.workers.dev`。

## 配置

在 `wrangler.jsonc` 的 `vars` 中配置（也可在 Cloudflare Dashboard 中设置）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BRUNO_LICENSE_PLAN` | `ULTIMATE_EDITION` | 许可证等级 |
| `BRUNO_LICENSE_TYPE` | `personal` | `personal`（OTP 验证）或 `organization`（直接激活） |
| `TRIAL_DURATION_DAYS` | `14` | 试用许可证有效期（天） |
| `BRUNO_UPGRADE_URL` | `https://www.usebruno.com/pricing` | 升级链接 |

## 本地开发

```bash
npx wrangler dev
```

## 运行测试

```bash
npm test
```

测试覆盖 13 个场景，包括个人/组织许可证双路径激活、OTP 验证、令牌刷新、试用许可证等。

## 无状态设计说明

Cloudflare Workers 是无状态的（每个请求可能落在不同 isolate），因此本版本不使用内存存储。关键设计：

- **activationId 编码**：将激活请求数据（deviceId、email、licenseKey 等）编码为 base64url JSON 嵌入 activationId 中。OTP 验证时解码恢复，无需服务端存储。
- **JWT 自包含**：licenseToken 本身就是 JWT，包含所有许可证信息，客户端本地验证。
- **verify 端点**：从 JWT 中解码 plan 等信息，无需服务端状态。

## 与 Python 版本的区别

| 特性 | Python 版本 | Workers 版本 |
|------|------------|-------------|
| 运行环境 | Flask + Gunicorn | Cloudflare Workers |
| 状态管理 | 内存存储 (dict) | 完全无状态 (activationId 编码) |
| 线程安全 | threading.Lock | 不需要（无共享状态） |
| 部署 | Docker / Render | `wrangler deploy` |
| 冷启动 | 秒级 | 毫秒级 |
| 优势 | 支持有状态场景 | 全球边缘节点，零延迟 |

## API 端点

与 Python 版本完全一致，参见项目根目录 [README.md](../README.md)。
