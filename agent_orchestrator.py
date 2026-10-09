"""Local-first orchestration for scheduled, review-first research workflows.

The orchestrator deliberately owns only workflow state.  It does not perform
network requests, call an LLM, or approve facts.  A caller supplies a collector
and/or deterministic research handlers; their JSON results are kept as drafts,
retrieval items, artifacts, and review gates in an atomic local state file.
"""
from __future__ import annotations

import inspect
import json
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

try:  # pragma: no cover - fcntl is available on the supported Unix runtime
    import fcntl
except ImportError:  # pragma: no cover - keeps the module importable elsewhere
    fcntl = None  # type: ignore[assignment]


STATE_FILENAME = "agent_orchestrator_state.json"
LOCK_FILENAME = "agent_orchestrator_state.lock"
STATE_VERSION = 1
MIN_INTERVAL_MINUTES = 15
DEFAULT_INTERVAL_MINUTES = 60

PLAN_KINDS = frozenset(
    {
        "collection_cycle",
        "company_snapshot_refresh",
        "sector_scan",
        "event_triage",
        "memo_draft",
    }
)
RUNNABLE_PLAN_STATUSES = frozenset({"approved", "active"})
TERMINAL_RUN_STATUSES = frozenset(
    {"succeeded", "succeeded_noop", "blocked", "failed"}
)
REVIEW_DECISIONS = frozenset({"accepted", "rejected"})


