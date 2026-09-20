"""Validate current revisions and write an explicitly incomplete-or-complete delivery."""

from collections import Counter

from .assessment import assess, revised_investigation
from .contracts import Intelligence, Issue
from .investigation import MaterialTools, merge_facts
from .workspace import digest, input_fingerprint, latest


def is_synthetic(candidate, investigation, manifest):
    reserved = ("example.com", "example.net", "example.org")
    return (manifest.synthetic or investigation.execution != "live"
            or any(candidate.domain == d or candidate.domain.endswith("." + d) for d in reserved))


def eligible(candidate, investigation, assessment, manifest):
    if not investigation or not assessment:
        raise ValueError("no completed investigation and assessment")
    if investigation.analysis is None or investigation.failure_reason:
        raise ValueError("latest investigation failed without a valid model analysis")
    if manifest.domain_state.get(candidate.domain) != "completed":
        raise ValueError("latest domain execution is unfinished or failed")
    if (assessment.investigation_version != investigation.version
            or assessment.fingerprint != investigation.fingerprint):
        raise ValueError("stale assessment: investigation version or fingerprint mismatch")
    if investigation.fingerprint != input_fingerprint(candidate, investigation.materials, manifest):
        raise ValueError("candidate, materials or configuration changed since analysis")
    if not (candidate.run_id == investigation.run_id == assessment.run_id == manifest.run_id
            and candidate.domain == investigation.domain == assessment.domain):
        raise ValueError("record identity mismatch")
    tools = MaterialTools(candidate.domain, investigation.materials)
    for call in investigation.tool_calls:
        if call.result == "ok":
            tools.execute(call.name, {"domain": candidate.domain, "material_ids": call.material_ids})
    tools.validate_analysis(investigation.analysis)
    if investigation.facts != merge_facts(investigation.materials, investigation.analysis):
        raise ValueError("stored facts differ from validated evidence")
    current = investigation
    if assessment.review_status == "completed":
        if not assessment.review or assessment.review.assessment_version != assessment.version - 1:
            raise ValueError("completed review does not reference the preceding assessment")
        if (assessment.review.run_id, assessment.review.domain) != (assessment.run_id, assessment.domain):
            raise ValueError("review identity mismatch")
        current = revised_investigation(investigation, assessment.review)
    expected = assess(current, manifest.policy, assessment.version)
    for name in ("label", "confidence", "confidence_grade", "confidence_method", "confidence_basis"):
        if getattr(expected, name) != getattr(assessment, name):
            raise ValueError(f"assessment does not match recomputed {name}")
    if assessment.confidence is None or assessment.review_status == "pending":
        raise ValueError("confidence undetermined or necessary review pending")
    if expected.review_required and assessment.review_status != "completed":
        raise ValueError("necessary review has not been completed")
    if assessment.review_required and assessment.review_status != "completed":
        raise ValueError("declared review is incomplete")
    if assessment.review_status == "not_required" and assessment.review is not None:
        raise ValueError("unnecessary review attached to automatic result")


def collect(manifest, candidates, investigations, assessments, issues):
    invs, ass = latest(investigations), latest(assessments)
    rows, skipped, generated_issues, synthetic = [], [], [], []
    for candidate in candidates:
        inv, assessment = invs.get(candidate.domain), ass.get(candidate.domain)
        try:
            eligible(candidate, inv, assessment, manifest)
            row = Intelligence(domain=candidate.domain, label=assessment.label, confidence=assessment.confidence,
                               evidence=inv.materials, reason=assessment.reason, discovery_source=candidate.sources,
                               last_verified=max(m.collected_at for m in inv.materials),
                               confidence_method=assessment.confidence_method, assessment_version=assessment.version,
                               review_status=assessment.review_status)
            rows.append(row)
            if is_synthetic(candidate, inv, manifest):
                synthetic.append(candidate.domain)
        except ValueError as exc:
            skipped.append({"domain": candidate.domain, "reason": str(exc)})
            generated_issues.append(Issue(run_id=manifest.run_id, domain=candidate.domain,
                                          stage="export", reason=str(exc)))
    formal = [r for r in rows if r.domain not in synthetic]
    real_expansion = any(e.material_ids and e.execution == "live" and e.seed_domain in {r.domain for r in formal}
                         for e in manifest.expansions)
    checks = {
        "classification_criteria": True,
        "at_least_50_real_domains": len(formal) >= 50,
        "real_seed_expansion_attempt": real_expansion,
        "six_real_cases_and_error_analysis": False,
        "live_agent_used": any(r.domain not in synthetic and invs[r.domain].requests > 0 for r in rows),
        "json_intelligence": bool(formal),
        "proposal_docx_max_5_pages_manually_verified": False,
        "runnable_prototype": True,
    }
    summary = {
        "run_id": manifest.run_id, "overall_complete": all(checks.values()),
        "candidate_count": len(candidates), "exported_count": len(rows), "formal_result_count": len(formal),
        "registered_domain_count": len({c.registered_domain for c in candidates}),
        "formal_registered_domain_count": len({c.registered_domain for c in candidates
                                              if c.domain in {r.domain for r in formal}}),
        "label_distribution": dict(Counter(r.label for r in formal)),
        "synthetic_domains": synthetic,
        "evidence_coverage": {"with_materials": sum(bool(i.materials) for i in invs.values()),
                              "successful_material_domains": sum(any(m.access_status == "ok" for m in i.materials)
                                                                  for i in invs.values())},
        "pending_review": [d for d, a in ass.items() if a.review_status == "pending"],
        "failed_domains": [d for d, s in manifest.domain_state.items() if s == "failed"],
        "unfinished": skipped,
        "issues": issues + generated_issues,
        "expansion": manifest.expansions,
        "checks": checks, "delivery_missing": [k for k, v in checks.items() if not v],
        "confidence_method": manifest.policy.confidence_method,
        "confidence_basis": manifest.policy.confidence_basis,
        "intelligence_digest": digest(rows),
    }
    return rows, summary, generated_issues


def export(workspace, manifest, candidates, investigations, assessments, issues):
    rows, summary, new_issues = collect(manifest, candidates, investigations, assessments, issues)
    manifest.status, manifest.export_completed = "exporting", False
    workspace.write(workspace.run_file(manifest.run_id, "manifest.json"), manifest)
    try:
        workspace.write(workspace.run_file(manifest.run_id, "exports/intelligence.json"), rows)
        workspace.write(workspace.run_file(manifest.run_id, "exports/summary.json"), summary)
        if new_issues:
            # Avoid multiplying identical export issues on repeated exports.
            seen = {(i.domain, i.location, i.stage, i.reason) for i in issues}
            issues = issues + [i for i in new_issues if (i.domain, i.location, i.stage, i.reason) not in seen]
            workspace.save_records(manifest.run_id, "issues.jsonl", issues)
        manifest.status, manifest.export_completed = "exported", True
        workspace.write(workspace.run_file(manifest.run_id, "manifest.json"), manifest)
    except OSError:
        manifest.status, manifest.export_completed = "failed", False
        # The persisted pre-write marker is already false even if this save fails.
        workspace.write(workspace.run_file(manifest.run_id, "manifest.json"), manifest)
        raise
    return summary
