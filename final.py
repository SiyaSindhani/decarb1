import os
import re
import json
from io import StringIO
from pathlib import Path
 
import pandas as pd
import streamlit as st
import tiktoken
from openai import OpenAI
 
# =========================
# Setup
# =========================
 
# api_key = os.getenv("OPENAI_API_KEY")
# if not api_key:
#     try:
#         api_key = st.secrets["OPENAI_API_KEY"]
#     except Exception:
#         st.error("Set OPENAI_API_KEY as an environment variable or in .streamlit/secrets.toml")
#         st.stop()
 
# =========================
# API Key setup (with sidebar input)
# =========================

# Try environment variable first
api_key = os.getenv("OPENAI_API_KEY")

# Sidebar input if not set
st.sidebar.subheader("🔑 API Key")
user_api_key = st.sidebar.text_input(
    "Enter your OpenAI API key",
    type="password",
    placeholder="sk-...",
    help="Used to authenticate with the OpenAI API. Not stored anywhere."
)

if user_api_key:
    api_key = user_api_key  # override env var if provided

# Final fallback: secrets file
if not api_key:
    try:
        api_key = st.secrets["OPENAI_API_KEY"]
    except Exception:
        st.error("Please enter your OpenAI API key on the left or set it as an environment variable.")
        st.stop()


client = OpenAI(api_key=api_key)
MODEL = "gpt-5"  # keep as-is
 
# Where prompt files live (you can change this or make it configurable)
#PROMPT_DIR = Path(__file__).parent / "prompts"
PROMPT_DIR = Path(__file__).parent
PROMPT_FILES = {
    "Default (Base System)": "base_system.md",
    "Partner Mode": "partner_mode.md",
    "Decarb Team Assistant": "decarb_assistant.md",
    "Custom (inline)": None,  # handled via textarea
}
 
# =========================
# Utilities
# =========================
 
def get_encoding(model):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")
 
ENCODING = get_encoding(MODEL)
 
@st.cache_data(show_spinner=False)
def load_prompt_text(path_str: str) -> str:
    """
    Read a prompt file from the same folder as this script.
    If not found, also try a legacy 'prompts/' subfolder.
    Cached to avoid disk churn on reruns.
    """
    if not path_str:
        return ""
    path = PROMPT_DIR / path_str
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        st.warning(f"Prompt file not found or unreadable: {path} — using a minimal fallback.\n{e}")
        return (
            "ROLE\nYou are a helpful assistant. "
            "If the user requests stage tables, output a JSON object with `blocks` "
            "containing `text` and `table` entries as previously specified."
        )
 
def ensure_messages_initialized(system_prompt: str):
    """Create st.session_state.messages with a system message if missing/empty."""
    if "messages" not in st.session_state or not st.session_state.messages:
        st.session_state.messages = [{"role": "system", "content": system_prompt}]
 
def _serialize_history(messages):
    """Serialize chat history (excluding system) into a single text block."""
    if not messages:
        return "Assistant:"
    lines = []
    start_idx = 1 if messages and messages[0].get("role") == "system" else 0
    for m in messages[start_idx:]:
        role = "User" if m.get("role") == "user" else "Assistant"
        lines.append(f"{role}: {m.get('content','')}")
    lines.append("Assistant:")
    return "\n".join(lines)
 
# =========================
# JSON “blocks” renderer
# =========================
 
JSON_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
SMALL_ROWS_MAX = 30
SMALL_COLS_MAX = 8
 
def _extract_json_text(raw: str) -> str | None:
    raw = raw.strip()
    if raw.startswith("{"):
        return raw
    m = JSON_FENCE.search(raw)
    if m:
        return m.group(1).strip()
    return None
 
def _render_small_markdown_table(df: pd.DataFrame):
    st.markdown(df.to_markdown(index=False))
 
def _render_large_dataframe(df: pd.DataFrame, filename: str = "table.csv"):
    st.dataframe(df, use_container_width=True)
    #csv_bytes = df.to_csv(index=False).encode("utf-8")
    #st.download_button("Download CSV", csv_bytes, file_name=filename, mime="text/csv")
 
def _render_table_block(block: dict):
    title   = block.get("title")
    dialect = (block.get("dialect") or "").lower()
    data    = block.get("data", "")
    stage   = block.get("stage") or "table"
    caption = block.get("caption")
 
    if title:
        st.subheader(title)
    if caption:
        st.caption(caption)
 
    if dialect == "markdown":
        st.markdown(data)
    elif dialect in ("tsv", "csv"):
        sep = "\t" if dialect == "tsv" else ","
        try:
            df = pd.read_csv(StringIO(data), sep=sep, engine="python", on_bad_lines="skip")
        except Exception:
            st.warning("Could not parse table data; showing raw text.")
            st.code(data, language=dialect)
            return
        rows, cols_count = df.shape
        if rows <= SMALL_ROWS_MAX and cols_count <= SMALL_COLS_MAX:
            _render_small_markdown_table(df)
        else:
            _render_large_dataframe(df, filename=f"{stage}.csv")
    else:
        st.warning(f"Unknown table dialect: {dialect or 'N/A'}. Showing raw data.")
        st.code(data)
 
