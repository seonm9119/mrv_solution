import asyncio
import json
import os
import re
from datetime import datetime, timezone
from html import escape as escape_html
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import APIRouter, Body


router = APIRouter()
MRV_LLM_API_DEFAULT_BASE_URL = "http://192.168.0.21:8006"
EMPTY_VALUE = "—"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
REPORT_CONTEXTS = {}
REPORT_CONTEXT_LIMIT = 20
LLM_REQUEST_SEMAPHORE = asyncio.Semaphore(max(1, int(os.environ.get("MRV_LLM_PARALLEL", "2") or "2")))

REPORT_ENDPOINT_INFO = {
    "status": "ready",
    "mode": "frontend-input-to-legacy-template",
    "generate_endpoint": "/api/report/generate",
}

FILE_CATEGORY_SPECS = {
    "monthly_raw": {
        "display": "월별 연료 사용 원시 데이터",
        "purpose": "활동자료 1차 근거 (Usage, Unit, Period)",
        "badge": "Required",
        "detail": "월별 테이블 및 대사 검증의 기초 자료",
    },
    "annual_report": {
        "display": "연간 연료/에너지 사용 보고서",
        "purpose": "연간 총계 공식값 · 월별 합계 ↔ 연간값 Cross-check",
        "badge": "Required",
        "detail": "연간 총계의 공식 출처",
    },
    "operation_log": {
        "display": "계측기 운영로그 또는 계측기 정보",
        "purpose": "DQ/Outlier 설명 근거 · 활동자료 교차검증",
        "badge": "Recommended",
        "detail": "교차검증 시 활용",
    },
    "ef_ref": {
        "display": "배출계수(EF) 참조 문서",
        "purpose": "배출계수 단일 진실원 (GWP 버전 포함)",
        "badge": "Required",
        "detail": "배출계수 공식 출처",
    },
}

LLM_SECTION_TAGS = {
    "submission_summary": "{{llm:submission_summary|ref=db:mrv_report.report_id,db:mrv_document_metadata.organization_name,db:mrv_activity_data.inventory_year,db:mrv_report.program_regime}}",
    "boundary_narrative": "{{llm:boundary_narrative|ref=db:mrv_report.org_boundary_type,db:mrv_report.operational_boundary_type,db:mrv_report.scopes_included,db:mrv_report.included_entities_sites,db:mrv_report.excluded_entities_sites}}",
    "activity_data": "{{llm:aggregation_narrative|ref=db:mrv_activity_data.usage,db:mrv_activity_data.usage_unit,db:mrv_activity_data.data_source,db:monthly_summary_text}}",
    "emission_factor": "{{llm:ef_rationale|ref=db:mrv_emission_factor_ref.emission_factor,db:mrv_emission_factor_ref.emission_factor_unit,db:mrv_emission_factor_ref.ef_source,db:mrv_emission_factor_ref.ef_version}}",
    "calculation_result": "{{llm:calculation_interpretation|ref=db:mrv_calculation_result.emission,db:mrv_calculation_result.emissions_unit,db:mrv_calculation_result.formula,db:mrv_calculation_result.yoy_change_display}}",
    "qaqc_readiness": "{{llm:qaqc_readiness_narrative|ref=db:mrv_opinion.llm_decl_val,db:mrv_opinion.llm_acc_val,db:mrv_opinion.llm_diff_pct,db:mrv_opinion.llm_rejection_reason,db:mrv_opinion.llm_adj_total,db:mrv_opinion.llm_fuel_eff_cv,db:mrv_opinion.llm_emissions,db:mrv_opinion.llm_op_unit}}",
}


@router.get("/report")
def report():
    return REPORT_ENDPOINT_INFO


