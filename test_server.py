#!/usr/bin/env python3
"""
BrunoServer 端点测试脚本

测试所有许可证 API 端点是否按预期工作。
"""
import requests
import json
import sys
import time

BASE_URL = "http://127.0.0.1:5000"

# 测试数据
TEST_LICENSE_KEY = "BRUNO-TEST-1234-5678"
TEST_EMAIL = "test@bruno.local"
TEST_DEVICE_ID = "test-device-id-abc123"
TEST_DEVICE_NAME = "TestMachine"
TEST_OTP = "123456"


def print_header(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_result(success, detail=""):
    status = "✅ PASS" if success else "❌ FAIL"
    print(f"  {status} - {detail}")


def test_health():
    """测试健康检查端点"""
    print_header("测试 1: 健康检查")
    resp = requests.get(f"{BASE_URL}/health")
    success = resp.status_code == 200 and resp.json().get("status") == "ok"
    print_result(success, f"GET /health → {resp.status_code}")
    if success:
        data = resp.json()
        print(f"    版本: {data.get('version')}")
        print(f"    默认等级: {data.get('defaultPlan')}")
    return success


def test_activate():
    """测试许可证激活"""
    print_header("测试 2: 许可证激活 (POST /api/v2/license/activate)")
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
    return data.get("activationId") if success else None


def test_otp_verify(activation_id):
    """测试 OTP 验证"""
    print_header("测试 3: OTP 验证 (POST /api/v1/license/activate/<id>)")
    resp = requests.post(
        f"{BASE_URL}/api/v1/license/activate/{activation_id}",
        json={"otp": TEST_OTP}
    )
    data = resp.json()
    success = resp.status_code == 200 and "licenseToken" in data
    print_result(success, f"POST /api/v1/license/activate/{activation_id} → {resp.status_code}")
    if success:
        token = data["licenseToken"]
        # 解码 JWT payload 验证内容
        import base64
        parts = token.split(".")
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        print(f"    JWT payload:")
        print(f"      licenseKey: {payload.get('licenseKey')}")
        print(f"      email: {payload.get('email')}")
        print(f"      deviceId: {payload.get('deviceId')}")
        print(f"      plan: {payload.get('plan')}")
        print(f"      type: {payload.get('type')}")
        # 验证关键字段
        checks = [
            payload.get("deviceId") == TEST_DEVICE_ID,
            payload.get("licenseKey") == TEST_LICENSE_KEY,
            payload.get("email") == TEST_EMAIL,
            payload.get("plan") == "ULTIMATE_EDITION",
        ]
        all_pass = all(checks)
        print_result(all_pass, "JWT payload 字段验证")
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
    return success


def test_refresh(token):
    """测试令牌刷新"""
    print_header("测试 5: 令牌刷新 (POST /api/v2/license/refresh)")
    resp = requests.post(f"{BASE_URL}/api/v2/license/refresh", json={
        "licenseToken": token,
        "deviceId": TEST_DEVICE_ID,
    })
    data = resp.json()
    success = resp.status_code == 200 and "licenseToken" in data
    print_result(success, f"POST /api/v2/license/refresh → {resp.status_code}")
    if success:
        print(f"    新令牌已签发: {data['licenseToken'][:40]}...")
    return data.get("licenseToken") if success else None


def test_upgrade_url():
    """测试升级 URL"""
    print_header("测试 6: 升级 URL (POST /api/v2/license/upgrade-url)")
    resp = requests.post(f"{BASE_URL}/api/v2/license/upgrade-url", json={
        "deviceId": TEST_DEVICE_ID,
    })
    data = resp.json()
    success = resp.status_code == 200 and "url" in data
    print_result(success, f"POST /api/v2/license/upgrade-url → {resp.status_code}")
    if success:
        print(f"    url: {data['url']}")
    return success


def test_discover():
    """测试发现许可证服务器"""
    print_header("测试 7: 发现服务器 (POST /api/v2/auth/v2/discover)")
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
    print_header("测试 8: 激活会话")
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
    print_header("测试 9: 试用许可证")
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
        import base64
        parts = token.split(".")
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        print(f"    type: {payload.get('type')}")
        print(f"    trialActive: {payload.get('trialActive')}")
        print(f"    endDate: {payload.get('endDate')}")

    return success and success2


def test_invalid_activation_id():
    """测试无效的 activationId"""
    print_header("测试 10: 无效 activationId")
    resp = requests.post(
        f"{BASE_URL}/api/v1/license/activate/nonexistent-id",
        json={"otp": "000000"}
    )
    success = resp.status_code == 404
    print_result(success, f"POST .../activate/nonexistent-id → {resp.status_code}")
    return success


def main():
    print("\n" + "🔥" * 30)
    print("  BrunoServer 端点测试")
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

    activation_id = test_activate()
    results.append(("许可证激活", activation_id is not None))
    if not activation_id:
        print("\n❌ 激活失败，无法继续后续测试")
        sys.exit(1)

    token = test_otp_verify(activation_id)
    results.append(("OTP 验证", token is not None))
    if not token:
        print("\n❌ OTP 验证失败，无法继续后续测试")
        sys.exit(1)

    results.append(("许可证验证", test_verify(token)))
    new_token = test_refresh(token)
    results.append(("令牌刷新", new_token is not None))
    results.append(("升级 URL", test_upgrade_url()))
    results.append(("发现服务器", test_discover()))
    results.append(("激活会话", test_activation_session()))
    results.append(("试用许可证", test_trial()))
    results.append(("无效 activationId", test_invalid_activation_id()))

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


if __name__ == "__main__":
    main()
