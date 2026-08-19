/**
 * Bruno License Server — Cloudflare Workers 版本
 *
 * 基于 Bruno v4.0.0 license.js 逆向源码实现。
 * 完全无状态设计：激活数据编码在 activationId 中，无需服务端存储。
 *
 * 部署：
 *   npx wrangler deploy
 *
 * 配置 (wrangler.jsonc 中的 vars 或 dashboard 环境变量):
 *   BRUNO_LICENSE_PLAN     默认 ULTIMATE_EDITION
 *   BRUNO_LICENSE_TYPE      personal (OTP) 或 organization (直接激活)
 *   TRIAL_DURATION_DAYS     试用天数，默认 14
 */

// ============================================================
//  配置
// ============================================================

const DEFAULT_PLAN = "ULTIMATE_EDITION";
const DEFAULT_LICENSE_TYPE = "personal";
const TRIAL_DURATION_DAYS_DEFAULT = 14;
const UPGRADE_URL_DEFAULT = "https://www.usebruno.com/pricing";

/**
 * 从 Worker env 读取配置
 */
function getConfig(env) {
  return {
    plan: env.BRUNO_LICENSE_PLAN || DEFAULT_PLAN,
    licenseType: env.BRUNO_LICENSE_TYPE || DEFAULT_LICENSE_TYPE,
    trialDays: parseInt(env.TRIAL_DURATION_DAYS || String(TRIAL_DURATION_DAYS_DEFAULT), 10),
    upgradeUrl: env.BRUNO_UPGRADE_URL || UPGRADE_URL_DEFAULT,
  };
}

// ============================================================
//  JWT 工具函数
// ============================================================

function b64urlEncode(data) {
  // 在 Workers 中 btoa 处理 Uint8Array → Base64
  // 然后 URL-safe 替换并去掉 padding
  const raw = btoa(String.fromCharCode(...new Uint8Array(data)));
  return raw.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlEncodeStr(str) {
  return b64urlEncode(new TextEncoder().encode(str));
}

function b64urlDecode(str) {
  const padded = str + "=".repeat((4 - (str.length % 4)) % 4);
  const raw = padded.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(raw);
  return new Uint8Array([...binary].map((c) => c.charCodeAt(0)));
}

/**
 * 创建 JWT-like token
 *
 * Bruno 客户端使用 jwt.decode(token) 解析 payload，不验证签名。
 */
async function makeJWT(payload) {
  const header = { alg: "HS256", typ: "JWT" };
  const headerB64 = b64urlEncodeStr(JSON.stringify(header));
  const payloadStr = JSON.stringify(payload);
  const payloadB64 = b64urlEncodeStr(payloadStr);

  // 用 Web Crypto API 生成 SHA-256 作为伪签名
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(payloadStr));
  const sigB64 = b64urlEncode(digest);

  return `${headerB64}.${payloadB64}.${sigB64}`;
}

/** 解码 JWT payload（不验签） */
function decodeJWTPayload(token) {
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    const bytes = b64urlDecode(parts[1]);
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return null;
  }
}

// ============================================================
//  无状态激活数据编码
//
//  Workers 无状态，每个请求可能落在不同 isolate。
//  将激活数据编码到 activationId 中，OTP 验证时解码恢复。
//  格式: base64url(JSON(activateData)) + "." + randomUUID
// ============================================================

function encodeActivationData(data) {
  const json = JSON.stringify(data);
  const b64 = b64urlEncodeStr(json);
  const rand = crypto.randomUUID();
  return `${b64}.${rand}`;
}

function decodeActivationData(activationId) {
  try {
    const b64 = activationId.split(".")[0];
    const bytes = b64urlDecode(b64);
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return null;
  }
}

// ============================================================
//  时间工具
// ============================================================

function utcNowISO() {
  return new Date().toISOString();
}

function addDaysISO(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString();
}

// ============================================================
//  许可证 Payload 构建
// ============================================================

