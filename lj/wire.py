"""lj.wire -- the protocol between the browser and the far side.

Two directions, two queues, exactly like the message channel but
carrying commands and their results:

    browser  --POST /cmd-->   COMMANDS   --poll-->  far side
    browser  <--poll /res--   RESULTS    <--POST--  far side

WHY POLLING AND NOT A HELD CONNECTION
--------------------------------------
The far side cannot be reached: it has no inbound port. So it dials
OUT to the listener and polls for work. A held connection would be
nice, but NAT tables expire silently and send no FIN, which turns a
dropped connection into a hang that nothing detects. Every request
here stands alone: a timeout costs one operation, not a session.

HOW A COMMAND GETS ITS ANSWER
------------------------------
1. The browser POSTs a command to /cmd. It gets a sequence number
   back.
2. The far side polls GET /cmd and receives the commands it has not
   seen yet, each with its sequence number.
3. The far side runs each one and POSTs the result to /res, carrying
   the sequence number.
4. The browser polls GET /res for everything newer than the last
   number it saw.

The sequence numbers are what make it work. The far side may answer
out of order -- a slow command started first, a fast one started
second -- and the browser may have several commands in flight at once.
A number says "everything after this one", which a uuid cannot.
"""
from __future__ import annotations

import itertools
import json
import queue
import threading

# The queues are bounded so a browser that never collects results
# cannot make the listener's memory grow without limit.
COMMAND_QUEUE_SIZE = 256
RESULT_QUEUE_SIZE = 1024


class Wire:
    """The command and result queues, with the bookkeeping that lets
    each side pick up only what it has not seen.

    Both sides poll, so the queues have to remember what has been
    handed out. Rather than a "seen" set that grows for the life of
    the process, each side remembers the last sequence number it
    handled: everything after that number is new.
    """

    def __init__(self) -> None:
        self._commands: queue.Queue = queue.Queue(maxsize=COMMAND_QUEUE_SIZE)
        self._results: list = []
        self._lock = threading.Lock()
        self._counter = itertools.count(1)

    # ---------------------------------------------------------- commands

    def put_command(self, op: str, args: dict) -> int:
        """Queue a command and return its sequence number."""
        seq = next(self._counter)
        payload = {"seq": seq, "op": op, "args": args}
        try:
            self._commands.put_nowait(payload)
        except queue.Full:
            # Drop the oldest so the newest still gets through: a stale
            # command is worth less than a fresh one.
            try:
                self._commands.get_nowait()
                self._commands.put_nowait(payload)
            except (queue.Empty, queue.Full):
                pass
        return seq

    def take_commands(self) -> list:
        """Commands the far side has not seen yet."""
        out = []
        while True:
            try:
                out.append(self._commands.get_nowait())
            except queue.Empty:
                break
        return out

    # ----------------------------------------------------------- results

    def put_result(self, result: dict) -> None:
        """Record a result from the far side."""
        with self._lock:
            self._results.append(result)
            # Keep the list bounded: drop the oldest when it grows.
            if len(self._results) > RESULT_QUEUE_SIZE:
                del self._results[:len(self._results) - RESULT_QUEUE_SIZE]

    def take_results(self, after: int = 0) -> list:
        """Results the browser has not collected yet."""
        with self._lock:
            fresh = [r for r in self._results
                     if isinstance(r.get("seq"), int) and r["seq"] > after]
            if fresh:
                # These have been handed over, so they can go.
                collected = max(r["seq"] for r in fresh)
                self._results = [r for r in self._results
                                 if not (isinstance(r.get("seq"), int)
                                         and r["seq"] <= collected)]
        return fresh

    def pending_results(self) -> int:
        with self._lock:
            return len(self._results)

    def pending_commands(self) -> int:
        return self._commands.qsize()


# One wire per process. The listener owns it; the far side talks to it
# over HTTP.
WIRE = Wire()


def encode(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def decode(raw: bytes) -> dict:
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}
