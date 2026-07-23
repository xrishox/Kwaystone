"""Fail-closed live ratio validation for one selected arbitrage loop."""

from __future__ import annotations

import logging
import time
import threading
from collections.abc import Callable

from poed import currency_exchange_scan
from poed.image_geometry import frame_source
from poed.screencast import ScreenCast

_LOG = logging.getLogger("waystone.arb_monitor")
_MIN_PROCESS_INTERVAL = 0.45
_MAX_VISUAL_DISTANCE = 0.08
_SUSPICIOUS_IMPROVEMENT = 1.15
_SUSPICIOUS_CONFIRMATIONS = 3
_UNCHANGED_REFRESH_SECONDS = 15.0


class LiveArbMonitor:
    """Validate a stream against one loop and emit only proven observations."""

    def __init__(
        self,
        brain,
        desktop,
        league: str,
        on_state: Callable[[str, str], None],
        on_observation: Callable[[dict], None],
        *,
        source_factory=ScreenCast,
    ):
        self._brain = brain
        self._desktop = desktop
        self._league = league
        self._on_state = on_state
        self._on_observation = on_observation
        self._source_factory = source_factory
        self._source = None
        self._allowed_ids: list[str] = []
        self._known_items: list[dict] = []
        self._allowed_pairs: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._pending = None
        self._worker_running = False
        self._stopped = True
        self._last_processed_at = 0.0
        self._evidence = None
        self._accepted = None
        self._loop_directions: dict[frozenset[str], tuple[str, str]] = {}
        self._accepted_rates: dict[frozenset[str], float] = {}
        self._emitted: dict[tuple[str, str], tuple[float, float]] = {}
        self._last_sequence = 0
        self._state = "off"

    def start(self, loop: dict) -> None:
        path = list(loop.get("path") or [])
        allowed_ids = list(
            dict.fromkeys(str(item.get("apiId") or "") for item in path)
        )
        allowed_ids = [api_id for api_id in allowed_ids if api_id]
        if len(allowed_ids) != 3:
            raise RuntimeError("live monitoring requires a complete three-item loop")
        self.stop(emit=False)
        self._allowed_ids = allowed_ids
        self._known_items = [dict(item) for item in path if isinstance(item, dict)]
        self._allowed_pairs = {
            (left, right)
            for left in allowed_ids
            for right in allowed_ids
            if left != right
        }
        self._loop_directions = {}
        self._accepted_rates = {}
        for leg in loop.get("legs") or []:
            from_id = str((leg.get("from") or {}).get("apiId") or "")
            to_id = str((leg.get("to") or {}).get("apiId") or "")
            rate = float(leg.get("rate") or 0)
            if from_id and to_id and rate > 0:
                key = frozenset((from_id, to_id))
                self._loop_directions[key] = (from_id, to_id)
                self._accepted_rates[key] = rate
        self._emitted = {}
        with self._lock:
            self._stopped = False
            self._pending = None
            self._worker_running = False
            self._last_processed_at = 0.0
            self._evidence = None
            self._accepted = None
            self._last_sequence = 0
        self._set_state("starting", "Choose the game monitor once if prompted")
        source = self._source_factory(
            self._on_frame,
            lambda: self._set_state("verifying", "Waiting for a stable exchange pair"),
            self._source_error,
        )
        self._source = source
        source.start()

    def stop(self, *, emit: bool = True) -> None:
        with self._lock:
            self._stopped = True
            self._pending = None
            self._evidence = None
            source = self._source
            self._source = None
        if source is not None:
            source.stop()
        if emit:
            self._set_state("off", "Live monitoring stopped")

    def is_active(self) -> bool:
        with self._lock:
            return not self._stopped

    def _source_error(self, message: str) -> None:
        self._reset_evidence()
        self._set_state("unavailable", message)

    def _on_frame(self, frame, sequence: int, captured_at: float) -> None:
        with self._lock:
            if self._stopped or sequence <= self._last_sequence:
                return
            self._last_sequence = sequence
            if captured_at - self._last_processed_at < _MIN_PROCESS_INTERVAL:
                return
            self._last_processed_at = captured_at
            self._pending = (frame, sequence)
            if self._worker_running:
                return
            self._worker_running = True
        threading.Thread(
            target=self._worker,
            name="waystone-arb-monitor",
            daemon=True,
        ).start()

    def _worker(self) -> None:
        while True:
            with self._lock:
                pending = self._pending
                self._pending = None
                if self._stopped or pending is None:
                    self._worker_running = False
                    return
            frame, sequence = pending
            self._process(frame, sequence)

    def _process(self, monitor_frame, sequence: int) -> None:
        try:
            if not self._desktop.is_game_focused():
                self._reset_evidence()
                self._set_state("paused", "Return focus to Path of Exile 2")
                return
            output = self._desktop.active_game_output()
            if output is None:
                raise RuntimeError("active game monitor is unavailable")
            rect = self._desktop.active_game_rect(
                output, (monitor_frame.shape[1], monitor_frame.shape[0])
            )
            game_frame, _x, _y, _source = frame_source(monitor_frame, rect)
            read = currency_exchange_scan.read_live_frame(game_frame)
            resolved = self._brain.request(
                {
                    "cmd": "arbresolvelive",
                    "league": self._league,
                    "allowedApiIds": self._allowed_ids,
                    "knownItems": self._known_items,
                    "wantText": read.want_text,
                    "haveText": read.have_text,
                    "wantAmount": read.want_amount,
                    "haveAmount": read.have_amount,
                    "observedAt": read.observed_at,
                },
                timeout=5.0,
            )
            observation = resolved.get("observation")
            if not isinstance(observation, dict):
                raise RuntimeError("live resolver returned no observation")
            pair = (
                str((observation.get("have") or {}).get("apiId") or ""),
                str((observation.get("want") or {}).get("apiId") or ""),
            )
            if pair not in self._allowed_pairs:
                raise RuntimeError("visible pair is outside the selected loop")
            candidate = {
                "sequence": sequence,
                "pair": pair,
                "wantVisual": read.want_visual,
                "haveVisual": read.have_visual,
                "observation": observation,
            }
            if not self._confirm(candidate):
                self._set_state("verifying", "Confirming the visible exchange pair")
                return
            if not self._should_emit(observation, pair):
                return
            self._set_state(
                "tracking",
                f"Tracking {(observation.get('have') or {}).get('name', '?')} → "
                f"{(observation.get('want') or {}).get('name', '?')}",
            )
            self._on_observation(observation)
        except (RuntimeError, OSError, TimeoutError, KeyError, ValueError) as error:
            _LOG.debug("live ratio frame rejected: %s", error)
            self._reset_evidence()
            self._set_state("verifying", str(error))

    def _confirm(self, candidate: dict) -> bool:
        previous = self._evidence
        if previous is None or previous["pair"] != candidate["pair"]:
            candidate["confirmations"] = 1
            self._evidence = candidate
            return False
        want_distance = currency_exchange_scan.visual_distance(
            previous["wantVisual"], candidate["wantVisual"]
        )
        have_distance = currency_exchange_scan.visual_distance(
            previous["haveVisual"], candidate["haveVisual"]
        )
        if max(want_distance, have_distance) > _MAX_VISUAL_DISTANCE:
            candidate["confirmations"] = 1
            self._evidence = candidate
            return False
        pair_key = frozenset(candidate["pair"])
        loop_rate = self._loop_rate(candidate["observation"], candidate["pair"])
        baseline = self._accepted_rates.get(pair_key)
        suspicious = baseline is not None and loop_rate > baseline * _SUSPICIOUS_IMPROVEMENT
        if suspicious:
            previous_rate = self._loop_rate(previous["observation"], previous["pair"])
            if abs(loop_rate - previous_rate) > max(loop_rate, previous_rate) * 0.02:
                candidate["confirmations"] = 1
            else:
                candidate["confirmations"] = int(previous.get("confirmations") or 1) + 1
            self._evidence = candidate
            if candidate["confirmations"] < _SUSPICIOUS_CONFIRMATIONS:
                return False
        self._evidence = candidate
        self._accepted = candidate
        self._accepted_rates[pair_key] = loop_rate
        return True

    def _loop_rate(self, observation: dict, pair: tuple[str, str]) -> float:
        rate = float(observation.get("rate") or 0)
        direction = self._loop_directions.get(frozenset(pair))
        if not direction or direction == pair:
            return rate
        return 1 / rate

    def _should_emit(self, observation: dict, pair: tuple[str, str]) -> bool:
        rate = float(observation.get("rate") or 0)
        now = time.monotonic()
        previous = self._emitted.get(pair)
        if previous is not None:
            old_rate, emitted_at = previous
            relative = abs(rate - old_rate) / max(rate, old_rate)
            if relative < 0.0005 and now - emitted_at < _UNCHANGED_REFRESH_SECONDS:
                return False
        self._emitted[pair] = (rate, now)
        return True

    def _reset_evidence(self) -> None:
        with self._lock:
            self._evidence = None

    def _set_state(self, state: str, detail: str) -> None:
        with self._lock:
            if self._stopped and state != "off":
                return
            if state == self._state and state not in {"tracking", "verifying"}:
                return
            self._state = state
        self._on_state(state, detail)
