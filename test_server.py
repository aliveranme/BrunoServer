#!/usr/bin/env python3
"""
BrunoServer 端点测试脚本 v2

测试所有许可证 API 端点是否按预期工作。
覆盖个人许可证（OTP）、组织许可证（直接激活）、验证、刷新、试用等全部流程。
"""
import requests
import json
import sys
import base64

BASE_URL = "http://127.0.0.1:5000"

# 测试数据
TEST_LICENSE_KEY = "BRUNO-TEST-1234-5678"
TEST_EMAIL = "test@bruno.local"
TEST_DEVICE_ID = "test-device-id-abc123"
TEST_DEVICE_NAME = "TestMachine"


def print_header(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_result(success, detail=""):
    status = "✅ PASS" if success else "❌ FAIL"
    print(f"  {status} - {detail}")


def decode_jwt_payload(token):
    """解码 JWT payload（不验签）。"""
    parts = token.split(".")
    payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64))


def test_health():
    """测试健康检查端点"""
    print_header("测试 1: 健康检查")
    resp = requests.get(f"{BASE_URL}/health")
    data = resp.json()
    success = resp.status_code == 200 and data.get("status") == "ok"
    print_result(success, f"GET /health → {resp.status_code}")
    if success:
        print(f"    版本: {data.get('version')}")
        print(f"    默认等级: {data.get('defaultPlan')}")
        print(f"    默认类型: {data.get('defaultLicenseType')}")
    return success


def test_personal_activate():
    """测试个人许可证激活（路径 A：返回 activationId）"""
    print_header("测试 2: 个人许可证激活 (POST /api/v2/license/activate)")
    resp = requests.post(f"{BASE_URL}/api/v2/license/activate", json={
        "licenseKey": TEST_LICENSE_KEY,
        "email": TEST_EMAIL,
        "deviceId": TEST_DEVICE_ID,
        "deviceName": TEST_DEVICE_NAME,
        "licenseServerUrl": BASE_URL,
    })
    data = resp.json()
    success = resp.status_code == 200 and "activationId" in data
    print_result(success, f"POST /api/v2/license/activate → {resp.status_code}")
    if success:
        print(f"    activationId: {data['activationId']}")
        # 确认响应中不含 licenseToken（个人许可证需要 OTP 步骤）
        has_token = "licenseToken" in data
        print_result(not has_token, "响应不含 licenseToken（需要 OTP 验证）")
        success = success and not has_token
    return data.get("activationId") if success else None


def test_otp_verify(activation_id):
    """测试 OTP 验证"""
    print_header("测试 3: OTP 验证 (POST /api/v1/license/activate/<id>)")
    resp = requests.post(
        f"{BASE_URL}/api/v1/license/activate/{activation_id}",
        json={"otp": "any-otp-value"}
    )
    data = resp.json()
    success = resp.status_code == 200 and "licenseToken" in data
    print_result(success, f"POST /api/v1/license/activate/{activation_id} → {resp.status_code}")
    if success:
        token = data["licenseToken"]
        payload = decode_jwt_payload(token)
        print(f"    JWT payload:")
        print(f"      licenseKey: {payload.get('licenseKey')}")
        print(f"      email: {payload.get('email')}")
        print(f"      deviceId: {payload.get('deviceId')}")
        print(f"      plan: {payload.get('plan')}")
        print(f"      type: {payload.get('type')}")
        # 验证关键字段
        checks = [
            ("deviceId 匹配", payload.get("deviceId") == TEST_DEVICE_ID),
            ("licenseKey 匹配", payload.get("licenseKey") == TEST_LICENSE_KEY),
            ("email 匹配", payload.get("email") == TEST_EMAIL),
            ("plan = ULTIMATE_EDITION", payload.get("plan") == "ULTIMATE_EDITION"),
            ("type = personal", payload.get("type") == "personal"),
            ("trialActive = False", payload.get("trialActive") is False),
            ("licenseServerUrl 存在", payload.get("licenseServerUrl") is not None),
            ("createdAt 存在", payload.get("createdAt") is not None),
            ("updatedAt 存在", payload.get("updatedAt") is not None),
        ]
        all_pass = True
        for name, ok in checks:
            print_result(ok, f"JWT 字段: {name}")
            if not ok:
                all_pass = False
        return token if all_pass else None
    return None