@router.post("/report/preview")
async def preview_report(payload=Body(default={})):
    form_values = payload.get("formValues") if isinstance(payload, dict) else {}
    monthly_rows = payload.get("monthlyRows") if isinstance(payload, dict) else []
    derived_emission = payload.get("derivedEmission") if isinstance(payload, dict) else None

    if not isinstance(form_values, dict):
        form_values = {}
    if not isinstance(monthly_rows, list):
        monthly_rows = []

    context = build_report_context(form_values, monthly_rows, derived_emission)
    db_ctx = build_legacy_db_context(form_values, monthly_rows, derived_emission)

    return {
        "status": "preview",
        "report_id": context["report_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_summary": context["summary"],
        "html": render_legacy_report_preview_html(db_ctx),
    }


@router.post("/report/generate")
async def generate_report(payload=Body(default={})):
    form_values = payload.get("formValues") if isinstance(payload, dict) else {}
    monthly_rows = payload.get("monthlyRows") if isinstance(payload, dict) else []
    derived_emission = payload.get("derivedEmission") if isinstance(payload, dict) else None

    if not isinstance(form_values, dict):
        form_values = {}
    if not isinstance(monthly_rows, list):
        monthly_rows = []

    context = build_report_context(form_values, monthly_rows, derived_emission)
    db_ctx = build_legacy_db_context(form_values, monthly_rows, derived_emission)
    llm_sections = await build_llm_report_sections(db_ctx)
    llm_error = ""

    try:
        report_token = store_report_context(context["report_id"], db_ctx)
        rendered_html, llm_tags = render_legacy_report_shell(db_ctx, report_token)
    except Exception as error:
        llm_error = str(error)
        report_token = ""
        llm_tags = []
        rendered_html = render_legacy_report_html_without_llm(db_ctx)

    return {
        "status": "rendering" if report_token else "generated",
        "report_id": context["report_id"],
        "report_token": report_token,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "qwen": {
            "status": "pending" if report_token else "failed",
            "base_url": get_mrv_llm_api_base_url(),
            "error": llm_error,
            "rendered_tags": 0,
            "total_tags": len(llm_tags),
            "failed_tags": [],
        },
        "input_summary": context["summary"],
        "sections": llm_sections,
        "html": rendered_html,
    }


@router.post("/report/html/llm")
async def generate_report_llm(payload=Body(default={})):
    report_token = clean_value(payload.get("report_token")) if isinstance(payload, dict) else ""
    tag = payload.get("tag") if isinstance(payload, dict) else ""
    if not report_token or report_token not in REPORT_CONTEXTS:
        return {"value": EMPTY_VALUE, "status": "missing_context"}

    db_ctx = REPORT_CONTEXTS[report_token]
    try:
        async with LLM_REQUEST_SEMAPHORE:
            value = await resolve_legacy_llm_tag(tag, db_ctx)
    except Exception as error:
        return {"value": f"LLM 실패: {error}", "status": "failed"}

    return {
        "value": value if value and value != "LLM 실패" else EMPTY_VALUE,
        "status": "ok",
    }


def store_report_context(report_id, db_ctx):
    report_token = f"{clean_value(report_id) or 'MRV-DRAFT'}-{uuid4().hex[:12]}"
    REPORT_CONTEXTS[report_token] = dict(db_ctx)

    while len(REPORT_CONTEXTS) > REPORT_CONTEXT_LIMIT:
        oldest_key = next(iter(REPORT_CONTEXTS))
        REPORT_CONTEXTS.pop(oldest_key, None)

    return report_token


def get_mrv_llm_api_base_url():
    return os.environ.get("MRV_LLM_API_BASE_URL", MRV_LLM_API_DEFAULT_BASE_URL).strip().rstrip("/")


def get_public_api_base_url():
    default_url = "/api/mrv-solution"
    return os.environ.get("PUBLIC_MRV_API_BASE_URL", default_url).strip().rstrip("/") or default_url


def get_mrv_llm_api_timeout_seconds():
    value = os.environ.get("MRV_LLM_API_TIMEOUT_SECONDS", "180").strip()
    try:
        return float(value)
    except ValueError:
        return 180


def get_section_llm_timeout_seconds():
    value = os.environ.get("MRV_SECTION_LLM_TIMEOUT_SECONDS", "45").strip()
    try:
        return float(value)
    except ValueError:
        return 45


async def call_mrv_solution_llm_api(tag, db_ctx):
    base_url = get_mrv_llm_api_base_url()
    if not base_url:
        raise RuntimeError("MRV_LLM_API_BASE_URL is empty")

    request_body = {
        "tag": tag,
        "db_context": db_ctx,
    }
    timeout = get_mrv_llm_api_timeout_seconds()

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{base_url}/api/mrv-solution/llm",
            json=request_body,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.json()


async def build_llm_report_sections(db_ctx):
    async def resolve_section(section_name, tag):
        try:
            async with LLM_REQUEST_SEMAPHORE:
                value = await asyncio.wait_for(
                    resolve_legacy_llm_tag(tag, db_ctx),
                    timeout=get_section_llm_timeout_seconds(),
                )
        except Exception:
            value = ""
        if not value or value == "LLM 실패":
            value = ""
        return section_name, value

    results = await asyncio.gather(
        *(resolve_section(name, tag) for name, tag in LLM_SECTION_TAGS.items()),
        return_exceptions=True,
    )
    sections = {}
    for result in results:
        if isinstance(result, Exception):
            continue
        section_name, value = result
        sections[section_name] = value
    return sections


def build_report_context(form_values, monthly_rows, derived_emission):
    emission_value = normalize_number(derived_emission)
    if emission_value == 0:
        emission_value = calculate_emission(form_values.get("usage"), form_values.get("emissionFactor"))

    monthly_summary = summarize_monthly_rows(monthly_rows, form_values.get("emissionFactor"))
    report_id = clean_value(form_values.get("reportId")) or "MRV-DRAFT"

    summary = {
        "organization": clean_value(form_values.get("organizationName")),
        "inventory_year": clean_value(form_values.get("inventoryYear")),
        "program_regime": clean_value(form_values.get("programRegime")),
        "scope": clean_value(form_values.get("scopeType")),
        "activity": clean_value(form_values.get("activityName")),
        "facility": clean_value(form_values.get("facility")),
        "period": f"{clean_value(form_values.get('startDate'))} ~ {clean_value(form_values.get('endDate'))}",
        "usage": clean_value(form_values.get("usage")),
        "usage_unit": clean_value(form_values.get("usageUnit")),
        "emission_factor": clean_value(form_values.get("emissionFactor")),
        "emission_factor_unit": clean_value(form_values.get("emissionFactorUnit")),
        "emission": round(emission_value, 4),
        "emissions_unit": clean_value(form_values.get("emissionsUnit")) or "tCO2e",
        "data_source": clean_value(form_values.get("dataSource")),
        "ef_source": clean_value(form_values.get("efSource")),
        "verification_standard": clean_value(form_values.get("verificationStandard")),
        "inventory_standard": clean_value(form_values.get("inventoryStandard")),
        "materiality_threshold": clean_value(form_values.get("materialityThreshold")),
        "qaqc": clean_value(form_values.get("etlChecks")),
        "monthly_total": round(monthly_summary["total"], 4),
        "monthly_peak_period": monthly_summary["peak_period"],
        "monthly_peak_value": round(monthly_summary["peak_value"], 4),
        "monthly_warn_periods": monthly_summary["warn_periods"],
    }

    return {
        "report_id": report_id,
        "summary": summary,
        "form_values": form_values,
        "monthly_rows": monthly_rows,
    }


def build_legacy_db_context(form_values, monthly_rows, derived_emission):
    ctx = {}

    def set_value(key, value):
        text = clean_value(value) or EMPTY_VALUE
        ctx[key] = text
        if "." in key:
            ctx.setdefault(key.split(".")[-1], text)

    field_map = {
        "reportId": "mrv_report.report_id",
        "reportVersion": "mrv_report.report_version",
        "programRegime": "mrv_report.program_regime",
        "baseYear": "mrv_report.base_year",
        "verificationStandard": "mrv_report.verification_standard",
        "inventoryStandard": "mrv_report.inventory_standard",
        "assetName": "mrv_report.asset_name",
        "orgBoundaryType": "mrv_report.org_boundary_type",
        "orgBoundaryApproach": "mrv_report.org_boundary_approach",
        "operationalBoundaryType": "mrv_report.operational_boundary_type",
        "scopesExcluded": "mrv_report.scopes_excluded",
        "includedEntitiesSites": "mrv_report.included_entities_sites",
        "excludedEntitiesSites": "mrv_report.excluded_entities_sites",
        "siteVisitType": "mrv_report.site_visit_type",
        "dataOwner": "mrv_report.data_owner",
        "retentionPolicy": "mrv_report.retention_policy",
        "accessControl": "mrv_report.access_control",
        "auditTrailLocation": "mrv_report.audit_trail_location",
        "etlTransformOps": "mrv_report.etl_transform_ops",
        "etlHandlingRule": "mrv_report.etl_handling_rule",
        "preparedBy": "mrv_report.prepared_by",
        "preparedSignatureDate": "mrv_report.prepared_signature_date",
        "reviewedBy": "mrv_report.reviewed_by",
        "reviewedSignatureDate": "mrv_report.reviewed_signature_date",
        "approvedBy": "mrv_report.approved_by",
        "approvedSignatureDate": "mrv_report.approved_signature_date",
        "inventoryYear": "mrv_activity_data.inventory_year",
        "activityName": "mrv_activity_data.activity_name",
        "facility": "mrv_activity_data.facility",
        "startDate": "mrv_activity_data.start_date",
        "endDate": "mrv_activity_data.end_date",
        "usage": "mrv_activity_data.usage",
        "usageUnit": "mrv_activity_data.usage_unit",
        "annualFuelUsage": "mrv_activity_data.annual_fuel_usage_nm3",
        "dataSource": "mrv_activity_data.data_source",
        "dataCollectionProcess": "mrv_activity_data.data_collection_process",
        "aggregationBasis": "mrv_activity_data.aggregation_basis",
        "periodUnit": "mrv_activity_data.period_unit",
        "outlierRule": "mrv_activity_data.outlier_rule",
        "missingDataRule": "mrv_activity_data.missing_data_rule",
        "reconciliationRule": "mrv_activity_data.reconciliation_rule",
        "etlSources": "mrv_activity_data.etl_sources",
        "etlOutput": "mrv_activity_data.etl_output",
        "organizationName": "mrv_document_metadata.organization_name",
        "preparedDate": "mrv_document_metadata.prepared_date",
        "materialityThreshold": "mrv_document_metadata.materiality_threshold",
        "instrumentName": "mrv_document_metadata.instrument_name",
        "instrumentId": "mrv_document_metadata.instrument_id",
        "calibrationDate": "mrv_document_metadata.calibration_date",
        "instrumentAccuracy": "mrv_document_metadata.instrument_accuracy",
        "calibrationEvidenceFile": "mrv_document_metadata.calibration_evidence_file",
        "etlChecks": "mrv_document_metadata.etl_checks",
        "emissionFactor": "mrv_emission_factor_ref.emission_factor",
        "emissionFactorUnit": "mrv_emission_factor_ref.emission_factor_unit",
        "efSource": "mrv_emission_factor_ref.ef_source",
        "efVersion": "mrv_emission_factor_ref.ef_version",
        "efPublishYear": "mrv_emission_factor_ref.ef_publish_year",
        "efTier": "mrv_emission_factor_ref.ef_tier",
        "efSelectionRationale": "mrv_emission_factor_ref.ef_selection_rationale",
        "standardProtocol": "mrv_calculation_result.standard_protocol",
        "verificationLevel": "mrv_calculation_result.verification_level",
        "formula": "mrv_calculation_result.formula",
        "unitConversions": "mrv_calculation_result.unit_conversions",
        "roundingRule": "mrv_calculation_result.rounding_rule",
        "emissionsUnit": "mrv_calculation_result.emissions_unit",
        "gwpVersion": "mrv_calculation_result.gwp_version",
        "priorYearEmission": "mrv_calculation_result.prior_year_emission",
        "calculationMethodSource": "mrv_calculation_result.calculation_method_source",
        "activityUncertainty": "mrv_calculation_result.activity_uncertainty",
        "efUncertainty": "mrv_calculation_result.ef_uncertainty",
        "uncertaintyMethod": "mrv_calculation_result.uncertainty_method",
        "combinedUncertainty": "mrv_calculation_result.combined_uncertainty",
    }

    for field_name, db_key in field_map.items():
        set_value(db_key, form_values.get(field_name))

    report_id = clean_value(form_values.get("reportId")) or "MRV-DRAFT"
    usage_unit = clean_value(form_values.get("usageUnit"))
    emission_unit = clean_value(form_values.get("emissionsUnit")) or "tCO2e"
    scope_label = get_legacy_scope_label(form_values.get("scopeType"))
    emission_value = normalize_number(derived_emission)
    if emission_value == 0:
        emission_value = calculate_emission(form_values.get("usage"), form_values.get("emissionFactor"))

    set_value("mrv_report.scopes_included", scope_label)
    set_value("mrv_calculation_result.scope", scope_label)
    set_value("mrv_calculation_result.emission", format_number(emission_value, 2))
    set_value("mrv_calculation_result.annual_emissions_tco2e", format_number(emission_value, 2))
    set_value("mrv_activity_data.activity_data_display", f"{ctx.get('mrv_activity_data.usage', EMPTY_VALUE)} {usage_unit}".strip())
    set_value("mrv_calculation_result.unit_conversions_display", ctx.get("mrv_calculation_result.unit_conversions"))
    set_value("mrv_calculation_result.prior_year_emission_display", ctx.get("mrv_calculation_result.prior_year_emission"))
    set_value("mrv_calculation_result.yoy_change_display", build_yoy_change_text(emission_value, form_values.get("priorYearEmission")))
    set_value("mrv_calculation_result.recalculation_difference_display", "0.00%")
    set_value("mrv_document_metadata.etl_checks_summary", ctx.get("mrv_document_metadata.etl_checks"))
    set_value("mrv_files.standardized_name", f"{report_id}_mrv_report.html" if report_id else EMPTY_VALUE)
    set_value("mrv_files.original_filename", clean_value(form_values.get("monthlyRawFile")) or EMPTY_VALUE)
    set_value("mrv_files.link_monthly_raw", clean_value(form_values.get("monthlyRawFile")) or EMPTY_VALUE)

    add_file_upload_context(ctx, form_values)
    add_etl_step_context(ctx)
    add_monthly_context(ctx, monthly_rows, form_values, emission_value)
    add_evidence_context(ctx, form_values)
    add_qc_context(ctx, monthly_rows, form_values)
    add_opinion_context(ctx, monthly_rows, form_values, emission_value)
    add_sensitivity_context(ctx, form_values, emission_value)
    add_context_aliases(ctx)
    return ctx


def add_etl_step_context(ctx):
    raw_etl = ctx.get("mrv_report.etl_transform_ops", "")
    if raw_etl and raw_etl != EMPTY_VALUE:
        steps = [part.strip() for part in re.split(r"→|>|,", raw_etl) if part.strip()]
        if steps:
            steps_html = "".join(f'<span class="etl-step">· {escape_html(step)}</span>' for step in steps)
        else:
            steps_html = escape_html(raw_etl)
    else:
        steps_html = EMPTY_VALUE
    ctx["mrv_report.etl_transform_ops_steps"] = steps_html
    ctx["etl_transform_ops_steps"] = steps_html


def add_file_upload_context(ctx, form_values):
    files = {
        "monthly_raw": clean_value(form_values.get("monthlyRawFile")),
        "annual_report": clean_value(form_values.get("annualReportFile")),
        "operation_log": clean_value(form_values.get("operationLogFile")),
        "ef_ref": clean_value(form_values.get("efRefFile")),
    }
    for key, value in files.items():
        spec = FILE_CATEGORY_SPECS[key]
        ctx[f"mrv_report_activity_uploads.file_{key}"] = value or EMPTY_VALUE
        ctx[f"mrv_report_activity_uploads.display_{key}"] = spec["display"]
        ctx[f"mrv_report_activity_uploads.purpose_{key}"] = spec["purpose"]

    ctx["mrv_report_activity_uploads.remark_monthly_raw_badge"] = FILE_CATEGORY_SPECS["monthly_raw"]["badge"]
    ctx["mrv_report_activity_uploads.remark_monthly_raw_detail"] = FILE_CATEGORY_SPECS["monthly_raw"]["detail"]
    ctx["mrv_report_activity_uploads.remark_annual_report"] = FILE_CATEGORY_SPECS["annual_report"]["detail"]
    ctx["mrv_report_activity_uploads.remark_operation_log_badge"] = FILE_CATEGORY_SPECS["operation_log"]["badge"]
    ctx["mrv_report_activity_uploads.remark_operation_log_detail"] = FILE_CATEGORY_SPECS["operation_log"]["detail"]
    ctx["mrv_report_activity_uploads.remark_ef_required"] = FILE_CATEGORY_SPECS["ef_ref"]["badge"]


def add_monthly_context(ctx, monthly_rows, form_values, emission_value):
    usage_unit = clean_value(form_values.get("usageUnit")) or ctx.get("mrv_activity_data.usage_unit", EMPTY_VALUE)
    emission_unit = clean_value(form_values.get("emissionsUnit")) or ctx.get("mrv_calculation_result.emissions_unit", "tCO2e")
    emission_factor = form_values.get("emissionFactor")
    rows = []
    total = 0
    emission_total = 0
    peak = {"period": "", "value": 0}
    low = {"period": "", "value": None}
    warn_periods = []

    for row in monthly_rows:
        if not isinstance(row, dict):
            continue
        period = clean_value(row.get("period")) or EMPTY_VALUE
        raw_value = normalize_number(row.get("rawValue"))
        adjustment = normalize_number(row.get("adjustment"))
        final_value = raw_value + adjustment
        row_unit = clean_value(row.get("unit")) or usage_unit
        dq_flag = clean_value(row.get("dqFlag")) or "OK"
        row_emission = calculate_emission(final_value, emission_factor)
        total += final_value
        emission_total += row_emission
        if final_value > peak["value"]:
            peak = {"period": period, "value": final_value}
        if low["value"] is None or final_value < low["value"]:
            low = {"period": period, "value": final_value}
        if dq_flag.upper() != "OK":
            warn_periods.append(period)
        rows.append({
            "period": period,
            "raw": raw_value,
            "adjustment": adjustment,
            "final": final_value,
            "emission": row_emission,
            "unit": row_unit,
            "dq": dq_flag,
        })

    if total > 0:
        ctx["mrv_activity_data.usage"] = format_number(total, 2)
        ctx["usage"] = ctx["mrv_activity_data.usage"]
        ctx["mrv_activity_data.annual_fuel_usage_nm3"] = format_number(total, 2)
    if emission_total > 0 and emission_value == 0:
        ctx["mrv_calculation_result.emission"] = format_number(emission_total, 2)
        ctx["mrv_calculation_result.annual_emissions_tco2e"] = format_number(emission_total, 2)

    ctx["monthly_table_rows"] = build_monthly_table_rows(rows)
    ctx["monthly_usage_emissions_rows"] = build_monthly_usage_emission_rows(rows)
    ctx["chart_monthly_json"] = json.dumps(
        build_monthly_chart_data(rows, usage_unit, emission_unit, total),
        ensure_ascii=False,
    )
    ctx["chart_agg_json"] = json.dumps(
        build_monthly_agg_chart_data(rows, usage_unit, total, emission_total),
        ensure_ascii=False,
    )
    ctx["chart_cost_json"] = "[]"
    ctx["chart_cost_estimated_json"] = "[]"
    ctx["chart_scenario_rates_json"] = json.dumps({"bau_rate": 0.03, "cut_rates": [-0.05, -0.10, -0.15], "labels": ["BAU +3%", "-5%", "-10%", "-15%"]}, ensure_ascii=False)
    avg_value = total / len(rows) if rows else 0
    ctx["monthly_summary_text"] = (
        f"총 {format_number(total, 2)} {usage_unit}, 월평균 {format_number(avg_value, 2)} {usage_unit}, "
        f"최대월 {peak['period'] or EMPTY_VALUE} {format_number(peak['value'], 2)} {usage_unit}, "
        f"주의월 {', '.join(warn_periods) if warn_periods else EMPTY_VALUE}"
    )
    ctx["chart_summary.max_month_label"] = peak["period"] or EMPTY_VALUE
    ctx["chart_summary.max_month_value"] = f"{format_number(peak['value'], 2)} {usage_unit}" if peak["period"] else EMPTY_VALUE
    ctx["chart_summary.min_month_label"] = low["period"] or EMPTY_VALUE
    ctx["chart_summary.min_month_value"] = f"{format_number(low['value'] or 0, 2)} {usage_unit}" if low["period"] else EMPTY_VALUE
    ctx["chart_summary.monthly_avg"] = f"{format_number(avg_value, 2)} {usage_unit}" if rows else EMPTY_VALUE
    ctx["chart_summary.total_operating_unit"] = EMPTY_VALUE
    ctx["chart_summary.operating_unit_label"] = "연간 운영단위"
    ctx["chart_summary.operating_unit_unit"] = EMPTY_VALUE
    ctx["chart_summary.avg_efficiency"] = EMPTY_VALUE
    ctx["chart_summary.efficiency_label"] = "연료 효율"
    ctx["chart_summary.efficiency_unit"] = usage_unit
    ctx["chart_summary.total_cost"] = EMPTY_VALUE
    ctx["chart_summary.total_cost_unit"] = "원"


def build_monthly_chart_data(rows, usage_unit, emission_unit, total):
    labels = [row["period"] for row in rows]
    fuel_values = [round(row["final"], 4) for row in rows]
    emission_values = [round(row["emission"], 4) for row in rows]
    cumulative_fuel = build_cumulative_values(fuel_values)
    cumulative_emissions = build_cumulative_values(emission_values)
    share_pct = [
        round((value / total) * 100, 1) if total else 0
        for value in fuel_values
    ]

    return {
        "labels": labels,
        "fuel": fuel_values,
        "emissions": emission_values,
        "cumulative_fuel": cumulative_fuel,
        "cumulative_emissions": cumulative_emissions,
        "share_pct": share_pct,
        "total_fuel": round(total, 4),
        "unit": usage_unit if usage_unit != EMPTY_VALUE else "",
        "emissions_unit": emission_unit if emission_unit != EMPTY_VALUE else "tCO2e",
        "rows": [
            {
                "period": row["period"],
                "usage": round(row["final"], 4),
                "emission": round(row["emission"], 4),
                "dqFlag": row["dq"],
            }
            for row in rows
        ],
    }


def build_monthly_agg_chart_data(rows, usage_unit, total, emission_total):
    return {
        "labels": [row["period"] for row in rows],
        "raw": [round(row["raw"], 4) for row in rows],
        "adjustments": [round(row["adjustment"], 4) for row in rows],
        "final": [round(row["final"], 4) for row in rows],
        "unit": usage_unit if usage_unit != EMPTY_VALUE else "",
        "totalUsage": round(total, 4),
        "totalEmission": round(emission_total, 4),
    }


def build_cumulative_values(values):
    cumulative = []
    running_total = 0
    for value in values:
        running_total += value
        cumulative.append(round(running_total, 4))
    return cumulative


def build_monthly_table_rows(rows):
    if not rows:
        return '<tr><td colspan="7">월별 사용량 입력값이 없습니다.</td></tr>'
    html_rows = []
    for row in rows:
        html_rows.append(
            "<tr>"
            f"<td>{escape_html(row['period'])}</td>"
            f"<td>{format_number(row['raw'], 2)}</td>"
            f"<td>{format_number(row['adjustment'], 2)}</td>"
            f"<td>{format_number(row['final'], 2)}</td>"
            f"<td>{format_number(row['emission'], 3)}</td>"
            f"<td>{escape_html(row['unit'])}</td>"
            f"<td>{escape_html(row['dq'])}</td>"
            "</tr>"
        )
    return "".join(html_rows)


def build_monthly_usage_emission_rows(rows):
    if not rows:
        return '<tr><td colspan="4">월별 활동자료가 없습니다.</td></tr>'
    html_rows = []
    for row in rows:
        html_rows.append(
            "<tr>"
            f"<td>{escape_html(row['period'])}</td>"
            f"<td>{format_number(row['final'], 2)} {escape_html(row['unit'])}</td>"
            f"<td>{format_number(row['emission'], 3)}</td>"
            f"<td>{escape_html(row['dq'])}</td>"
            "</tr>"
        )
    return "".join(html_rows)


def add_evidence_context(ctx, form_values):
    evidence_rows = [
        (FILE_CATEGORY_SPECS["monthly_raw"]["display"], form_values.get("monthlyRawFile"), FILE_CATEGORY_SPECS["monthly_raw"]["purpose"]),
        (FILE_CATEGORY_SPECS["annual_report"]["display"], form_values.get("annualReportFile"), FILE_CATEGORY_SPECS["annual_report"]["purpose"]),
        (FILE_CATEGORY_SPECS["operation_log"]["display"], form_values.get("operationLogFile"), FILE_CATEGORY_SPECS["operation_log"]["purpose"]),
        (FILE_CATEGORY_SPECS["ef_ref"]["display"], form_values.get("efRefFile"), FILE_CATEGORY_SPECS["ef_ref"]["purpose"]),
        ("교정 증빙", form_values.get("calibrationEvidenceFile"), "계측기 정확도 확인"),
    ]
    ctx["evidence_register_table_rows"] = "".join(
        "<tr>"
        f"<td>{escape_html(label)}</td>"
        f"<td>{escape_html(clean_value(file_name) or EMPTY_VALUE)}</td>"
        f"<td>{escape_html(purpose)}</td>"
        "</tr>"
        for label, file_name, purpose in evidence_rows
    )
    ctx["changelog_table_rows"] = (
        f"<tr><td>{escape_html(ctx.get('mrv_report.report_version', EMPTY_VALUE))}</td>"
        f"<td>{escape_html(ctx.get('mrv_report.data_status', EMPTY_VALUE))}</td><td>"
        f"{escape_html(ctx.get('mrv_report.prepared_signature_date', EMPTY_VALUE))}</td></tr>"
    )


def add_qc_context(ctx, monthly_rows, form_values):
    monthly_summary = summarize_monthly_rows(monthly_rows, form_values.get("emissionFactor"))
    usage = normalize_number(form_values.get("usage"))
    usage_unit = clean_value(form_values.get("usageUnit"))
    start_date = clean_value(form_values.get("startDate"))
    end_date = clean_value(form_values.get("endDate"))
    materiality_rate = get_materiality_rate(form_values.get("materialityThreshold"))
    tolerance = max(usage * materiality_rate, 0.001) if usage > 0 else 0.001
    monthly_diff = abs(usage - monthly_summary["total"]) if usage > 0 else 0
    dq_flags = [
        clean_value(row.get("dqFlag")).upper()
        for row in monthly_rows
        if isinstance(row, dict) and clean_value(row.get("dqFlag"))
    ]
    has_dq_issue = any(flag not in ("OK", "PASS") for flag in dq_flags)
    has_missing_rule = bool(clean_value(form_values.get("missingDataRule")) or clean_value(form_values.get("outlierRule")))
    evidence_files = [
        form_values.get("monthlyRawFile"),
        form_values.get("annualReportFile"),
        form_values.get("efRefFile"),
    ]
    has_required_evidence = all(clean_value(file_name) for file_name in evidence_files)
    has_ef_basis = all(
        clean_value(form_values.get(field_name))
        for field_name in ("emissionFactor", "emissionFactorUnit", "efSource")
    )

    qc_checks = {
        "qc1_result": build_qc_result(
            bool(start_date and end_date and usage_unit),
            "보고기간/단위 확인",
            "보고기간 또는 단위 입력 필요",
        ),
        "qc2_result": build_qc_result(
            bool(monthly_rows) and (usage <= 0 or monthly_diff <= tolerance),
            f"월별 합계 {format_number(monthly_summary['total'], 2)} {usage_unit or EMPTY_VALUE}",
            "연간 사용량과 월별 합계 차이 확인 필요",
        ),
        "qc3_result": build_qc_result(
            not has_dq_issue or has_missing_rule,
            "DQ 플래그 처리 기준 확인",
            "DQ 플래그에 대한 결측/이상치 처리 기준 필요",
        ),
        "qc4_result": build_qc_result(
            has_required_evidence,
            "필수 증빙 파일 연결",
            "월별 원시자료/연간보고서/배출계수 증빙 연결 필요",
        ),
        "qc5_result": build_qc_result(
            has_ef_basis,
            "배출계수 값·단위·출처 확인",
            "배출계수 값·단위·출처 입력 필요",
        ),
    }
    for key, value in qc_checks.items():
        ctx[f"mrv_qc_checks.{key}"] = value
    ctx["mrv_qc_checks.qc_result"] = " | ".join(qc_checks.values())


def build_qc_result(is_pass, pass_detail, warn_detail):
    status = "PASS" if is_pass else "WARN"
    detail = pass_detail if is_pass else warn_detail
    return f"{status} · {detail}"


def add_opinion_context(ctx, monthly_rows, form_values, emission_value):
    usage_unit = ctx.get("mrv_activity_data.usage_unit", EMPTY_VALUE)
    materiality_rate = get_materiality_rate(form_values.get("materialityThreshold"))
    adj_total = sum(
        normalize_number(row.get("adjustment"))
        for row in monthly_rows
        if isinstance(row, dict)
    )
    if abs(adj_total) < 0.001:
        ctx["mrv_opinion.fuel_adj_status"] = "PASS"
        ctx["mrv_opinion.fuel_adj_value"] = f"0.00 {usage_unit}"
        ctx["mrv_opinion.fuel_adj_reason"] = "조정값 합계 = 0"
    else:
        sign = "+" if adj_total > 0 else ""
        ctx["mrv_opinion.fuel_adj_status"] = "WARN"
        ctx["mrv_opinion.fuel_adj_value"] = f"{sign}{format_number(adj_total, 2)} {usage_unit}"
        ctx["mrv_opinion.fuel_adj_reason"] = "조정값 존재 — 원인 확인 필요"

    op_total = normalize_number(form_values.get("operatingUnitTotal"))
    declared_op_total = normalize_number(form_values.get("declaredOperatingUnitTotal"))
    op_unit = clean_value(form_values.get("operatingUnitUnit")) or clean_value(form_values.get("declaredOperatingUnitUnit"))
    op_diff_pct = EMPTY_VALUE
    op_rejection_reason = EMPTY_VALUE

    if declared_op_total > 0 and op_total > 0:
        diff_pct = abs(declared_op_total - op_total) / op_total * 100
        op_diff_pct = f"{diff_pct:.1f}%"
        if diff_pct > materiality_rate * 100:
            op_rejection_reason = (
                f"선언값 {format_number(declared_op_total, 1)} {op_unit} vs "
                f"측정값 {format_number(op_total, 1)} {op_unit}, 차이 {diff_pct:.1f}%"
            )
            ctx["mrv_opinion.op_decl_status"] = "WARN · Rejected"
            ctx["mrv_opinion.op_decl_reason"] = "선언값과 측정값 차이 확인 필요"
            ctx["mrv_opinion.op_acc_status"] = "PASS · Accepted"
            ctx["mrv_opinion.op_acc_reason"] = "측정 운영단위 채택"
        else:
            ctx["mrv_opinion.op_decl_status"] = "PASS"
            ctx["mrv_opinion.op_decl_reason"] = "선언값과 측정값 허용 기준 내"
            ctx["mrv_opinion.op_acc_status"] = "PASS · Accepted"
            ctx["mrv_opinion.op_acc_reason"] = "선언 운영단위와 측정값 일치 확인"
        ctx["mrv_opinion.op_decl_value"] = f"{format_number(declared_op_total, 1)} {op_unit}".strip()
        ctx["mrv_opinion.op_acc_value"] = f"{format_number(op_total, 1)} {op_unit}".strip()
    elif declared_op_total > 0:
        ctx["mrv_opinion.op_decl_status"] = "WARN"
        ctx["mrv_opinion.op_decl_value"] = f"{format_number(declared_op_total, 1)} {op_unit}".strip()
        ctx["mrv_opinion.op_decl_reason"] = "선언 운영단위만 입력됨"
        ctx["mrv_opinion.op_acc_status"] = EMPTY_VALUE
        ctx["mrv_opinion.op_acc_value"] = EMPTY_VALUE
        ctx["mrv_opinion.op_acc_reason"] = "교차검증 입력 없음"
    elif op_total > 0:
        ctx["mrv_opinion.op_decl_status"] = EMPTY_VALUE
        ctx["mrv_opinion.op_decl_value"] = EMPTY_VALUE
        ctx["mrv_opinion.op_decl_reason"] = "선언 운영단위 입력 없음"
        ctx["mrv_opinion.op_acc_status"] = "PASS · Accepted"
        ctx["mrv_opinion.op_acc_value"] = f"{format_number(op_total, 1)} {op_unit}".strip()
        ctx["mrv_opinion.op_acc_reason"] = "측정 운영단위 입력 확인"
    else:
        for key in ("op_decl_status", "op_decl_value", "op_decl_reason", "op_acc_status", "op_acc_value", "op_acc_reason"):
            ctx[f"mrv_opinion.{key}"] = EMPTY_VALUE

    ctx["mrv_opinion.fuel_eff_status"] = EMPTY_VALUE
    ctx["mrv_opinion.fuel_eff_value"] = EMPTY_VALUE
    ctx["mrv_opinion.fuel_eff_reason"] = EMPTY_VALUE
    has_warn = any(
        str(ctx.get(f"mrv_opinion.{key}", "")).startswith("WARN")
        for key in ("fuel_adj_status", "op_decl_status", "fuel_eff_status")
    )
    ctx["mrv_opinion.overall_status"] = "WARN" if has_warn else "PASS"
    ctx["mrv_opinion.border_color"] = "#fbbf24" if has_warn else "#6ee7b7"
    ctx["mrv_opinion.readiness_risk"] = "Medium" if has_warn else "Low"
    ctx["mrv_opinion.readiness_verdict"] = (
        "검토 필요 — WARN 항목 존재"
        if has_warn else
        "제출 가능 — WARN 항목 없음"
    )
    ctx["mrv_opinion.llm_decl_val"] = ctx.get("mrv_opinion.op_decl_value", EMPTY_VALUE)
    ctx["mrv_opinion.llm_acc_val"] = ctx.get("mrv_opinion.op_acc_value", EMPTY_VALUE)
    ctx["mrv_opinion.llm_diff_pct"] = op_diff_pct
    ctx["mrv_opinion.llm_rejection_reason"] = op_rejection_reason
    ctx["mrv_opinion.llm_adj_total"] = ctx["mrv_opinion.fuel_adj_value"]
    ctx["mrv_opinion.llm_fuel_eff_cv"] = ctx["mrv_opinion.fuel_eff_value"]
    ctx["mrv_opinion.llm_emissions"] = f"{format_number(emission_value, 2)} {ctx.get('mrv_calculation_result.emissions_unit', 'tCO2e')}"
    ctx["mrv_opinion.llm_op_unit"] = op_unit or EMPTY_VALUE


def add_sensitivity_context(ctx, form_values, emission_value):
    usage = normalize_number(form_values.get("usage"))
    materiality_rate = get_materiality_rate(form_values.get("materialityThreshold"))
    ctx["sensitivity_usage_base"] = format_number(usage, 2)
    ctx["sensitivity_usage_minus"] = format_number(usage * (1 - materiality_rate), 2)
    ctx["sensitivity_usage_plus"] = format_number(usage * (1 + materiality_rate), 2)
    ctx["sensitivity_usage_notes"] = f"활동자료 ±{materiality_rate * 100:.1f}% 민감도"
    ctx["sensitivity_ef_base"] = clean_value(form_values.get("emissionFactor")) or "0"
    ctx["sensitivity_ef_minus"] = format_number(normalize_number(form_values.get("emissionFactor")) * (1 - materiality_rate), 4)
    ctx["sensitivity_ef_plus"] = format_number(normalize_number(form_values.get("emissionFactor")) * (1 + materiality_rate), 4)
    ctx["sensitivity_ef_notes"] = f"배출계수 ±{materiality_rate * 100:.1f}% 민감도"


def add_context_aliases(ctx):
    ctx["unit"] = ctx.get("mrv_activity_data.usage_unit", EMPTY_VALUE)
    ctx["ef_unit"] = ctx.get("mrv_emission_factor_ref.emission_factor_unit", EMPTY_VALUE)
    ctx["emission_unit"] = ctx.get("mrv_calculation_result.emissions_unit", EMPTY_VALUE)
    ctx["org_inclusions"] = ctx.get("mrv_report.included_entities_sites", EMPTY_VALUE)
    ctx["org_exclusions"] = ctx.get("mrv_report.excluded_entities_sites", EMPTY_VALUE)
    for key, value in list(ctx.items()):
        if "." in key:
            ctx.setdefault(key.split(".")[-1], value)


async def render_legacy_report_html(db_ctx):
    html = load_legacy_template()
    tags = list(dict.fromkeys(re.findall(r"\{\{llm:[^}]+\}\}", html)))
    llm_values = {}
    failed_tags = []
    parallel = int(os.environ.get("MRV_LLM_PARALLEL", "2") or "2")
    semaphore = asyncio.Semaphore(max(1, parallel))

    async def resolve_tag(tag):
        async with semaphore:
            value = await resolve_legacy_llm_tag(tag, db_ctx)
            return tag, value

    if tags:
        results = await asyncio.gather(*(resolve_tag(tag) for tag in tags), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                failed_tags.append(str(result))
                continue
            tag, value = result
            field = parse_llm_field(tag)
            if not value or value == "LLM 실패":
                failed_tags.append(field or tag)
                value = EMPTY_VALUE
            llm_values[field or tag] = value
            html = html.replace(tag, escape_html(value))

    html = replace_db_tags(html, db_ctx)
    return html, llm_values, failed_tags


def render_legacy_report_html_without_llm(db_ctx):
    html = load_legacy_template()
    html = re.sub(r"\{\{llm:[^}]+\}\}", EMPTY_VALUE, html)
    return replace_db_tags(html, db_ctx)


def render_legacy_report_preview_html(db_ctx):
    html = load_legacy_template()
    html = re.sub(r"\{\{llm:[^}]+\}\}", '<span class="llm-preview-empty"></span>', html)
    html = replace_db_tags(html, db_ctx)
    return inject_report_preview_style(html)


def inject_report_preview_style(html):
    style = """
<style>
  .llm-preview-empty {
    display: inline-block;
    min-width: 1px;
    min-height: 1em;
  }
</style>"""
    marker = "</head>"
    if marker in html:
        return html.replace(marker, f"{style}\n{marker}", 1)
    return f"{style}\n{html}"


def render_legacy_report_shell(db_ctx, report_token):
    html = load_legacy_template()
    tags = list(dict.fromkeys(re.findall(r"\{\{llm:[^}]+\}\}", html)))

    for index, tag in enumerate(tags):
        placeholder = (
            f'<span id="llm-{index}" class="llm-placeholder" '
            f'data-llm-field="{escape_html(parse_llm_field(tag))}">처리중...</span>'
        )
        html = html.replace(tag, placeholder)

    html = replace_db_tags(html, db_ctx)
    html = inject_report_shell_script(html, report_token, tags)
    return html, tags


def inject_report_shell_script(html, report_token, tags):
    script = build_report_shell_script(report_token, tags)
    marker = "</body>"
    if marker in html:
        return html.replace(marker, f"{script}\n{marker}", 1)
    return f"{html}\n{script}"


def build_report_shell_script(report_token, tags):
    api_base_url = get_public_api_base_url()
    script_config = json.dumps(
        {
            "apiBaseUrl": api_base_url,
            "reportToken": report_token,
            "tags": tags,
        },
        ensure_ascii=False,
    ).replace("{{llm:", "\\u007b\\u007bllm:")
    return f"""
<style>
  .static-typing-text {{
    white-space: pre-wrap;
  }}

  .llm-placeholder {{
    display: inline-block;
    min-width: 72px;
    min-height: 1em;
    border-radius: 4px;
    background: #edf7f6;
    color: #277a75;
    font-weight: 800;
  }}
</style>
<script>
(function () {{
  var config = {script_config};
  var tags = Array.isArray(config.tags) ? config.tags : [];
  var typingNodeSelector = "script, style, noscript, canvas, svg, textarea, select, option";

  function shouldTypeTextNode(node) {{
    if (!node || !node.nodeValue || !node.nodeValue.trim()) {{
      return false;
    }}

    var parent = node.parentElement;
    if (!parent || parent.closest(typingNodeSelector)) {{
      return false;
    }}

    if (parent.closest(".llm-placeholder")) {{
      return false;
    }}

    return true;
  }}

  function collectStaticTextNodes() {{
    var walker = document.createTreeWalker(
      document.body,
      NodeFilter.SHOW_TEXT,
      {{
        acceptNode: function (node) {{
          return shouldTypeTextNode(node) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
        }}
      }}
    );
    var nodes = [];
    while (walker.nextNode()) {{
      nodes.push(walker.currentNode);
    }}
    return nodes;
  }}

  function wrapStaticTextNode(node) {{
    var span = document.createElement("span");
    span.className = "static-typing-text";
    span.dataset.typingText = node.nodeValue;
    span.textContent = "";
    node.parentNode.replaceChild(span, node);
    return span;
  }}

  function typeStaticText(element, index) {{
    var source = element.dataset.typingText || "";
    var cursor = 0;
    var chunkSize = source.length > 120 ? Math.ceil(source.length / 60) : 1;
    var interval = source.length > 120 ? 8 : 12;

    function tick() {{
      element.textContent += source.slice(cursor, cursor + chunkSize);
      cursor += chunkSize;
      if (cursor < source.length) {{
        window.setTimeout(tick, interval);
      }}
    }}

    window.setTimeout(tick, Math.min(index * 8, 320));
  }}

  function typeStaticReportText() {{
    collectStaticTextNodes()
      .map(wrapStaticTextNode)
      .forEach(typeStaticText);
  }}

  function typeIn(element, text) {{
    var source = String(text || "");
    var index = 0;
    element.textContent = "";

    function tick() {{
      element.textContent += source.charAt(index);
      index += 1;
      if (index < source.length) {{
        window.setTimeout(tick, 18);
      }}
    }}

    tick();
  }}

  function resolveTag(tag, index) {{
    var element = document.getElementById("llm-" + index);
    if (!element) {{
      return Promise.resolve();
    }}

    return fetch(config.apiBaseUrl + "/report/html/llm", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{
        report_token: config.reportToken,
        tag: tag
      }})
    }})
      .then(function (response) {{
        return response.json();
      }})
      .then(function (data) {{
        element.classList.remove("llm-placeholder");
        typeIn(element, data.value || "—");
      }})
      .catch(function () {{
        element.classList.remove("llm-placeholder");
        element.textContent = "LLM 실패";
      }});
  }}

  typeStaticReportText();

  Promise.all(tags.map(resolveTag)).then(function () {{
    if (window.parent) {{
      window.parent.postMessage({{
        type: "mrv-report-ready",
        reportToken: config.reportToken
      }}, "*");
    }}
  }});
}}());
</script>"""


def load_legacy_template():
    template_path = TEMPLATE_DIR / "template.html"
    partial_path = TEMPLATE_DIR / "partials" / "c_scope1.html"
    html = template_path.read_text(encoding="utf-8")
    partial_html = partial_path.read_text(encoding="utf-8") if partial_path.exists() else ""
    return html.replace("{{partial:c_section}}", partial_html)


def replace_db_tags(html, db_ctx):
    def repl(match):
        key = match.group(1).strip()
        value = get_context_value(db_ctx, key)
        if key in RAW_HTML_KEYS or key.startswith("chart_") or key.endswith("_json"):
            return value
        return escape_html(value)

    return re.sub(r"\{\{db:([^}]+)\}\}", repl, html)


RAW_HTML_KEYS = {
    "monthly_table_rows",
    "monthly_usage_emissions_rows",
    "evidence_register_table_rows",
    "changelog_table_rows",
    "mrv_report.etl_transform_ops_steps",
}


async def resolve_legacy_llm_tag(tag, db_ctx):
    data = await call_mrv_solution_llm_api(tag, db_ctx)
    if not isinstance(data, dict):
        return "LLM 실패"
    value = str(data.get("value") or "").strip()
    return value or "LLM 실패"




def parse_llm_field(tag):
    tag = normalize_llm_tag(tag)
    match = re.search(r"\{\{llm:([a-zA-Z0-9_]+)", tag)
    return match.group(1) if match else ""




def normalize_llm_tag(tag):
    return str(tag).replace("\u200b", "").replace("\ufeff", "")




def get_context_value(db_ctx, key):
    key = clean_value(key)
    if key.startswith("db:"):
        key = key[3:]
    value = db_ctx.get(key)
    if value is None and "." in key:
        value = db_ctx.get(key.split(".")[-1])
    if value is None:
        return EMPTY_VALUE
    text = str(value).strip()
    return text or EMPTY_VALUE






def get_legacy_scope_label(scope_type):
    if scope_type == "scope1_mobile":
        return "Scope 1 Mobile Combustion"
    if scope_type == "scope2_electricity":
        return "Scope 2 Purchased Electricity"
    if scope_type == "scope1_stationary":
        return "Scope 1 Stationary Combustion"
    return clean_value(scope_type) or EMPTY_VALUE


def build_yoy_change_text(emission_value, prior_value):
    prior = normalize_number(prior_value)
    if prior <= 0:
        return EMPTY_VALUE
    change = ((emission_value - prior) / prior) * 100
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.1f}%"




def summarize_monthly_rows(monthly_rows, emission_factor):
    total = 0
    peak_value = 0
    peak_period = ""
    warn_periods = []

    for row in monthly_rows:
        if not isinstance(row, dict):
            continue
        final_value = get_monthly_final_value(row)
        total += final_value
        if final_value > peak_value:
            peak_value = final_value
            peak_period = clean_value(row.get("period"))
        dq_flag = clean_value(row.get("dqFlag"))
        if dq_flag and dq_flag.upper() != "OK":
            warn_periods.append(clean_value(row.get("period")))

    return {
        "total": total,
        "peak_value": peak_value,
        "peak_period": peak_period,
        "warn_periods": warn_periods,
        "emission_total": calculate_emission(total, emission_factor),
    }


def get_monthly_final_value(row):
    return normalize_number(row.get("rawValue")) + normalize_number(row.get("adjustment"))


def calculate_emission(usage, emission_factor):
    return normalize_number(usage) * normalize_number(emission_factor) / 1000


def get_materiality_rate(value):
    rate = normalize_number(value)
    if rate <= 0:
        return 0.05
    if rate > 1:
        rate = rate / 100
    return rate


def normalize_number(value):
    if value is None:
        return 0
    text = str(value).replace(",", "").strip()
    if not text:
        return 0
    try:
        return float(text)
    except ValueError:
        return 0


def clean_value(value):
    if value is None:
        return ""
    return str(value).strip()


def format_number(value, digits):
    return f"{value:,.{digits}f}".rstrip("0").rstrip(".")
