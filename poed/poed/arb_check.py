"""Alt+S target selection and Alt+A market-capture lifecycle."""

from __future__ import annotations

import copy
import json
import logging
import threading
import uuid
from collections.abc import Callable

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from poed import config, currency_exchange_scan  # noqa: E402

ArbPanel = None
LiveArbMonitor = None

_LOG = logging.getLogger("waystone.arb_check")
_CAPTURE_MAX_AGE_MS = 120_000


def _item_log(item: object) -> dict:
    if not isinstance(item, dict):
        return {}
    return {
        "id": str(item.get("apiId") or ""),
        "name": str(item.get("name") or ""),
    }


def _observation_log(observation: object) -> dict:
    if not isinstance(observation, dict):
        return {}
    return {
        "id": str(observation.get("id") or ""),
        "have": _item_log(observation.get("have")),
        "want": _item_log(observation.get("want")),
        "haveAmount": observation.get("haveAmount"),
        "wantAmount": observation.get("wantAmount"),
        "rate": observation.get("rate"),
        "observedAt": observation.get("observedAt"),
    }


def _analysis_log(answer: object, *, include_outcomes: bool) -> dict:
    if not isinstance(answer, dict):
        return {}
    loops = []
    for loop in answer.get("loops") or []:
        if not isinstance(loop, dict):
            continue
        entry = {
            "id": loop.get("id"),
            "path": [
                str(item.get("apiId") or "")
                for item in loop.get("path") or []
                if isinstance(item, dict)
            ],
            "status": loop.get("status"),
            "confidence": loop.get("estimateConfidence"),
            "stale": loop.get("stale"),
            "nominalPercent": loop.get("nominalPercent"),
            "executionPercent": loop.get("executionPercent"),
            "bufferedPercent": loop.get("bufferedPercent"),
            "actionable": loop.get("actionable"),
            "legs": [
                {
                    "from": str((leg.get("from") or {}).get("apiId") or ""),
                    "to": str((leg.get("to") or {}).get("apiId") or ""),
                    "rate": leg.get("rate"),
                    "executionRate": leg.get("executionRate"),
                    "source": leg.get("source"),
                    "inputAmount": leg.get("inputAmount"),
                    "outputAmount": leg.get("outputAmount"),
                    "observedAt": leg.get("observedAt"),
                    "scoutEvidence": leg.get("scoutEvidence"),
                }
                for leg in loop.get("legs") or []
                if isinstance(leg, dict)
            ],
        }
        if include_outcomes:
            outcomes = [
                point
                for point in loop.get("quantityOutcomes") or []
                if isinstance(point, dict)
            ]
            milestones = {1, 5, 10, 25, 50, 100}
            entry["quantityOutcomes"] = [
                {
                    "q": point.get("quantity"),
                    "nominalFinal": point.get("nominalFinalUnits"),
                    "executionFinal": point.get("executionFinalUnits"),
                    "bufferedFinal": point.get("bufferedFinalUnits"),
                    "nominalComplete": point.get("nominalComplete"),
                    "executionComplete": point.get("executionComplete"),
                    "bufferedComplete": point.get("bufferedComplete"),
                    "nominalPercent": point.get("nominalReturnPercent"),
                    "executionPercent": point.get("executionReturnPercent"),
                    "bufferedPercent": point.get("bufferedReturnPercent"),
                    "nominalBlockedStep": point.get("nominalBlockedStep"),
                    "executionBlockedStep": point.get("executionBlockedStep"),
                    "bufferedBlockedStep": point.get("bufferedBlockedStep"),
                    "nominalBlockedUnits": point.get("nominalBlockedUnits"),
                    "executionBlockedUnits": point.get("executionBlockedUnits"),
                    "bufferedBlockedUnits": point.get("bufferedBlockedUnits"),
                    "budgetBest": point.get("budgetBest"),
                    "localPeak": point.get("localPeak"),
                    "actionable": point.get("actionable"),
                }
                for point in outcomes
                if int(point.get("quantity") or 0) in milestones
                or point.get("budgetBest")
            ]
        loops.append(entry)
    return {
        "target": _item_log(answer.get("target")),
        "rates": {
            "epoch": answer.get("ratesEpoch"),
            "snapshotId": answer.get("ratesSnapshotId"),
            "fetchedAt": answer.get("ratesFetchedAt"),
            "ageMs": answer.get("ratesAgeMs"),
            "status": answer.get("ratesStatus"),
        },
        "safetyBufferBps": answer.get("safetyBufferBps"),
        "perLegSafetyBufferBps": answer.get("perLegSafetyBufferBps"),
        "executionConcessionBps": answer.get("executionConcessionBps"),
        "executionConcessionLoopPercent": answer.get(
            "executionConcessionLoopPercent"
        ),
        "loopsEvaluated": answer.get("loopsEvaluated"),
        "capturedCurrencyCount": answer.get("capturedCurrencyCount"),
        "unavailable": answer.get("unavailable") or [],
        "loops": loops,
    }


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


