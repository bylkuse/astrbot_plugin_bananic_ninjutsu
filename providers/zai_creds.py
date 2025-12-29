import json
import time
import sys
import base64
from playwright.sync_api import sync_playwright

# ==========================================
# 💀 步骤一：提取
# ==========================================
POISON_SCRIPT = """
(() => {
    const target = (typeof window !== 'undefined' ? window.crypto : self.crypto);
    if (!target || !target.subtle) return;
    const originalGenerate = target.subtle.generateKey;
    target.subtle.generateKey = async function(algo, extractable, usages) {
        return originalGenerate.call(this, algo, true, usages);
    };
})();
"""

DB_EXTRACT_SCRIPT = """
async () => {
    return new Promise((resolve) => {
        const req = indexedDB.open("darkknight");
        req.onerror = () => resolve({status: "error", msg: "无法打开数据库"});
        req.onsuccess = (e) => {
            const db = e.target.result;
            if (!db.objectStoreNames.contains("keys")) { resolve({status: "pending", msg: "等待 keys 表...一直等待请尝试重新运行"}); return; }
            const tx = db.transaction(["keys"], "readonly");
            tx.objectStore("keys").get("current_keypair").onsuccess = (evt) => {
                const res = evt.target.result;
                if (!res) { resolve({status: "pending", msg: "等待 Key 生成..."}); return; }
                let targetKey = res.privateKey || (res.keyPair && res.keyPair.privateKey);
                if (targetKey && targetKey.extractable) {
                    window.crypto.subtle.exportKey("jwk", targetKey).then(jwk => {
                        resolve({status: "success", jwk: jwk});
                    });
                } else { resolve({status: "pending", msg: "Key 不可导出"}); }
            };
        };
    });
}
"""

def step_1_harvest_safe():
    print("\n" + "="*50)
    print("🧨 步骤一：提取私钥")
    print("="*50)
    extracted_jwk = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--no-sandbox","--disable-blink-features=AutomationControlled"])
        context = browser.new_context()
        context.add_init_script(POISON_SCRIPT)
        page = context.new_page()
        page.route("**/*", lambda route, req: route.continue_())
        try: page.goto("https://zai.is", timeout=60000)
        except: pass
        try: page.evaluate("""indexedDB.deleteDatabase("darkknight"); localStorage.clear();""")
        except: pass
        page.reload()

        for i in range(20):
            result = page.evaluate(DB_EXTRACT_SCRIPT)
            if result.get("status") == "success":
                extracted_jwk = result.get("jwk")
                print("✅ 提取成功！")
                break
            sys.stdout.write(f"\r⏳ {result.get('msg')} ({i+1}/20)")
            time.sleep(1.5)
        browser.close()
    if not extracted_jwk: sys.exit(1)
    return extracted_jwk

# ==========================================
# 🧬 步骤二：登录 (上下文级监听 + LS轮询)
# ==========================================
def get_restore_script(jwk_data):
    return f"""
    (async () => {{
        const keyData = {json.dumps(jwk_data)};
        try {{
            const privateKey = await crypto.subtle.importKey("jwk", keyData, {{ name: "ECDSA", namedCurve: "P-256" }}, true, ["sign"]);
            const pubData = {{ ...keyData }};
            delete pubData.d; delete pubData.key_ops;
            const publicKey = await crypto.subtle.importKey("jwk", pubData, {{ name: "ECDSA", namedCurve: "P-256" }}, true, ["verify"]);

            const req = indexedDB.open("darkknight");
            req.onsuccess = (e) => {{
                const db = e.target.result;
                if (!db.objectStoreNames.contains("keys")) return;
                const tx = db.transaction(["keys"], "readwrite");
                tx.objectStore("keys").put({{
                    id: "current_keypair",
                    keyPair: {{ privateKey, publicKey }},
                    publicKeyJwk: pubData
                }});
                window.INJECTION_STATUS = "SUCCESS";
            }};
        }} catch(e) {{}}
    }})();
    """

def decode_fp_from_header(header_val):
    try:
        payload_b64 = header_val.split('.')[0]
        payload_b64 += '=' * (-len(payload_b64) % 4)
        payload_json = base64.urlsafe_b64decode(payload_b64).decode('utf-8')
        return json.loads(payload_json).get("fp")
    except: return None

