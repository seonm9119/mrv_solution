import asyncio
import json
from datetime import datetime, timezone
from html import escape as escape_html
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Body

from app.report.router import (
    EMPTY_VALUE,
    LLM_REQUEST_SEMAPHORE,
    build_legacy_db_context,
    build_report_context,
    calculate_emission,
    clean_value,
    get_public_api_base_url,
    get_section_llm_timeout_seconds,
    normalize_number,
    resolve_legacy_llm_tag,
)


router = APIRouter()

AI_ANALYSIS_ENDPOINT_INFO = {
    "status": "ready",
    "mode": "frontend-input-to-ai-analysis-template",
    "generate_endpoint": "/api/ai_analysis/generate",
}
AI_ANALYSIS_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "ai_analysis.html"
AI_ANALYSIS_CONTEXTS = {}
AI_ANALYSIS_CONTEXT_LIMIT = 20

AI_ANALYSIS_TAGS = {
    "qaqc_readiness": "{{llm:qaqc_readiness_narrative|ref=db:mrv_opinion.llm_decl_val,db:mrv_opinion.llm_acc_val,db:mrv_opinion.llm_diff_pct,db:mrv_opinion.llm_rejection_reason,db:mrv_opinion.llm_adj_total,db:mrv_opinion.llm_fuel_eff_cv,db:mrv_opinion.llm_emissions,db:mrv_opinion.llm_op_unit}}",
    "monthly_spike": "{{llm:monthly_spike_analysis|ref=db:monthly_summary_text,db:chart_summary.max_month_label,db:chart_summary.max_month_value,db:chart_summary.min_month_label,db:chart_summary.min_month_value,db:mrv_report_activity_uploads.file_operation_log}}",
    "fuel_reduction": "{{llm:fuel_reduction_levers|ref=db:mrv_activity_data.usage,db:mrv_activity_data.usage_unit,db:mrv_calculation_result.emission,db:mrv_calculation_result.emissions_unit,db:mrv_activity_data.activity_name,db:mrv_activity_data.facility}}",
    "benchmark": "{{llm:industry_benchmark|ref=db:mrv_activity_data.activity_name,db:mrv_activity_data.facility,db:mrv_activity_data.usage,db:mrv_activity_data.usage_unit,db:mrv_calculation_result.emission,db:mrv_calculation_result.emissions_unit}}",
    "cost_reduction": "{{llm:cost_reduction_analysis|ref=db:mrv_activity_data.usage,db:mrv_activity_data.usage_unit,db:mrv_calculation_result.emission,db:mrv_calculation_result.emissions_unit,db:mrv_activity_data.activity_name,db:mrv_activity_data.facility,db:mrv_emission_factor_ref.emission_factor,db:monthly_summary_text}}",
}

AI_ANALYSIS_LLM_KEY_TAGS = {
    "qaqc_narrative": AI_ANALYSIS_TAGS["qaqc_readiness"],
    "spike_analysis": AI_ANALYSIS_TAGS["monthly_spike"],
    "reduction_levers": AI_ANALYSIS_TAGS["fuel_reduction"],
    "benchmark": AI_ANALYSIS_TAGS["benchmark"],
    "cost_reduction": AI_ANALYSIS_TAGS["cost_reduction"],
    "cost_trend": AI_ANALYSIS_TAGS["cost_reduction"],
    "target_proposal": AI_ANALYSIS_TAGS["fuel_reduction"],
}


@router.get("/ai_analysis")
def ai_analysis():
    return AI_ANALYSIS_ENDPOINT_INFO


