from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.contract import HOME, STAGES
from app.errors import InputError
from app.manifest import sha256
from app.modules.m8_incidents.rules import run as build_incident_ledger
from app.pipeline import calculate
from app.samples import SAMPLE_NAMES, load_sample
from app.settings import ROOT, RULE_VERSION, TITLE

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(ROOT / "app/templates"))
templates.env.autoescape = True
INCIDENT_SAMPLE = (ROOT / "sample_data/fault_incident.json")


@router.get("/work/incidents")
def incident_workbench(request: Request, sample: str = "clean"):
    import json

    data = load_sample(sample)
    payload = json.loads(INCIDENT_SAMPLE.read_text(encoding="utf-8"))
    current = build_incident_ledger(data, payload)
    titles = {name: caption for name, caption, _, _ in STAGES}
    titles["incidents"] = "故障改派账"
    return templates.TemplateResponse(request=request, name="workbench.html", context={
        "project": TITLE, "titles": titles, "stage": "incidents", "sample": sample,
        "samples": SAMPLE_NAMES, "result": calculate(data), "current": current,
        "rule_version": RULE_VERSION, "input_ref": sha256(data)[:10],
    })


@router.get("/")
@router.get("/work/{stage}")
def workbench(request: Request, stage: str = HOME, sample: str = "clean"):
    titles = {name: caption for name, caption, _, _ in STAGES}
    if stage not in titles:
        raise InputError("UNKNOWN_STAGE", "找不到该业务页面", 404)
    data = load_sample(sample)
    result = calculate(data)
    return templates.TemplateResponse(request=request, name="workbench.html", context={
        "project": TITLE, "titles": titles, "stage": stage, "sample": sample,
        "samples": SAMPLE_NAMES, "result": result, "current": result[stage],
        "rule_version": RULE_VERSION, "input_ref": sha256(data)[:10],
    })
