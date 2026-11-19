"""Twitch-Chat anonym per IRC lesen und Spikes / Lach-Wellen erkennen."""
from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("chat")
HOST, PORT = "irc.chat.twitch.tv", 6667
STRIP = ".,!?:;\"'()[]"


@dataclass
class ChanStats:
    msgs: int = 0
    laughs: int = 0
    hist_msgs: deque = field(default_factory=deque)
    hist_laughs: deque = field(default_factory=deque)


class ChatWatcher(threading.Thread):
    """Eine IRC-Verbindung fuer alle Kanaele; ruft on_trigger(login, moment, reason, strong) auf."""

    def __init__(self, settings: dict, on_trigger: Callable[[str, float, str, bool], None]):
        super().__init__(name="chat", daemon=True)
        C = settings["trigger"]["chat"]
        self.C = C
        self.laugh = {e.upper() for e in C["laugh_emotes"]}
        self.on_trigger = on_trigger
        self.want: set[str] = set()
        self.joined: set[str] = set()
        self.stats: dict[str, ChanStats] = {}
        self.lock = threading.Lock()
        self._halt = threading.Event()

    # ------------------------------------------------------------------ API

    def set_channels(self, logins: set[str]):
        with self.lock:
            self.want = set(logins)

    def stop(self):
        self._halt.set()

    def snapshot(self) -> dict[str, str]:
        with self.lock:
            out = {}
            for ch, s in self.stats.items():
                base = sum(s.hist_msgs) / len(s.hist_msgs) if s.hist_msgs else 0
                out[ch] = f"{s.msgs} msg im aktuellen Fenster, Schnitt {base:.0f}/{self.C['window_s']}s"
            return out

    # ------------------------------------------------------------------ Loop

    def run(self):
        backoff = 2
        while not self._halt.is_set():
            try:
                self._session()
                backoff = 2
            except Exception as e:
                log.warning("IRC getrennt: %s -> neu verbinden in %ss", e, backoff)
            self.joined = set()
            self._halt.wait(backoff)
            backoff = min(backoff * 2, 60)

    def _session(self):
        sock = socket.create_connection((HOST, PORT), timeout=15)
        sock.settimeout(1.0)
        send = lambda line: sock.sendall((line + "\r\n").encode("utf-8"))
        send("PASS SCHMOOPIIE")
        send("NICK justinfan%d" % (int(time.time()) % 90000 + 10000))
        log.info("IRC verbunden")
        buf = b""
        win = float(self.C["window_s"])
        next_roll = (time.time() // win + 1) * win
        last_rx = time.time()
        try:
            while not self._halt.is_set():
                self._sync_joins(send)
                try:
                    data = sock.recv(65536)
                    if not data:
                        raise ConnectionError("Verbindung geschlossen")
                    last_rx = time.time()
                    buf += data
                    *lines, buf = buf.split(b"\r\n")
                    for raw in lines:
                        self._on_line(raw.decode("utf-8", "replace"), send)
                except socket.timeout:
                    if time.time() - last_rx > 360:
                        raise ConnectionError("keine Daten seit 6 Min")
                now = time.time()
                if now >= next_roll:
                    self._roll(next_roll)
                    next_roll += win
                    if now - next_roll > win:  # nach Haenger nicht nachholen
                        next_roll = (now // win + 1) * win
        finally:
            sock.close()

    def _sync_joins(self, send):
        with self.lock:
            want = set(self.want)
        for ch in want - self.joined:
            send(f"JOIN #{ch}")
            self.joined.add(ch)
            with self.lock:
                self.stats[ch] = ChanStats()
            log.info("Chat: #%s beigetreten", ch)
        for ch in self.joined - want:
            send(f"PART #{ch}")
            self.joined.discard(ch)
            with self.lock:
                self.stats.pop(ch, None)
            log.info("Chat: #%s verlassen", ch)

    def _on_line(self, line: str, send):
        if line.startswith("PING"):
            send("PONG" + line[4:])
            return
        if " PRIVMSG #" not in line:
            return
        try:
            _, rest = line.split(" PRIVMSG #", 1)
            ch, text = rest.split(" :", 1)
        except ValueError:
            return
        tokens = {t.strip(STRIP).upper() for t in text.split()}
        with self.lock:
            s = self.stats.get(ch.lower())
            if s is None:
                return
            s.msgs += 1
            if tokens & self.laugh:
                s.laughs += 1

    def _roll(self, window_end: float):
        C = self.C
        fired = []
        with self.lock:
            for ch, s in self.stats.items():
                n, lg = s.msgs, s.laughs
                reason, strong = None, False
                if len(s.hist_msgs) >= C["warmup_windows"]:
                    base = sum(s.hist_msgs) / len(s.hist_msgs)
                    base_l = sum(s.hist_laughs) / len(s.hist_laughs)
                    ratio = n / max(base, 1.0)
                    if n >= C["min_msgs"] and ratio >= C["spike_ratio"]:
                        reason = f"Chat-Spike {ratio:.1f}x ({n} Nachr./{C['window_s']}s, Schnitt {base:.0f})"
                        strong = ratio >= C.get("strong_ratio", 5.0)
                    elif (lg >= C["laugh_min"] and n and lg / n >= C["laugh_ratio"]
                          and lg >= C["laugh_vs_baseline"] * max(base_l, 1.0)):
                        reason = f"Lach-Welle: {lg}/{n} Nachr. mit Lach-Emotes"
                        strong = lg >= C.get("laugh_strong_min", 25)
                if reason:
                    fired.append((ch, reason, strong))
                s.hist_msgs.append(n)
                s.hist_laughs.append(lg)
                while len(s.hist_msgs) > C["history_windows"]:
                    s.hist_msgs.popleft()
                    s.hist_laughs.popleft()
                s.msgs = s.laughs = 0
        for ch, reason, strong in fired:
            moment = window_end - float(C["delay_s"])
            try:
                self.on_trigger(ch, moment, reason, strong)
            except Exception:
                log.exception("on_trigger")