@router.post("/ai_analysis/generate")
async def generate_ai_analysis(payload=Body(default={})):
    form_values = payload.get("formValues") if isinstance(payload, dict) else {}
    monthly_rows = payload.get("monthlyRows") if isinstance(payload, dict) else []
    derived_emission = payload.get("derivedEmission") if isinstance(payload, dict) else None

    if not isinstance(form_values, dict):
        form_values = {}
    if not isinstance(monthly_rows, list):
        monthly_rows = []

    context = build_report_context(form_values, monthly_rows, derived_emission)
    db_ctx = build_legacy_db_context(form_values, monthly_rows, derived_emission)
    analysis_token = store_ai_analysis_context(context["report_id"], db_ctx)
    html = build_ai_analysis_shell_html(context, db_ctx, analysis_token)

    return {
        "status": "rendering",
        "report_id": context["report_id"],
        "analysis_token": analysis_token,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "awesomi": {
            "status": "pending",
            "rendered_tags": 0,
            "total_tags": len(AI_ANALYSIS_LLM_KEY_TAGS),
            "failed_tags": [],
        },
        "qwen": {
            "status": "pending",
            "rendered_tags": 0,
            "total_tags": len(AI_ANALYSIS_LLM_KEY_TAGS),
            "failed_tags": [],
        },
        "sections": {},
        "html": html,
    }


@router.post("/ai_analysis/html/llm")
async def generate_ai_analysis_llm(payload=Body(default={})):
    analysis_token = clean_value(payload.get("analysis_token")) if isinstance(payload, dict) else ""
    key = clean_value(payload.get("key")) if isinstance(payload, dict) else ""
    if not analysis_token or analysis_token not in AI_ANALYSIS_CONTEXTS:
        return {"value": EMPTY_VALUE, "status": "missing_context"}
    if key not in AI_ANALYSIS_LLM_KEY_TAGS:
        return {"value": EMPTY_VALUE, "status": "unknown_key"}

    context = AI_ANALYSIS_CONTEXTS[analysis_token]
    if key in context["values"]:
        return {"value": context["values"][key], "status": "cached"}

    try:
        async with LLM_REQUEST_SEMAPHORE:
            value = await asyncio.wait_for(
                resolve_legacy_llm_tag(AI_ANALYSIS_LLM_KEY_TAGS[key], context["db_ctx"]),
                timeout=get_section_llm_timeout_seconds(),
            )
    except Exception as error:
        value = f"LLM 실패: {error}"

    value = clean_value(value)
    if not value or value == "LLM 실패":
        value = EMPTY_VALUE
    context["values"][key] = value
    return {"value": value, "status": "ok"}


def store_ai_analysis_context(report_id, db_ctx):
    analysis_token = f"{clean_value(report_id) or 'MRV-AI'}-{uuid4().hex[:12]}"
    AI_ANALYSIS_CONTEXTS[analysis_token] = {
        "db_ctx": dict(db_ctx),
        "values": {},
    }

    while len(AI_ANALYSIS_CONTEXTS) > AI_ANALYSIS_CONTEXT_LIMIT:
        oldest_key = next(iter(AI_ANALYSIS_CONTEXTS))
        AI_ANALYSIS_CONTEXTS.pop(oldest_key, None)

    return analysis_token


async def build_ai_analysis_sections(db_ctx):
    async def resolve_section(section_name, tag):
        try:
            async with LLM_REQUEST_SEMAPHORE:
                value = await asyncio.wait_for(
                    resolve_legacy_llm_tag(tag, db_ctx),
                    timeout=get_section_llm_timeout_seconds(),
                )
        except Exception:
            value = EMPTY_VALUE
        return section_name, clean_value(value) or EMPTY_VALUE

    results = await asyncio.gather(
        *(resolve_section(name, tag) for name, tag in AI_ANALYSIS_TAGS.items()),
        return_exceptions=True,
    )
    sections = {}
    for result in results:
        if isinstance(result, Exception):
            continue
        section_name, value = result
        sections[section_name] = value
    return sections


def build_ai_analysis_shell_html(context, db_ctx, analysis_token):
    row = build_legacy_analysis_row(context, db_ctx)
    charts = build_legacy_analysis_charts(db_ctx, row)
    meta = build_legacy_analysis_meta(db_ctx)
    opinion = build_legacy_analysis_opinion(db_ctx)
    llm_insights = build_legacy_llm_insights({})
    llm_insights.update({
        key: build_ai_llm_placeholder(key)
        for key in AI_ANALYSIS_LLM_KEY_TAGS
    })
    html = render_ai_analysis_template(row, charts, meta, opinion, llm_insights, escape_llm=False)
    return inject_ai_analysis_shell_script(html, analysis_token)


