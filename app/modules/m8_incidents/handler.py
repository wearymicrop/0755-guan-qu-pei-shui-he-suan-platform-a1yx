"""M8 故障改派账接口。

POST /api/incidents 提交一次放水中途故障申报，返回幂等的改派账；
同一 incident 故障号重试返回首次落库记录（reused=true），不重复扣减。
GET  /api/incidents/{incident_id} 回读不可变改派账。
"""
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from app.errors import InputError
from app.samples import load_sample

router = APIRouter(prefix="/api/incidents", tags=["故障改派"])


class IncidentSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample: str | None = None
    data: dict[str, Any] | None = None
    incident: dict[str, Any]


@router.post("")
def submit(body: IncidentSubmission, request: Request):
    if (body.sample is None) == (body.data is None):
        raise InputError("CHOOSE_SOURCE", "请选择一个样例或提交数据")
    data = load_sample(body.sample) if body.sample is not None else body.data
    record, reused = request.app.state.incidents.submit(data, body.incident)
    return {**record, "reused": reused}


@router.get("/{incident_id}")
def inspect(incident_id: str, request: Request):
    record = request.app.state.incidents.get(incident_id)
    if record is None:
        raise InputError("INCIDENT_NOT_FOUND", "找不到该故障改派账", 404)
    return record
