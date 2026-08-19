"""
Bruno 自托管许可证验证服务器 v2.1.0

基于从 Bruno v4.0.0 app.asar 逆向提取的 src/utils/license.js 源码实现。
支持完整的许可证激活、OTP 验证、许可证验证、刷新、试用许可证等全部 API 端点。

Bruno 客户端的许可证验证流程：

  路径 A — 个人许可证（OTP 验证）:
    1. POST /api/v2/license/activate → 返回 activationId
    2. POST /api/v1/license/activate/<activationId> → 返回 licenseToken (JWT)
       客户端设置 licenseType='personal'

  路径 B — 组织许可证（直接激活，跳过 OTP）:
    1. POST /api/v2/license/activate → 直接返回 licenseToken
       客户端设置 licenseType='organization'

  后台验证（定期执行）:
    3. POST /api/v2/license/verify → { verified, subscription, needsRefresh }
    4. 如 needsRefresh=true → POST /api/v2/license/refresh → 返回新 licenseToken

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
import threading
import time

from flask import Flask, jsonify, request


app = Flask(__name__)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("BrunoServer")

# --- 环境变量配置 ---
DEFAULT_PLAN = os.getenv("BRUNO_LICENSE_PLAN", "ULTIMATE_EDITION")
# PRO_EDITION < GOLDEN_EDITION < ULTIMATE_EDITION
DEFAULT_LICENSE_TYPE = os.getenv("BRUNO_LICENSE_TYPE", "personal")
# "personal" 或 "organization"
# 当为 "organization" 时，activate 端点直接返回 licenseToken（跳过 OTP）
# 当为 "personal" 时，activate 端点返回 activationId（需要 OTP 验证）

# 待激活请求过期时间（秒），默认 30 分钟
PENDING_EXPIRY_SECONDS = int(os.getenv("PENDING_EXPIRY_SECONDS", "1800"))

# 试用许可证有效期（天），默认 14 天
TRIAL_DURATION_DAYS = int(os.getenv("TRIAL_DURATION_DAYS", "14"))

# --- 内存存储（加锁保证线程安全）---
_lock = threading.Lock()
PENDING_ACTIVATIONS = {}       # activationId → { **请求详情, _created_at }
ACTIVE_LICENSES = {}           # licenseToken → { **许可证详情, licensePayload }
ACTIVATION_SESSIONS = {}       # sessionId → { createdAt, status }


# ============================================================
#  工具函数
# ============================================================

def _utcnow_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串（以 Z 结尾）。"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _b64url_encode(data: bytes) -> str:
    """Base64 URL-safe 编码，去掉末尾的 '='。"""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    """Base64 URL-safe 解码，自动补齐 '='。"""
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


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

    # 签名部分：Bruno 不验签，用 payload 的 SHA-256 作为伪签名
    signature_b64 = _b64url_encode(hashlib.sha256(payload_json).digest())

    return f"{header_b64}.{payload_b64}.{signature_b64}"


def _decode_jwt_payload(token: str) -> dict | None:
    """解码 JWT 的 payload 部分（不验签），失败返回 None。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_bytes = _b64url_decode(parts[1])
        return json.loads(payload_bytes)
    except Exception:
        return None


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
            datetime.now(timezone.utc) + timedelta(days=TRIAL_DURATION_DAYS)
        ).isoformat().replace("+00:00", "Z")

    return payload


def _cleanup_expired_pending():
    """清理过期的待激活请求，防止内存泄漏。"""
    now = time.time()
    expired = [
        aid for aid, info in PENDING_ACTIVATIONS.items()
        if now - info.get("_created_at_ts", now) > PENDING_EXPIRY_SECONDS
    ]
    for aid in expired:
        PENDING_ACTIVATIONS.pop(aid, None)
        log.debug("清理过期 activationId=%s", aid)
    if expired:
        log.info("清理了 %d 个过期待激活请求", len(expired))


# ============================================================
#  核心端点：许可证激活
# ============================================================

