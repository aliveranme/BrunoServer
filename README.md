# Bruno 自托管许可证服务器

一个自托管的许可证激活和验证服务器，适用于 [Bruno](https://github.com/usebruno/bruno/) API 客户端。

> **⚠️ 免责声明**：本项目仅供**开发和教育目的**。如果你喜欢 Bruno 并在专业场景中使用，请[购买正版许可证](https://www.usebruno.com/pricing)以支持开发者。

## 概述

基于从 Bruno v4.0.0 客户端 `app.asar` 逆向提取的 `src/utils/license.js` 源码实现，完整覆盖 Bruno 的许可证验证流程。

### 支持的 API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/v2/license/activate` | POST | 激活许可证（返回 activationId，需后续 OTP 验证） |
| `/api/v1/license/activate/<activationId>` | POST | OTP 验证，返回 licenseToken (JWT) |
| `/api/v2/license/verify` | POST | 后台验证许可证令牌 |
| `/api/v2/license/refresh` | POST | 刷新许可证令牌 |
| `/api/v2/license/upgrade-url` | POST | 获取升级链接 |
| `/api/v2/auth/v2/discover` | POST | 通过邮箱发现许可证服务器 |
| `/api/v1/auth/license-activation/session` | POST | 创建激活会话 |
| `/api/v1/auth/license-activation/session/get` | POST | 获取激活会话详情 |
| `/api/v1/trials` | POST | 请求试用许可证 |
| `/api/v1/trials/activate` | POST | 激活试用许可证 |
| `/health` | GET | 健康检查 |

### 许可证验证流程

```
┌──────────┐                  ┌──────────────┐                  ┌──────────┐
│  Bruno   │                  │ BrunoServer  │                  │  用户    │
│  客户端  │                  │   (本服务)   │                  │          │
└────┬─────┘                  └──────┬───────┘                  └────┬─────┘
     │                               │                               │
     │  1. POST /activate            │                               │
     │  {licenseKey, email, deviceId}│                               │
     │──────────────────────────────>│                               │
     │                               │                               │
     │  2. 返回 activationId         │                               │
     │<──────────────────────────────│                               │
     │                               │                               │
     │                               │  3. 用户输入 OTP              │
     │                               │<──────────────────────────────│
     │                               │                               │
     │  4. POST /activate/<id>       │                               │
     │  {otp: "用户输入的值"}        │                               │
     │──────────────────────────────>│                               │
     │                               │                               │
     │  5. 返回 licenseToken (JWT)   │                               │
     │<──────────────────────────────│                               │
     │                               │                               │
     │  6. jwt.decode(token)         │                               │
     │  验证 deviceId 匹配          │                               │
     │  存入 license.json            │                               │
     │                               │                               │
     │  7. POST /verify (后台定期)   │                               │
     │──────────────────────────────>│                               │
     │  8. {verified: true}          │                               │
     │<──────────────────────────────│                               │
     │                               │                               │
     │  激活完成 ✅                  │                               │
     ─────────────────────────────────────────────────────────────────
```

## 安装

### 方式一：本地运行

```bash
git clone https://github.com/aliveranme/BrunoServer.git && cd BrunoServer

# 创建虚拟环境并安装依赖
uv venv .venv
uv pip install -r requirements.txt --python .venv/Scripts/python.exe

# 运行服务器
python server.py
```

### 方式二：Docker 运行

```bash
# 构建镜像
docker build -t bruno-server .

# 运行容器
docker run -d --name bruno-server -p 5000:5000 bruno-server
```

### 方式三：自定义配置

```bash
# 通过环境变量配置
export FLASK_HOST=0.0.0.0
export FLASK_PORT=8080
export BRUNO_LICENSE_PLAN=ULTIMATE_EDITION  # PRO_EDITION / GOLDEN_EDITION / ULTIMATE_EDITION
export BRUNO_LICENSE_TYPE=personal          # personal / organization
python server.py
```

## 配置 Bruno 客户端

### 方式一：使用自托管服务器

1. 启动本服务器（默认监听 `0.0.0.0:5000`）
2. 在 Bruno 中打开 **License** 设置页面
3. 选择 **License Server** 选项
4. 输入服务器地址：`http://localhost:5000`
5. 输入任意许可证密钥和邮箱
6. 当提示输入 OTP 时，输入任意值即可
7. Bruno Ultimate 将被激活

### 方式二：修改 Bruno 客户端连接地址

Bruno 客户端默认连接 `https://license-api.usebruno.com`。通过以下方式指向本地服务器：

```bash
# 设置环境变量
export BRUNO_LICENSE_ENDPOINT=http://localhost:5000
# 然后启动 Bruno
```

## 运行测试

```bash
# 启动服务器
python server.py &

# 运行测试
uv pip install requests --python .venv/Scripts/python.exe
python test_server.py
```

测试覆盖所有 API 端点，包括激活、OTP 验证、令牌验证、刷新、试用许可证等。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FLASK_HOST` | `0.0.0.0` | 服务器监听地址 |
| `FLASK_PORT` | `5000` | 服务器监听端口 |
| `FLASK_DEBUG` | `false` | 调试模式 |
| `BRUNO_LICENSE_PLAN` | `ULTIMATE_EDITION` | 许可证等级 |
| `BRUNO_LICENSE_TYPE` | `personal` | 许可证类型 |
| `BRUNO_UPGRADE_URL` | `https://www.usebruno.com/pricing` | 升级链接 URL |

## 技术细节

### JWT Token 结构

Bruno 客户端使用 `jwt.decode(token)` 解析令牌 payload，**不验证签名**。
`verifyLicense()` 函数仅检查 `decoded.deviceId === machineIdSync()`，匹配即认为有效。

```json
{
  "licenseKey": "任意值",
  "email": "user@example.com",
  "deviceId": "机器码 (machineIdSync)",
  "deviceName": "主机名",
  "licenseServerUrl": "http://localhost:5000",
  "plan": "ULTIMATE_EDITION",
  "type": "personal",
  "createdAt": "2024-01-01T00:00:00Z",
  "updatedAt": "2024-01-01T00:00:00Z",
  "trialActive": false
}
```

### 60 天重新激活机制

Bruno 客户端 `verifyLicense()` 函数检查 `updatedAt` 字段，如果距今超过 60 天，
会清除本地许可证并要求用户重新激活。本服务器在 `/verify` 端点始终返回 `verified: true`，
客户端会自动更新 `updatedAt` 为当前时间，因此无需每 60 天手动重新激活。

### 许可证等级

| 等级 | 功能 |
|------|------|
| `PRO_EDITION` | Pro 功能 |
| `GOLDEN_EDITION` | Golden 功能（默认） |
| `ULTIMATE_EDITION` | 全部功能 |

## 部署

### Render

本项目包含 `render.yaml`，可直接部署到 Render：

```bash
# 使用 render CLI
render deploy
```

### Docker Compose

```yaml
version: "3"
services:
  bruno-server:
    build: .
    ports:
      - "5000:5000"
    environment:
      - BRUNO_LICENSE_PLAN=ULTIMATE_EDITION
    restart: unless-stopped
```

## 法律声明

本软件仅供教育和开发目的使用。作者不认可盗版或许可证违规行为。

**请支持 Bruno 项目**，如果你在专业/商业工作中使用 Bruno，请购买正版许可证：
- 需要官方支持
- 想支持持续开发

访问 [Bruno 官方网站](https://www.usebruno.com/) 了解更多信息。

## 免责声明

本项目与 Bruno 项目或其开发者无关，也未获得其认可或关联。所有商标和版权属于其各自所有者。
