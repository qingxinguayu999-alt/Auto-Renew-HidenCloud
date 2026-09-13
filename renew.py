import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://dash.hidencloud.com"
TIMEZONE = ZoneInfo("Asia/Shanghai")
RENEW_DAYS = 10

REMEMBER_COOKIE_NAME = (
    "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"
)

COOKIE_VALUE = os.getenv("COOKIE_VALUE", "").strip()
EMAIL = os.getenv("EMAIL", "").strip()
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()


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


def send_tg(message):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("⚠️ Telegram 未配置，跳过通知。")
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            data={
                "chat_id": TG_CHAT_ID,
                "text": message,
            },
            timeout=20,
        )

        data = response.json()

        if data.get("ok"):
            log("📨 Telegram 通知发送成功。")
            return True

        log(
            "❌ Telegram 通知失败："
            + str(data.get("description", "未知错误"))
        )

        return False

    except Exception as exc:
        log(f"❌ Telegram 通知异常：{exc}")
        return False


def fatal(message):
    log(f"❌ {message}")

    send_tg(
        "❌ HidenCloud 自动续期异常\n"
        f"账号：{mask_email(EMAIL)}\n"
        f"原因：{message}"
    )

    sys.exit(1)


def check_cloudflare(response):
    text = response.text.lower()

    if (
        response.status_code in (403, 503)
        or "challenges.cloudflare.com" in text
        or "just a moment..." in text
        or "cf-chl-" in text
    ):
        raise RuntimeError(
            "遇到 Cloudflare 验证，无法继续。"
        )


def request(method, path, **kwargs):
    url = (
        path
        if path.startswith("http")
        else urljoin(BASE_URL, path)
    )

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


def get_meta_csrf(html):
    soup = BeautifulSoup(html, "html.parser")

    meta = soup.find(
        "meta",
        attrs={"name": "csrf-token"},
    )

    if meta and meta.get("content"):
        return meta["content"]

    return ""


def get_form_csrf(html):
    soup = BeautifulSoup(html, "html.parser")

    field = soup.find(
        "input",
        attrs={"name": "_token"},
    )

    if field and field.get("value"):
        return field["value"]

    return ""


def find_services(html):
    service_ids = set()

    soup = BeautifulSoup(html, "html.parser")

    for link in soup.find_all("a", href=True):
        match = re.search(
            r"/service/(\d+)/manage",
            link["href"],
        )

        if match:
            service_ids.add(match.group(1))

    return sorted(service_ids)


def parse_date_string(value):
    value = value.strip().replace(",", "")

    formats = [
        "%Y/%m/%d",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d %Y",
        "%B %d %Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(
                value,
                fmt,
            ).date()

        except ValueError:
            pass

    return None


def get_due_date(html):
    text = BeautifulSoup(
        html,
        "html.parser",
    ).get_text(" ", strip=True)

    label = (
        r"(?:"
        r"Due\s*Date|"
        r"Expiry(?:\s*Date)?|"
        r"Expiration(?:\s*Date)?|"
        r"Expires?|"
        r"到期(?:时间|日期)?|"
        r"有效期"
        r")"
    )

    date_value = (
        r"("
        r"20\d{2}[/-]\d{1,2}[/-]\d{1,2}"
        r"|"
        r"\d{1,2}[/-]\d{1,2}[/-]20\d{2}"
        r"|"
        r"\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2}"
        r"|"
        r"[A-Za-z]{3,9}\s+\d{1,2},?\s+20\d{2}"
        r")"
    )

    patterns = [
        label + r".{0,50}?" + date_value,
        date_value + r".{0,50}?" + label,
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.I,
        )

        if match:
            # 两种正则的日期分组位置不同
            candidates = [
                item
                for item in match.groups()
                if item
                and re.search(r"20\d{2}", item)
            ]

            for candidate in candidates:
                parsed = parse_date_string(candidate)

                if parsed:
                    return parsed

    return None


def find_payment(html, current_url):
    soup = BeautifulSoup(html, "html.parser")

    for form in soup.find_all("form"):
        action = form.get("action")

        button_text = " ".join(
            button.get_text(" ", strip=True)
            for button in form.find_all("button")
        ).lower()

        if (
            action
            and "pay" in button_text
            and "balance/add" not in action
        ):
            data = {}

            for field in form.find_all("input"):
                name = field.get("name")

                if name:
                    data[name] = field.get(
                        "value",
                        "",
                    )

            return {
                "type": "form",
                "url": urljoin(
                    current_url,
                    action,
                ),
                "data": data,
            }

    for link in soup.find_all("a", href=True):
        link_text = link.get_text(
            " ",
            strip=True,
        ).lower()

        if (
            link_text == "pay"
            or link_text.startswith("pay ")
        ):
            return {
                "type": "link",
                "url": urljoin(
                    current_url,
                    link["href"],
                ),
            }

    return None