def build_ai_analysis_html(context, db_ctx, sections, escape_llm=True):
    row = build_legacy_analysis_row(context, db_ctx)
    charts = build_legacy_analysis_charts(db_ctx, row)
    meta = build_legacy_analysis_meta(db_ctx)
    opinion = build_legacy_analysis_opinion(db_ctx)
    llm_insights = build_legacy_llm_insights(sections)

    return render_ai_analysis_template(row, charts, meta, opinion, llm_insights, escape_llm=escape_llm)


def render_ai_analysis_template(row, charts, meta, opinion, llm_insights, escape_llm=True):
    html = AI_ANALYSIS_TEMPLATE_PATH.read_text(encoding="utf-8")
    context = build_ai_analysis_template_context(row, charts, meta, opinion, llm_insights, escape_llm=escape_llm)
    for key, value in context.items():
        html = html.replace(f"{{{{{key}}}}}", str(value))
    return html


def build_ai_analysis_template_context(row, charts, meta, opinion, llm_insights, escape_llm=True):
    monthly_fuel = charts.get("monthly_fuel") or {}
    monthly_emission = charts.get("monthly_emission") or {}
    monthly_cost = charts.get("monthly_cost") or {}
    scenarios = (charts.get("emission_scenarios") or {}).get("scenarios") or []
    labels = as_list(monthly_fuel.get("labels"))
    fuel_values = [value or 0 for value in as_list(monthly_fuel.get("values"))]
    emission_values = [value or 0 for value in as_list(monthly_emission.get("values"))]
    annual_usage = normalize_number(monthly_fuel.get("total") or row.get("usage"))
    annual_emission = normalize_number(monthly_emission.get("total") or row.get("annual_emissions_tco2e"))
    unit_price = normalize_number(monthly_cost.get("unit_price")) or infer_unit_price(row.get("usage_unit"))
    total_cost = normalize_number(monthly_cost.get("total_cost")) or annual_usage * unit_price
    cost_values = [round((value or 0) * unit_price / 1000, 1) for value in fuel_values]
    bau_cost_values = [round(value * 1.03, 1) for value in cost_values]
    opt_cost_values = [round(value * 0.90, 1) for value in cost_values]
    prior_emission = normalize_number(row.get("prior_year_emission"))
    yoy_change = EMPTY_VALUE
    if prior_emission > 0 and annual_emission > 0:
        change = ((annual_emission - prior_emission) / prior_emission) * 100
        yoy_change = f"{change:+.1f}%"

    return {
        "activity_name": h(row.get("activity_name")),
        "facility": h(row.get("facility")),
        "report_id": h(row.get("report_id")),
        "inventory_year": h(row.get("inventory_year")),
        "annual_usage": fmt(annual_usage, 1),
        "usage_unit": h(row.get("usage_unit")),
        "annual_emission": fmt(annual_emission, 1),
        "emissions_unit": h(row.get("emissions_unit")),
        "save10_cost_k": fmt(total_cost * 0.1 / 1000, 0),
        "save10_emission": fmt(annual_emission * 0.1, 2),
        "yoy_change": h(yoy_change),
        "prior_year_emission": fmt(prior_emission, 1) if prior_emission else EMPTY_VALUE,
        "emission_factor": fmt(row.get("emission_factor"), 4),
        "emission_factor_unit": h(row.get("emission_factor_unit")),
        "ef_tier": h(row.get("ef_tier")),
        "ef_source": h(row.get("ef_source")),
        "instrument_name": h(meta.get("instrument_name")),
        "instrument_accuracy": h(meta.get("instrument_accuracy")),
        "instrument_id": h(meta.get("instrument_id")),
        "calibration_date": h(meta.get("calibration_date")),
        "calibration_evidence_file": h(meta.get("calibration_evidence_file")),
        "data_source": h(row.get("data_source")),
        "verification_standard": h(row.get("verification_standard")),
        "organization_name": h(meta.get("organization_name")),
        "asset_name": h(row.get("asset_name")),
        "data_owner": h(row.get("data_owner")),
        "total_cost_m": fmt(total_cost / 1_000_000, 1),
        "total_cost": fmt(total_cost, 0),
        "unit_price": fmt(unit_price, 0),
        "overall_status": h(opinion.get("overall_status")),
        "readiness_verdict": h(opinion.get("readiness_verdict")),
        "opinion_rows_html": build_opinion_rows_html(opinion),
        "invoice_rows_html": build_invoice_rows_html(labels, fuel_values, unit_price),
        "scenario_cards_html": build_scenario_cards_html(scenarios, row.get("usage_unit"), unit_price),
        "matrix_rows_html": build_matrix_rows_html(scenarios, annual_usage, annual_emission, row.get("usage_unit"), unit_price, llm_insights),
        "monthly_labels_json": json.dumps(labels, ensure_ascii=False),
        "fuel_values_json": json.dumps(fuel_values, ensure_ascii=False),
        "emission_values_json": json.dumps(emission_values, ensure_ascii=False),
        "cost_values_json": json.dumps(cost_values, ensure_ascii=False),
        "bau_cost_values_json": json.dumps(bau_cost_values, ensure_ascii=False),
        "opt_cost_values_json": json.dumps(opt_cost_values, ensure_ascii=False),
        "scenario_5_values_json": json.dumps([round(value * 0.95, 2) for value in fuel_values], ensure_ascii=False),
        "scenario_10_values_json": json.dumps([round(value * 0.90, 2) for value in fuel_values], ensure_ascii=False),
        "scenario_15_values_json": json.dumps([round(value * 0.85, 2) for value in fuel_values], ensure_ascii=False),
        "qaqc_narrative": render_llm_template_value(llm_insights.get("qaqc_narrative"), escape_llm),
        "spike_analysis": render_llm_template_value(llm_insights.get("spike_analysis"), escape_llm),
        "reduction_levers": render_llm_template_value(llm_insights.get("reduction_levers"), escape_llm),
        "benchmark": render_llm_template_value(llm_insights.get("benchmark"), escape_llm),
        "cost_reduction": render_llm_template_value(llm_insights.get("cost_reduction"), escape_llm),
        "cost_trend": render_llm_template_value(llm_insights.get("cost_trend"), escape_llm),
        "target_proposal": render_llm_template_value(llm_insights.get("target_proposal"), escape_llm),
    }


