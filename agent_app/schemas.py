"""Typed user-task contract, separate from the optimizer's search-center JSON."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Mapping

CASE_ID = 'disk_2d_current'
OBJECTIVE = 'minimize_maximum_von_mises_stress'
CONFIGURED_OBJECTIVE = 'configured_objectives'
TASK_SCHEMA = 'disk-agent.task.v1'
RESULT_SCHEMA = 'disk-agent.result.v1'


class TaskValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TaskSpec:
    operation: str
    case_id: str = CASE_ID
    objective: str = OBJECTIVE
    total_candidate_budget: int | None = None
    additional_candidate_budget: int | None = None
    run_id: str | None = None
    schema_version: str = TASK_SCHEMA
    optimization_policy: dict | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | TaskSpec) -> TaskSpec:
        if isinstance(value, cls):
            value = value.to_dict()
        if not isinstance(value, dict):
            raise TaskValidationError('TASK_FORMAT', 'Task must be a JSON object.')
        allowed = set(cls.__dataclass_fields__)
        if set(value) - allowed:
            raise TaskValidationError('UNKNOWN_FIELDS', 'Unsupported task fields: ' +
                                      ', '.join(sorted(map(str, set(value) - allowed))))
        if value.get('operation') not in ('check', 'optimize', 'status'):
            raise TaskValidationError('OPERATION', 'operation must be check, optimize or status.')
        if value.get('case_id', CASE_ID) != CASE_ID:
            raise TaskValidationError('CASE_ID', 'This adapter supports only ' + CASE_ID + '.')
        objective = value.get('objective', OBJECTIVE)
        if objective not in (OBJECTIVE, CONFIGURED_OBJECTIVE):
            raise TaskValidationError('OBJECTIVE', 'The current case only minimizes maximum von Mises stress.')
        if value.get('schema_version', TASK_SCHEMA) != TASK_SCHEMA:
            raise TaskValidationError('TASK_SCHEMA', 'Unsupported task schema version.')
        if value.get('optimization_policy') is not None:
            from python_control.objectives import validate_policy, ControllerError
            try:
                value = dict(value, optimization_policy=validate_policy(value['optimization_policy']))
            except (ValueError, ControllerError) as exc:
                raise TaskValidationError('OPTIMIZATION_POLICY', str(exc)) from exc
            if value['operation'] != 'optimize':
                raise TaskValidationError('OPTIMIZATION_POLICY', 'Objective policies apply to optimization tasks only.')
            value['objective'] = CONFIGURED_OBJECTIVE
        elif objective == CONFIGURED_OBJECTIVE:
            raise TaskValidationError('OPTIMIZATION_POLICY', 'Configured objectives require an optimization policy.')
        for key in ('total_candidate_budget', 'additional_candidate_budget'):
            budget = value.get(key)
            if budget is not None and (type(budget) is not int or budget < 1):
                raise TaskValidationError('BUDGET', key + ' must be a positive integer, not a boolean.')
        total, additional = value.get('total_candidate_budget'), value.get('additional_candidate_budget')
        run_id = value.get('run_id')
        if run_id is not None and (not isinstance(run_id, str) or
                re.fullmatch(r'run_[A-Za-z0-9][A-Za-z0-9_-]{0,180}', run_id) is None):
            raise TaskValidationError('RUN_ID', 'run_id must be a Python run directory name, not a path.')
        if value['operation'] == 'optimize':
            if (total is None) == (additional is None):
                raise TaskValidationError('BUDGET', 'Specify exactly one total or additional candidate budget.')
            if additional is not None and run_id is None:
                raise TaskValidationError('BUDGET', 'Additional budget requires an explicit run_id.')
        elif total is not None or additional is not None:
            raise TaskValidationError('BUDGET', 'Budgets are supported only for optimize tasks.')
        if value['operation'] == 'check' and run_id is not None:
            raise TaskValidationError('RUN_ID', 'check inspects the current case; use status to inspect a run.')
        return cls(**value)

    def to_dict(self) -> dict[str, Any]:
        return {key: val for key, val in asdict(self).items() if val is not None}


@dataclass
class ToolResult:
    ok: bool
    operation: str
    status: str
    data: dict[str, Any] = field(default_factory=dict)
    error: dict[str, str] | None = None
    warnings: list[str] = field(default_factory=list)
    schema_version: str = RESULT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