def render_assistant_blocks(raw_reply: str):
    json_text = _extract_json_text(raw_reply)
    if not json_text:
        st.markdown(raw_reply)
        return
    try:
        doc = json.loads(json_text)
    except json.JSONDecodeError:
        st.warning("Response was not valid JSON; showing raw message.")
        st.code(raw_reply, language="json")
        return
 
    blocks = doc.get("blocks", [])
    if not isinstance(blocks, list) or not blocks:
        st.warning("JSON parsed but no blocks found; showing raw message.")
        st.code(json_text, language="json")
        return
 
    meta = doc.get("meta") or {}
    if meta:
        with st.expander("Context (meta)", expanded=False):
            st.json(meta)
 
    for block in blocks:
        btype = (block.get("type") or "").lower()
        if btype == "text":
            st.markdown(block.get("markdown", ""))
        elif btype == "table":
            _render_table_block(block)
        else:
            st.info(f"Unsupported block type: {btype or 'N/A'} — showing raw.")
            st.code(json.dumps(block, indent=2), language="json")
 
# =========================
# Core chat engine
# =========================
 
# =========================
# Core chat engine — web_search on, no tool_choice
# =========================
 
def _safe_create_response_with_json_fallback(model, system_prompt, history_text):
    """
    Try Responses API with web_search tool.
    - Keep web_search enabled via `tools=[{"type":"web_search"}]`
    - Do NOT pass `tool_choice` (your server rejects 'auto')
    - Try JSON mode first; if SDK lacks it, retry without response_format
    """
    base_kwargs = dict(
        model=model,
        instructions=system_prompt,
        input=history_text,
        tools=[{"type": "web_search"}],  
        # tool_choice intentionally omitted  
    )
 
    # 1) Try with JSON mode (newer SDKs)
    try:
        with st.spinner("Thinking..."):
            r = client.responses.create(
                **base_kwargs,
                response_format={"type": "json_object"},
            )
        return r.output_text
    except TypeError as e:
        # Older SDKs: remove `response_format` and retry
        if "response_format" not in str(e):
            raise
        st.info("JSON mode not supported by this SDK — retrying without response_format.")
        with st.spinner("Thinking..."):
            r = client.responses.create(**base_kwargs)
        return r.output_text
 
 
def chat(user_input: str):
    """Main chat handler: sends message, gets model reply, updates session."""
    # messages must already be initialized before this is called
    st.session_state.messages.append({"role": "user", "content": user_input})
 
    try:
        reply = _safe_create_response_with_json_fallback(
            model=MODEL,
            system_prompt=st.session_state.messages[0]["content"],
            history_text=_serialize_history(st.session_state.messages),
        )
    except Exception as e:
        st.exception(e)
        reply = ""
 
    st.session_state.messages.append({"role": "assistant", "content": reply})
    return reply
 
 
# =========================
# UI – Top
# =========================
 
st.title("DecarbAI")
st.caption(
    "Hi there! I can help you explore your product or service’s value chain — step by step — "
    "to see where emissions come from and how to reduce them."
)
 
st.sidebar.header("DecarbAI")
st.sidebar.write("Consulting-grade value-chain & emissions mapper")
 
# Prompt source selector
mode = st.sidebar.selectbox(
    "Developer instructions source",
    list(PROMPT_FILES.keys()),
    index=0  # Default (Base System)
)
 
# Load prompt from file or textarea
if PROMPT_FILES[mode] is None:
    # Custom inline prompt
    system_prompt = st.sidebar.text_area(
        "Custom System Message",
        value=load_prompt_text("base_system.md"),  # prefill with base as a convenience
        height=300,
    )
else:
    filename = PROMPT_FILES[mode]
    system_prompt = load_prompt_text(filename)
    with st.sidebar.expander("Prompt file", expanded=False):
        st.code(str(PROMPT_DIR / filename))
 
# IMPORTANT: initialize messages AFTER final system_prompt has been chosen
ensure_messages_initialized(system_prompt)
 
# Buttons
col1, col2 = st.sidebar.columns(2)
with col1:
    if st.button("Apply Prompt"):
        ensure_messages_initialized(system_prompt)
        st.session_state.messages[0] = {"role": "system", "content": system_prompt}
        st.success("System message applied.")
with col2:
    if st.button("Reset Chat"):
        st.session_state.messages = [{"role": "system", "content": system_prompt}]
        st.success("Conversation reset.")
 
# =========================
# Chat input + call
# =========================
 
if prompt := st.chat_input("What is up?"):
    chat(prompt)
 
# =========================
# Display loop
# =========================
 
if "messages" in st.session_state and st.session_state.messages:
    for message in st.session_state.messages[1:]:  # skip system
        with st.chat_message(message["role"]):
            if message["role"] == "assistant":
                render_assistant_blocks(message["content"])
            else:

                st.markdown(message["content"])

