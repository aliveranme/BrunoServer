/**
 * BrunoServer Workers 端点测试
 *
 * 使用 Wrangler 的 unstable_dev API 在本地启动 Worker 进行测试，
 * 无需部署到 Cloudflare 即可验证所有端点。
 */
import { unstable_dev } from "wrangler";
import { describe, it, before, after } from "node:test";
import assert from "node:assert";

const BASE_CONFIG = {
  BRUNO_LICENSE_PLAN: "ULTIMATE_EDITION",
  BRUNO_LICENSE_TYPE: "personal",
  TRIAL_DURATION_DAYS: "14",
  BRUNO_UPGRADE_URL: "https://www.usebruno.com/pricing",
};

const TEST = {
  licenseKey: "BRUNO-TEST-1234-5678",
  email: "test@bruno.local",
  deviceId: "test-device-id-abc123",
  deviceName: "TestMachine",
  otp: "any-otp-value",
};

let worker;

// ============================================================
//  辅助函数
// ============================================================

function decodeJWTPayload(token) {
  const parts = token.split(".");
  const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
  const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
  const json = Buffer.from(padded, "base64").toString("utf-8");
  return JSON.parse(json);
}

async function post(worker, path, body) {
  const resp = await worker.fetch(`http://localhost${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return { status: resp.status, data: await resp.json() };
}

async function get(worker, path) {
  const resp = await worker.fetch(`http://localhost${path}`);
  return { status: resp.status, data: await resp.json() };
}

// ============================================================
//  测试
// ============================================================

describe("BrunoServer Workers", () => {
  before(async () => {
    worker = await unstable_dev("src/index.js", {
      experimental: { disableExperimentalWarning: true },
      vars: BASE_CONFIG,
    });
  });

  after(async () => {
    await worker.stop();
  });

  it("健康检查", async () => {
    const { status, data } = await get(worker, "/health");
    assert.strictEqual(status, 200);
    assert.strictEqual(data.status, "ok");
    assert.strictEqual(data.defaultPlan, "ULTIMATE_EDITION");
    console.log("  ✅ 健康检查通过");
  });

  it("个人许可证激活 (返回 activationId)", async () => {
    const { status, data } = await post(worker, "/api/v2/license/activate", {
      licenseKey: TEST.licenseKey,
      email: TEST.email,
      deviceId: TEST.deviceId,
      deviceName: TEST.deviceName,
      licenseServerUrl: "http://localhost",
    });
    assert.strictEqual(status, 200);
    assert.ok(data.activationId);
    assert.ok(!data.licenseToken, "个人许可证响应不应包含 licenseToken");
    TEST.activationId = data.activationId;
    console.log("  ✅ 个人许可证激活通过");
  });

  it("OTP 验证 (返回 licenseToken JWT)", async () => {
    const { status, data } = await post(
      worker,
      `/api/v1/license/activate/${TEST.activationId}`,
      { otp: TEST.otp }
    );
    assert.strictEqual(status, 200);
    assert.ok(data.licenseToken);

    const payload = decodeJWTPayload(data.licenseToken);
    assert.strictEqual(payload.deviceId, TEST.deviceId);
    assert.strictEqual(payload.licenseKey, TEST.licenseKey);
    assert.strictEqual(payload.email, TEST.email);
    assert.strictEqual(payload.plan, "ULTIMATE_EDITION");
    assert.strictEqual(payload.type, "personal");
    assert.strictEqual(payload.trialActive, false);
    assert.ok(payload.licenseServerUrl);
    assert.ok(payload.createdAt);
    assert.ok(payload.updatedAt);
    TEST.token = data.licenseToken;
    console.log("  ✅ OTP 验证通过 (9 项 JWT 字段检查)");
  });

  it("许可证验证 (已知令牌)", async () => {
    const { status, data } = await post(worker, "/api/v2/license/verify", {
      licenseToken: TEST.token,
      deviceId: TEST.deviceId,
    });
    assert.strictEqual(status, 200);
    assert.strictEqual(data.verified, true);
    assert.strictEqual(data.subscription.plan, "ULTIMATE_EDITION");
    assert.strictEqual(data.needsRefresh, false);
    console.log("  ✅ 许可证验证通过");
  });

  it("未知令牌验证 (从 JWT 解码 plan)", async () => {
    const fakeToken =
      "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." +
      btoa(JSON.stringify({ plan: "GOLDEN_EDITION", deviceId: "x" })) +
      ".fake";
    const { status, data } = await post(worker, "/api/v2/license/verify", {
      licenseToken: fakeToken,
      deviceId: "x",
    });
    assert.strictEqual(status, 200);
    assert.strictEqual(data.verified, true);
    assert.strictEqual(data.subscription.plan, "GOLDEN_EDITION");
    console.log("  ✅ 未知令牌验证通过 (plan=GOLDEN_EDITION)");
  });

  it("令牌刷新 (type 变为 organization)", async () => {
    const { status, data } = await post(worker, "/api/v2/license/refresh", {
      licenseToken: TEST.token,
      deviceId: TEST.deviceId,
    });
    assert.strictEqual(status, 200);
    assert.ok(data.licenseToken);
    const payload = decodeJWTPayload(data.licenseToken);
    assert.strictEqual(payload.type, "organization");
    TEST.refreshedToken = data.licenseToken;
    console.log("  ✅ 令牌刷新通过 (type=organization)");
  });

  it("升级 URL", async () => {
    const { status, data } = await post(worker, "/api/v2/license/upgrade-url", {
      deviceId: TEST.deviceId,
    });
    assert.strictEqual(status, 200);
    assert.strictEqual(typeof data.url, "string");
    console.log("  ✅ 升级 URL 通过");
  });

  it("发现服务器", async () => {
    const { status, data } = await post(worker, "/api/v2/auth/v2/discover", {
      email: TEST.email,
    });
    assert.strictEqual(status, 200);
    assert.ok(data.licenseServerUrl);
    console.log("  ✅ 发现服务器通过");
  });

  it("激活会话 (创建 + 获取)", async () => {
    const r1 = await post(worker, "/api/v1/auth/license-activation/session", {});
    assert.strictEqual(r1.status, 200);
    assert.ok(r1.data.sessionId);

    const r2 = await post(
      worker,
      "/api/v1/auth/license-activation/session/get",
      { sessionId: r1.data.sessionId }
    );
    assert.strictEqual(r2.status, 200);
    assert.strictEqual(r2.data.sessionId, r1.data.sessionId);
    console.log("  ✅ 激活会话通过");
  });

  it("试用许可证 (请求 + 激活)", async () => {
    const r1 = await post(worker, "/api/v1/trials", {
      email: TEST.email,
      name: "TestUser",
    });
    assert.strictEqual(r1.status, 200);

    const r2 = await post(worker, "/api/v1/trials/activate", {
      email: TEST.email,
      verifier: "trial-code-1234",
      deviceId: TEST.deviceId,
      posthogDistinctId: "test-id",
    });
    assert.strictEqual(r2.status, 200);
    assert.ok(r2.data.licenseToken);

    const payload = decodeJWTPayload(r2.data.licenseToken);
    assert.strictEqual(payload.type, "trial");
    assert.strictEqual(payload.trialActive, true);
    assert.ok(payload.endDate);
    assert.strictEqual(payload.deviceId, TEST.deviceId);
    console.log("  ✅ 试用许可证通过 (4 项字段检查)");
  });

  it("无效 activationId 返回 404", async () => {
    const { status } = await post(
      worker,
      "/api/v1/license/activate/nonexistent",
      { otp: "000" }
    );
    assert.strictEqual(status, 404);
    console.log("  ✅ 无效 activationId 通过");
  });

  it("404 错误处理", async () => {
    const { status, data } = await get(worker, "/nonexistent");
    assert.strictEqual(status, 404);
    assert.ok(data.error);
    console.log("  ✅ 404 错误处理通过");
  });

  it("组织许可证直接激活 (BRUNO_LICENSE_TYPE=organization)", async () => {
    // 停止当前 worker，用 organization 配置重新启动
    await worker.stop();
    worker = await unstable_dev("src/index.js", {
      experimental: { disableExperimentalWarning: true },
      vars: { ...BASE_CONFIG, BRUNO_LICENSE_TYPE: "organization" },
    });

    const { status, data } = await post(worker, "/api/v2/license/activate", {
      licenseKey: TEST.licenseKey,
      email: TEST.email,
      deviceId: TEST.deviceId,
      deviceName: TEST.deviceName,
      licenseServerUrl: "http://localhost",
    });
    assert.strictEqual(status, 200);
    assert.ok(data.licenseToken, "组织许可证应直接返回 licenseToken");
    assert.ok(!data.activationId, "组织许可证不应返回 activationId");

    const payload = decodeJWTPayload(data.licenseToken);
    assert.strictEqual(payload.type, "organization");
    assert.strictEqual(payload.deviceId, TEST.deviceId);
    assert.strictEqual(payload.plan, "ULTIMATE_EDITION");
    console.log("  ✅ 组织许可证直接激活通过");
  });
});
