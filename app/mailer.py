import os
import json
import ssl
import smtplib
import urllib.request
import urllib.error
from email.message import EmailMessage


def _build_email_content(first_name, code):
    subject = f"Код подтверждения TheJobTree: {code}"
    text_body = (
        f"Здравствуйте, {first_name}!\n\n"
        f"Ваш код для завершения регистрации на платформе TheJobTree: {code}\n"
        f"Код действителен 10 минут.\n\n"
        f"Если вы не регистрировались на TheJobTree, просто проигнорируйте это письмо."
    )
    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                background: #131314; color: #e8eaed; padding: 32px; border-radius: 16px; max-width: 480px;">
        <h2 style="margin: 0 0 12px; color: #ff6b6b;">TheJobTree</h2>
        <p style="margin: 0 0 16px; color: #c4c7c5; font-size: 15px;">
            Здравствуйте, <strong>{first_name}</strong>! Введите этот код, чтобы завершить регистрацию:
        </p>
        <div style="background: #1e1f22; border: 1px solid #2c2e33; border-radius: 12px;
                    padding: 18px; text-align: center; font-size: 32px; font-weight: 800;
                    letter-spacing: 8px; color: #ffffff; margin: 20px 0;">
            {code}
        </div>
        <p style="margin: 0; color: #9aa0a6; font-size: 13px;">
            Код действителен в течение 10 минут. Никому его не сообщайте.
        </p>
    </div>
    """
    return subject, text_body, html_body


def _send_via_webhook(webhook_url, token, to_email, subject, text_body, html_body):
    payload = json.dumps({
        "token": token,
        "to": to_email,
        "subject": subject,
        "text": text_body,
        "html": html_body,
    }).encode("utf-8")

    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                result = json.loads(raw)
                if result.get("ok"):
                    return True, None
                return False, result.get("error", "Неизвестная ошибка почтового шлюза")
            except json.JSONDecodeError:
                return False, "Некорректный ответ от Google Apps Script (проверьте права доступа 'Все')"
    except urllib.error.URLError as err:
        return False, f"Ошибка HTTPS-подключения: {err}"
    except Exception as err:
        return False, str(err)


def send_verification_email(to_email, first_name, code):
    """
    Отправляет 6-значный код подтверждения.
    Приоритет: 1) HTTPS-шлюз Google Apps Script (не зависит от VPN) -> 2) SMTP -> 3) консоль.
    """
    subject, text_body, html_body = _build_email_content(first_name, code)

    # 1. Отправка по HTTPS через Google Apps Script (работает и с VPN, и без VPN)
    webhook_url = os.getenv("MAIL_WEBHOOK_URL", "").strip()
    webhook_token = os.getenv("MAIL_WEBHOOK_TOKEN", "").strip()
    if webhook_url:
        return _send_via_webhook(webhook_url, webhook_token, to_email, subject, text_body, html_body)

    # 2. Запасной вариант через SMTP
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    smtp_from = os.getenv("SMTP_FROM", smtp_user).strip()

    if not smtp_host or not smtp_user or not smtp_password:
        print("\n" + "=" * 56)
        print("  [TheJobTree · DEV-РЕЖИМ ПОЧТЫ]")
        print(f"  Кому: {to_email}")
        print(f"  Код подтверждения: {code}")
        print("=" * 56 + "\n")
        return False, None

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"TheJobTree <{smtp_from}>"
    msg["To"] = to_email
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    try:
        context = ssl.create_default_context()
        if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
            context.options |= ssl.OP_IGNORE_UNEXPECTED_EOF

        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=15) as server:
                server.ehlo()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
        return True, None
    except Exception as err:
        return False, str(err)