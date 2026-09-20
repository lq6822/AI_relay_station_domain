"""sys.argv CLI and sequential batch orchestration; no workflow engine."""

import asyncio
import sys
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from . import agent, delivery
from .assessment import apply_review, assess
from .candidates import expand_seed, merge_ids, normalize, prepare
from .contracts import (
    Assessment, Candidate, Investigation, Issue, Lead, Manifest, Material, Policy, Review, now,
)
from .workspace import Workspace, digest, encode, implementation_digest, input_fingerprint, latest, validation_message

HELP = """Usage: relay-intel COMMAND --run-id ID [options]
Commands:
  run     [--leads data/inputs/leads.jsonl] [--materials data/inputs/materials.jsonl] [--synthetic]
  review  --actions data/inputs/reviews.jsonl
  expand  (one round, eligible initial confirmed seeds only)
  export  (valid current records and explicit completion summary)
Common: --root PATH (default: current directory)
Help: relay-intel --help | relay-intel COMMAND --help
API: ANTHROPIC_BASE_URL and ANTHROPIC_API_KEY; model fixed in config/policy.json
Exit: 0 command complete; 1 configuration/storage error; 2 usage error or unfinished work.
"""


def parse_args(argv):
    if not argv or argv == ["--help"] or (len(argv) == 2 and argv[1] == "--help" and argv[0] in
                                           {"run", "review", "expand", "export"}):
        return None
    command = argv[0]
    options = {"run": {"leads", "materials", "synthetic"}, "review": {"actions"},
               "expand": set(), "export": set()}
    if command not in options:
        raise ValueError("unknown command")
    allowed = options[command] | {"root", "run-id"}
    result = {"command": command}
    i = 1
    while i < len(argv):
        flag = argv[i]
        if not flag.startswith("--") or flag[2:] not in allowed or flag[2:] in result:
            raise ValueError(f"unknown or repeated option: {flag}")
        name = flag[2:]
        if name == "synthetic":
            result[name] = True
        else:
            i += 1
            if i == len(argv) or argv[i].startswith("--") or not argv[i].strip():
                raise ValueError(f"missing value for {flag}")
            result[name] = argv[i]
        i += 1
    if "run-id" not in result or (command == "review" and "actions" not in result):
        raise ValueError("missing --run-id or --actions")
    return result


def configuration(policy):
    return digest({"policy": policy, "prompt": agent.PROMPT_VERSION, "api_base_url": agent.api_url()})


def fingerprint(candidate, materials, manifest):
    return input_fingerprint(candidate, materials, manifest)


def load_state(ws, run_id):
    manifest = ws.read_json(ws.run_file(run_id, "manifest.json"), Manifest)
    if manifest.run_id != run_id:
        raise ValueError("manifest run_id mismatch")
    return (manifest, ws.read_records(run_id, "candidates.jsonl", Candidate),
            ws.read_records(run_id, "investigations.jsonl", Investigation),
            ws.read_records(run_id, "assessments.jsonl", Assessment),
            ws.read_records(run_id, "issues.jsonl", Issue))


def save_manifest(ws, manifest):
    ws.write(ws.run_file(manifest.run_id, "manifest.json"), manifest)


def add_issues(ws, run_id, issues, incoming):
    seen = {(i.domain, i.location, i.stage, i.reason) for i in issues}
    issues.extend(i for i in incoming if (i.domain, i.location, i.stage, i.reason) not in seen)
    if issues:
        ws.save_records(run_id, "issues.jsonl", issues)


async def analyze_domains(ws, manifest, candidates, materials, investigations, assessments, issues,
                          client=None, *, execution="live"):
    invs, ass = latest(investigations), latest(assessments)
    owned_client = None
    try:
        for candidate in candidates:
            domain_materials = [m for m in materials if m.domain == candidate.domain]
            previous_i, previous_a = invs.get(candidate.domain), ass.get(candidate.domain)
            key = fingerprint(candidate, domain_materials, manifest)
            if (previous_i and previous_a and previous_i.fingerprint == key
                    and previous_a.fingerprint == key and previous_a.investigation_version == previous_i.version
                    and manifest.domain_state.get(candidate.domain) == "completed"):
                continue
            manifest.domain_state[candidate.domain] = "pending"
            manifest.status, manifest.export_completed = "running", False
            save_manifest(ws, manifest)
            try:
                if not domain_materials:
                    raise agent.AgentFailure("no actual investigation material; candidate remains unfinished")
                if client is None:
                    owned_client = agent.create_client(manifest.policy)
                    client = owned_client
                investigation = await agent.analyze(
                    candidate, domain_materials, manifest.policy, key,
                    previous_i.version + 1 if previous_i else 1, client, execution=execution)
                investigations.append(investigation)
                ws.save_records(manifest.run_id, "investigations.jsonl", investigations)
                assessment = assess(investigation, manifest.policy, previous_a.version + 1 if previous_a else 1)
                assessments.append(assessment)
                ws.save_records(manifest.run_id, "assessments.jsonl", assessments)
                manifest.domain_state[candidate.domain] = "completed"
            except agent.AgentFailure as exc:
                failed = Investigation(run_id=manifest.run_id, domain=candidate.domain,
                    version=previous_i.version + 1 if previous_i else 1, fingerprint=key,
                    materials=domain_materials, facts=[], analysis=None, failure_reason=str(exc),
                    tool_calls=exc.tool_calls, requests=exc.requests, usage=exc.usage,
                    execution=execution, created_at=now())
                investigations.append(failed)
                ws.save_records(manifest.run_id, "investigations.jsonl", investigations)
                manifest.domain_state[candidate.domain] = "failed"
                add_issues(ws, manifest.run_id, issues, [Issue(run_id=manifest.run_id, domain=candidate.domain,
                    stage="agent", reason=f"{exc}; requests={exc.requests}; tools={encode(exc.tool_calls)}; usage={encode(exc.usage)}")])
            save_manifest(ws, manifest)
        manifest.status = "analyzed"
        save_manifest(ws, manifest)
    finally:
        if owned_client:
            await owned_client.close()