class ArbCheckController:
    """Owns the live screen observations and the docked arbitrage panel."""

    def __init__(self, application, cfg, brain, desktop, on_visibility_changed):
        self._application = application
        self._cfg = cfg
        self._brain = brain
        self._desktop = desktop
        self._on_visibility_changed = on_visibility_changed
        self._panel = None
        self._target: dict | None = None
        self._observations: list[dict] = []
        self._bridges: list[dict] = []
        self._pending_pair: dict | None = None
        self._side = "right"
        self._min_percent = float(cfg.get("arb_min_percent", 5.0))
        self._safety_buffer_percent = float(
            cfg.get("arb_safety_buffer_percent", 5.0)
        )
        self._execution_concession_percent = float(
            cfg.get("arb_execution_concession_percent", 5.0)
        )
        self._show_losing_candidates = bool(
            cfg.get("arb_show_losing_candidates", False)
        )
        self._threshold_save_source = 0
        self._buffer_save_source = 0
        self._concession_save_source = 0
        self._show_losing_save_source = 0
        self._analysis_seq = 0
        self._last_answer: dict | None = None
        self._previous_session: dict | None = None
        self._monitor = None
        self._monitor_loop_id: str | None = None
        self._monitor_quantity = 1
        self._monitor_safe: bool | None = None
        self._session_id: str | None = None

    def _ensure_panel(self):
        global ArbPanel
        if self._panel is None:
            if ArbPanel is None:
                from poed.arb_panel import ArbPanel as Panel  # noqa: PLC0415

                ArbPanel = Panel
            self._panel = ArbPanel(
                self._application,
                on_visibility=self._on_visibility_changed,
                on_close=self.hide,
                on_recalculate=self.recalculate,
                on_threshold=self._set_threshold,
                on_buffer=self._set_safety_buffer,
                on_concession=self._set_execution_concession,
                on_show_losing=self._set_show_losing_candidates,
                on_selection=self._selection_changed,
                min_percent=self._min_percent,
                safety_buffer_percent=self._safety_buffer_percent,
                execution_concession_percent=self._execution_concession_percent,
                show_losing_candidates=self._show_losing_candidates,
                desktop=self._desktop,
            )
        return self._panel

    def is_visible(self) -> bool:
        return bool(self._panel is not None and self._panel.is_visible())

    def has_session(self) -> bool:
        return self._target is not None

    def has_previous_session(self) -> bool:
        return self._previous_session is not None

    def sync_config(self, cfg) -> None:
        """Follow config objects replaced by the native league selector."""
        self._cfg = cfg

    def prepare_capture(self) -> bool:
        """GTK-thread hook: hide the panel before compositor capture."""
        self._stop_monitor()
        was_visible = self.is_visible()
        if was_visible and self._panel is not None:
            self._panel.hide()
        return was_visible

    def hide(self) -> None:
        self._stop_monitor()
        self._analysis_seq += 1
        if self._panel is not None:
            self._panel.hide()
        snapshot = self._session_snapshot()
        if snapshot is not None:
            self._previous_session = snapshot
            _LOG.info(
                "arb session archived session=%s target=%s observations=%d bridges=%d",
                self._session_id,
                str(self._target.get("apiId") or ""),
                len(self._observations),
                len(self._bridges),
            )
        self._target = None
        self._observations = []
        self._bridges = []
        self._pending_pair = None
        self._last_answer = None
        self._session_id = None

    def _session_snapshot(self) -> dict | None:
        if self._target is None:
            return None
        return {
            "session_id": self._session_id,
            "target": copy.deepcopy(self._target),
            "observations": copy.deepcopy(self._observations),
            "bridges": copy.deepcopy(self._bridges),
            "answer": copy.deepcopy(self._last_answer),
            "side": self._side,
        }

    def stop(self) -> None:
        self._stop_monitor()

    def toggle_monitor(self) -> None:
        """Start or stop live validation for the panel's selected loop."""
        global LiveArbMonitor
        if self._monitor is not None and self._monitor.is_active():
            _LOG.info("arb monitor stopped session=%s reason=user-toggle", self._session_id)
            self._stop_monitor()
            return
        if self._target is None or self._last_answer is None:
            self._ensure_panel().show_error(
                "Select and calculate an arbitrage loop before starting live monitoring",
                self._side,
                can_recalculate=self._target is not None,
            )
            return
        panel = self._ensure_panel()
        loop = panel.selected_loop()
        if not isinstance(loop, dict):
            panel.show_error("No complete loop is selected", self._side, can_recalculate=True)
            return
        if LiveArbMonitor is None:
            from poed.arb_monitor import LiveArbMonitor as Monitor  # noqa: PLC0415

            LiveArbMonitor = Monitor
        self._monitor_loop_id = str(loop.get("id") or "")
        self._monitor_quantity = panel.selected_quantity()
        self._monitor_safe = None
        monitor = LiveArbMonitor(
            self._brain,
            self._desktop,
            str(self._cfg["league"]),
            self._monitor_state_from_thread,
            self._monitor_observation_from_thread,
        )
        self._monitor = monitor
        try:
            _LOG.info(
                "arb monitor starting session=%s loop=%s quantity=%d legs=%s",
                self._session_id,
                self._monitor_loop_id,
                self._monitor_quantity,
                _json(
                    [
                        {
                            "from": str((leg.get("from") or {}).get("apiId") or ""),
                            "to": str((leg.get("to") or {}).get("apiId") or ""),
                            "rate": leg.get("rate"),
                            "source": leg.get("source"),
                        }
                        for leg in loop.get("legs") or []
                        if isinstance(leg, dict)
                    ]
                ),
            )
            monitor.start(loop)
        except (RuntimeError, OSError, ValueError) as error:
            _LOG.warning(
                "arb monitor failed session=%s loop=%s error=%s",
                self._session_id,
                self._monitor_loop_id,
                error,
            )
            self._monitor = None
            panel.set_monitor_state("off", "")
            panel.show_error(str(error), self._side, can_recalculate=True)

    def _stop_monitor(self) -> None:
        monitor = self._monitor
        self._monitor = None
        self._monitor_loop_id = None
        self._monitor_safe = None
        if monitor is not None:
            monitor.stop(emit=False)
        if self._panel is not None:
            self._panel.set_monitor_state("off", "")

    def _monitor_state_from_thread(self, state: str, detail: str) -> None:
        GLib.idle_add(self._apply_monitor_state, state, detail)

    def _apply_monitor_state(self, state: str, detail: str):
        if self._monitor is None:
            return GLib.SOURCE_REMOVE
        _LOG.info(
            "arb monitor state session=%s loop=%s state=%s detail=%s",
            self._session_id,
            self._monitor_loop_id,
            state,
            detail,
        )
        self._ensure_panel().set_monitor_state(state, detail)
        return GLib.SOURCE_REMOVE

    def _monitor_observation_from_thread(self, observation: dict) -> None:
        GLib.idle_add(self._apply_monitor_observation, copy.deepcopy(observation))

    def _apply_monitor_observation(self, observation: dict):
        if self._monitor is None or self._target is None:
            return GLib.SOURCE_REMOVE
        have_id = str((observation.get("have") or {}).get("apiId") or "")
        want_id = str((observation.get("want") or {}).get("apiId") or "")
        pair_ids = {have_id, want_id}
        if len(pair_ids) != 2:
            return GLib.SOURCE_REMOVE

        def other_direction(existing: dict) -> bool:
            return not (
                str((existing.get("have") or {}).get("apiId") or "") == have_id
                and str((existing.get("want") or {}).get("apiId") or "") == want_id
            )

        target_id = str(self._target.get("apiId") or "")
        if target_id in pair_ids:
            self._observations = [
                item for item in self._observations if other_direction(item)
            ]
            self._observations.append(observation)
        else:
            self._bridges = [item for item in self._bridges if other_direction(item)]
            self._bridges.append(observation)
        _LOG.info(
            "arb monitor capture accepted session=%s loop=%s observation=%s",
            self._session_id,
            self._monitor_loop_id,
            _json(_observation_log(observation)),
        )
        self._last_answer = None
        self._ensure_panel().set_monitor_state("tracking", "Recalculating the selected loop")
        self._analyze_async(reuse_rates=True)
        return GLib.SOURCE_REMOVE

    def start(self, gen: int, is_current: Callable[[int], bool]) -> None:
        self._capture_pair(gen, is_current, action="start")

    def add(self, gen: int, is_current: Callable[[int], bool]) -> None:
        if self._target is None:
            GLib.idle_add(self._show_error, gen, is_current, "No active arbitrage session")
            return
        self._capture_pair(gen, is_current, action="add")

    def _restore_snapshot(self, snapshot: dict, *, reuse_rates: bool = False) -> None:
        target = snapshot.get("target")
        if not isinstance(target, dict):
            return
        self._target = target
        self._session_id = str(snapshot.get("session_id") or uuid.uuid4().hex[:12])
        self._observations = list(snapshot.get("observations") or [])
        self._bridges = list(snapshot.get("bridges") or [])
        self._side = str(snapshot.get("side") or "right")
        self._last_answer = None
        _LOG.info(
            "arb session restored session=%s target=%s observations=%d bridges=%d reuseRates=%s",
            self._session_id,
            str(target.get("apiId") or ""),
            len(self._observations),
            len(self._bridges),
            reuse_rates,
        )
        self.recalculate(reuse_rates=reuse_rates)

    def recalculate(
        self, *, force_rates: bool = False, reuse_rates: bool = False
    ) -> None:
        """Re-run loop analysis over the current captured market graph."""
        if self._target is None:
            return
        target_name = str(self._target.get("name") or self._target.get("apiId") or "")
        self._ensure_panel().show_loading(target_name, self._side)
        self._analyze_async(force_rates=force_rates, reuse_rates=reuse_rates)

    def _capture_pair(
        self,
        gen: int,
        is_current: Callable[[int], bool],
        *,
        action: str,
    ) -> None:
        try:
            read = currency_exchange_scan.capture(self._desktop)
            if not is_current(gen):
                return
            pair = self._brain.request(
                {
                    "cmd": "arbpair",
                    "league": self._cfg["league"],
                    "wantText": read.want_text,
                    "haveText": read.have_text,
                    "wantAmount": read.want_amount,
                    "haveAmount": read.have_amount,
                    "observedAt": read.observed_at,
                    "forceRates": action == "start",
                    "knownItems": self._known_items(),
                },
                timeout=30.0,
            )
            if not is_current(gen):
                return
            _LOG.info(
                "arb capture resolved session=%s action=%s side=%s raw=%s resolved=%s rates=%s",
                self._session_id or "pending",
                action,
                read.panel_side,
                _json(
                    {
                        "wantText": read.want_text,
                        "haveText": read.have_text,
                        "wantAmount": read.want_amount,
                        "haveAmount": read.have_amount,
                        "observedAt": read.observed_at,
                    }
                ),
                _json(_observation_log(pair.get("observation"))),
                _json(
                    {
                        "epoch": pair.get("ratesEpoch"),
                        "fetchedAt": pair.get("ratesFetchedAt"),
                    }
                ),
            )
            GLib.idle_add(
                {
                    "start": self._choose_target,
                    "add": self._accept_add,
                }[action],
                gen,
                is_current,
                pair,
                read.panel_side,
            )
        except (RuntimeError, OSError, TimeoutError, KeyError, ValueError) as error:
            _LOG.warning(
                "arb capture failed session=%s action=%s error=%s",
                self._session_id or "pending",
                action,
                error,
            )
            GLib.idle_add(self._show_error, gen, is_current, str(error))

    def _known_items(self) -> list[dict]:
        """Return the canonical identities already proven in this session."""
        items: dict[str, dict] = {}
        candidates = [self._target]
        for observation in (*self._observations, *self._bridges):
            candidates.extend((observation.get("want"), observation.get("have")))
        for item in candidates:
            if not isinstance(item, dict):
                continue
            api_id = str(item.get("apiId") or "")
            name = str(item.get("name") or "")
            if api_id and name:
                items[api_id] = dict(item)
        return list(items.values())

    def _choose_target(self, gen: int, is_current, pair: dict, side: str):
        if not is_current(gen):
            return GLib.SOURCE_REMOVE
        observation = pair.get("observation")
        if not isinstance(observation, dict):
            return self._show_error(gen, is_current, "Brain returned an invalid exchange pair")
        self._pending_pair = pair
        self._side = side
        restore_snapshot = self._session_snapshot()
        if restore_snapshot is None and self._previous_session is not None:
            restore_snapshot = copy.deepcopy(self._previous_session)
        restore_target = (restore_snapshot or {}).get("target") or {}
        self._ensure_panel().show_choice(
            pair,
            lambda api_id: self._select_target(api_id, pair),
            side,
            on_restore=(
                (
                    lambda snapshot=restore_snapshot: self._restore_from_choice(
                        pair, snapshot
                    )
                )
                if restore_snapshot is not None
                else None
            ),
            restore_target_name=str(
                restore_target.get("name") or restore_target.get("apiId") or ""
            ),
        )
        return GLib.SOURCE_REMOVE

    def _restore_from_choice(self, pair: dict, snapshot: dict) -> None:
        if pair is not self._pending_pair:
            return
        self._pending_pair = None
        self._restore_snapshot(copy.deepcopy(snapshot), reuse_rates=True)

    def _select_target(self, api_id: str, pair: dict) -> None:
        if pair is not self._pending_pair:
            return
        observation = pair["observation"]
        choices = [observation["want"], observation["have"]]
        target = next((item for item in choices if item.get("apiId") == api_id), None)
        if target is None:
            return
        quote = next(
            (item for item in choices if item.get("apiId") != target.get("apiId")),
            None,
        )
        if not quote or not quote.get("isCurrency"):
            self._pending_pair = None
            _LOG.warning(
                "arb target rejected session=pending reason=unsupported-quote "
                "target=%s quote=%s observation=%s",
                _json(_item_log(target)),
                _json(_item_log(quote)),
                _json(_observation_log(observation)),
            )
            self._ensure_panel().show_error(
                "The other side must be a currency, not another exchange commodity",
                self._side,
            )
            return
        current = self._session_snapshot()
        if current is not None:
            self._previous_session = current
        self._target = target
        self._session_id = uuid.uuid4().hex[:12]
        self._observations = [observation]
        self._bridges = []
        self._pending_pair = None
        self._last_answer = None
        _LOG.info(
            "arb session started session=%s league=%s target=%s initial=%s",
            self._session_id,
            str(self._cfg["league"]),
            _json(_item_log(target)),
            _json(_observation_log(observation)),
        )
        self.recalculate()

    def _accept_add(self, gen: int, is_current, pair: dict, side: str):
        if not is_current(gen) or self._target is None:
            return GLib.SOURCE_REMOVE
        observation = pair.get("observation")
        if not isinstance(observation, dict):
            return self._show_error(gen, is_current, "Brain returned an invalid exchange pair")
        target_id = self._target.get("apiId")
        pair_ids = {
            (observation.get("want") or {}).get("apiId"),
            (observation.get("have") or {}).get("apiId"),
        }
        self._side = side
        observation_id = observation.get("id")
        replaced = False
        role = "target"
        if target_id in pair_ids:
            quote = next(
                (
                    item
                    for item in (
                        observation.get("want") or {},
                        observation.get("have") or {},
                    )
                    if item.get("apiId") != target_id
                ),
                None,
            )
            if not quote or not quote.get("isCurrency"):
                _LOG.warning(
                    "arb capture rejected session=%s reason=unsupported-target-pair observation=%s",
                    self._session_id,
                    _json(_observation_log(observation)),
                )
                self._ensure_panel().show_error(
                    "The added side must be a currency, not another exchange commodity",
                    self._side,
                )
                return GLib.SOURCE_REMOVE
            replaced = any(
                existing.get("id") == observation_id
                for existing in self._observations
            )
            self._observations = [
                existing
                for existing in self._observations
                if existing.get("id") != observation_id
            ]
            self._observations.append(observation)
        else:
            role = "bridge"
            captured_ids = {
                item.get("apiId")
                for captured in self._observations
                for item in (
                    captured.get("have") or {},
                    captured.get("want") or {},
                )
                if item.get("apiId") != target_id
            }
            if len(pair_ids) != 2 or not pair_ids.issubset(captured_ids):
                _LOG.warning(
                    "arb capture rejected session=%s reason=items-not-in-session "
                    "capturedIds=%s observation=%s",
                    self._session_id,
                    _json(sorted(str(item) for item in captured_ids if item)),
                    _json(_observation_log(observation)),
                )
                self._ensure_panel().show_error(
                    "Alt+A items must already be part of this arbitrage session",
                    self._side,
                )
                return GLib.SOURCE_REMOVE
            replaced = any(
                existing.get("id") == observation_id for existing in self._bridges
            )
            self._bridges = [
                existing
                for existing in self._bridges
                if existing.get("id") != observation_id
            ]
            self._bridges.append(observation)
        self._last_answer = None
        _LOG.info(
            "arb capture accepted session=%s role=%s replaced=%s observation=%s graph=%s",
            self._session_id,
            role,
            replaced,
            _json(_observation_log(observation)),
            _json(
                {
                    "target": str(target_id or ""),
                    "targetEdges": [
                        _observation_log(item) for item in self._observations
                    ],
                    "bridges": [_observation_log(item) for item in self._bridges],
                }
            ),
        )
        self.recalculate()
        return GLib.SOURCE_REMOVE

    def _analyze_async(
        self, *, force_rates: bool = False, reuse_rates: bool = False
    ) -> None:
        self._analysis_seq += 1
        analysis_seq = self._analysis_seq
        target = dict(self._target or {})
        observations = [dict(item) for item in (*self._observations, *self._bridges)]
        _LOG.info(
            "arb analysis requested session=%s seq=%d league=%s target=%s "
            "minPercent=%.2f totalBufferPercent=%.2f "
            "executionConcessionPercent=%.2f forceRates=%s reuseRates=%s graph=%s",
            self._session_id,
            analysis_seq,
            str(self._cfg["league"]),
            str(target.get("apiId") or ""),
            self._min_percent,
            self._safety_buffer_percent,
            self._execution_concession_percent,
            force_rates,
            reuse_rates,
            _json([_observation_log(item) for item in observations]),
        )

        def work():
            try:
                answer = self._brain.request(
                    {
                        "cmd": "arbanalyze",
                        "league": self._cfg["league"],
                        "targetApiId": target.get("apiId"),
                        "observations": observations,
                        "minPercent": self._min_percent,
                        "captureMaxAgeMs": _CAPTURE_MAX_AGE_MS,
                        "safetyBufferBps": round(
                            self._safety_buffer_percent * 100
                        ),
                        "executionConcessionBps": round(
                            self._execution_concession_percent * 100
                        ),
                        "forceRates": force_rates,
                        "reuseRates": reuse_rates,
                    },
                    timeout=30.0,
                )
                GLib.idle_add(
                    self._show_analysis, analysis_seq, target.get("apiId"), answer
                )
            except (RuntimeError, OSError, TimeoutError, KeyError, ValueError) as error:
                _LOG.warning(
                    "arb analysis failed session=%s seq=%d target=%s error=%s",
                    self._session_id,
                    analysis_seq,
                    str(target.get("apiId") or ""),
                    error,
                )
                GLib.idle_add(
                    self._show_analysis_error,
                    analysis_seq,
                    target.get("apiId"),
                    str(error),
                )

        threading.Thread(target=work, daemon=True).start()

    def _show_analysis(self, analysis_seq: int, target_id: str, answer: dict):
        if (
            analysis_seq != self._analysis_seq
            or self._target is None
            or self._target.get("apiId") != target_id
        ):
            return GLib.SOURCE_REMOVE
        self._last_answer = copy.deepcopy(answer)
        diagnostic = _analysis_log(answer, include_outcomes=False)
        _LOG.info(
            "arb analysis completed session=%s seq=%d result=%s",
            self._session_id,
            analysis_seq,
            _json(diagnostic),
        )
        if _LOG.isEnabledFor(logging.DEBUG):
            _LOG.debug(
                "arb analysis quantity detail session=%s seq=%d result=%s",
                self._session_id,
                analysis_seq,
                _json(_analysis_log(answer, include_outcomes=True)),
            )
        panel = self._ensure_panel()
        panel.show_analysis(answer, self._side)
        if self._monitor is not None and self._monitor_loop_id:
            loop = next(
                (
                    candidate
                    for candidate in answer.get("loops") or []
                    if candidate.get("id") == self._monitor_loop_id
                ),
                None,
            )
            outcome = None
            if isinstance(loop, dict):
                outcomes = list(loop.get("quantityOutcomes") or [])
                index = self._monitor_quantity - 1
                if 0 <= index < len(outcomes) and isinstance(outcomes[index], dict):
                    outcome = outcomes[index]
            safe = bool(
                isinstance(loop, dict)
                and loop.get("status") == "verified"
                and not loop.get("stale")
                and isinstance(outcome, dict)
                and outcome.get("actionable")
            )
            if safe:
                percent = float(outcome.get("bufferedReturnPercent") or 0)
                detail = (
                    f"{self._monitor_quantity} → "
                    f"{int(outcome.get('bufferedFinalUnits') or 0)} · "
                    f"{percent:+.1f}% buffered"
                )
                panel.set_monitor_state("safe", detail)
            else:
                detail = "Selected loop is not safely executable at the locked quantity"
                panel.set_monitor_state("unsafe", detail)
            if self._monitor_safe is True and not safe:
                panel.alert()
            self._monitor_safe = safe
        return GLib.SOURCE_REMOVE

    def _selection_changed(self, loop_id: str, quantity: int, manual: bool) -> None:
        loop = next(
            (
                candidate
                for candidate in (self._last_answer or {}).get("loops") or []
                if candidate.get("id") == loop_id
            ),
            None,
        )
        outcome = None
        if isinstance(loop, dict):
            outcomes = list(loop.get("quantityOutcomes") or [])
            index = quantity - 1
            if 0 <= index < len(outcomes) and isinstance(outcomes[index], dict):
                outcome = outcomes[index]
        _LOG.info(
            "arb selection session=%s seq=%d mode=%s loop=%s quantity=%d outcome=%s",
            self._session_id,
            self._analysis_seq,
            "manual" if manual else "automatic",
            loop_id,
            quantity,
            _json(outcome or {}),
        )

    def _show_analysis_error(self, analysis_seq: int, target_id: str, message: str):
        if (
            analysis_seq != self._analysis_seq
            or self._target is None
            or self._target.get("apiId") != target_id
        ):
            return GLib.SOURCE_REMOVE
        self._ensure_panel().show_error(message, self._side, can_recalculate=True)
        return GLib.SOURCE_REMOVE

    def _show_error(self, gen: int, is_current, message: str):
        if not is_current(gen):
            return GLib.SOURCE_REMOVE
        self._ensure_panel().show_error(message, self._side)
        return GLib.SOURCE_REMOVE

    def _set_threshold(self, value: float) -> None:
        self._min_percent = max(0.0, float(value))
        self._cfg["arb_min_percent"] = self._min_percent
        if self._threshold_save_source:
            GLib.source_remove(self._threshold_save_source)
        self._threshold_save_source = GLib.timeout_add(350, self._persist_threshold)
        if self._target is not None:
            self._analyze_async(reuse_rates=True)

    def _persist_threshold(self):
        self._threshold_save_source = 0
        try:
            config.save_values(None, {"arb_min_percent": self._min_percent})
        except (OSError, ValueError, SystemExit) as error:
            _LOG.warning("could not persist arbitrage threshold: %s", error)
        return GLib.SOURCE_REMOVE

    def _set_safety_buffer(self, value: float) -> None:
        value = max(0.0, min(15.0, round(float(value) * 2) / 2))
        if abs(value - self._safety_buffer_percent) < 1e-9:
            return
        self._safety_buffer_percent = value
        self._cfg["arb_safety_buffer_percent"] = value
        if self._buffer_save_source:
            GLib.source_remove(self._buffer_save_source)
        self._buffer_save_source = GLib.timeout_add(350, self._persist_safety_buffer)
        if self._target is not None:
            self._analyze_async(reuse_rates=True)

    def _persist_safety_buffer(self):
        self._buffer_save_source = 0
        try:
            config.save_values(
                None,
                {"arb_safety_buffer_percent": self._safety_buffer_percent},
            )
        except (OSError, ValueError, SystemExit) as error:
            _LOG.warning("could not persist arbitrage safety buffer: %s", error)
        return GLib.SOURCE_REMOVE

    def _set_execution_concession(self, value: float) -> None:
        value = max(0.0, min(15.0, round(float(value) * 2) / 2))
        if abs(value - self._execution_concession_percent) < 1e-9:
            return
        self._execution_concession_percent = value
        self._cfg["arb_execution_concession_percent"] = value
        if self._concession_save_source:
            GLib.source_remove(self._concession_save_source)
        self._concession_save_source = GLib.timeout_add(
            350, self._persist_execution_concession
        )
        if self._target is not None:
            self._analyze_async(reuse_rates=True)

    def _persist_execution_concession(self):
        self._concession_save_source = 0
        try:
            config.save_values(
                None,
                {
                    "arb_execution_concession_percent": self._execution_concession_percent
                },
            )
        except (OSError, ValueError, SystemExit) as error:
            _LOG.warning("could not persist arbitrage execution concession: %s", error)
        return GLib.SOURCE_REMOVE

    def _set_show_losing_candidates(self, value: bool) -> None:
        value = bool(value)
        if value == self._show_losing_candidates:
            return
        self._show_losing_candidates = value
        self._cfg["arb_show_losing_candidates"] = value
        _LOG.info("arb candidate filter showLosing=%s", value)
        if self._show_losing_save_source:
            GLib.source_remove(self._show_losing_save_source)
        self._show_losing_save_source = GLib.timeout_add(
            350, self._persist_show_losing_candidates
        )

    def _persist_show_losing_candidates(self):
        self._show_losing_save_source = 0
        try:
            config.save_values(
                None,
                {"arb_show_losing_candidates": self._show_losing_candidates},
            )
        except (OSError, ValueError, SystemExit) as error:
            _LOG.warning("could not persist arbitrage candidate filter: %s", error)
        return GLib.SOURCE_REMOVE