def render_llm_template_value(value, escape_llm=True):
    if escape_llm:
        return h(value)
    return clean_value(value) or EMPTY_VALUE


def build_ai_llm_placeholder(key):
    return (
        f'<span class="llm-placeholder" data-ai-llm-placeholder="{h(key)}">'
        "처리중..."
        "</span>"
    )


def inject_ai_analysis_shell_script(html, analysis_token):
    script = build_ai_analysis_shell_script(analysis_token)
    marker = "</body>"
    if marker in html:
        return html.replace(marker, f"{script}\n{marker}", 1)
    return f"{html}\n{script}"


def build_ai_analysis_shell_script(analysis_token):
    script_config = json.dumps(
        {
            "apiBaseUrl": get_public_api_base_url(),
            "analysisToken": analysis_token,
            "keys": list(AI_ANALYSIS_LLM_KEY_TAGS.keys()),
        },
        ensure_ascii=False,
    )
    return f"""
<style>
  .llm-placeholder {{
    display: inline-block;
    min-width: 72px;
    min-height: 1em;
    border-radius: 4px;
    background: #edf7f6;
    color: #277a75;
    font-weight: 800;
  }}

  .llm-cursor::after {{
    content: "|";
    color: #1f8a70;
    animation: llmBlink .7s step-end infinite;
  }}

  @keyframes llmBlink {{
    50% {{ opacity: 0; }}
  }}
</style>
<script>
(function () {{
  var config = {script_config};
  var keys = Array.isArray(config.keys) ? config.keys : [];
  var statusBadge = document.getElementById("llm-status-badge");

  function setStatus(text) {{
    if (statusBadge) {{
      statusBadge.textContent = text;
    }}
  }}

  function typeIn(element, text) {{
    var source = String(text || "{EMPTY_VALUE}");
    var cursor = 0;
    var chunk = source.length > 140 ? Math.ceil(source.length / 70) : 1;
    var interval = source.length > 140 ? 8 : 16;

    element.textContent = "";
    element.classList.add("llm-cursor");

    function tick() {{
      element.textContent += source.slice(cursor, cursor + chunk);
      cursor += chunk;
      if (cursor < source.length) {{
        window.setTimeout(tick, interval);
      }} else {{
        element.classList.remove("llm-cursor");
      }}
    }}

    tick();
  }}

  function applyLlm(key, content) {{
    document.querySelectorAll('[data-llm-key="' + key + '"]').forEach(function (element) {{
      var placeholder = element.querySelector(".llm-placeholder");
      if (placeholder) {{
        placeholder.remove();
      }}
      typeIn(element, content || "{EMPTY_VALUE}");
    }});
  }}

  function resolveKey(key) {{
    return fetch(config.apiBaseUrl + "/ai_analysis/html/llm", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{
        analysis_token: config.analysisToken,
        key: key
      }})
    }})
      .then(function (response) {{
        return response.json();
      }})
      .then(function (data) {{
        applyLlm(key, data.value || "{EMPTY_VALUE}");
      }})
      .catch(function () {{
        applyLlm(key, "LLM 실패");
      }});
  }}

  setStatus("AI 분석 중...");
  Promise.all(keys.map(resolveKey)).then(function () {{
    setStatus("AI 분석 완료");
    if (window.parent) {{
      window.parent.postMessage({{
        type: "mrv-ai-analysis-ready",
        analysisToken: config.analysisToken
      }}, "*");
    }}
  }});
}}());
</script>
"""