async def run(ws, run_id, policy, leads_path, materials_path, synthetic=False, client=None, execution="live"):
    manifest_path = ws.run_file(run_id, "manifest.json")
    if manifest_path.exists():
        manifest, candidates, investigations, assessments, issues = load_state(ws, run_id)
        if manifest.configuration_digest != configuration(policy) or manifest.synthetic != synthetic:
            raise ValueError("model, endpoint, policy, prompt or synthetic flag changed; create a new batch")
    else:
        manifest = Manifest(run_id=run_id, created_at=now(), policy=policy, program_version=__version__,
                            implementation_digest=implementation_digest(), prompt_version=agent.PROMPT_VERSION,
                            configuration_digest=configuration(policy), input_digest="pending", synthetic=synthetic,
                            api_base_url=agent.api_url(), status="running")
        candidates, investigations, assessments, issues = [], [], [], []
    leads, errors1 = ws.read_input(leads_path, Lead, policy, run_id)
    materials, errors2 = ws.read_input(materials_path, Material, policy, run_id)
    # Seed provenance is created only by the checked expand command, never accepted on trust.
    external_seed = [lead for lead in leads if lead.seed_domain]
    leads = [lead for lead in leads if not lead.seed_domain]
    errors1.extend(Issue(run_id=run_id, location=l.lead_id, stage="candidate",
                         reason="seed sources must be created by expand") for l in external_seed)
    candidates, errors3 = prepare(run_id, leads, candidates)
    domains = {c.domain for c in candidates}
    normalized = []
    for material in materials:
        try:
            domain = normalize(material.domain)
            if domain not in domains:
                raise ValueError("material does not belong to an existing candidate")
            material = material.model_copy(update={"domain": domain})
            from .investigation import MaterialTools
            MaterialTools(domain, [material])
            normalized.append(material)
        except ValueError as exc:
            errors2.append(Issue(run_id=run_id, location=material.material_id, stage="material", reason=str(exc)))
    old_materials = [m for inv in latest(investigations).values() for m in inv.materials]
    materials, errors4 = merge_ids(old_materials, normalized, "material_id", run_id)
    manifest.input_digest = digest({"leads": candidates, "materials": materials})
    manifest.implementation_digest = implementation_digest()
    manifest.domain_state = {c.domain: manifest.domain_state.get(c.domain, "pending") for c in candidates}
    manifest.export_completed, manifest.status = False, "running"
    save_manifest(ws, manifest)
    ws.save_records(run_id, "candidates.jsonl", candidates)
    add_issues(ws, run_id, issues, errors1 + errors2 + errors3 + errors4)
    await analyze_domains(ws, manifest, candidates, materials, investigations, assessments, issues, client,
                          execution=execution)
    return manifest, candidates, investigations, assessments, issues


def verify_configuration(ws, manifest):
    policy = ws.read_json(ws.path("config/policy.json"), Policy)
    if policy != manifest.policy or agent.PROMPT_VERSION != manifest.prompt_version:
        raise ValueError("configuration changed; create a new batch")
    if implementation_digest() != manifest.implementation_digest:
        raise ValueError("implementation changed; rerun analysis before review, expansion or export")