def step_2_login_nuclear(jwk):
    print("\n" + "="*50)
    print("🧬 步骤二：无毒环境登录")
    print("="*50)

    creds = {"token": None, "fp": None}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--no-sandbox","--disable-blink-features=AutomationControlled"])

        # 🔥 监听器挂在 context 上
        context = browser.new_context()

        def on_request(req):
            url = req.url
            if "zai.is" not in url: return # 只看 zai.is

            try:
                headers = req.all_headers()

                # 打印日志证明活着
                if "/api/" in url or "/tools" in url:
                    auth = headers.get("authorization", "None")[:20]
                    print(f"📡 [NET] {url.split('?')[0][-30:]} | Auth: {auth}...")

                # 抓 Token
                auth = headers.get("authorization")
                if auth and "Bearer ey" in auth:
                    if not creds["token"]:
                        creds["token"] = auth
                        print(f"\n⚡ [NET] 网络流捕获 Token: {auth[:15]}...")

                # 抓 FP
                dk = headers.get("x-zai-darkknight")
                if dk and not creds["fp"]:
                    decoded = decode_fp_from_header(dk)
                    if decoded:
                        creds["fp"] = decoded
                        print("⚡ [NET] 网络流捕获 Fingerprint")

            except: pass

        # 挂载全局监听
        context.on("request", on_request)

        page = context.new_page()

        print("🔗 打开首页 https://zai.is ...")
        page.goto("https://zai.is")
        time.sleep(3)

        print("💉 尝试植入私钥...")
        for _ in range(5):
            page.evaluate(get_restore_script(jwk))
            if page.evaluate("window.INJECTION_STATUS") == "SUCCESS":
                print("   ✅ 植入成功")
                break
            time.sleep(1)

        print("\n👇 [请手动登录]")
        print("   现在即使跳转 Discord 再跳回来，控制台也应该继续滚动。")
        print("   如果网络监听失效，脚本会自动尝试读取 LocalStorage。")
        print("   ⏳ 双通道监听中...\n")

        # 🔥 双保险循环
        while True:
            # 通道1：检查网络捕获结果
            if creds["token"] and creds["fp"]:
                print("\n🎉 凭证收集完毕 (来源: 网络监听)！")
                break

            # 通道2：轮询 LocalStorage
            try:
                # 只有在 zai.is 域名下才读取
                if "zai.is" in page.url:
                    # 尝试读取常见 token key
                    token_ls = page.evaluate("localStorage.getItem('token') || localStorage.getItem('access_token') || localStorage.getItem('sb-access-token')")
                    if token_ls and token_ls.startswith("eyJ"):
                        auth_val = f"Bearer {token_ls}"
                        if not creds["token"]:
                            creds["token"] = auth_val
                            print(f"\n💾 [DISK] LocalStorage 读取到 Token: {auth_val[:15]}...")

                    # 尝试从 cookie 读 (有时 token 在 cookie 里)
                    cookies = context.cookies("https://zai.is")
                    for c in cookies:
                        if c['name'] == 'token' and c['value'].startswith("eyJ"):
                             auth_val = f"Bearer {c['value']}"
                             if not creds["token"]:
                                creds["token"] = auth_val
                                print(f"\n🍪 [DISK] Cookie 读取到 Token: {auth_val[:15]}...")
            except Exception as e:
                # 页面可能正在跳转中，evaluate 会报错，忽略
                pass

            if page.is_closed():
                sys.exit(1)

            time.sleep(1)

        browser.close()
        return creds

if __name__ == "__main__":
    jwk = step_1_harvest_safe()
    print(f"\n🔑 Key Ready. Entering Step 2...")

    final = step_2_login_nuclear(jwk)

    result = {
        "private_key": jwk,
        "token": final["token"],
        "fingerprint": final["fp"] or {"c": "default", "wgl": "default"} # 如果没抓到fp，给个默认的防止脚本崩
    }

    with open("zai_creds.json", "w") as f:
        f.write(json.dumps(result))

    print(f"\n✅ 最终配置文件已生成: zai_creds.json")