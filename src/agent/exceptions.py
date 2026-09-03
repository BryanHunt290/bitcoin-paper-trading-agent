from pydantic import BaseModel
from typing import Optional


class PaperOrderRejectionModel(BaseModel):
    reason_code: str
    message: str
    correlation_id: Optional[str] = None
    signal_id: Optional[str] = None
    strategy_id: Optional[str] = None
    risk_decision_id: Optional[str] = None
    requested_notional: Optional[float] = None
    approved_notional: Optional[float] = None


class PaperOrderRejected(Exception):
    def __init__(self, details: PaperOrderRejectionModel):
        self.details = details
        super().__init__(str(details))

    def __str__(self) -> str:
        return f"PaperOrderRejected({self.details.reason_code}): {self.details.message}"