def build_opinion_rows_html(opinion):
    rows = [
        ("Fuel AD Adjustment", opinion.get("fuel_adj_value"), opinion.get("fuel_adj_reason"), opinion.get("fuel_adj_status")),
        ("Operating Unit - Declared", opinion.get("op_decl_value"), opinion.get("op_decl_reason"), opinion.get("op_decl_status")),
        ("Operating Unit - Accepted", opinion.get("op_acc_value"), opinion.get("op_acc_reason"), opinion.get("op_acc_status")),
        ("Fuel Efficiency Consistency", opinion.get("fuel_eff_value"), opinion.get("fuel_eff_reason"), opinion.get("fuel_eff_status")),
    ]
    return "".join(
        "<tr>"
        f"<td>{h(title)}</td>"
        f"<td>{h(value)}</td>"
        f"<td>{h(reason)}</td>"
        f"<td><span class=\"status-pill\">{h(status)}</span></td>"
        "</tr>"
        for title, value, reason, status in rows
    )


def build_invoice_rows_html(labels, fuel_values, unit_price):
    rows = []
    for index, value in enumerate(fuel_values):
        label = labels[index] if index < len(labels) else f"P{index + 1}"
        cost = (value or 0) * unit_price
        rows.append(
            "<tr>"
            f"<td>{h(label)}</td>"
            f"<td>{fmt(value, 2)}</td>"
            f"<td>{fmt(unit_price, 0)}</td>"
            f"<td>{fmt(cost, 0)}</td>"
            "</tr>"
        )
    return "".join(rows)


def build_scenario_cards_html(scenarios, usage_unit, unit_price):
    if not scenarios:
        return '<div class="sc-item">시나리오 데이터가 없습니다.</div>'

    cards = []
    for index, scenario in enumerate(scenarios[:4]):
        rate = normalize_number(scenario.get("rate"))
        saved_usage = normalize_number(scenario.get("saved_usage"))
        saved_emission = normalize_number(scenario.get("saved_emission"))
        after_usage = normalize_number(scenario.get("after_usage"))
        saved_cost_k = saved_usage * unit_price / 1000
        width = min(100, max(12, int(rate * 500)))
        cards.append(
            '<div class="sc-item">'
            '<div class="sc-header">'
            f'<div class="sc-title">{h(scenario.get("label") or f"시나리오 {index + 1}")}</div>'
            f'<div class="sc-savings">{fmt(saved_cost_k, 0)}천 KRW 절감</div>'
            '</div>'
            '<div class="sc-bar-bg">'
            f'<div class="sc-bar-fill" style="width:{width}%"></div>'
            '</div>'
            '<div class="sc-details">'
            f'<div class="sc-detail">연료 절감: <strong>{fmt(saved_usage, 1)} {h(usage_unit)}</strong></div>'
            f'<div class="sc-detail">CO2e 감축: <strong>{fmt(saved_emission, 2)} tCO2e</strong></div>'
            f'<div class="sc-detail">절감 후 소비: <strong>{fmt(after_usage, 1)} {h(usage_unit)}</strong></div>'
            '</div>'
            '</div>'
        )
    return "".join(cards)


