import os
import re
import sys
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://dash.hidencloud.com"

# HidenCloud 当前使用的 remember cookie 名称
REMEMBER_COOKIE_NAME = (
    "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"
)

COOKIE_VALUE = os.getenv("COOKIE_VALUE", "").strip()
EMAIL = os.getenv("EMAIL", "").strip()

RENEW_DAYS = 10

session = requests.Session()

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
})


def log(message):
    print(message, flush=True)


def mask_email(email):
    if not email or "@" not in email:
        return "未设置"
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        return f"**@{domain}"
    return f"{name[:2]}***@{domain}"


def check_cloudflare(response):
    text = response.text.lower()

    if (
        response.status_code in (403, 503)
        or "challenges.cloudflare.com" in text
        or "just a moment..." in text
        or "cf-chl-" in text
    ):
        raise RuntimeError(
            "遇到 Cloudflare 验证，GitHub Actions 无法直接完成。"
        )


def request(method, path, **kwargs):
    url = path if path.startswith("http") else urljoin(BASE_URL, path)

    response = session.request(
        method,
        url,
        timeout=30,
        allow_redirects=True,
        **kwargs,
    )

    check_cloudflare(response)

    log(f"HTTP {response.status_code}  {response.url}")

    return response


def get_csrf(html):
    soup = BeautifulSoup(html, "html.parser")

    field = soup.find("input", attrs={"name": "_token"})
    if field and field.get("value"):
        return field["value"]

    meta = soup.find("meta", attrs={"name": "csrf-token"})
    if meta and meta.get("content"):
        return meta["content"]

    return ""