def pay_invoice(response, csrf_header):
    payment = find_payment(
        response.text,
        response.url,
    )

    if not payment:
        log("⚪ 没找到支付按钮，可能已经支付。")
        return True

    log("💳 找到账单支付入口……")

    headers = {
        "Referer": response.url,
    }

    if csrf_header:
        headers["X-CSRF-TOKEN"] = csrf_header

    if payment["type"] == "form":
        result = request(
            "POST",
            payment["url"],
            data=payment["data"],
            headers=headers,
        )

    else:
        result = request(
            "GET",
            payment["url"],
            headers=headers,
        )

    if result.status_code >= 400:
        log("❌ 支付请求失败。")
        return False

    log("✅ 支付请求完成。")
    return True


def check_unpaid_invoices(service_id, csrf_header):
    response = request(
        "GET",
        f"/service/{service_id}/invoices?where=unpaid",
    )

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    urls = []

    for link in soup.find_all("a", href=True):
        href = link["href"]

        if (
            "/invoice/" in href
            and "download" not in href
        ):
            urls.append(
                urljoin(
                    response.url,
                    href,
                )
            )

    urls = list(dict.fromkeys(urls))

    if not urls:
        log("⚪ 没发现未支付账单。")
        return True

    log(f"📄 找到 {len(urls)} 个未支付账单。")

    for invoice_url in urls:
        invoice = request(
            "GET",
            invoice_url,
        )

        if not pay_invoice(
            invoice,
            csrf_header,
        ):
            return False

    return True


def renew_service(service_id, csrf_header):
    log("")
    log("=" * 50)
    log(f"🖥️ 服务 #{service_id}")

    result = {
        "service_id": service_id,
        "status": "skip",
        "old_due": None,
        "new_due": None,
        "message": "",
        "notify": False,
        "success": True,
    }

    manage_url = f"/service/{service_id}/manage"

    manage = request(
        "GET",
        manage_url,
    )

    due_date = get_due_date(
        manage.text
    )

    if not due_date:
        result["status"] = "failed"
        result["success"] = False
        result["notify"] = True
        result["message"] = "无法识别服务到期日期"

        log("❌ 无法识别服务到期日期。")
        return result

    result["old_due"] = due_date
    result["new_due"] = due_date

    today = datetime.now(
        TIMEZONE
    ).date()

    target_date = (
        due_date - timedelta(days=1)
    )

    log(
        f"📅 当前到期日：{due_date.strftime('%Y/%m/%d')}"
    )

    log(
        f"🎯 计划续期日：{target_date.strftime('%Y/%m/%d')} 23:00"
    )

    log(
        f"🕐 今天：{today.strftime('%Y/%m/%d')}"
    )

    # 还没到目标日期
    if today < target_date:
        result["status"] = "skip"
        result["message"] = "还没到续期日期"

        log("🔕 还没到续期日期，本次不续期。")
        return result

    # 已经过期
    if today > due_date:
        result["status"] = "failed"
        result["success"] = False
        result["notify"] = True
        result["message"] = "检测到服务到期日已经过去"

        log("❌ 服务到期日已经过去。")
        return result

    # 到期日当天仍允许补救尝试
    if today == due_date:
        log("⚠️ 已到到期日，执行补救续期。")

    else:
        log("✅ 今天是到期前一天，开始续期。")

    form_token = get_form_csrf(
        manage.text
    )

    if not form_token:
        result["status"] = "failed"
        result["success"] = False
        result["notify"] = True
        result["message"] = "没有找到 CSRF Token"

        log("❌ 没找到 CSRF Token。")
        return result

    headers = {
        "Referer": urljoin(
            BASE_URL,
            manage_url,
        ),
    }

    if csrf_header:
        headers["X-CSRF-TOKEN"] = csrf_header

    log(
        f"🔄 正在提交 {RENEW_DAYS} 天续期……"
    )

    renew = request(
        "POST",
        f"/service/{service_id}/renew",
        data={
            "_token": form_token,
            "days": str(RENEW_DAYS),
        },
        headers=headers,
    )

    page_text = BeautifulSoup(
        renew.text,
        "html.parser",
    ).get_text(
        " ",
        strip=True,
    ).lower()

    restricted_words = [
        "renewal restricted",
        "can only renew",
        "not eligible",
        "too early",
    ]

    if any(
        word in page_text
        for word in restricted_words
    ):
        result["status"] = "restricted"
        result["notify"] = True
        result["message"] = (
            "HidenCloud 暂未开放续期，请稍后手动再运行一次"
        )

        log("⏳ 当前暂时不允许续期。")
        return result

    if (
        "/invoice/" in renew.url
        or "/payment/invoice/" in renew.url
    ):
        if not pay_invoice(
            renew,
            csrf_header,
        ):
            result["status"] = "failed"
            result["success"] = False
            result["notify"] = True
            result["message"] = "账单支付失败"
            return result

    else:
        log("🔎 检查未支付账单……")

        if not check_unpaid_invoices(
            service_id,
            csrf_header,
        ):
            result["status"] = "failed"
            result["success"] = False
            result["notify"] = True
            result["message"] = "账单处理失败"
            return result

    final_page = request(
        "GET",
        manage_url,
    )

    new_due_date = get_due_date(
        final_page.text
    )

    if new_due_date:
        result["new_due"] = new_due_date

    if (
        new_due_date
        and new_due_date != due_date
    ):
        result["status"] = "renewed"
        result["notify"] = True
        result["message"] = "续期成功，到期日期已更新"

        log(
            "🎉 续期成功："
            f"{due_date.strftime('%Y/%m/%d')}"
            " → "
            f"{new_due_date.strftime('%Y/%m/%d')}"
        )

        return result

    result["status"] = "warning"
    result["notify"] = True
    result["message"] = (
        "已执行续期流程，但到期日期没有变化，请检查"
    )

    log("⚠️ 执行后到期日期没有变化。")

    return result