def build_matrix_rows_html(scenarios, annual_usage, annual_emission, usage_unit, unit_price, llm_insights):
    action_map = [
        llm_insights.get("action_a"),
        llm_insights.get("action_b"),
        llm_insights.get("action_c"),
        llm_insights.get("action_d"),
    ]
    rows = [
        "<tr>"
        "<td><strong>현상 유지 (BAU)</strong></td>"
        "<td>+3%</td>"
        f"<td>{fmt(annual_usage * 1.03, 0)} {h(usage_unit)}</td>"
        f"<td>{fmt(annual_usage * 1.03 * unit_price / 10000, 1)}만원</td>"
        f"<td>-{fmt(annual_emission * 0.03, 2)}</td>"
        "<td>변경 없음</td>"
        "<td>비권장</td>"
        "</tr>"
    ]
    for index, scenario in enumerate(scenarios[:4]):
        rate = normalize_number(scenario.get("rate"))
        after_usage = normalize_number(scenario.get("after_usage"))
        saved_emission = normalize_number(scenario.get("saved_emission"))
        rows.append(
            "<tr>"
            f"<td><strong>{h(scenario.get('label') or f'시나리오 {index + 1}')}</strong></td>"
            f"<td>-{int(rate * 100)}%</td>"
            f"<td>{fmt(after_usage, 0)} {h(usage_unit)}</td>"
            f"<td>{fmt(after_usage * unit_price / 10000, 1)}만원</td>"
            f"<td>+{fmt(saved_emission, 2)}</td>"
            f"<td>{h(action_map[index % len(action_map)])}</td>"
            f"<td>{h(['즉시', 'H1', 'H2', '차년도'][index % 4])}</td>"
            "</tr>"
        )
    return "".join(rows)


def h(value):
    return escape_html(clean_value(value) or EMPTY_VALUE)


def fmt(value, digits):
    number = normalize_number(value)
    rendered = f"{number:,.{digits}f}"
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def build_legacy_analysis_row(context, db_ctx):
    summary = context.get("summary") or {}
    emission = db_number(db_ctx, "mrv_calculation_result.emission", summary.get("emission"))
    if not emission:
        emission = db_number(db_ctx, "mrv_calculation_result.annual_emissions_tco2e", 0)

    usage = db_number(db_ctx, "mrv_activity_data.usage", summary.get("monthly_total"))
    if not usage:
        usage = db_number(db_ctx, "mrv_activity_data.annual_fuel_usage_nm3", 0)

    return {
        "report_id": context.get("report_id") or db_text(db_ctx, "mrv_report.report_id"),
        "report_version": db_text(db_ctx, "mrv_report.report_version"),
        "asset_name": db_text(db_ctx, "mrv_report.asset_name"),
        "data_owner": db_text(db_ctx, "mrv_report.data_owner"),
        "verification_standard": db_text(db_ctx, "mrv_report.verification_standard"),
        "base_year": db_text(db_ctx, "mrv_report.base_year"),
        "program_regime": db_text(db_ctx, "mrv_report.program_regime"),
        "activity_name": db_text(db_ctx, "mrv_activity_data.activity_name", summary.get("activity")),
        "facility": db_text(db_ctx, "mrv_activity_data.facility", summary.get("facility")),
        "inventory_year": db_text(db_ctx, "mrv_activity_data.inventory_year", summary.get("inventory_year")),
        "usage": usage,
        "usage_unit": db_text(db_ctx, "mrv_activity_data.usage_unit", summary.get("usage_unit")) or "Nm3",
        "data_source": db_text(db_ctx, "mrv_activity_data.data_source"),
        "aggregation_basis": db_text(db_ctx, "mrv_activity_data.aggregation_basis"),
        "emission": emission,
        "annual_emissions_tco2e": emission,
        "prior_year_emission": db_number(db_ctx, "mrv_calculation_result.prior_year_emission", 0),
        "scope": db_text(db_ctx, "mrv_calculation_result.scope"),
        "combined_uncertainty": db_text(db_ctx, "mrv_calculation_result.combined_uncertainty"),
        "emissions_unit": db_text(db_ctx, "mrv_calculation_result.emissions_unit", "tCO2e"),
        "emission_factor": db_number(db_ctx, "mrv_emission_factor_ref.emission_factor", 0),
        "emission_factor_unit": db_text(db_ctx, "mrv_emission_factor_ref.emission_factor_unit"),
        "ef_source": db_text(db_ctx, "mrv_emission_factor_ref.ef_source"),
        "ef_tier": db_text(db_ctx, "mrv_emission_factor_ref.ef_tier"),
    }