def review(ws, manifest, candidates, investigations, assessments, issues, actions_path):
    actions, errors = ws.read_input(actions_path, Review, manifest.policy, manifest.run_id)
    invs, ass = latest(investigations), latest(assessments)
    for action in actions:
        try:
            action = action.model_copy(update={"domain": normalize(action.domain)})
            current = ass.get(action.domain)
            if not current or manifest.domain_state.get(action.domain) != "completed":
                raise ValueError("review target has no completed current assessment")
            # Replaying an old exact action never approves a later investigation.
            if any(a.review == action for a in assessments):
                continue
            updated = apply_review(current, invs[action.domain], action, manifest.policy)
            manifest.export_completed = False
            save_manifest(ws, manifest)
            if updated is not current:
                assessments.append(updated)
                ass[action.domain] = updated
                ws.save_records(manifest.run_id, "assessments.jsonl", assessments)
        except ValueError as exc:
            errors.append(Issue(run_id=manifest.run_id, domain=action.domain, stage="review", reason=str(exc)))
    add_issues(ws, manifest.run_id, issues, errors)
    return len(errors)


async def expand(ws, manifest, candidates, investigations, assessments, issues, client=None, execution="live"):
    invs, ass = latest(investigations), latest(assessments)
    # Fix initial seed list before adding new candidates: one round, never recursive.
    seeds = [c for c in candidates if any(not source.seed_domain for source in c.sources)]
    owned_client, attempted = None, 0
    try:
        for seed in seeds:
            if any(e.seed_domain == seed.domain for e in manifest.expansions):
                continue
            inv, assessment = invs.get(seed.domain), ass.get(seed.domain)
            if not assessment or assessment.label != "确认":
                continue
            try:
                delivery.eligible(seed, inv, assessment, manifest)
            except ValueError:
                continue
            if client is None:
                owned_client = agent.create_client(manifest.policy)
                client = owned_client
            try:
                searched = await agent.analyze(seed, inv.materials, manifest.policy, inv.fingerprint, inv.version,
                                              client, expansion=True, execution=execution)
                # Search result is used only for validated links; original assessment is unchanged.
                leads, record = expand_seed(seed, searched, assessment, candidates, manifest.policy.max_expansion)
                candidates[:], rejected = prepare(manifest.run_id, leads, candidates)
                manifest.export_completed = False
                for candidate in candidates:
                    manifest.domain_state.setdefault(candidate.domain, "pending")
                save_manifest(ws, manifest)
                ws.save_records(manifest.run_id, "candidates.jsonl", candidates)
                manifest.expansions.append(record)
                save_manifest(ws, manifest)
                add_issues(ws, manifest.run_id, issues, rejected)
                attempted += 1
            except agent.AgentFailure as exc:
                add_issues(ws, manifest.run_id, issues, [Issue(run_id=manifest.run_id, domain=seed.domain,
                                                            stage="expand", reason=str(exc))])
        if not attempted and not manifest.expansions:
            add_issues(ws, manifest.run_id, issues, [Issue(run_id=manifest.run_id, stage="expand",
                                                        reason="no eligible seed or no successful actual lookup")])
    finally:
        if owned_client:
            await owned_client.close()
    return bool(manifest.expansions)


async def dispatch(options):
    ws = Workspace(options.get("root", Path.cwd()))
    run_id, command = options["run-id"], options["command"]
    ws.run_dir(run_id)
    with ws.lock(run_id):
        if command == "run":
            policy = ws.read_json(ws.path("config/policy.json"), Policy)
            state = await run(ws, run_id, policy, options.get("leads", "data/inputs/leads.jsonl"),
                              options.get("materials", "data/inputs/materials.jsonl"), options.get("synthetic", False))
            manifest, candidates, investigations, assessments, issues = state
            pending = sum(a.review_status == "pending" for a in latest(assessments).values())
            failed = sum(s != "completed" for s in manifest.domain_state.values())
            print(encode({"candidates": len(candidates), "pending_review": pending, "unfinished": failed}))
            return 2 if pending or failed else 0
        state = load_state(ws, run_id)
        manifest, candidates, investigations, assessments, issues = state
        verify_configuration(ws, manifest)
        if command == "review":
            errors = review(ws, *state, options["actions"])
            print(encode({"review_errors": errors}))
            return 2 if errors else 0
        if command == "expand":
            if agent.api_url() != manifest.api_base_url:
                raise ValueError("API endpoint changed; create a new batch")
            attempted = await expand(ws, *state)
            print(encode({"expansion_attempted": attempted, "candidates": len(candidates)}))
            return 0 if attempted else 2
        summary = delivery.export(ws, *state)
        print(encode({k: summary[k] for k in ("exported_count", "formal_result_count", "overall_complete")}))
        return 0 if summary["overall_complete"] else 2


def main(argv=None):
    try:
        options = parse_args(list(sys.argv[1:] if argv is None else argv))
    except ValueError as exc:
        print(f"argument error: {exc}", file=sys.stderr)
        return 2
    if options is None:
        print(HELP)
        return 0
    try:
        return asyncio.run(dispatch(options))
    except (ValueError, OSError, ValidationError) as exc:
        reason = validation_message(exc) if isinstance(exc, ValidationError) else str(exc)
        print(f"error: {reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
