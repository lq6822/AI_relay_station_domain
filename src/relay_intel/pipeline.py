"""业务主线：准备批次 → 逐域分析与保存 → 按需复核 → 关联扩展。"""

from . import agent
from .assessment import apply_review, assess
from .candidates import expand_seed, normalize, prepare_candidates
from .contracts import Batch, DomainResult, Issue, Lead, Material, Policy, Review
from .investigation import prepare_materials
from .workspace import Workspace


def prepare_batch(workspace: Workspace, run_id: str, policy: Policy, *,
                  leads_path="data/inputs/leads.jsonl",
                  materials_path="data/inputs/materials.jsonl", synthetic=False) -> Batch:
    """读取并整理固定输入，返回待分析批次；此步骤不调用模型、不写文件。"""
    # 一批对应一份固定输入；补材料或改规则时使用新的 run_id。
    if workspace.batch_dir(run_id).exists():
        raise ValueError("batch already exists; use a new run_id")

    # 1. 读入线索与证据，分别记录格式问题。
    leads, lead_issues = workspace.read_input(leads_path, Lead)
    materials, material_issues = workspace.read_input(materials_path, Material)
    # 2. 按完整主机名去重，再将证据归入对应域名。
    candidates, candidate_issues = prepare_candidates(leads)
    if not candidates:
        raise ValueError("no valid candidates; check the leads input")
    grouped, subject_issues = prepare_materials(materials, candidates)
    return Batch(
        run_id=run_id, policy=policy, synthetic=synthetic,
        results=[DomainResult(candidate=c, materials=grouped[c.domain]) for c in candidates],
        issues=lead_issues + material_issues + candidate_issues + subject_issues,
    )


async def run_batch(workspace: Workspace, batch: Batch, client) -> Batch:
    """分析尚待处理的域名，每处理完一个就保存批次。

    新批次来自 prepare_batch，中断后的批次来自 load_batch，两者走同一条主线。
    client 由 CLI 创建和关闭。单域模型失败记录错误后继续；文件写入失败立即上报。
    """
    # 首次请求之前保存材料和配置；继续执行时也只使用批次里已固定的输入。
    workspace.save_batch(batch)
    for result in batch.results:
        if not result.needs_analysis:
            continue
        try:
            # 一次单域工作包括模型分析和规则判定，全部完成后才更新并保存记录。
            investigation = await agent.analyze(
                result.candidate.domain, result.materials, batch.policy, client,
            )
            assessment = assess(result.materials, investigation.analysis)
            result.investigation, result.assessment = investigation, assessment
        except agent.AgentFailure as exc:
            result.error = str(exc)
        # 包括已记录的失败也立即保存；进程中断不会丢失此前完成的域名。
        workspace.save_batch(batch)
    return batch


def review_batch(workspace: Workspace, batch: Batch, actions_path: str) -> list[Issue]:
    """逐条应用本批次的复核意见，保存成功的修订并返回被拒绝的意见。

    apply_review 负责证据核验和重判，这里只负责定位域名及保存。
    """
    actions, issues = workspace.read_input(actions_path, Review)
    results = {result.candidate.domain: result for result in batch.results}
    for action in actions:
        try:
            action = action.model_copy(update={"domain": normalize(action.domain)})
            if action.run_id != batch.run_id or action.domain not in results:
                raise ValueError("review does not belong to this batch and domain")
            result = results[action.domain]
            # 同一条复核重复提交时跳过；不建立多轮审核或历史版本链。
            if result.review == action:
                continue
            updated = apply_review(result, action)
            result.assessment, result.review = updated, action
        except ValueError as exc:
            issues.append(Issue(location=action.domain, reason=str(exc)))
    batch.issues.extend(issues)
    workspace.save_batch(batch)
    return issues


def expand_batch(workspace: Workspace, batch: Batch) -> list[Lead]:
    """整理本批确认种子的关联线索，保存并返回下一批的 leads。

    每个种子仅处理一次，不递归、不自动取证；无新增也保存实际查找记录。
    新候选不会进入本批结果，需人工补材料后用新 run_id 独立分析。
    """
    # 模型分析时已查找材料中的关联链接；这里直接整理，不重复调用模型。
    known = {result.candidate.domain for result in batch.results}
    known.update(normalize(lead.raw_value) for item in batch.expansions for lead in item.leads)
    searched = {item.seed_domain for item in batch.expansions}
    for result in batch.results:
        assessment = result.assessment
        if (result.candidate.domain in searched or not assessment
                or assessment.label != "确认" or assessment.review_status == "pending"):
            continue
        expansion = expand_seed(result, known)
        batch.expansions.append(expansion)
        known.update(normalize(lead.raw_value) for lead in expansion.leads)
    leads = [lead for expansion in batch.expansions for lead in expansion.leads]
    workspace.save_batch(batch)
    workspace.write(workspace.batch_dir(batch.run_id) / "expanded_leads.jsonl", leads, jsonl=True)
    return leads
