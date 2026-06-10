"""System Configuration — global operational parameters."""
import sys; sys.path.insert(0, "/app")
import streamlit as st
import pandas as pd
from app.database import get_db
from app.models.config import SystemConfig
from app.models.audit import AuditLog, AuditAction

st.set_page_config(page_title="System Config — AstraTrade", layout="wide")
st.title("🔧 System Configuration")

with get_db() as db:
    configs = db.query(SystemConfig).order_by(SystemConfig.group, SystemConfig.key).all()
    groups = sorted(set(c.group for c in configs))

for group in groups:
    st.subheader(group.upper())
    group_configs = [c for c in configs if c.group == group]
    for cfg in group_configs:
        col1, col2, col3 = st.columns([2, 3, 1])
        with col1:
            st.text(cfg.label or cfg.key)
            if cfg.description:
                st.caption(cfg.description)
        with col2:
            val_display = "••••••" if cfg.is_secret else cfg.value
            new_val = st.text_input("", value=cfg.value if not cfg.is_secret else "",
                                    key=f"cfg_{cfg.id}", label_visibility="collapsed",
                                    placeholder=val_display)
        with col3:
            if st.button("Save", key=f"save_cfg_{cfg.id}"):
                if new_val and new_val != cfg.value:
                    with get_db() as db:
                        c = db.query(SystemConfig).get(cfg.id)
                        if c:
                            old = c.value
                            c.value = new_val
                            c.updated_by = "dashboard"
                            db.add(AuditLog(action=AuditAction.CONFIG_CHANGED,
                                entity_id=c.key, before_value=old,
                                after_value=new_val, actor="dashboard"))
                    st.success("Saved!")
    st.divider()