def test_verify(token):
    """测试许可证验证"""
    print_header("测试 4: 许可证验证 (POST /api/v2/license/verify)")
    resp = requests.post(f"{BASE_URL}/api/v2/license/verify", json={
        "licenseToken": token,
        "deviceId": TEST_DEVICE_ID,
    })
    data = resp.json()
    success = resp.status_code == 200 and data.get("verified") is True
    print_result(success, f"POST /api/v2/license/verify → {resp.status_code}")
    if success:
        print(f"    verified: {data.get('verified')}")
        print(f"    plan: {data.get('subscription', {}).get('plan')}")
        print(f"    needsRefresh: {data.get('needsRefresh')}")
        print(f"    trial: {data.get('trial')}")
        print(f"    aiPolicy: {data.get('aiPolicy')}")
    return success


def test_verify_unknown_token():
    """测试对未知令牌的验证"""
    print_header("测试 5: 未知令牌验证")
    fake_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJsaWNlbnNlS2V5IjoidGVzdCIsImVtYWlsIjoidGVzdEBicnVuby5sb2NhbCIsImRldmljZUlkIjoidGVzdC1kZXZpY2UtaWQtYWJjMTIzIiwicGxhbiI6IlVMVElNQVRFX0VESVRJT04iLCJ0eXBlIjoicGVyc29uYWwifQ.fake_signature"
    resp = requests.post(f"{BASE_URL}/api/v2/license/verify", json={
        "licenseToken": fake_token,
        "deviceId": TEST_DEVICE_ID,
    })
    data = resp.json()
    success = resp.status_code == 200 and data.get("verified") is True
    print_result(success, f"POST /api/v2/license/verify (未知令牌) → {resp.status_code}")
    if success:
        # 未知令牌应从 JWT 中解码 plan
        plan = data.get("subscription", {}).get("plan")
        print_result(plan == "ULTIMATE_EDITION", f"从 JWT 解码 plan={plan}")
    return success


def test_refresh(token):
    """测试令牌刷新"""
    print_header("测试 6: 令牌刷新 (POST /api/v2/license/refresh)")
    resp = requests.post(f"{BASE_URL}/api/v2/license/refresh", json={
        "licenseToken": token,
        "deviceId": TEST_DEVICE_ID,
    })
    data = resp.json()
    success = resp.status_code == 200 and "licenseToken" in data
    print_result(success, f"POST /api/v2/license/refresh → {resp.status_code}")
    if success:
        new_token = data["licenseToken"]
        payload = decode_jwt_payload(new_token)
        # Bruno 源码在 refresh 后设置 licenseType='organization'
        print_result(payload.get("type") == "organization",
                     f"刷新后 type={payload.get('type')}（应为 organization）")
        success = payload.get("type") == "organization"
    return data.get("licenseToken") if success else None


def test_upgrade_url():
    """测试升级 URL"""
    print_header("测试 7: 升级 URL (POST /api/v2/license/upgrade-url)")
    resp = requests.post(f"{BASE_URL}/api/v2/license/upgrade-url", json={
        "deviceId": TEST_DEVICE_ID,
    })
    data = resp.json()
    success = resp.status_code == 200 and "url" in data and isinstance(data["url"], str)
    print_result(success, f"POST /api/v2/license/upgrade-url → {resp.status_code}")
    if success:
        print(f"    url: {data['url']}")
    return success


def test_discover():
    """测试发现许可证服务器"""
    print_header("测试 8: 发现服务器 (POST /api/v2/auth/v2/discover)")
    resp = requests.post(f"{BASE_URL}/api/v2/auth/v2/discover", json={
        "email": TEST_EMAIL,
    })
    data = resp.json()
    success = resp.status_code == 200 and "licenseServerUrl" in data
    print_result(success, f"POST /api/v2/auth/v2/discover → {resp.status_code}")
    if success:
        print(f"    licenseServerUrl: {data.get('licenseServerUrl')}")
    return success


def test_activation_session():
    """测试激活会话"""
    print_header("测试 9: 激活会话")
    # 创建会话
    resp = requests.post(f"{BASE_URL}/api/v1/auth/license-activation/session")
    data = resp.json()
    success = resp.status_code == 200 and "sessionId" in data
    print_result(success, f"POST .../session (创建) → {resp.status_code}")
    if not success:
        return False

    session_id = data["sessionId"]
    print(f"    sessionId: {session_id}")

    # 获取会话
    resp2 = requests.post(
        f"{BASE_URL}/api/v1/auth/license-activation/session/get",
        json={"sessionId": session_id}
    )
    data2 = resp2.json()
    success2 = resp2.status_code == 200 and data2.get("sessionId") == session_id
    print_result(success2, f"POST .../session/get (获取) → {resp2.status_code}")
    return success and success2


