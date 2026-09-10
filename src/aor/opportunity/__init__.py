"""机会选择与评分依据；不改变既有商业门槛。"""

from aor.opportunity.basis import OpportunityError, validate_score_basis
from aor.opportunity.selection import build_experiment_context, select_candidates

__all__ = ["OpportunityError", "validate_score_basis", "build_experiment_context", "select_candidates"]