def build_legacy_analysis_meta(db_ctx):
    return {
        "organization_name": db_text(db_ctx, "mrv_document_metadata.organization_name"),
        "instrument_name": db_text(db_ctx, "mrv_document_metadata.instrument_name"),
        "instrument_id": db_text(db_ctx, "mrv_document_metadata.instrument_id"),
        "calibration_date": db_text(db_ctx, "mrv_document_metadata.calibration_date"),
        "instrument_accuracy": db_text(db_ctx, "mrv_document_metadata.instrument_accuracy"),
        "calibration_evidence_file": db_text(db_ctx, "mrv_document_metadata.calibration_evidence_file"),
    }


def build_legacy_analysis_opinion(db_ctx):
    return {
        "overall_status": db_text(db_ctx, "mrv_opinion.overall_status"),
        "readiness_risk": db_text(db_ctx, "mrv_opinion.readiness_risk"),
        "readiness_verdict": db_text(db_ctx, "mrv_opinion.readiness_verdict"),
        "fuel_adj_value": db_text(db_ctx, "mrv_opinion.fuel_adj_value"),
        "fuel_adj_reason": db_text(db_ctx, "mrv_opinion.fuel_adj_reason"),
        "fuel_adj_status": db_text(db_ctx, "mrv_opinion.fuel_adj_status"),
        "op_decl_value": db_text(db_ctx, "mrv_opinion.op_decl_value"),
        "op_decl_reason": db_text(db_ctx, "mrv_opinion.op_decl_reason"),
        "op_decl_status": db_text(db_ctx, "mrv_opinion.op_decl_status"),
        "op_acc_value": db_text(db_ctx, "mrv_opinion.op_acc_value"),
        "op_acc_reason": db_text(db_ctx, "mrv_opinion.op_acc_reason"),
        "op_acc_status": db_text(db_ctx, "mrv_opinion.op_acc_status"),
        "fuel_eff_value": db_text(db_ctx, "mrv_opinion.fuel_eff_value"),
        "fuel_eff_reason": db_text(db_ctx, "mrv_opinion.fuel_eff_reason"),
        "fuel_eff_status": db_text(db_ctx, "mrv_opinion.fuel_eff_status"),
    }


def build_legacy_analysis_charts(db_ctx, row):
    chart = parse_json_dict(db_ctx.get("chart_monthly_json"))
    labels = as_list(chart.get("labels"))
    fuel_values = [number_or_none(value) for value in as_list(chart.get("fuel"))]
    emission_values = [number_or_none(value) for value in as_list(chart.get("emissions"))]

    usage_unit = row.get("usage_unit") or chart.get("unit") or "Nm3"
    emissions_unit = row.get("emissions_unit") or chart.get("emissions_unit") or "tCO2e"
    annual_usage = sum(value for value in fuel_values if value is not None) or normalize_number(row.get("usage"))
    annual_emission = sum(value for value in emission_values if value is not None) or normalize_number(row.get("annual_emissions_tco2e"))
    emission_factor = normalize_number(row.get("emission_factor"))
    unit_price = infer_unit_price(usage_unit)

    if labels and fuel_values and not emission_values:
        emission_values = [calculate_emission(value or 0, emission_factor) for value in fuel_values]

    charts = {
        "monthly_fuel": {
            "labels": labels,
            "values": fuel_values,
            "unit": usage_unit,
            "total": annual_usage,
        },
        "monthly_emission": {
            "labels": labels,
            "values": emission_values,
            "unit": emissions_unit,
            "total": annual_emission,
        },
        "monthly_cost": {
            "unit_price": unit_price,
            "total_cost": annual_usage * unit_price,
            "monthly_costs": build_monthly_costs(labels, fuel_values, unit_price),
        },
        "emission_scenarios": {
            "annual_usage": annual_usage,
            "annual_emission": annual_emission,
            "usage_unit": usage_unit,
            "ef": emission_factor,
            "scenarios": build_emission_scenarios(annual_usage, annual_emission),
        },
    }

    prior_emission = normalize_number(row.get("prior_year_emission"))
    if prior_emission > 0 and annual_emission > 0:
        charts["yoy_comparison"] = {
            "prior_emission": prior_emission,
            "current_emission": annual_emission,
            "change_pct": ((annual_emission - prior_emission) / prior_emission) * 100,
            "current_year": row.get("inventory_year"),
        }

    return charts


