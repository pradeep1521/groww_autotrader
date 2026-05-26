"""
Broker Connect — Groww authentication setup.
"""

import os

import streamlit as st
from dotenv import set_key

from broker.groww import connector

st.set_page_config(page_title="Broker Connect", page_icon="🔑", layout="centered")
st.title("🔑 Broker Connect — Groww")

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")

if connector.is_connected:
    st.success("✅ Groww is connected!")
    m = connector.margin()
    col1, col2, col3 = st.columns(3)
    col1.metric("Available margin", f"₹{m.get('available', 0):,.0f}")
    col2.metric("Equity",           f"₹{m.get('equity', 0):,.0f}")
    col3.metric("F&O margin",       f"₹{m.get('fno', 0):,.0f}")
    if st.button("🔌 Disconnect", type="secondary"):
        connector.disconnect()
        st.rerun()
    st.stop()

st.info("Choose an authentication method and enter your credentials. "
        "They will be saved to your local `.env` file.")

method = st.radio("Auth method", ["Access Token", "TOTP (API key + secret)"],
                  horizontal=True, key="bc_method")

if method == "Access Token":
    with st.form("bc_token"):
        token = st.text_input("Groww Access Token", type="password")
        save  = st.checkbox("Save to .env file")
        if st.form_submit_button("Connect", type="primary"):
            if not token.strip():
                st.error("Token is required.")
            else:
                ok, msg = connector.auth_token(token.strip())
                if ok:
                    if save:
                        set_key(ENV_FILE, "GROWW_ACCESS_TOKEN", token.strip())
                    st.success("✅ Connected via access token!")
                    st.rerun()
                else:
                    st.error(f"❌ Connection failed — {msg}")

else:  # TOTP
    with st.form("bc_totp"):
        api_key     = st.text_input("API Key")
        totp_secret = st.text_input("TOTP Secret", type="password",
                                    help="32-char base-32 secret used to generate the OTP")
        save        = st.checkbox("Save to .env file")
        if st.form_submit_button("Connect", type="primary"):
            if not api_key.strip() or not totp_secret.strip():
                st.error("Both API Key and TOTP Secret are required.")
            else:
                ok, msg = connector.auth_totp(api_key.strip(), totp_secret.strip())
                if ok:
                    if save:
                        set_key(ENV_FILE, "GROWW_API_KEY",      api_key.strip())
                        set_key(ENV_FILE, "GROWW_TOTP_SECRET",  totp_secret.strip())
                    st.success("✅ Connected via TOTP!")
                    st.rerun()
                else:
                    st.error(f"❌ Connection failed — {msg}")

st.divider()
st.caption("""
**Paper mode**: If you skip authentication, all strategies run in Paper mode automatically.
No real orders will be placed until you connect your Groww account.
""")
