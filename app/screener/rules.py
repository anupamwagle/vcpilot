"""
AstraTrade — Rule Engine
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
    trend_score: int = 0      # 0–8 (AstraTrade trend template criteria met)
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

    asset_type: "EQUITY" | "CRYPTO" — filters which rules are evaluated.
      Rules marked asset_types="EQUITY" are skipped when asset_type="CRYPTO" and vice-versa.
      Rules marked asset_types="BOTH" are always evaluated.
    """

    def __init__(
        self,
        organization_id: Optional[int] = None,
        tier: str = "GOLD",
        asset_type: str = "EQUITY",
    ):
        self.organization_id = organization_id
        self.tier = tier
        self.asset_type = asset_type.upper()   # "EQUITY" or "CRYPTO"
        self._rules: dict[str, RuleConfig] = {}
        self._signal_overrides: dict[str, bool] = {}   # per-signal temporary overrides
        self._load_rules()

    def _load_rules(self):
        with get_db() as db:
            query = db.query(RuleConfig)
            if self.organization_id is not None:
                rules = query.filter(
                    RuleConfig.organization_id == self.organization_id
                ).order_by(RuleConfig.sort_order).all()
                if not rules:
                    rules = query.filter(
                        RuleConfig.organization_id == None
                    ).order_by(RuleConfig.sort_order).all()
            else:
                rules = query.filter(
                    RuleConfig.organization_id == None
                ).order_by(RuleConfig.sort_order).all()
            self._rules = {r.rule_id: r for r in rules}
        logger.debug(f"RuleEngine loaded {len(self._rules)} rules for org={self.organization_id}, tier={self.tier}")

    def _rule_applies_to_asset(self, rule: RuleConfig) -> bool:
        """Return True if this rule applies to the current asset_type."""
        rule_scope = (rule.asset_types or "BOTH").upper()
        if rule_scope == "BOTH":
            return True
        return rule_scope == self.asset_type

    def is_enabled(self, rule_id: str) -> bool:
        rule = self._rules.get(rule_id)
        if not rule:
            return False
        # Skip rules that don't apply to this asset type
        if not self._rule_applies_to_asset(rule):
            return False
        # Signal override: respected only when rule is globally enabled and not mandatory
        if rule_id in self._signal_overrides and rule.enabled_globally and not rule.is_mandatory:
            return self._signal_overrides[rule_id]
        return rule.is_enabled_for_tier(self.tier)

    def apply_signal_overrides(self, overrides: dict):
        """
        Temporarily override rule enabled-state for the current signal.
        Mandatory rules and globally-disabled rules are immune.
        """
        for rule_id, enabled in overrides.items():
            rule = self._rules.get(rule_id)
            if rule and rule.enabled_globally and not rule.is_mandatory:
                self._signal_overrides[rule_id] = bool(enabled)

    def clear_signal_overrides(self):
        """Reset all per-signal overrides (call after processing each signal)."""
        self._signal_overrides.clear()

    def get_rule_meta(self, rule_id: str) -> Optional[dict]:
        """Return label, mandatory flag, and global-enabled flag for UI rendering."""
        rule = self._rules.get(rule_id)
        if not rule:
            return None
        return {
            "rule_id": rule.rule_id,
            "label": rule.label,
            "is_mandatory": rule.is_mandatory,
            "globally_enabled": rule.enabled_globally,
            "tier_enabled": rule.is_enabled_for_tier(self.tier),
        }

    def all_rules_meta(self) -> list[dict]:
        """Return metadata for all rules — used for per-signal override UI."""
        return [
            {
                "rule_id": r.rule_id,
                "label": r.label,
                "category": r.category.value,
                "is_mandatory": r.is_mandatory,
                "globally_enabled": r.enabled_globally,
                "tier_enabled": r.is_enabled_for_tier(self.tier),
            }
            for r in sorted(self._rules.values(), key=lambda x: x.sort_order)
        ]

    def threshold(self, rule_id: str) -> Optional[float]:
        rule = self._rules.get(rule_id)
        if not rule:
            return None
        return rule.threshold_for_tier(self.tier)

    def get_enabled_by_category(self, category: RuleCategory) -> list[RuleConfig]:
        return [
            r for r in self._rules.values()
            if r.category == category
            and r.is_enabled_for_tier(self.tier)
            and self._rule_applies_to_asset(r)
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
