from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from engine.llm_constraints import evaluate_constraint_feasibility

router = APIRouter(prefix="/api/constraints", tags=["constraints"])

class ConstraintQuery(BaseModel):
    query: str
    run_id: Optional[int] = None

@router.post("/evaluate")
def evaluate_constraint(body: ConstraintQuery):
    # In a real implementation, we'd need to fetch the problem data for the run_id
    # For now, we pass None and let the engine handle missing problem data
    result = evaluate_constraint_feasibility(body.query, None)
    return result