class OrchestratorError(ValueError):
    """A safe, stable error whose string is an internal error code only."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


# A shorter alias is useful to callers that prefer a domain-specific name.
AgentOrchestratorError = OrchestratorError


def _copy_json(value: Any) -> Any:
    """Make a detached JSON-compatible copy and reject non-JSON values."""

    try:
        return json.loads(
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        )
    except (TypeError, ValueError, OverflowError, json.JSONDecodeError) as exc:
        raise OrchestratorError("json_value_invalid") from None


def _iso_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            result = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "plans": [],
        "runs": [],
        "tasks": [],
        "review_gates": [],
        "artifacts": [],
        "retrieval_items": [],
        "audit": [],
    }


class AgentOrchestrator:
    """Persisted scheduler and review-first workflow coordinator.

    Parameters
    ----------
    state_dir:
        Directory containing the atomic JSON state and advisory lock file.
    collector:
        Optional object exposing ``run_once()`` and, for plans carrying an
        explicit ``source_scope``, ``run_scoped(source_scope)``.  It is never
        constructed or contacted by this class.
    research_handlers:
        Optional mapping from research plan kind to a callable.  A callable is
        invoked as ``handler(kind, payload)``; a one-argument callable receiving
        ``payload`` is also accepted for simple test doubles.
    clock:
        Optional callable returning a ``datetime``.  Naive datetimes are
        treated as UTC.  It is useful for deterministic tests.
    """

    STATE_FILENAME = STATE_FILENAME
    LOCK_FILENAME = LOCK_FILENAME
    MIN_INTERVAL_MINUTES = MIN_INTERVAL_MINUTES
    PLAN_KINDS = PLAN_KINDS

    def __init__(
        self,
        state_dir: os.PathLike[str] | str,
        collector: Any = None,
        research_handlers: Optional[Mapping[str, Callable[..., Any]]] = None,
        clock: Optional[Callable[[], Any]] = None,
    ) -> None:
        try:
            self.state_dir = Path(state_dir).expanduser()
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, TypeError, ValueError):
            raise OrchestratorError("state_dir_invalid") from None

        self.state_path = self.state_dir / self.STATE_FILENAME
        self.lock_path = self.state_dir / self.LOCK_FILENAME
        self.collector = collector
        self.research_handlers = dict(research_handlers or {})
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._thread_lock = threading.RLock()

        try:
            self.lock_path.touch(exist_ok=True)
        except OSError:
            raise OrchestratorError("state_lock_unavailable") from None

        # This also creates a valid state file on first use and validates an
        # existing file before any public method can operate on it.
        with self._locked_state():
            pass

    # ------------------------------------------------------------------
    # Persistence and audit primitives
    # ------------------------------------------------------------------
    @contextmanager
    def _locked_state(self) -> Iterator[dict[str, Any]]:
        """Read, exclusively lock, mutate, and atomically persist state."""

        with self._thread_lock:
            try:
                lock_handle = self.lock_path.open("a+", encoding="utf-8")
            except OSError:
                raise OrchestratorError("state_lock_unavailable") from None
            try:
                if fcntl is not None:
                    try:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                    except OSError:
                        raise OrchestratorError("state_lock_unavailable") from None
                state = self._read_state()
                yield state
                self._write_state(state)
            finally:
                if fcntl is not None:
                    try:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                lock_handle.close()

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return _default_state()
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise OrchestratorError("state_invalid") from None
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            raise OrchestratorError("state_invalid")
        state = _copy_json(raw)
        for key in (
            "plans",
            "runs",
            "tasks",
            "review_gates",
            "artifacts",
            "retrieval_items",
            "audit",
        ):
            if not isinstance(state.get(key), list):
                raise OrchestratorError("state_invalid")
        return state

    def _write_state(self, state: Mapping[str, Any]) -> None:
        try:
            encoded = json.dumps(
                state,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
        except (TypeError, ValueError, OverflowError):
            raise OrchestratorError("state_write_failed") from None

        temporary: Optional[str] = None
        try:
            fd, temporary = tempfile.mkstemp(
                prefix=f".{self.STATE_FILENAME}.",
                suffix=".tmp",
                dir=str(self.state_dir),
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            temporary = None
            try:
                directory_fd = os.open(str(self.state_dir), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # The replace is already durable enough on platforms where a
                # directory cannot be opened for fsync.
                pass
        except (OSError, UnicodeError):
            raise OrchestratorError("state_write_failed") from None
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _audit(
        self,
        state: dict[str, Any],
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        at: str,
        from_status: Optional[str] = None,
        to_status: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        event: dict[str, Any] = {
            "id": _new_id("audit"),
            "at": at,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
        }
        if from_status is not None:
            event["from_status"] = from_status
        if to_status is not None:
            event["to_status"] = to_status
        if details:
            event["details"] = _copy_json(dict(details))
        state["audit"].append(event)

    def _set_status(
        self,
        state: dict[str, Any],
        *,
        entity_type: str,
        entity: dict[str, Any],
        to_status: str,
        at: str,
        action: str = "status_transition",
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        old_status = entity.get("status")
        if old_status == to_status:
            return
        entity["status"] = to_status
        entity["updated_at"] = at
        self._audit(
            state,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity["id"]),
            at=at,
            from_status=old_status,
            to_status=to_status,
            details=details,
        )

    def _now(self) -> datetime:
        try:
            value = self.clock() if callable(self.clock) else self.clock.now()  # type: ignore[union-attr]
        except Exception:
            raise OrchestratorError("clock_invalid") from None
        parsed = _parse_datetime(value)
        if parsed is None:
            raise OrchestratorError("clock_invalid")
        return parsed

    @staticmethod
    def _clean_id(value: Any) -> str:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise OrchestratorError("id_invalid")
        return value.strip()

    @staticmethod
    def _canonical_time(value: Any) -> str:
        parsed = _parse_datetime(value)
        if parsed is None:
            raise OrchestratorError("scheduled_for_invalid")
        return _iso_datetime(parsed)

    @staticmethod
    def _find(state: Mapping[str, Any], collection: str, object_id: str) -> Optional[dict[str, Any]]:
        for item in state[collection]:
            if isinstance(item, dict) and item.get("id") == object_id:
                return item
        return None

    def _require(
        self, state: Mapping[str, Any], collection: str, object_id: Any, error_code: str
    ) -> dict[str, Any]:
        clean_id = self._clean_id(object_id)
        found = self._find(state, collection, clean_id)
        if found is None:
            raise OrchestratorError(error_code)
        return found

    # ------------------------------------------------------------------
    # Plan lifecycle
    # ------------------------------------------------------------------
    @staticmethod
    def _interval_from_payload(payload: Mapping[str, Any]) -> int:
        schedule = payload.get("schedule")
        candidate: Any = None
        if isinstance(schedule, Mapping):
            candidate = schedule.get("interval_minutes", schedule.get("interval"))
        elif schedule is not None:
            candidate = schedule
        if candidate is None:
            candidate = payload.get("interval_minutes")
        if isinstance(candidate, bool) or candidate is None:
            raise OrchestratorError("schedule_interval_required")
        try:
            interval = int(candidate)
        except (TypeError, ValueError, OverflowError):
            raise OrchestratorError("schedule_interval_invalid") from None
        if interval < MIN_INTERVAL_MINUTES:
            raise OrchestratorError("schedule_interval_too_short")
        return interval

    @staticmethod
    def _normalise_authorization(payload: Mapping[str, Any]) -> bool:
        if "authorization" in payload:
            candidate = payload["authorization"]
        elif "authorized" in payload:
            candidate = payload["authorized"]
        else:
            raise OrchestratorError("authorization_required")
        if isinstance(candidate, Mapping):
            if "authorized" in candidate:
                candidate = candidate["authorized"]
            elif "granted" in candidate:
                candidate = candidate["granted"]
        if not isinstance(candidate, bool):
            raise OrchestratorError("authorization_must_be_boolean")
        return candidate

    def create_plan(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise OrchestratorError("payload_must_be_object")
        try:
            clean_payload = _copy_json(dict(payload))
        except OrchestratorError:
            raise
        kind = clean_payload.get("kind")
        if not isinstance(kind, str) or kind not in PLAN_KINDS:
            raise OrchestratorError("plan_kind_invalid")
        interval = self._interval_from_payload(clean_payload)

        source_scope: Any = None
        authorization: Optional[bool] = None
        if kind == "collection_cycle":
            if "source_scope" not in clean_payload or not _has_content(clean_payload.get("source_scope")):
                raise OrchestratorError("source_scope_required")
            source_scope = clean_payload["source_scope"]
            authorization = self._normalise_authorization(clean_payload)
        elif "source_scope" in clean_payload:
            source_scope = clean_payload["source_scope"]
        if "authorization" in clean_payload and authorization is None:
            authorization = self._normalise_authorization(clean_payload)

        now = _iso_datetime(self._now())
        plan_id = _new_id("plan")
        plan: dict[str, Any] = {
            "id": plan_id,
            "kind": kind,
            "name": clean_payload.get("name") or clean_payload.get("title") or kind,
            "payload": clean_payload,
            "schedule": {"interval_minutes": interval},
            "source_scope": source_scope,
            "authorization": authorization,
            "status": "draft",
            "created_at": now,
            "updated_at": now,
            "approved_at": None,
            "paused_at": None,
            "last_run_at": None,
            "last_run_id": None,
            "next_run": None,
        }
        with self._locked_state() as state:
            state["plans"].append(plan)
            self._audit(
                state,
                action="plan_created",
                entity_type="plan",
                entity_id=plan_id,
                at=now,
                to_status="draft",
                details={"kind": kind},
            )
            return _copy_json(plan)

    def plans(self) -> list[dict[str, Any]]:
        with self._locked_state() as state:
            return _copy_json(state["plans"])

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        with self._locked_state() as state:
            return _copy_json(self._require(state, "plans", plan_id, "plan_not_found"))

    def approve_plan(self, plan_id: str) -> dict[str, Any]:
        now = _iso_datetime(self._now())
        with self._locked_state() as state:
            plan = self._require(state, "plans", plan_id, "plan_not_found")
            status = plan.get("status")
            if status in {"approved", "active"}:
                return _copy_json(plan)
            if status != "draft":
                raise OrchestratorError("plan_not_approvable")
            self._set_status(
                state,
                entity_type="plan",
                entity=plan,
                to_status="approved",
                at=now,
                action="plan_approved",
            )
            plan["approved_at"] = now
            plan["next_run"] = now
            plan["updated_at"] = now
            return _copy_json(plan)

    def pause_plan(self, plan_id: str) -> dict[str, Any]:
        now = _iso_datetime(self._now())
        with self._locked_state() as state:
            plan = self._require(state, "plans", plan_id, "plan_not_found")
            if plan.get("status") == "paused":
                return _copy_json(plan)
            if plan.get("status") not in RUNNABLE_PLAN_STATUSES:
                raise OrchestratorError("plan_not_pauseable")
            self._set_status(
                state,
                entity_type="plan",
                entity=plan,
                to_status="paused",
                at=now,
                action="plan_paused",
            )
            plan["paused_at"] = now
            plan["updated_at"] = now
            return _copy_json(plan)

    def resume_plan(self, plan_id: str) -> dict[str, Any]:
        now = _iso_datetime(self._now())
        with self._locked_state() as state:
            plan = self._require(state, "plans", plan_id, "plan_not_found")
            if plan.get("status") in {"approved", "active"}:
                return _copy_json(plan)
            if plan.get("status") != "paused":
                raise OrchestratorError("plan_not_resumable")
            self._set_status(
                state,
                entity_type="plan",
                entity=plan,
                to_status="active",
                at=now,
                action="plan_resumed",
            )
            plan["next_run"] = now
            plan["paused_at"] = None
            plan["updated_at"] = now
            return _copy_json(plan)

    # ------------------------------------------------------------------
    # Run and task execution
    # ------------------------------------------------------------------
    @staticmethod
    def _step_specs(plan: Mapping[str, Any]) -> list[tuple[str, str]]:
        payload = plan.get("payload")
        if isinstance(payload, Mapping) and isinstance(payload.get("steps"), list):
            specs: list[tuple[str, str]] = []
            for index, step in enumerate(payload["steps"]):
                if isinstance(step, str) and step.strip():
                    specs.append((f"step_{index + 1}", step.strip()))
                elif isinstance(step, Mapping):
                    label = step.get("name") or step.get("label") or f"step_{index + 1}"
                    specs.append((f"step_{index + 1}", str(label)))
            if specs:
                return specs
        if plan.get("kind") == "collection_cycle":
            return [("collect", "collect_authorized_sources")]
        return [("research", f"execute_{plan.get('kind', 'research')}")]

    def _new_task(
        self,
        state: dict[str, Any],
        *,
        run: dict[str, Any],
        plan: Mapping[str, Any],
        step_key: str,
        label: str,
        at: str,
    ) -> dict[str, Any]:
        task = {
            "id": _new_id("task"),
            "run_id": run["id"],
            "plan_id": plan["id"],
            "step": step_key,
            "label": label,
            "status": "pending",
            "created_at": at,
            "updated_at": at,
            "started_at": None,
            "completed_at": None,
            "error": None,
            "artifact_ids": [],
            "retrieval_item_ids": [],
        }
        state["tasks"].append(task)
        run["task_ids"].append(task["id"])
        self._audit(
            state,
            action="task_created",
            entity_type="task",
            entity_id=task["id"],
            at=at,
            to_status="pending",
            details={"run_id": run["id"], "step": step_key},
        )
        return task

    def _finish_task(
        self,
        state: dict[str, Any],
        task: dict[str, Any],
        status: str,
        at: str,
        *,
        error: Optional[str] = None,
    ) -> None:
        if task.get("started_at") is None:
            task["started_at"] = at
        self._set_status(
            state,
            entity_type="task",
            entity=task,
            to_status=status,
            at=at,
            details={"error": error} if error else None,
        )
        task["completed_at"] = at
        task["error"] = error
        task["updated_at"] = at

    def _advance_plan(self, plan: dict[str, Any], now: datetime, run_id: str) -> None:
        interval = int(plan["schedule"]["interval_minutes"])
        now_iso = _iso_datetime(now)
        plan["last_run_at"] = now_iso
        plan["last_run_id"] = run_id
        plan["next_run"] = _iso_datetime(now + timedelta(minutes=interval))
        plan["updated_at"] = now_iso

    def _normalise_collector_result(self, result: Any) -> list[dict[str, Any]]:
        if result is None:
            return []
        if isinstance(result, Mapping):
            if "items" in result:
                result = result["items"]
            elif "retrieval_items" in result:
                result = result["retrieval_items"]
            elif "results" in result and isinstance(result["results"], list):
                result = result["results"]
            else:
                result = [result]
        if not isinstance(result, (list, tuple)):
            raise OrchestratorError("collection_result_invalid")
        normalised: list[dict[str, Any]] = []
        for item in result:
            if not isinstance(item, Mapping):
                raise OrchestratorError("collection_item_invalid")
            try:
                normalised.append(_copy_json(dict(item)))
            except OrchestratorError:
                raise OrchestratorError("collection_item_invalid") from None
        return normalised

    def _store_retrieval_items(
        self,
        state: dict[str, Any],
        *,
        run: dict[str, Any],
        task: dict[str, Any],
        raw_items: list[dict[str, Any]],
        at: str,
    ) -> list[dict[str, Any]]:
        stored_items: list[dict[str, Any]] = []
        used_ids = {str(item.get("id")) for item in state["retrieval_items"]}
        for index, raw in enumerate(raw_items):
            candidate = raw.get("id")
            if not isinstance(candidate, str) or not candidate.strip() or candidate in used_ids:
                candidate = f"{run['id']}:item:{index + 1}"
            if candidate in used_ids:
                candidate = _new_id("retrieval")
            used_ids.add(candidate)
            item = dict(raw)
            item.update(
                {
                    "id": candidate,
                    "run_id": run["id"],
                    "plan_id": run["plan_id"],
                    "source_item_id": raw.get("id"),
                    "status": "pending",
                    "facts_approved": False,
                    "created_at": at,
                    "updated_at": at,
                    "reviewed_at": None,
                }
            )
            state["retrieval_items"].append(item)
            run["retrieval_item_ids"].append(candidate)
            task["retrieval_item_ids"].append(candidate)
            gate = {
                "id": _new_id("gate"),
                "run_id": run["id"],
                "plan_id": run["plan_id"],
                "kind": "retrieval_review",
                "item_id": candidate,
                "target_type": "retrieval_item",
                "target_id": candidate,
                "status": "pending",
                "decision": None,
                "reason": None,
                "created_at": at,
                "updated_at": at,
                "decided_at": None,
            }
            state["review_gates"].append(gate)
            self._audit(
                state,
                action="retrieval_item_created",
                entity_type="retrieval_item",
                entity_id=candidate,
                at=at,
                to_status="pending",
                details={"run_id": run["id"]},
            )
            self._audit(
                state,
                action="review_gate_created",
                entity_type="review_gate",
                entity_id=gate["id"],
                at=at,
                to_status="pending",
                details={"item_id": candidate},
            )
            stored_items.append(item)
        return stored_items

    def _handler_for(self, kind: str) -> Optional[Callable[..., Any]]:
        handler = self.research_handlers.get(kind)
        if handler is None:
            handler = self.research_handlers.get("*")
        if handler is not None and not callable(handler):
            raise OrchestratorError("research_handler_invalid")
        return handler

    @staticmethod
    def _invoke_handler(handler: Callable[..., Any], kind: str, payload: Mapping[str, Any]) -> Any:
        # Prefer the documented two-argument contract.  Signature inspection
        # lets tiny one-argument fakes remain convenient without swallowing a
        # TypeError raised inside a real handler.
        try:
            signature = inspect.signature(handler)
        except (TypeError, ValueError):
            # Some extension/builtin callables do not expose a signature.
            return handler(kind, payload)

        parameters = list(signature.parameters.values())
        accepts_varargs = any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters
        )
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        if accepts_varargs or len(positional) >= 2:
            return handler(kind, payload)
        if len(positional) == 1:
            return handler(payload)
        return handler()

    def _run_plan_locked(
        self,
        state: dict[str, Any],
        *,
        plan: dict[str, Any],
        trigger: str,
        scheduled_for: str,
        now: datetime,
    ) -> dict[str, Any]:
        existing = next(
            (
                run
                for run in state["runs"]
                if run.get("plan_id") == plan["id"]
                and run.get("scheduled_for") == scheduled_for
            ),
            None,
        )
        if existing is not None:
            # A process may have persisted a completed run before crashing
            # while advancing the schedule.  A scheduled retry is still
            # idempotent, but it must not leave the plan permanently due.
            if trigger == "scheduled":
                due = _parse_datetime(plan.get("next_run"))
                if due is None or due <= now:
                    self._advance_plan(plan, now, existing["id"])
            return _copy_json(existing)

        if plan.get("status") not in RUNNABLE_PLAN_STATUSES:
            raise OrchestratorError("plan_not_approved")

        now_iso = _iso_datetime(now)
        run = {
            "id": _new_id("run"),
            "plan_id": plan["id"],
            "kind": plan["kind"],
            "trigger": trigger,
            "scheduled_for": scheduled_for,
            "status": "running",
            "created_at": now_iso,
            "updated_at": now_iso,
            "started_at": now_iso,
            "finished_at": None,
            "error": None,
            "task_ids": [],
            "artifact_ids": [],
            "retrieval_item_ids": [],
            "review_outcome": None,
        }
        state["runs"].append(run)
        self._audit(
            state,
            action="run_created",
            entity_type="run",
            entity_id=run["id"],
            at=now_iso,
            to_status="running",
            details={"plan_id": plan["id"], "scheduled_for": scheduled_for, "trigger": trigger},
        )
        if plan.get("status") == "approved":
            self._set_status(
                state,
                entity_type="plan",
                entity=plan,
                to_status="active",
                at=now_iso,
                action="plan_activated",
                details={"run_id": run["id"]},
            )

        specs = self._step_specs(plan)
        tasks = [
            self._new_task(
                state,
                run=run,
                plan=plan,
                step_key=step_key,
                label=label,
                at=now_iso,
            )
            for step_key, label in specs
        ]
        # The current implementation executes one controlled operation per
        # plan.  Any additional declared steps are retained as blocked steps
        # rather than silently pretending they ran.
        task = tasks[0]
        self._set_status(
            state,
            entity_type="task",
            entity=task,
            to_status="running",
            at=now_iso,
            action="task_started",
        )
        task["started_at"] = now_iso

        if plan["kind"] == "collection_cycle":
            authorized = plan.get("authorization") is True
            # The plan's source_scope is an explicit allow-list.  Scoped
            # plans must go through the collector's scoped entry point;
            # silently falling back to the global run_once would widen
            # collection beyond the approved scope, so a collector without
            # a callable run_scoped blocks the run instead.
            source_scope = plan.get("source_scope")
            scoped = _has_content(source_scope)
            collect_method = "run_scoped" if scoped else "run_once"
            if not authorized:
                self._finish_task(state, task, "blocked", now_iso, error="authorization_required")
                run["error"] = "authorization_required"
                self._set_status(
                    state,
                    entity_type="run",
                    entity=run,
                    to_status="blocked",
                    at=now_iso,
                    details={"reason": "authorization_required"},
                )
            elif self.collector is None or not callable(getattr(self.collector, collect_method, None)):
                self._finish_task(state, task, "blocked", now_iso, error="collector_unavailable")
                run["error"] = "collector_unavailable"
                self._set_status(
                    state,
                    entity_type="run",
                    entity=run,
                    to_status="blocked",
                    at=now_iso,
                    details={"reason": "collector_unavailable"},
                )
            else:
                try:
                    raw_result = (
                        self.collector.run_scoped(source_scope) if scoped else self.collector.run_once()
                    )
                    raw_items = self._normalise_collector_result(raw_result)
                    items = self._store_retrieval_items(
                        state,
                        run=run,
                        task=task,
                        raw_items=raw_items,
                        at=now_iso,
                    )
                    if items:
                        self._finish_task(state, task, "waiting_review", now_iso)
                        self._set_status(
                            state,
                            entity_type="run",
                            entity=run,
                            to_status="waiting_review",
                            at=now_iso,
                            details={"retrieval_count": len(items)},
                        )
                    else:
                        self._finish_task(state, task, "succeeded", now_iso)
                        self._set_status(
                            state,
                            entity_type="run",
                            entity=run,
                            to_status="succeeded_noop",
                            at=now_iso,
                        )
                except OrchestratorError as exc:
                    self._finish_task(state, task, "failed", now_iso, error=exc.code)
                    run["error"] = exc.code
                    self._set_status(
                        state,
                        entity_type="run",
                        entity=run,
                        to_status="failed",
                        at=now_iso,
                        details={"reason": exc.code},
                    )
                except Exception:
                    self._finish_task(state, task, "failed", now_iso, error="collection_failed")
                    run["error"] = "collection_failed"
                    self._set_status(
                        state,
                        entity_type="run",
                        entity=run,
                        to_status="failed",
                        at=now_iso,
                        details={"reason": "collection_failed"},
                    )
        else:
            try:
                handler = self._handler_for(plan["kind"])
                if handler is None:
                    self._finish_task(state, task, "blocked", now_iso, error="research_handler_unavailable")
                    run["error"] = "research_handler_unavailable"
                    self._set_status(
                        state,
                        entity_type="run",
                        entity=run,
                        to_status="blocked",
                        at=now_iso,
                        details={"reason": "research_handler_unavailable"},
                    )
                else:
                    result = self._invoke_handler(handler, plan["kind"], plan["payload"])
                    artifact_payload = _copy_json(result)
                    artifact = {
                        "id": _new_id("artifact"),
                        "run_id": run["id"],
                        "plan_id": plan["id"],
                        "kind": plan["kind"],
                        "type": "research_result",
                        "status": "draft",
                        "payload": artifact_payload,
                        "data": artifact_payload,
                        "created_at": now_iso,
                        "updated_at": now_iso,
                    }
                    state["artifacts"].append(artifact)
                    run["artifact_ids"].append(artifact["id"])
                    task["artifact_ids"].append(artifact["id"])
                    self._audit(
                        state,
                        action="artifact_created",
                        entity_type="artifact",
                        entity_id=artifact["id"],
                        at=now_iso,
                        to_status="draft",
                        details={"run_id": run["id"], "kind": plan["kind"]},
                    )
                    self._finish_task(state, task, "succeeded", now_iso)
                    self._set_status(
                        state,
                        entity_type="run",
                        entity=run,
                        to_status="succeeded",
                        at=now_iso,
                    )
            except OrchestratorError as exc:
                self._finish_task(state, task, "failed", now_iso, error=exc.code)
                run["error"] = exc.code
                self._set_status(
                    state,
                    entity_type="run",
                    entity=run,
                    to_status="failed",
                    at=now_iso,
                    details={"reason": exc.code},
                )
            except Exception:
                self._finish_task(state, task, "failed", now_iso, error="research_failed")
                run["error"] = "research_failed"
                self._set_status(
                    state,
                    entity_type="run",
                    entity=run,
                    to_status="failed",
                    at=now_iso,
                    details={"reason": "research_failed"},
                )

        self._advance_plan(plan, now, run["id"])
        run["updated_at"] = now_iso
        if run["status"] in TERMINAL_RUN_STATUSES:
            run["finished_at"] = now_iso
        return _copy_json(run)

    def run_plan(
        self,
        plan_id: str,
        trigger: str = "manual",
        *,
        scheduled_for: Optional[Any] = None,
    ) -> dict[str, Any]:
        if not isinstance(trigger, str) or not trigger.strip():
            raise OrchestratorError("trigger_invalid")
        now = self._now()
        effective_scheduled = self._canonical_time(scheduled_for if scheduled_for is not None else now)
        with self._locked_state() as state:
            plan = self._require(state, "plans", plan_id, "plan_not_found")
            if plan.get("status") not in RUNNABLE_PLAN_STATUSES:
                raise OrchestratorError("plan_not_approved")
            return self._run_plan_locked(
                state,
                plan=plan,
                trigger=trigger.strip(),
                scheduled_for=effective_scheduled,
                now=now,
            )

    def runs(self) -> list[dict[str, Any]]:
        with self._locked_state() as state:
            return _copy_json(state["runs"])

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._locked_state() as state:
            return _copy_json(self._require(state, "runs", run_id, "run_not_found"))

    def tasks(self, run_id: str) -> list[dict[str, Any]]:
        clean_id = self._clean_id(run_id)
        with self._locked_state() as state:
            self._require(state, "runs", clean_id, "run_not_found")
            return _copy_json([task for task in state["tasks"] if task.get("run_id") == clean_id])

    def artifacts(self, run_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Return persisted research draft artifacts, optionally scoped to a run."""
        with self._locked_state() as state:
            artifacts = state["artifacts"]
            if run_id is not None:
                clean_id = self._clean_id(run_id)
                self._require(state, "runs", clean_id, "run_not_found")
                artifacts = [item for item in artifacts if item.get("run_id") == clean_id]
            return _copy_json(artifacts)

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        with self._locked_state() as state:
            return _copy_json(self._require(state, "artifacts", artifact_id, "artifact_not_found"))

    def review_artifact(self, artifact_id: str, decision: str, reason: str) -> dict[str, Any]:
        """Review a research draft without approving facts."""
        clean_id = self._clean_id(artifact_id)
        normalized = self._normalise_decision(decision)
        clean_reason = self._require_reason(reason)
        now = _iso_datetime(self._now())
        with self._locked_state() as state:
            artifact = self._require(state, "artifacts", clean_id, "artifact_not_found")
            current = artifact.get("status")
            if current in {"accepted", "rejected"}:
                if current == normalized:
                    return _copy_json(artifact)
                raise OrchestratorError("artifact_already_reviewed")
            artifact["status"] = normalized
            artifact["review_reason"] = clean_reason
            artifact["reviewed_at"] = now
            artifact["updated_at"] = now
            artifact["facts_approved"] = False
            self._audit(state, action="artifact_reviewed", entity_type="artifact", entity_id=clean_id,
                        at=now, from_status=current, to_status=normalized,
                        details={"reason": clean_reason, "facts_approved": False})
            return _copy_json(artifact)

    @staticmethod
    def _normalise_decision(decision: Any) -> str:
        if not isinstance(decision, str):
            raise OrchestratorError("decision_invalid")
        aliases = {"accept": "accepted", "reject": "rejected"}
        normalised = aliases.get(decision.strip().lower(), decision.strip().lower())
        if normalised not in REVIEW_DECISIONS:
            # In particular, "approved" is intentionally not accepted: this
            # layer can accept/reject a draft but can never approve a fact.
            raise OrchestratorError("decision_invalid")
        return normalised

    @staticmethod
    def _require_reason(reason: Any) -> str:
        if not isinstance(reason, str) or not reason.strip() or "\x00" in reason:
            raise OrchestratorError("reason_required")
        return reason.strip()[:2000]

    def review_gates(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        if status is not None and (not isinstance(status, str) or not status.strip()):
            raise OrchestratorError("status_invalid")
        with self._locked_state() as state:
            gates = state["review_gates"]
            if status is not None:
                gates = [gate for gate in gates if gate.get("status") == status]
            return _copy_json(gates)

    def retrieval_items(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        if status is not None and (not isinstance(status, str) or not status.strip()):
            raise OrchestratorError("status_invalid")
        with self._locked_state() as state:
            items = state["retrieval_items"]
            if status is not None:
                items = [item for item in items if item.get("status") == status]
            return _copy_json(items)

    def _maybe_complete_reviewed_run(
        self, state: dict[str, Any], run_id: str, at: str
    ) -> None:
        run = self._find(state, "runs", run_id)
        if run is None or run.get("status") != "waiting_review":
            return
        gates = [gate for gate in state["review_gates"] if gate.get("run_id") == run_id]
        if not gates or any(gate.get("status") == "pending" for gate in gates):
            return
        rejected = any(gate.get("status") == "rejected" for gate in gates)
        run["review_outcome"] = "rejected_items" if rejected else "accepted_items"
        self._set_status(
            state,
            entity_type="run",
            entity=run,
            to_status="succeeded",
            at=at,
            action="run_review_completed",
        )
        run["finished_at"] = at
        for task in state["tasks"]:
            if task.get("run_id") == run_id and task.get("status") == "waiting_review":
                self._finish_task(state, task, "succeeded", at)

    def _set_retrieval_status(
        self,
        state: dict[str, Any],
        item: dict[str, Any],
        status: str,
        at: str,
        *,
        action: str,
        reason: Optional[str] = None,
    ) -> None:
        self._set_status(
            state,
            entity_type="retrieval_item",
            entity=item,
            to_status=status,
            at=at,
            action=action,
            details={"reason": reason} if reason else None,
        )
        item["reviewed_at"] = at
        item["updated_at"] = at
        # This flag is deliberately forced false even when a collector sends a
        # misleading value.  Retrieval review is not fact approval.
        item["facts_approved"] = False

    def decide_gate(self, gate_id: str, decision: str, reason: str) -> dict[str, Any]:
        clean_gate_id = self._clean_id(gate_id)
        normalised = self._normalise_decision(decision)
        clean_reason = self._require_reason(reason)
        now = _iso_datetime(self._now())
        with self._locked_state() as state:
            gate = self._require(state, "review_gates", clean_gate_id, "gate_not_found")
            current = gate.get("status")
            if current != "pending":
                if current == normalised:
                    return _copy_json(gate)
                raise OrchestratorError("gate_already_decided")
            gate["decision"] = normalised
            gate["reason"] = clean_reason
            gate["decided_at"] = now
            self._set_status(
                state,
                entity_type="review_gate",
                entity=gate,
                to_status=normalised,
                at=now,
                action="gate_decided",
                details={"decision": normalised, "reason": clean_reason},
            )
            self._audit(
                state,
                action="review_decision_recorded",
                entity_type="review_gate",
                entity_id=gate["id"],
                at=now,
                details={"decision": normalised, "reason": clean_reason},
            )
            item = self._find(state, "retrieval_items", gate.get("item_id"))
            if item is not None and item.get("status") == "pending":
                self._set_retrieval_status(
                    state,
                    item,
                    normalised,
                    now,
                    action="retrieval_reviewed_by_gate",
                    reason=clean_reason,
                )
            self._maybe_complete_reviewed_run(state, gate["run_id"], now)
            return _copy_json(gate)

    def review_retrieval(self, item_id: str, status: str) -> dict[str, Any]:
        clean_item_id = self._clean_id(item_id)
        normalised = self._normalise_decision(status)
        now = _iso_datetime(self._now())
        with self._locked_state() as state:
            item = self._require(
                state, "retrieval_items", clean_item_id, "retrieval_item_not_found"
            )
            current = item.get("status")
            if current != "pending":
                if current == normalised:
                    return _copy_json(item)
                raise OrchestratorError("retrieval_already_reviewed")
            self._set_retrieval_status(
                state,
                item,
                normalised,
                now,
                action="retrieval_reviewed",
            )
            for gate in state["review_gates"]:
                if gate.get("item_id") != clean_item_id or gate.get("status") != "pending":
                    continue
                gate["decision"] = normalised
                gate["reason"] = "retrieval_review"
                gate["decided_at"] = now
                self._set_status(
                    state,
                    entity_type="review_gate",
                    entity=gate,
                    to_status=normalised,
                    at=now,
                    action="gate_decided_from_retrieval_review",
                    details={"decision": normalised},
                )
                self._audit(
                    state,
                    action="review_decision_recorded",
                    entity_type="review_gate",
                    entity_id=gate["id"],
                    at=now,
                    details={"decision": normalised, "reason": "retrieval_review"},
                )
            self._maybe_complete_reviewed_run(state, item["run_id"], now)
            return _copy_json(item)

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------
    def tick(self) -> list[dict[str, Any]]:
        now = self._now()
        with self._locked_state() as state:
            due: list[tuple[str, str]] = []
            for plan in state["plans"]:
                if plan.get("status") not in RUNNABLE_PLAN_STATUSES:
                    continue
                next_run = _parse_datetime(plan.get("next_run"))
                if next_run is None or next_run <= now:
                    scheduled_for = _iso_datetime(next_run) if next_run is not None else _iso_datetime(now)
                    due.append((plan["id"], scheduled_for))

            result: list[dict[str, Any]] = []
            for plan_id, scheduled_for in due:
                plan = self._find(state, "plans", plan_id)
                if plan is None or plan.get("status") not in RUNNABLE_PLAN_STATUSES:
                    continue
                # _run_plan_locked advances from the current clock, not from
                # the old due timestamp, so a long outage never replays a
                # backlog of intervals.
                result.append(
                    self._run_plan_locked(
                        state,
                        plan=plan,
                        trigger="scheduled",
                        scheduled_for=scheduled_for,
                        now=now,
                    )
                )
            return result


__all__ = [
    "AgentOrchestrator",
    "AgentOrchestratorError",
    "OrchestratorError",
    "PLAN_KINDS",
    "MIN_INTERVAL_MINUTES",
    "STATE_FILENAME",
    "LOCK_FILENAME",
]