function buildLicensePayload(pending, licenseType, config) {
  const now = utcNowISO();
  const licType = licenseType || pending.licenseType || config.licenseType;

  const payload = {
    licenseKey: pending.licenseKey,
    email: pending.email,
    deviceId: pending.deviceId,
    deviceName: pending.deviceName,
    licenseServerUrl: pending.licenseServerUrl,
    plan: pending.plan || config.plan,
    type: licType,
    createdAt: pending.activatedAt || now,
    updatedAt: now,
    trialActive: false,
  };

  if (licType && licType.toLowerCase() === "trial") {
    payload.type = "trial";
    payload.trialActive = true;
    payload.endDate = addDaysISO(config.trialDays);
  }

  return payload;
}

// ============================================================
//  路由处理
// ============================================================

/** 主 fetch handler */
export default {
  async fetch(request, env, ctx) {
    return handleRequest(request, env);
  },
};

async function handleRequest(request, env) {
  const config = getConfig(env);
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method;

  try {
    // --- 核心端点 ---
    if (path === "/api/v2/license/activate" && method === "POST") {
      return handleActivate(request, config);
    }

    const otpMatch = path.match(/^\/api\/v1\/license\/activate\/(.+)$/);
    if (otpMatch && method === "POST") {
      return handleOTPVerify(request, otpMatch[1], config);
    }

    if (path === "/api/v2/license/verify" && method === "POST") {
      return handleVerify(request, config);
    }

    if (path === "/api/v2/license/refresh" && method === "POST") {
      return handleRefresh(request, config);
    }

    if (path === "/api/v2/license/upgrade-url" && method === "POST") {
      return handleUpgradeUrl(config);
    }

    // --- 认证端点 ---
    if (path === "/api/v2/auth/v2/discover" && method === "POST") {
      return handleDiscover(request, url);
    }

    if (path === "/api/v1/auth/license-activation/session" && method === "POST") {
      return handleCreateSession();
    }

    if (path === "/api/v1/auth/license-activation/session/get" && method === "POST") {
      return handleGetSession(request);
    }

    // --- 试用端点 ---
    if (path === "/api/v1/trials" && method === "POST") {
      return handleTrialRequest(request);
    }

    if (path === "/api/v1/trials/activate" && method === "POST") {
      return handleTrialActivate(request, config, url);
    }

    // --- SAML SSO 占位 ---
    const samlMatch = path.match(/^\/api\/v2\/auth\/sso\/saml\/acs\/(.+)$/);
    if (samlMatch && (method === "POST" || method === "GET")) {
      return json({ status: "ok", subscriptionId: samlMatch[1], message: "SAML SSO is not supported on self-hosted server." });
    }

    // --- 健康检查 ---
    if ((path === "/health" || path === "/api/health") && method === "GET") {
      return json({
        status: "ok",
        service: "Bruno License Server (Cloudflare Workers)",
        version: "2.1.0",
        timestamp: utcNowISO(),
        defaultPlan: config.plan,
        defaultLicenseType: config.licenseType,
        trialDurationDays: config.trialDays,
      });
    }

    // --- 404 ---
    return json({ error: "Not found" }, 404);
  } catch (err) {
    console.error("Unhandled error:", err);
    return json({ error: "Internal server error" }, 500);
  }
}

// ============================================================
//  端点实现
// ============================================================

/** POST /api/v2/license/activate */
async function handleActivate(request, config) {
  const body = await readJSON(request);
  const pending = {
    licenseKey: body.licenseKey || "",
    deviceId: body.deviceId || crypto.randomUUID(),
    deviceName: body.deviceName || "Unnamed Device",
    email: body.email,
    licenseServerUrl: body.licenseServerUrl,
    activatedAt: utcNowISO(),
    plan: config.plan,
    licenseType: config.licenseType,
  };

  // 路径 B — 组织许可证：直接返回 licenseToken，跳过 OTP
  if (config.licenseType === "organization") {
    const payload = buildLicensePayload(pending, "organization", config);
    const token = await makeJWT(payload);
    return json({ licenseToken: token });
  }

  // 路径 A — 个人许可证：返回 activationId（编码激活数据）
  const activationId = encodeActivationData(pending);
  return json({ activationId });
}

/** POST /api/v1/license/activate/<activationId> */
async function handleOTPVerify(request, activationId, config) {
  const pending = decodeActivationData(activationId);
  if (!pending) {
    return json({ error: "Invalid or expired activationId" }, 404);
  }

  const payload = buildLicensePayload(pending, "personal", config);
  const token = await makeJWT(payload);
  return json({ licenseToken: token });
}

