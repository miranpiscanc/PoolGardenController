import json
import threading
import urllib.error
import urllib.request


class NotificationService:
    def __init__(self, timeout_seconds=8):
        self.timeout_seconds = timeout_seconds

    def send_notification(self, cfg, message, async_send=True):
        notifications = self._telegram_config(cfg)
        if not notifications.get("enabled"):
            return {"ok": True, "skipped": True, "error": ""}
        if async_send:
            threading.Thread(
                target=self._send_telegram_safely,
                args=(notifications, message),
                daemon=True
            ).start()
            return {"ok": True, "queued": True, "error": ""}
        return self._send_telegram(notifications, message)

    def test_connection(self, cfg, message):
        notifications = self._telegram_config(cfg)
        return self._send_telegram(notifications, message)

    def _telegram_config(self, cfg):
        notifications = cfg.get("notifications") if isinstance(cfg.get("notifications"), dict) else {}
        telegram = notifications.get("telegram") if isinstance(notifications.get("telegram"), dict) else {}
        return {
            "enabled": bool(telegram.get("enabled")),
            "bot_token": str(telegram.get("bot_token") or "").strip(),
            "chat_id": str(telegram.get("chat_id") or "").strip()
        }

    def _send_telegram_safely(self, telegram, message):
        try:
            self._send_telegram(telegram, message)
        except Exception:
            pass

    def _send_telegram(self, telegram, message):
        token = telegram.get("bot_token")
        chat_id = telegram.get("chat_id")
        if not token:
            return {"ok": False, "error": "Telegram bot token is missing."}
        if not chat_id:
            return {"ok": False, "error": "Telegram chat ID is missing."}

        payload = json.dumps({
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True
        }).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
                description = data.get("description") or body
            except Exception:
                description = body or str(exc)
            return {"ok": False, "error": description}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        if data.get("ok"):
            return {"ok": True, "error": ""}
        return {"ok": False, "error": data.get("description") or "Telegram API returned an error."}