def build_monthly_costs(labels, fuel_values, unit_price):
    costs = []
    for index, value in enumerate(fuel_values):
        qty = value or 0
        costs.append({
            "period_label": labels[index] if index < len(labels) else f"P{index + 1}",
            "qty": qty,
            "value": qty,
            "unit_price": unit_price,
            "cost": round(qty * unit_price, 0),
        })
    return costs


def build_emission_scenarios(annual_usage, annual_emission):
    scenarios = []
    for rate in (0.05, 0.10, 0.15, 0.20):
        saved_usage = annual_usage * rate
        saved_emission = annual_emission * rate
        scenarios.append({
            "label": f"{int(rate * 100)}% 효율 개선",
            "rate": rate,
            "saved_usage": saved_usage,
            "saved_emission": saved_emission,
            "after_usage": annual_usage - saved_usage,
            "after_emission": annual_emission - saved_emission,
        })
    return scenarios


def build_legacy_llm_insights(sections):
    qaqc = sections.get("qaqc_readiness") or EMPTY_VALUE
    spike = sections.get("monthly_spike") or EMPTY_VALUE
    reduction = sections.get("fuel_reduction") or EMPTY_VALUE
    benchmark = sections.get("benchmark") or EMPTY_VALUE
    cost = sections.get("cost_reduction") or EMPTY_VALUE

    return {
        "qaqc_narrative": qaqc,
        "spike_analysis": spike,
        "reduction_levers": reduction,
        "benchmark": benchmark,
        "cost_reduction": cost,
        "yoy_cost_analysis": cost,
        "cost_trend": cost,
        "cost_strategy": reduction,
        "baseline_narrative": spike,
        "peak_analysis": spike,
        "valley_analysis": spike,
        "maintenance_note": qaqc,
        "target_proposal": reduction,
        "short_term": reduction,
        "mid_term": benchmark,
        "long_term": cost,
        "action_a": "운항/운전 절차 최적화",
        "action_b": "설비 제어 조건 최적화",
        "action_c": "정비 주기 및 이상치 관리 강화",
        "action_d": "고효율 장비 전환 검토",
        "bau_rate": 3.0,
        "opt_rate": 10.0,
        "baseline_rate": 12.0,
    }


def parse_json_dict(raw_value):
    if isinstance(raw_value, dict):
        return raw_value
    try:
        parsed = json.loads(raw_value or "{}")
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def as_list(value):
    return value if isinstance(value, list) else []


def number_or_none(value):
    if value is None or value == "":
        return None
    return normalize_number(value)


def infer_unit_price(usage_unit):
    unit = clean_value(usage_unit).lower()
    if "kwh" in unit:
        return 130
    if unit == "l":
        return 1700
    return 700


def db_text(db_ctx, key, fallback=""):
    value = clean_value(db_ctx.get(key))
    if not value or value == EMPTY_VALUE:
        value = clean_value(fallback)
    return value or EMPTY_VALUE


def db_number(db_ctx, key, fallback=0):
    value = db_ctx.get(key)
    if value in (None, "", EMPTY_VALUE):
        value = fallback
    return normalize_number(value)
