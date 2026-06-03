"""
VCPilot — Rule Engine
Loads all RuleConfig rows from DB and provides helpers to check rules
against a stock's data. Each rule returns a RuleResult with pass/fail + detail.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Any
from loguru import logger

from app.database import get_db
from app.models.config import RuleConfig, RuleCategory
from app.models.audit import AuditLog, AuditAction


@dataclass
class RuleResult:
    rule_id: str
    passed: bool
    value: Optional[Any] = None       # Actual value evaluated
    threshold: Optional[Any] = None   # Threshold used
    message: str = ""

    def __bool__(self):
        return self.passed


@dataclass
class ScreenResult:
    """Aggregated result for one stock on one screening run."""
    ticker: str
    passed: bool = False
    rule_results: dict[str, RuleResult] = field(default_factory=dict)
    trend_score: int = 0      # 0–8 (Minervini trend template criteria met)
    fund_score: int = 0       # Fundamental criteria met
    rs_rating: float = 0.0

    @property
    def summary(self) -> dict:
        return {
            rule_id: {"passed": r.passed, "value": r.value, "threshold": r.threshold}
            for rule_id, r in self.rule_results.items()
        }


class RuleEngine:
    """
    Loads active rules from the database and evaluates them against stock data.
    Instantiate once per screener run; rules are cached for the run duration.
    """

    def __init__(self, tier: str = "ADMIN"):
        self.tier = tier
        self._rules: dict[str, RuleConfig] = {}
        self._load_rules()

    def _load_rules(self):
        with get_db() as db:
            rules = db.query(RuleConfig).filter(
                RuleConfig.enabled_globally == True
            ).order_by(RuleConfig.sort_order).all()
            self._rules = {r.rule_id: r for r in rules}
        logger.debug(f"RuleEngine loaded {len(self._rules)} active rules for tier={self.tier}")

    def is_enabled(self, rule_id: str) -> bool:
        rule = self._rules.get(rule_id)
        if not rule:
            return False
        return rule.is_enabled_for_tier(self.tier)

    def threshold(self, rule_id: str) -> Optional[float]:
        rule = self._rules.get(rule_id)
        if not rule:
            return None
        return rule.threshold_for_tier(self.tier)

    def get_enabled_by_category(self, category: RuleCategory) -> list[RuleConfig]:
        return [
            r for r in self._rules.values()
            if r.category == category and r.is_enabled_for_tier(self.tier)
        ]

    @staticmethod
    def log_audit(action: AuditAction, ticker: str, detail: dict):
        try:
            with get_db() as db:
                db.add(AuditLog(
                    action=action,
                    actor="screener",
                    ticker=ticker,
                    detail=detail,
                ))
        except Exception as e:
            logger.warning(f"Audit log failed: {e}")
