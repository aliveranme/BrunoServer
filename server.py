"""
Bruno 自托管许可证验证服务器

基于从 Bruno v4.0.0 app.asar 逆向提取的 src/utils/license.js 源码实现。
支持完整的许可证激活、OTP 验证、许可证验证、刷新、试用许可证等全部 API 端点。

Bruno 客户端的许可证验证流程：
1. 用户输入许可证密钥 → POST /api/v2/license/activate → 返回 activationId（需要 OTP 验证）
   或直接返回 licenseToken（组织许可证，无需 OTP）
2. 用户输入 OTP → POST /api/v1/license/activate/<activationId> → 返回 licenseToken (JWT)
3. 客户端定期后台验证 → POST /api/v2/license/verify → 返回 { verified, subscription, needsRefresh }
4. 如需刷新令牌 → POST /api/v2/license/refresh → 返回新的 licenseToken

licenseToken 是一个 JWT，Bruno 客户端仅做 jwt.decode()（不验签名），
检查 payload 中的 deviceId 是否匹配本机 machineIdSync()。
"""
from datetime import datetime, timedelta, timezone
import os
import uuid
import json
import base64
import hashlib
import logging

from flask import Flask, jsonify, request


app = Flask(__name__)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("BrunoServer")

# --- 内存存储 ---
PENDING_ACTIVATIONS = {}       # activationId → 激活请求详情
ACTIVE_LICENSES = {}           # licenseToken → 许可证详情（用于 verify 端点）
ACTIVATION_SESSIONS = {}       # sessionId → 激活会话

# --- 环境变量配置 ---
DEFAULT_PLAN = os.getenv("BRUNO_LICENSE_PLAN", "ULTIMATE_EDITION")
# PRO_EDITION < GOLDEN_EDITION < ULTIMATE_EDITION
DEFAULT_LICENSE_TYPE = os.getenv("BRUNO_LICENSE_TYPE", "personal")
# "personal" 或 "organization"


def _utcnow_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch_now() -> int:
    """返回当前 Unix 时间戳（秒）。"""
    return int(datetime.now(timezone.utc).timestamp())