def date_text(value):
    if not value:
        return "未知"

    return value.strftime("%Y/%m/%d")


def build_notification(results):
    important = [
        item
        for item in results
        if item["notify"]
    ]

    if not important:
        return None

    if any(
        not item["success"]
        for item in important
    ):
        title = "❌ HidenCloud 自动续期异常"

    elif any(
        item["status"] == "renewed"
        for item in important
    ):
        title = "🎉 HidenCloud 续期成功"

    else:
        title = "⚠️ HidenCloud 续期提醒"

    lines = [
        title,
        f"账号：{mask_email(EMAIL)}",
        "",
    ]

    for item in important:
        lines.append(
            f"服务：#{item['service_id']}"
        )

        if item["status"] == "renewed":
            lines.append("状态：🎉 续期成功")

        elif item["status"] == "restricted":
            lines.append("状态：⏳ 暂未允许续期")

        elif item["status"] == "failed":
            lines.append("状态：❌ 失败")

        else:
            lines.append("状态：⚠️ 需要检查")

        lines.append(
            f"原到期：{date_text(item['old_due'])}"
        )

        lines.append(
            f"新到期：{date_text(item['new_due'])}"
        )

        lines.append(
            f"说明：{item['message']}"
        )

        lines.append("")

    return "\n".join(lines).strip()


def main():
    log("=" * 50)
    log(" HidenCloud 到期前一天自动续期")
    log("=" * 50)

    now = datetime.now(TIMEZONE)

    log(
        "🕐 北京时间："
        + now.strftime("%Y/%m/%d %H:%M:%S")
    )

    if not COOKIE_VALUE:
        fatal(
            "GitHub Secret COOKIE_VALUE 没有读取到。"
        )

    log(
        f"👤 账号：{mask_email(EMAIL)}"
    )

    session.cookies.set(
        REMEMBER_COOKIE_NAME,
        COOKIE_VALUE,
        domain="dash.hidencloud.com",
        path="/",
    )

    log("🔐 正在验证 HidenCloud 登录状态……")

    dashboard = request(
        "GET",
        "/dashboard",
    )

    if (
        "/auth/login" in dashboard.url
        or "/login" in dashboard.url
    ):
        fatal(
            "COOKIE_VALUE 已失效，请重新获取 remember_web Cookie。"
        )

    csrf_header = get_meta_csrf(
        dashboard.text
    )

    services = find_services(
        dashboard.text
    )

    if not services:
        fatal(
            "登录成功后没有找到服务器。"
        )

    log(
        f"✅ 登录成功，找到 {len(services)} 个服务。"
    )

    results = []

    for service_id in services:
        try:
            result = renew_service(
                service_id,
                csrf_header,
            )

            results.append(result)

        except Exception as exc:
            log(
                f"❌ 服务 #{service_id} 异常：{exc}"
            )

            results.append({
                "service_id": service_id,
                "status": "failed",
                "old_due": None,
                "new_due": None,
                "message": str(exc),
                "notify": True,
                "success": False,
            })

    notification = build_notification(
        results
    )

    if notification:
        send_tg(notification)

    else:
        log(
            "🔕 今天不是续期日，"
            "不发送 Telegram。"
        )

    failed = any(
        not item["success"]
        for item in results
    )

    if failed:
        log("❌ 本次检查存在错误。")
        sys.exit(1)

    log("✅ 本次检查完成。")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        log(
            f"❌ 程序异常：{exc}"
        )

        send_tg(
            "❌ HidenCloud 程序异常\n"
            f"账号：{mask_email(EMAIL)}\n"
            f"错误：{exc}"
        )

        sys.exit(1)
