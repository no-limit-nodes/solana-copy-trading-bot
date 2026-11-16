"""Trade notifications: console, JSON lines and Telegram."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger("notify")


class Notifier:
    def __init__(self, json_lines: bool = False, quiet: bool = False,
                 telegram_token: str = "", telegram_chat: str = ""):
        self.json_lines = json_lines
        self.quiet = quiet
        self.telegram: Optional[_Telegram] = None
        if telegram_token and telegram_chat:
            self.telegram = _Telegram(telegram_token, telegram_chat)

    def send(self, kind: str, text: str, data: Optional[dict] = None) -> None:
        if not self.quiet:
            if self.json_lines:
                print(json.dumps({"kind": kind, "text": text, **(data or {})}), flush=True)
            else:
                print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)
        if self.telegram:
            self.telegram.put(text)


class _Telegram:
    """Background sender: one message per second, honors ``retry_after`` on 429."""

    def __init__(self, token: str, chat_id: str, max_queue: int = 200):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.q: "queue.Queue[str]" = queue.Queue(maxsize=max_queue)
        threading.Thread(target=self._run, daemon=True, name="telegram").start()

    def put(self, text: str) -> None:
        try:
            self.q.put_nowait(text)
        except queue.Full:
            log.warning("telegram queue full; dropping message")

    def _post(self, text: str) -> None:
        body = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text,
                                       "disable_web_page_preview": "true"}).encode()
        for _ in range(3):
            try:
                urllib.request.urlopen(urllib.request.Request(self.url, data=body), timeout=10).read()
                return
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    try:
                        wait = json.loads(exc.read()).get("parameters", {}).get("retry_after", 5)
                    except ValueError:
                        wait = 5
                    time.sleep(float(wait))
                    continue
                log.warning("telegram HTTP %s: %s", exc.code, exc.read()[:200])
                return
            except (urllib.error.URLError, TimeoutError) as exc:
                log.warning("telegram send failed: %s", exc)
                time.sleep(2)

    def _run(self) -> None:
        while True:
            self._post(self.q.get())
            time.sleep(1.0)