def get_due_date(html):
    text = BeautifulSoup(html, "html.parser").get_text(
        " ", strip=True
    )

    patterns = [
        r"Due date\s*[:\-]?\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
        r"Due Date\s*[:\-]?\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1)

    return "未知"


def find_services(html):
    ids = set()

    soup = BeautifulSoup(html, "html.parser")

    for link in soup.find_all("a", href=True):
        match = re.search(
            r"/service/(\d+)/manage",
            link["href"]
        )
        if match:
            ids.add(match.group(1))

    return sorted(ids)


def find_payment(html, current_url):
    soup = BeautifulSoup(html, "html.parser")

    # 优先找支付表单
    for form in soup.find_all("form"):
        button_text = " ".join(
            button.get_text(" ", strip=True)
            for button in form.find_all("button")
        ).lower()

        action = form.get("action")

        if "pay" in button_text and action and "balance/add" not in action:
            data = {}

            for field in form.find_all("input"):
                name = field.get("name")

                if name:
                    data[name] = field.get("value", "")

            return {
                "type": "form",
                "url": urljoin(current_url, action),
                "data": data,
            }

    # 再寻找 Pay 链接
    for link in soup.find_all("a", href=True):
        text = link.get_text(" ", strip=True).lower()

        if text == "pay" or text.startswith("pay "):
            return {
                "type": "link",
                "url": urljoin(current_url, link["href"]),
            }

    return None


def pay_invoice(response):
    payment = find_payment(
        response.text,
        response.url,
    )

    if not payment:
        log("⚪ 没找到支付按钮，可能账单已支付。")
        return True

    log("💳 找到支付入口，准备支付免费续期账单……")

    if payment["type"] == "form":
        result = request(
            "POST",
            payment["url"],
            data=payment["data"],
            headers={
                "Referer": response.url,
            },
        )
    else:
        result = request(
            "GET",
            payment["url"],
            headers={
                "Referer": response.url,
            },
        )

    if result.status_code >= 400:
        log("❌ 支付请求失败。")
        return False

    log("✅ 支付请求完成。")
    return True


def check_unpaid_invoice(service_id):
    response = request(
        "GET",
        f"/service/{service_id}/invoices?where=unpaid",
    )

    soup = BeautifulSoup(response.text, "html.parser")

    invoice_urls = []

    for link in soup.find_all("a", href=True):
        href = link["href"]

        if "/invoice/" in href and "download" not in href:
            invoice_urls.append(
                urljoin(response.url, href)
            )

    invoice_urls = list(dict.fromkeys(invoice_urls))

    if not invoice_urls:
        return False

    log(f"📄 找到 {len(invoice_urls)} 个未支付账单。")

    for invoice_url in invoice_urls:
        invoice = request("GET", invoice_url)

        if not pay_invoice(invoice):
            return False

    return True


def renew_service(service_id):
    log("")
    log("=" * 50)
    log(f"🖥️ 处理服务 #{service_id}")

    manage_url = f"/service/{service_id}/manage"

    manage = request("GET", manage_url)

    csrf = get_csrf(manage.text)
    old_due = get_due_date(manage.text)

    log(f"📅 当前到期时间：{old_due}")

    if not csrf:
        log("❌ 没找到 CSRF Token，页面结构可能已变化。")
        return False

    log(f"🔄 尝试续期 {RENEW_DAYS} 天……")

    renew = request(
        "POST",
        f"/service/{service_id}/renew",
        data={
            "_token": csrf,
            "days": str(RENEW_DAYS),
        },
        headers={
            "Referer": urljoin(BASE_URL, manage_url),
            "X-CSRF-TOKEN": csrf,
        },
    )

    page_text = BeautifulSoup(
        renew.text,
        "html.parser"
    ).get_text(" ", strip=True).lower()

    if (
        "renewal restricted" in page_text
        or "can only renew" in page_text
        or "not eligible" in page_text
    ):
        log("⏳ 还没到允许续期的时间。")
        return True

    # 如果直接进入账单页面
    if "/invoice/" in renew.url or "/payment/invoice/" in renew.url:
        if not pay_invoice(renew):
            return False

    else:
        log("🔎 检查是否生成了未支付账单……")
        check_unpaid_invoice(service_id)

    # 最后重新读取到期时间
    final_page = request(
        "GET",
        manage_url,
    )

    new_due = get_due_date(final_page.text)

    log(f"📅 处理后的到期时间：{new_due}")

    if old_due != "未知" and new_due != "未知":
        if old_due != new_due:
            log("🎉 续期成功，到期时间已经变化。")
        else:
            log("ℹ️ 到期时间没有变化，可能尚未到续期时间。")

    return True


def main():
    log("==========================================")
    log(" HidenCloud Auto Renew")
    log("==========================================")

    if not COOKIE_VALUE:
        log("❌ GitHub Secret COOKIE_VALUE 没有读取到。")
        sys.exit(1)

    log(f"👤 账号：{mask_email(EMAIL)}")

    # 注入 remember cookie
    session.cookies.set(
        REMEMBER_COOKIE_NAME,
        COOKIE_VALUE,
        domain="dash.hidencloud.com",
        path="/",
    )

    log("🔐 正在使用 Cookie 登录……")

    dashboard = request(
        "GET",
        "/dashboard",
    )

    if (
        "/auth/login" in dashboard.url
        or "/login" in dashboard.url
    ):
        log("❌ COOKIE_VALUE 已失效，请重新获取 remember_web Cookie。")
        sys.exit(1)

    services = find_services(
        dashboard.text
    )

    if not services:
        log("❌ 登录后没有找到任何服务器。")
        log("可能是 Cookie 无效，或者 HidenCloud 页面结构发生变化。")
        sys.exit(1)

    log(f"✅ 登录成功，找到 {len(services)} 个服务：")
    log(", ".join(f"#{x}" for x in services))

    success = True

    for service_id in services:
        try:
            if not renew_service(service_id):
                success = False

        except Exception as exc:
            success = False
            log(f"❌ 服务 #{service_id} 出错：{exc}")

    log("")
    log("=" * 50)

    if success:
        log("✅ 本次检查完成。")
        sys.exit(0)

    log("❌ 本次运行存在失败项目，请查看上面的日志。")
    sys.exit(1)


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        log(f"❌ 程序异常：{exc}")
        sys.exit(1)