@app.route("/api/v2/license/activate", methods=["POST"])
def activate_license():
    """
    处理许可证激活请求。

    Bruno 客户端调用流程（activateLicense 函数）：
    1. 发送 { deviceId, deviceName, licenseKey, email, licenseServerUrl }
    2. 如果响应包含 activationId → 需要后续 OTP 验证（路径 A，personal）
    3. 如果响应直接包含 licenseToken → 立即激活成功（路径 B，organization）
    4. 客户端对 licenseToken 做 jwt.decode()，检查 deviceId 是否存在且匹配
    """
    payload = request.get_json(silent=True) or {}
    # 日志中脱敏：不记录 licenseKey
    log.info("收到激活请求: deviceId=%s, email=%s, licenseServerUrl=%s",
             payload.get("deviceId"), payload.get("email"), payload.get("licenseServerUrl"))

    license_key = payload.get("licenseKey", "")
    device_id = payload.get("deviceId", str(uuid.uuid4()))
    device_name = payload.get("deviceName", "Unnamed Device")
    email = payload.get("email")
    license_server_url = payload.get("licenseServerUrl")

    pending = {
        "licenseKey": license_key,
        "deviceId": device_id,
        "deviceName": device_name,
        "email": email,
        "licenseServerUrl": license_server_url,
        "activatedAt": _utcnow_iso(),
        "plan": DEFAULT_PLAN,
        "licenseType": DEFAULT_LICENSE_TYPE,
    }

    # 路径 B — 组织许可证：直接返回 licenseToken，跳过 OTP
    if DEFAULT_LICENSE_TYPE == "organization":
        license_payload = _build_license_payload(pending, license_type="organization")
        token = _make_jwt(license_payload)

        with _lock:
            ACTIVE_LICENSES[token] = {
                **pending,
                "licensePayload": license_payload,
                "activatedAt": _utcnow_iso(),
            }

        log.info("组织许可证直接激活成功 (deviceId=%s)", device_id)
        return jsonify({"licenseToken": token}), 200

    # 路径 A — 个人许可证：返回 activationId，等待 OTP 验证
    activation_id = str(uuid.uuid4())

    with _lock:
        _cleanup_expired_pending()
        PENDING_ACTIVATIONS[activation_id] = {
            **pending,
            "_created_at_ts": time.time(),
        }

    log.info("个人许可证激活请求已受理，activationId=%s，等待 OTP 验证", activation_id)
    return jsonify({"activationId": activation_id}), 200


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
    5. 将 licenseToken 存入 licenseStore（license.json），licenseType='personal'
    """
    payload = request.get_json(silent=True) or {}
    log.info("收到 OTP 验证: activationId=%s", activation_id)

    with _lock:
        pending = PENDING_ACTIVATIONS.pop(activation_id, None)

    if not pending:
        log.warning("无效的 activationId: %s", activation_id)
        return jsonify({"error": "Invalid or expired activationId"}), 404

    # 任意 OTP 都接受（自托管服务器不做实际验证）
    license_payload = _build_license_payload(pending, license_type="personal")
    token = _make_jwt(license_payload)

    # 存储已激活的许可证，用于后续 verify 端点
    with _lock:
        ACTIVE_LICENSES[token] = {
            **pending,
            "licensePayload": license_payload,
            "activatedAt": _utcnow_iso(),
        }

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
        # 已知令牌，返回验证成功
        plan = license_info.get("plan", DEFAULT_PLAN)
        log.info("许可证验证成功 (deviceId=%s, plan=%s)", device_id, plan)
    else:
        # 未知令牌：尝试解码 JWT 获取 plan 信息
        decoded = _decode_jwt_payload(license_token)
        plan = decoded.get("plan", DEFAULT_PLAN) if decoded else DEFAULT_PLAN
        log.info("未知令牌，解码后返回验证成功 (deviceId=%s, plan=%s)", device_id, plan)

    response = {
        "verified": True,
        "needsRefresh": False,
        "subscription": {
            "plan": plan,
        },
        "trial": None,
        "aiPolicy": None,
    }

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
       注意：Bruno 源码在 refresh 后设置 licenseType='organization'
    """
    payload = request.get_json(silent=True) or {}
    license_token = payload.get("licenseToken", "")
    device_id = payload.get("deviceId", "")
    log.info("收到刷新请求: deviceId=%s", device_id)

    # 查找原许可证信息
    old_info = ACTIVE_LICENSES.get(license_token, {})

    # 如果旧令牌存在，尝试从其 JWT payload 中获取更多信息
    if not old_info:
        decoded = _decode_jwt_payload(license_token)
        if decoded:
            old_info = decoded

    pending = {
        "licenseKey": old_info.get("licenseKey", f"refreshed-{uuid.uuid4().hex[:8]}"),
        "deviceId": device_id,
        "deviceName": old_info.get("deviceName", "Unknown Device"),
        "email": old_info.get("email", "user@bruno.local"),
        "licenseServerUrl": old_info.get("licenseServerUrl"),
        "activatedAt": _utcnow_iso(),
        "plan": old_info.get("plan", DEFAULT_PLAN),
        # Bruno 源码在 refresh 后设置 licenseType='organization'
        "licenseType": "organization",
    }

    # Bruno 源码: licenseStore.set('licenseType', 'organization')
    new_payload = _build_license_payload(pending, license_type="organization")
    new_token = _make_jwt(new_payload)

    # 更新存储
    with _lock:
        ACTIVE_LICENSES.pop(license_token, None)
        ACTIVE_LICENSES[new_token] = {
            **pending,
            "licensePayload": new_payload,
            "activatedAt": _utcnow_iso(),
        }

    log.info("许可证令牌已刷新 (deviceId=%s, type=organization)", device_id)
    return jsonify({"licenseToken": new_token}), 200