/** POST /api/v2/license/verify */
async function handleVerify(request, config) {
  const body = await readJSON(request);
  const token = body.licenseToken || "";
  const deviceId = body.deviceId || "";

  let plan = config.plan;
  let trial = null;

  // 尝试解码令牌获取 plan
  const decoded = decodeJWTPayload(token);
  if (decoded) {
    plan = decoded.plan || plan;
    if (decoded.type === "trial") {
      trial = { active: decoded.trialActive, endDate: decoded.endDate };
    }
  }

  return json({
    verified: true,
    needsRefresh: false,
    subscription: { plan },
    trial,
    aiPolicy: null,
  });
}

/** POST /api/v2/license/refresh */
async function handleRefresh(request, config) {
  const body = await readJSON(request);
  const oldToken = body.licenseToken || "";
  const deviceId = body.deviceId || "";

  // 尝试从旧令牌解码信息
  const decoded = decodeJWTPayload(oldToken) || {};

  const pending = {
    licenseKey: decoded.licenseKey || `refreshed-${crypto.randomUUID().slice(0, 8)}`,
    deviceId,
    deviceName: decoded.deviceName || "Unknown Device",
    email: decoded.email || "user@bruno.local",
    licenseServerUrl: decoded.licenseServerUrl,
    activatedAt: utcNowISO(),
    plan: decoded.plan || config.plan,
    licenseType: "organization", // Bruno 源码 refresh 后设为 organization
  };

  const payload = buildLicensePayload(pending, "organization", config);
  const newToken = await makeJWT(payload);
  return json({ licenseToken: newToken });
}

/** POST /api/v2/license/upgrade-url */
function handleUpgradeUrl(config) {
  return json({ url: config.upgradeUrl });
}

/** POST /api/v2/auth/v2/discover */
async function handleDiscover(request, url) {
  const body = await readJSON(request);
  const origin = `${url.protocol}//${url.host}`;
  return json({
    licenseServerUrl: origin,
    method: "self-hosted",
    email: body.email || "",
    sessionId: body.sessionId || crypto.randomUUID(),
  });
}

/** POST /api/v1/auth/license-activation/session */
function handleCreateSession() {
  return json({
    sessionId: crypto.randomUUID(),
    createdAt: utcNowISO(),
  });
}

/** POST /api/v1/auth/license-activation/session/get */
async function handleGetSession(request) {
  const body = await readJSON(request);
  const sessionId = body.sessionId;
  if (!sessionId) {
    return json({ error: "Session not found" }, 404);
  }
  // 无状态：返回有效的会话信息
  return json({
    sessionId,
    createdAt: utcNowISO(),
    status: "active",
  });
}

/** POST /api/v1/trials */
async function handleTrialRequest(request) {
  const body = await readJSON(request);
  const code = crypto.randomUUID().slice(0, 8).toUpperCase();
  return json({
    email: body.email || "",
    name: body.name || "",
    trialCode: code,
    message: `Trial license requested for ${body.email || ""}. Use code: ${code}`,
  });
}

/** POST /api/v1/trials/activate */
async function handleTrialActivate(request, config, url) {
  const body = await readJSON(request);
  const origin = `${url.protocol}//${url.host}`;
  const now = utcNowISO();

  const trialPayload = {
    licenseKey: body.verifier || "",
    email: body.email || "",
    deviceId: body.deviceId || crypto.randomUUID(),
    deviceName: "Trial Device",
    licenseServerUrl: origin,
    plan: config.plan,
    type: "trial",
    trialActive: true,
    endDate: addDaysISO(config.trialDays),
    createdAt: now,
    updatedAt: now,
  };

  const token = await makeJWT(trialPayload);
  return json({ licenseToken: token });
}

// ============================================================
//  辅助函数
// ============================================================

/** 安全读取 JSON body */
async function readJSON(request) {
  try {
    const text = await request.text();
    return text ? JSON.parse(text) : {};
  } catch {
    return {};
  }
}

/** 返回 JSON Response */
function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    },
  });
}
