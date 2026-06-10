"""AstraTrade Rules Configuration — enable/disable and tune thresholds."""
import sys; sys.path.insert(0, "/app")
import streamlit as st
import pandas as pd
from app.database import get_db
from app.models.config import RuleConfig, RuleCategory
from app.models.audit import AuditLog, AuditAction

st.set_page_config(page_title="Rules — AstraTrade", layout="wide")
st.title("⚙️ AstraTrade Rules Configuration")
st.caption("Enable/disable rules globally. Mandatory rules cannot be disabled.")

# Filter by category
categories = [c.value for c in RuleCategory]
selected_cat = st.selectbox("Filter by category", ["ALL"] + categories)

with get_db() as db:
    query = db.query(RuleConfig)
    if selected_cat != "ALL":
        query = query.filter(RuleConfig.category == selected_cat)
    rules = query.order_by(RuleConfig.category, RuleConfig.sort_order).all()
    rules_data = [
        {"id": r.id, "rule_id": r.rule_id, "category": r.category.value,
         "label": r.label, "enabled": r.enabled_globally,
         "threshold": float(r.threshold) if r.threshold else None,
         "threshold_label": r.threshold_label or "",
         "is_mandatory": r.is_mandatory, "description": r.description or ""}
        for r in rules
    ]

st.divider()

for rule in rules_data:
    with st.expander(
        f"{'🔒' if rule['is_mandatory'] else ('✅' if rule['enabled'] else '❌')}  "
        f"**{rule['label']}** — `{rule['rule_id']}`  [{rule['category']}]",
        expanded=False
    ):
        col1, col2 = st.columns([1, 3])
        with col1:
            if rule["is_mandatory"]:
                st.toggle("Enabled", value=True, disabled=True, key=f"tog_{rule['id']}")
                st.caption("🔒 Mandatory — cannot be disabled")
            else:
                new_enabled = st.toggle("Enabled globally", value=rule["enabled"], key=f"tog_{rule['id']}")
                if new_enabled != rule["enabled"]:
                    with get_db() as db:
                        r = db.query(RuleConfig).get(rule["id"])
                        if r:
                            old = r.enabled_globally
                            r.enabled_globally = new_enabled
                            r.updated_by = "dashboard"
                            db.add(AuditLog(action=AuditAction.RULE_TOGGLED,
                                entity_type="RuleConfig", entity_id=r.rule_id,
                                before_value=str(old), after_value=str(new_enabled),
                                actor="dashboard"))
                    st.rerun()

        with col2:
            st.caption(rule["description"])
            if rule["threshold"] is not None:
                new_thresh = st.number_input(
                    rule["threshold_label"] or "Threshold",
                    value=float(rule["threshold"]),
                    key=f"thr_{rule['id']}",
                    step=0.5
                )
                if st.button("Save threshold", key=f"save_{rule['id']}"):
                    with get_db() as db:
                        r = db.query(RuleConfig).get(rule["id"])
                        if r:
                            old = float(r.threshold)
                            r.threshold = new_thresh
                            r.updated_by = "dashboard"
                            db.add(AuditLog(action=AuditAction.RULE_THRESHOLD_SET,
                                entity_type="RuleConfig", entity_id=r.rule_id,
                                before_value=str(old), after_value=str(new_thresh),
                                actor="dashboard"))
                    st.success(f"Threshold updated to {new_thresh}")