# ============================================================
#  端点：升级 URL
# ============================================================

@app.route("/api/v2/license/upgrade-url", methods=["POST"])
def get_upgrade_url():
    """
    获取升级 URL。

    Bruno 客户端调用 getUpgradeUrl() 函数：
    1. 发送 { deviceId, licenseToken 或 email }
    2. 响应必须包含 url 字段（string 类型）
    """
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
    created_at = _utcnow_iso()

    with _lock:
        ACTIVATION_SESSIONS[session_id] = {
            "createdAt": created_at,
            "status": "active",
        }

    return jsonify({
        "sessionId": session_id,
        "createdAt": created_at,
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

    Bruno isTrialLicenseValid() 检查：
    - type='trial' 时 trialActive=true 且 endDate > now
    """
    payload = request.get_json(silent=True) or {}
    email = payload.get("email", "")
    verifier = payload.get("verifier", "")
    device_id = payload.get("deviceId", str(uuid.uuid4()))
    log.info("收到试用激活请求: email=%s, deviceId=%s", email, device_id)

    now_iso = _utcnow_iso()
    trial_end = (
        datetime.now(timezone.utc) + timedelta(days=TRIAL_DURATION_DAYS)
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

    with _lock:
        ACTIVE_LICENSES[token] = {
            **trial_payload,
            "licensePayload": trial_payload,
            "activatedAt": now_iso,
        }

    log.info("试用许可证已激活 (deviceId=%s, 有效期 %d 天)", device_id, TRIAL_DURATION_DAYS)
    return jsonify({"licenseToken": token}), 200


# ============================================================
#  SAML SSO 端点（占位）
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
    with _lock:
        _cleanup_expired_pending()
        pending_count = len(PENDING_ACTIVATIONS)
        active_count = len(ACTIVE_LICENSES)
        session_count = len(ACTIVATION_SESSIONS)

    return jsonify({
        "status": "ok",
        "service": "Bruno License Server",
        "version": "2.1.0",
        "timestamp": _utcnow_iso(),
        "pendingActivations": pending_count,
        "activeLicenses": active_count,
        "activationSessions": session_count,
        "defaultPlan": DEFAULT_PLAN,
        "defaultLicenseType": DEFAULT_LICENSE_TYPE,
        "trialDurationDays": TRIAL_DURATION_DAYS,
    }), 200


# ============================================================
#  错误处理
# ============================================================

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method not allowed"}), 405


@app.errorhandler(500)
def internal_error(e):
    log.error("内部错误: %s", e)
    return jsonify({"error": "Internal server error"}), 500


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
    log.info("Bruno License Server v2.1.0")
    log.info("监听地址: %s:%s", host, port)
    log.info("默认许可证等级: %s", DEFAULT_PLAN)
    log.info("默认许可证类型: %s", DEFAULT_LICENSE_TYPE)
    log.info("试用有效期: %d 天", TRIAL_DURATION_DAYS)
    log.info("待激活过期时间: %d 秒", PENDING_EXPIRY_SECONDS)
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