def _b64url_encode(data: bytes) -> str:
    """Base64 URL-safe 编码，去掉末尾的 '='。"""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _make_jwt(payload: dict) -> str:
    """
    创建一个 JWT-like token。

    Bruno 客户端使用 jwt.decode(token) 来解析 payload，
    **不会验证签名**（verifyLicense 函数中只做 jwt.decode，不做 jwt.verify）。
    所以签名可以是任意值。
    """
    header = {"alg": "HS256", "typ": "JWT"}
    header_json = json.dumps(header, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")
    header_b64 = _b64url_encode(header_json)

    payload_json = json.dumps(payload, separators=(",", ":"),
                              ensure_ascii=False).encode("utf-8")
    payload_b64 = _b64url_encode(payload_json)

    # 签名部分：Bruno 不验签，用随机字符串即可
    signature_b64 = _b64url_encode(hashlib.sha256(payload_json).digest())

    return f"{header_b64}.{payload_b64}.{signature_b64}"


def _build_license_payload(pending: dict, license_type: str = None) -> dict:
    """
    构建 JWT payload（licenseToken 的内容）。

    Bruno 客户端 verifyLicense() 检查以下字段：
    - deviceId: 必须与 machineIdSync() 匹配
    - licenseKey, email: 用于显示
    - plan: 决定功能等级（PRO_EDITION / GOLDEN_EDITION / ULTIMATE_EDITION）
    - type: "trial" 时检查 trialActive 和 endDate
    - createdAt, updatedAt: 用于显示和 60 天过期检查
    - licenseServerUrl: 用于后台验证时的请求目标
    """
    now_iso = _utcnow_iso()
    lic_type = license_type or pending.get("licenseType", DEFAULT_LICENSE_TYPE)

    payload = {
        "licenseKey": pending.get("licenseKey"),
        "email": pending.get("email"),
        "deviceId": pending.get("deviceId"),
        "deviceName": pending.get("deviceName"),
        "licenseServerUrl": pending.get("licenseServerUrl"),
        "plan": pending.get("plan", DEFAULT_PLAN),
        "type": lic_type,
        "createdAt": pending.get("activatedAt", now_iso),
        "updatedAt": now_iso,
        "trialActive": False,
    }

    # 如果是试用许可证，添加试用相关字段
    if lic_type and lic_type.lower() == "trial":
        payload["type"] = "trial"
        payload["trialActive"] = True
        payload["endDate"] = (
            datetime.now(timezone.utc) + timedelta(days=14)
        ).isoformat().replace("+00:00", "Z")

    return payload


def _build_license_response(decoded: dict) -> dict:
    """构建激活/验证成功后返回给客户端的许可证详情。"""
    return {
        "licenseType": decoded.get("type", DEFAULT_LICENSE_TYPE),
        "licenseKey": decoded.get("licenseKey"),
        "email": decoded.get("email"),
        "deviceId": decoded.get("deviceId"),
        "createdAt": decoded.get("createdAt"),
        "updatedAt": decoded.get("updatedAt"),
        "plan": decoded.get("plan", DEFAULT_PLAN),
    }


# ============================================================
#  核心端点：许可证激活
# ============================================================

@app.route("/api/v2/license/activate", methods=["POST"])
def activate_license():
    """
    处理许可证激活请求。

    Bruno 客户端调用流程（activateLicense 函数）：
    1. 发送 { deviceId, deviceName, licenseKey, email, licenseServerUrl }
    2. 如果响应包含 activationId → 需要后续 OTP 验证
    3. 如果响应直接包含 licenseToken → 立即激活成功（组织许可证模式）
    4. 客户端对 licenseToken 做 jwt.decode()，检查 deviceId 是否存在且匹配
    """
    payload = request.get_json(silent=True) or {}
    log.info("收到激活请求: %s", {k: v for k, v in payload.items() if k != "licenseKey"})
    log.debug("完整激活请求: %s", payload)

    license_key = payload.get("licenseKey", "")
    device_id = payload.get("deviceId", str(uuid.uuid4()))
    device_name = payload.get("deviceName", "Unnamed Device")
    email = payload.get("email")
    license_server_url = payload.get("licenseServerUrl")

    # 生成激活 ID，进入待 OTP 验证状态
    activation_id = str(uuid.uuid4())
    activated_at = _utcnow_iso()

    pending = {
        "licenseKey": license_key,
        "deviceId": device_id,
        "deviceName": device_name,
        "email": email,
        "licenseServerUrl": license_server_url,
        "activatedAt": activated_at,
        "plan": DEFAULT_PLAN,
        "licenseType": DEFAULT_LICENSE_TYPE,
    }
    PENDING_ACTIVATIONS[activation_id] = pending

    # 返回 activationId，Bruno 客户端会进入 OTP 验证流程
    resp = {
        "activationId": activation_id,
    }

    log.info("激活请求已受理，activationId=%s，等待 OTP 验证", activation_id)
    return jsonify(resp), 200


# ============================================================
#  核心端点：OTP 验证
# ============================================================

@app.route("/api/v1/license/activate/<activation_id>", methods=["POST"])
def verify_activation_otp(activation_id: str):
    """
    验证 OTP 并返回 licenseToken。

    Bruno 客户端调用流程（activateLicenseUsingOtp 函数）：
    1. 发送 { otp: "<用户输入的OTP>" } 到 /api/v1/license/activate/<activationId>
    2. 如果响应包含 licenseToken → 验证成功
    3. 如果响应包含 error → 验证失败，显示错误信息
    4. 客户端对 licenseToken 做 jwt.decode()，检查 deviceId 是否存在且匹配
    5. 将 licenseToken 存入 licenseStore（license.json）
    """
    payload = request.get_json(silent=True) or {}
    otp = payload.get("otp", "")
    log.info("收到 OTP 验证: activationId=%s, otp=%s", activation_id, otp)

    pending = PENDING_ACTIVATIONS.get(activation_id)
    if not pending:
        log.warning("无效的 activationId: %s", activation_id)
        return jsonify({"error": "Invalid activationId"}), 404

    # 任意 OTP 都接受（自托管服务器不做实际验证）
    license_payload = _build_license_payload(pending)
    token = _make_jwt(license_payload)

    # 存储已激活的许可证，用于后续 verify 端点
    ACTIVE_LICENSES[token] = {
        **pending,
        "licensePayload": license_payload,
        "activatedAt": _utcnow_iso(),
    }

    # 从待验证列表中移除
    PENDING_ACTIVATIONS.pop(activation_id, None)

    log.info("OTP 验证成功，licenseToken 已签发 (deviceId=%s)", pending.get("deviceId"))
    return jsonify({"licenseToken": token}), 200


# ============================================================
#  核心端点：许可证验证（后台验证）
# ============================================================

@app.route("/api/v2/license/verify", methods=["POST"])
def verify_license():
    """
    后台验证许可证令牌。

    Bruno 客户端调用流程（verifyLicenseInBackground 函数）：
    1. 发送 { licenseToken, deviceId }
    2. 如果响应 verified=true 且 needsRefresh=true → 客户端调用 refresh 端点
    3. 如果响应 verified=true → 更新 licenseStore（包括 plan, trial, aiPolicy）
    4. 如果响应 verified=false → 清除本地许可证
    5. 此端点在后台异步调用，不影响 UI
    """
    payload = request.get_json(silent=True) or {}
    license_token = payload.get("licenseToken", "")
    device_id = payload.get("deviceId", "")
    log.info("收到验证请求: deviceId=%s", device_id)

    # 查找已激活的许可证
    license_info = ACTIVE_LICENSES.get(license_token)

    if license_info:
        # 已知令牌，直接返回验证成功
        response = {
            "verified": True,
            "needsRefresh": False,
            "subscription": {
                "plan": license_info.get("plan", DEFAULT_PLAN),
            },
            "trial": None,
            "aiPolicy": None,
        }
        log.info("许可证验证成功 (deviceId=%s)", device_id)
    else:
        # 未知令牌也返回验证成功（自托管服务器接受所有令牌）
        response = {
            "verified": True,
            "needsRefresh": False,
            "subscription": {
                "plan": DEFAULT_PLAN,
            },
            "trial": None,
            "aiPolicy": None,
        }
        log.info("未知令牌，但仍返回验证成功 (deviceId=%s)", device_id)

    return jsonify(response), 200


# ============================================================
#  端点：刷新许可证令牌
# ============================================================

@app.route("/api/v2/license/refresh", methods=["POST"])
def refresh_license():
    """
    刷新许可证令牌。

    Bruno 客户端调用流程（refreshLicenseToken 函数）：
    1. 发送 { licenseToken, deviceId }
    2. 如果响应包含 activationId → 需要重新 OTP 验证
    3. 如果响应包含 error → 刷新失败
    4. 如果响应包含 licenseToken → 刷新成功，更新本地存储
    """
    payload = request.get_json(silent=True) or {}
    license_token = payload.get("licenseToken", "")
    device_id = payload.get("deviceId", "")
    log.info("收到刷新请求: deviceId=%s", device_id)

    # 查找原许可证信息
    old_info = ACTIVE_LICENSES.get(license_token, {})
    pending = {
        "licenseKey": old_info.get("licenseKey", f"refreshed-{uuid.uuid4().hex[:8]}"),
        "deviceId": device_id,
        "deviceName": old_info.get("deviceName", "Unknown Device"),
        "email": old_info.get("email", "user@bruno.local"),
        "licenseServerUrl": old_info.get("licenseServerUrl"),
        "activatedAt": _utcnow_iso(),
        "plan": old_info.get("plan", DEFAULT_PLAN),
        "licenseType": old_info.get("licenseType", DEFAULT_LICENSE_TYPE),
    }

    new_payload = _build_license_payload(pending)
    new_token = _make_jwt(new_payload)

    # 更新存储
    ACTIVE_LICENSES.pop(license_token, None)
    ACTIVE_LICENSES[new_token] = {
        **pending,
        "licensePayload": new_payload,
        "activatedAt": _utcnow_iso(),
    }

    log.info("许可证令牌已刷新 (deviceId=%s)", device_id)
    return jsonify({"licenseToken": new_token}), 200


# ============================================================
#  端点：升级 URL（用于显示购买/升级链接）
# ============================================================

@app.route("/api/v2/license/upgrade-url", methods=["POST"])
def get_upgrade_url():
    """
    获取升级 URL。

    Bruno 客户端调用 getUpgradeUrl() 函数：
    1. 发送 { deviceId, licenseToken 或 email }
    2. 响应必须包含 url 字段
    """
    payload = request.get_json(silent=True) or {}
    log.info("收到升级 URL 请求")

    upgrade_url = os.getenv(
        "BRUNO_UPGRADE_URL",
        "https://www.usebruno.com/pricing"
    )
    return jsonify({"url": upgrade_url}), 200


# ============================================================
#  端点：发现许可证服务器
# ============================================================

@app.route("/api/v2/auth/v2/discover", methods=["POST"])
def discover_license_server():
    """
    通过邮箱发现许可证服务器。

    Bruno 客户端调用 discoverLicenseServer() 函数：
    1. 发送 { email, sessionId? }
    2. 返回许可证服务器信息
    """
    payload = request.get_json(silent=True) or {}
    email = payload.get("email", "")
    session_id = payload.get("sessionId")
    log.info("收到发现请求: email=%s", email)

    # 返回自身作为许可证服务器
    host = request.host_url.rstrip("/")
    return jsonify({
        "licenseServerUrl": host,
        "method": "self-hosted",
        "email": email,
        "sessionId": session_id or str(uuid.uuid4()),
    }), 200


# ============================================================
#  端点：许可证激活会话
# ============================================================

@app.route("/api/v1/auth/license-activation/session", methods=["POST"])
def create_activation_session():
    """
    创建许可证激活会话。

    Bruno 客户端调用 createLicenseActivationSession() 函数。
    """
    log.info("收到创建激活会话请求")

    session_id = str(uuid.uuid4())
    ACTIVATION_SESSIONS[session_id] = {
        "createdAt": _utcnow_iso(),
        "status": "active",
    }

    return jsonify({
        "sessionId": session_id,
        "createdAt": ACTIVATION_SESSIONS[session_id]["createdAt"],
    }), 200


@app.route("/api/v1/auth/license-activation/session/get", methods=["POST"])
def get_activation_session():
    """
    获取激活会话详情。

    Bruno 客户端调用 getLicenseActivationSession() 函数。
    """
    payload = request.get_json(silent=True) or {}
    session_id = payload.get("sessionId", "")
    log.info("收到获取会话请求: sessionId=%s", session_id)

    session = ACTIVATION_SESSIONS.get(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    return jsonify({
        "sessionId": session_id,
        **session,
    }), 200


# ============================================================
#  端点：试用许可证
# ============================================================

@app.route("/api/v1/trials", methods=["POST"])
def request_trial():
    """
    请求试用许可证。

    Bruno 客户端调用 requestTrialLicense() 函数：
    1. 发送 { email, name }
    2. 返回试用许可证信息（用于显示验证码输入界面）
    """
    payload = request.get_json(silent=True) or {}
    email = payload.get("email", "")
    name = payload.get("name", "")
    log.info("收到试用请求: email=%s", email)

    # 生成一个试用验证码
    trial_code = str(uuid.uuid4().hex[:8]).upper()

    return jsonify({
        "email": email,
        "name": name,
        "trialCode": trial_code,
        "message": f"Trial license requested for {email}. Use code: {trial_code}",
    }), 200


@app.route("/api/v1/trials/activate", methods=["POST"])
def activate_trial():
    """
    激活试用许可证。

    Bruno 客户端调用 verifyTrialLicense() 函数：
    1. 发送 { email, verifier, deviceId, posthogDistinctId }
    2. 返回 licenseToken（试用类型）
    """
    payload = request.get_json(silent=True) or {}
    email = payload.get("email", "")
    verifier = payload.get("verifier", "")
    device_id = payload.get("deviceId", str(uuid.uuid4()))
    log.info("收到试用激活请求: email=%s, deviceId=%s", email, device_id)

    now_iso = _utcnow_iso()
    trial_end = (
        datetime.now(timezone.utc) + timedelta(days=14)
    ).isoformat().replace("+00:00", "Z")

    trial_payload = {
        "licenseKey": verifier,
        "email": email,
        "deviceId": device_id,
        "deviceName": "Trial Device",
        "licenseServerUrl": request.host_url.rstrip("/"),
        "plan": DEFAULT_PLAN,
        "type": "trial",
        "trialActive": True,
        "endDate": trial_end,
        "createdAt": now_iso,
        "updatedAt": now_iso,
    }

    token = _make_jwt(trial_payload)
    ACTIVE_LICENSES[token] = {
        **trial_payload,
        "licensePayload": trial_payload,
        "activatedAt": now_iso,
    }

    log.info("试用许可证已激活 (deviceId=%s)", device_id)
    return jsonify({"licenseToken": token}), 200


# ============================================================
#  SAML SSO 端点（基础占位）
# ============================================================

@app.route("/api/v2/auth/sso/saml/acs/<subscription_id>", methods=["POST", "GET"])
def saml_acs(subscription_id: str):
    """SAML SSO ACS 端点占位。"""
    log.info("收到 SAML ACS 请求: subscriptionId=%s", subscription_id)
    return jsonify({
        "status": "ok",
        "subscriptionId": subscription_id,
        "message": "SAML SSO is not supported on self-hosted server."
    }), 200


# ============================================================
#  健康检查
# ============================================================

@app.route("/health", methods=["GET"])
@app.route("/api/health", methods=["GET"])
def health_check():
    """健康检查端点。"""
    return jsonify({
        "status": "ok",
        "service": "Bruno License Server",
        "version": "2.0.0",
        "timestamp": _utcnow_iso(),
        "pendingActivations": len(PENDING_ACTIVATIONS),
        "activeLicenses": len(ACTIVE_LICENSES),
        "defaultPlan": DEFAULT_PLAN,
    }), 200


# ============================================================
#  应用入口
# ============================================================

def create_app():
    """创建并返回 Flask app。"""
    return app


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() in {"1", "true", "yes"}

    log.info("=" * 60)
    log.info("Bruno License Server v2.0.0")
    log.info("监听地址: %s:%s", host, port)
    log.info("默认许可证等级: %s", DEFAULT_PLAN)
    log.info("默认许可证类型: %s", DEFAULT_LICENSE_TYPE)
    log.info("调试模式: %s", debug)
    log.info("=" * 60)
    log.info("支持的 API 端点:")
    log.info("  POST /api/v2/license/activate          - 激活许可证")
    log.info("  POST /api/v1/license/activate/<id>     - OTP 验证")
    log.info("  POST /api/v2/license/verify             - 验证许可证")
    log.info("  POST /api/v2/license/refresh            - 刷新令牌")
    log.info("  POST /api/v2/license/upgrade-url        - 获取升级 URL")
    log.info("  POST /api/v2/auth/v2/discover           - 发现许可证服务器")
    log.info("  POST /api/v1/auth/license-activation/session - 创建激活会话")
    log.info("  POST /api/v1/auth/license-activation/session/get - 获取会话")
    log.info("  POST /api/v1/trials                      - 请求试用")
    log.info("  POST /api/v1/trials/activate             - 激活试用")
    log.info("  GET  /health                             - 健康检查")
    log.info("=" * 60)

    app.run(host=host, port=port, debug=debug)
