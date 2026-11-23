"""Action plan: deterministic phase assignment. No LLM."""

from __future__ import annotations

from collections.abc import Iterable

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import SEVERITY_ORDER, Requirements, Severity
from seoprobe.core.rules.catalog import t

from .models import ActionPlan, AuditReport, Phase, PhaseItem, Recommendation

IMPACT_W = {"low": 1.0, "medium": 2.0, "high": 3.0}
CONF_W = {"low": 0.5, "medium": 0.75, "high": 1.0}
EFFORT_W = {"S": 1.0, "M": 2.0, "L": 4.0}
EFFORT_HOURS = {"S": (0.25, 1.0), "M": (2.0, 8.0), "L": (8.0, 24.0)}
SEV_PHASE = {
    Severity.CRITICAL: 1,
    Severity.HIGH: 2,
    Severity.MEDIUM: 3,
    Severity.LOW: 4,
    Severity.INFO: 4,
}
_PHASES = [
    (1, "plan.phase1", "plan.week1"),
    (2, "plan.phase2", "plan.weeks2-3"),
    (3, "plan.phase3", "plan.month2"),
    (4, "plan.phase4", "plan.ongoing"),
]


def priority_score(impact: str, confidence: str, effort: str) -> float:
    return round(IMPACT_W[impact] * CONF_W[confidence] / EFFORT_W[effort], 3)


def is_quick_win(r: Recommendation) -> bool:
    return r.effort.level == "S" and r.impact.level in ("medium", "high")


def find_cycles(recs: Iterable[Recommendation]) -> list[str]:
    """Return ids that sit on a dependency cycle (empty when the graph is acyclic)."""
    graph = {r.id: [d for d in r.depends_on] for r in recs}
    state: dict[str, int] = {}
    bad: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if nxt not in graph:
                continue
            if state.get(nxt) == 1:
                bad.update(stack[stack.index(nxt) :])
            elif nxt not in state:
                visit(nxt, stack)
        stack.pop()
        state[node] = 2

    for n in list(graph):
        if n not in state:
            visit(n, [])
    return sorted(bad)


def assign_phases(recs: list[Recommendation], gate_rec_ids: set[str]) -> dict[str, int]:
    """Base phase from severity; gate -> 1; quick-win bump; dependants never move earlier."""
    phase: dict[str, int] = {}
    for r in recs:
        p = SEV_PHASE[r.severity]
        if r.id in gate_rec_ids:
            p = 1
        elif is_quick_win(r) and r.severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM):
            p = max(1, p - 1)
        phase[r.id] = p
    by_id = {r.id: r for r in recs}
    for _ in range(len(recs) + 1):  # monotone and bounded by 4 -> terminates even with cycles
        changed = False
        for r in recs:
            for d in r.depends_on:
                if d in by_id and phase[r.id] < phase[d]:
                    phase[r.id] = phase[d]
                    changed = True
        if not changed:
            break
    return phase


def _sort_key(r: Recommendation) -> tuple[float, int, str]:
    return (
        -r.priority_score,
        SEVERITY_ORDER.index(r.severity),
        r.check_ids[0] if r.check_ids else "",
    )


def build_action_plan(
    recs: list[Recommendation], gate_rec_ids: set[str], lang: str
) -> tuple[ActionPlan, dict[str, int]]:
    phase_of = assign_phases(recs, gate_rec_ids)
    phases: list[Phase] = []
    for number, name_key, tf_key in _PHASES:
        members = sorted((r for r in recs if phase_of[r.id] == number), key=_sort_key)
        items = [PhaseItem(recommendation_id=r.id, order=i + 1) for i, r in enumerate(members)]
        notes = [t(lang, "plan.monitor1"), t(lang, "plan.monitor2")] if number == 4 else []
        phases.append(
            Phase(
                id=f"phase-{number}",
                number=number,
                name_key=name_key,
                name=t(lang, name_key),
                timeframe_key=tf_key,
                timeframe=t(lang, tf_key),
                items=items,
                notes=notes,
            )
        )
    return ActionPlan(phases=phases), phase_of


@operation("report.plan", output=ActionPlan, requires=Requirements(network=False), timeout_s=30)
async def plan(ctx: Context, report: AuditReport) -> Outcome[ActionPlan]:
    """The 4-phase action plan of a report's recommendations."""
    gate_finding_ids = {f.id for f in report.findings if f.is_gate}
    gate_rec_ids = {r.id for r in report.recommendations if set(r.finding_ids) & gate_finding_ids}
    action_plan, _ = build_action_plan(report.recommendations, gate_rec_ids, report.audit.locale)
    return Outcome(data=action_plan)