def test_trial():
    """测试试用许可证"""
    print_header("测试 10: 试用许可证")
    # 请求试用
    resp = requests.post(f"{BASE_URL}/api/v1/trials", json={
        "email": TEST_EMAIL,
        "name": "TestUser",
    })
    data = resp.json()
    success = resp.status_code == 200
    print_result(success, f"POST /api/v1/trials (请求) → {resp.status_code}")
    if not success:
        return False

    # 激活试用
    resp2 = requests.post(f"{BASE_URL}/api/v1/trials/activate", json={
        "email": TEST_EMAIL,
        "verifier": "trial-code-1234",
        "deviceId": TEST_DEVICE_ID,
        "posthogDistinctId": "test-posthog-id",
    })
    data2 = resp2.json()
    success2 = resp2.status_code == 200 and "licenseToken" in data2
    print_result(success2, f"POST /api/v1/trials/activate (激活) → {resp2.status_code}")

    if success2:
        token = data2["licenseToken"]
        payload = decode_jwt_payload(token)
        print(f"    type: {payload.get('type')}")
        print(f"    trialActive: {payload.get('trialActive')}")
        print(f"    endDate: {payload.get('endDate')}")
        # 验证试用字段
        checks = [
            ("type = trial", payload.get("type") == "trial"),
            ("trialActive = True", payload.get("trialActive") is True),
            ("endDate 存在", payload.get("endDate") is not None),
            ("deviceId 匹配", payload.get("deviceId") == TEST_DEVICE_ID),
        ]
        all_ok = True
        for name, ok in checks:
            print_result(ok, f"试用字段: {name}")
            if not ok:
                all_ok = False
        success2 = all_ok

    return success and success2


def test_invalid_activation_id():
    """测试无效的 activationId"""
    print_header("测试 11: 无效 activationId")
    resp = requests.post(
        f"{BASE_URL}/api/v1/license/activate/nonexistent-id",
        json={"otp": "000000"}
    )
    success = resp.status_code == 404
    print_result(success, f"POST .../activate/nonexistent-id → {resp.status_code}")
    return success


def test_404_handler():
    """测试 404 错误处理"""
    print_header("测试 12: 404 错误处理")
    resp = requests.get(f"{BASE_URL}/nonexistent-endpoint")
    success = resp.status_code == 404 and "error" in resp.json()
    print_result(success, f"GET /nonexistent-endpoint → {resp.status_code}")
    return success


def main():
    print("\n" + "🔥" * 30)
    print("  BrunoServer 端点测试 v2")
    print("🔥" * 30)

    # 检查服务器是否在线
    try:
        requests.get(f"{BASE_URL}/health", timeout=5)
    except requests.ConnectionError:
        print(f"\n❌ 无法连接到 {BASE_URL}，请确认服务器已启动。")
        sys.exit(1)

    results = []

    # 执行所有测试
    results.append(("健康检查", test_health()))

    activation_id = test_personal_activate()
    results.append(("个人许可证激活", activation_id is not None))
    if not activation_id:
        print("\n❌ 激活失败，无法继续后续测试")
        sys.exit(1)

    token = test_otp_verify(activation_id)
    results.append(("OTP 验证", token is not None))
    if not token:
        print("\n❌ OTP 验证失败，无法继续后续测试")
        sys.exit(1)

    results.append(("许可证验证", test_verify(token)))
    results.append(("未知令牌验证", test_verify_unknown_token()))
    new_token = test_refresh(token)
    results.append(("令牌刷新", new_token is not None))
    results.append(("升级 URL", test_upgrade_url()))
    results.append(("发现服务器", test_discover()))
    results.append(("激活会话", test_activation_session()))
    results.append(("试用许可证", test_trial()))
    results.append(("无效 activationId", test_invalid_activation_id()))
    results.append(("404 错误处理", test_404_handler()))

    # 汇总
    print_header("测试汇总")
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print_result(ok, name)
    print(f"\n  总计: {passed}/{total} 通过")
    if passed == total:
        print("  🎉 全部测试通过！")
    else:
        print(f"  ⚠️ {total - passed} 个测试失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
