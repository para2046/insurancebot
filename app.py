"""Gradio test UI: chat with the SOP agent and watch the controller state live."""
import os

import gradio as gr

from sop_agent import config
from sop_agent.agent import Agent
from sop_agent.data import Store
from sop_agent.llm import LLM, LLMError
from sop_agent.state import Session

STORE = Store()
GREETING = Agent(None, STORE).greeting()

SAMPLES = [
    "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied healthcare "
    "claim from January. DOB is 1985-03-15, SSN last four is 4472.",
    "I already told you who I am. This is ridiculous. Just tell me why my claim was denied.",
    "Hi, I'm David Chen, calling for my mom Margaret Chen about her claim.",
    "What is RL?",
    "Can I talk to a real person?",
]


def new_session(scenario: str):
    s = Session(consent_scenario=scenario)
    s.history.append({"role": "assistant", "content": GREETING})
    return s, list(s.history), s.panel(), ""


def chat(message: str | None, s: Session | None, key: str | None, model: str | None):
    # Browsers may send None for untouched fields, and a fast first message can arrive before demo.load ran.
    if s is None:
        s = new_session("default")[0]
    message = (message or "").strip()
    if not message:
        return "", list(s.history), s.panel(), _outbox(s), s
    try:
        agent = Agent(LLM(api_key=(key or "").strip() or None, model=(model or "").strip() or None), STORE)
        agent.handle(s, message)
    except LLMError as e:
        s.history.append({"role": "user", "content": message})
        s.history.append({"role": "assistant", "content": f"⚠️ {e}"})
    return "", list(s.history), s.panel(), _outbox(s), s


def _outbox(s: Session) -> str:
    if not s.outbox:
        return "_No email sent yet._"
    e = s.outbox[-1]
    return f"**To:** {e['to']}  \n**Subject:** {e['subject']}\n\n```\n{e['body']}\n```"


with gr.Blocks(title="Insurance Claims SOP Agent") as demo:
    gr.Markdown("## Insurance Claims SOP Agent\nVERIFY_ID → RESOLVE_INTENT → PROCESS_CASE → POST_PROCESS. "
                "The right panel shows what the SOP controller is doing on each turn.")
    session = gr.State()
    with gr.Group():
        with gr.Row(equal_height=True):
            key = gr.Textbox(
                label="🔑 Your LLM API key (OpenRouter)",
                placeholder="sk-or-...   Get a key at https://openrouter.ai/keys",
                info=("The agent's model calls use this key, for your browser session only. It is never stored. "
                      + ("Leave empty to use the demo's shared key." if config.api_key()
                         else "Required to chat.")),
                type="password", scale=4,
                # stop password managers from auto-filling a saved password into the key field
                html_attributes=gr.InputHTMLAttributes(autocomplete="new-password", autocorrect="off",
                                                       spellcheck=False, autocapitalize="off"))
            model = gr.Textbox(label="Model", value=config.MODEL, scale=2)
    with gr.Row():
        with gr.Column(scale=3):
            chatbot = gr.Chatbot(height=520, label="Conversation")
            msg = gr.Textbox(placeholder="Type as the caller…", show_label=False, autofocus=True)
            with gr.Row():
                send = gr.Button("Send", variant="primary")
                reset = gr.Button("New conversation")
            gr.Examples(SAMPLES, inputs=msg, label="Sample caller messages")
        with gr.Column(scale=2):
            with gr.Accordion("Demo settings", open=False):
                scenario = gr.Dropdown(list(STORE.consent_scenarios), value="default",
                                       label="Consent scenario (representative callers)")
            with gr.Tab("SOP state"):
                state_view = gr.JSON(label="Controller state")
            with gr.Tab("Email outbox"):
                outbox = gr.Markdown("_No email sent yet._")
            with gr.Tab("Test identities"):
                gr.JSON(value=STORE.policyholders, label="policyholders.json")
                gr.JSON(value=STORE.representatives, label="representatives.json")

    outputs = [msg, chatbot, state_view, outbox, session]
    send.click(chat, [msg, session, key, model], outputs)
    msg.submit(chat, [msg, session, key, model], outputs)
    reset.click(new_session, [scenario], [session, chatbot, state_view, msg]).then(
        lambda: "_No email sent yet._", None, outbox)
    scenario.change(new_session, [scenario], [session, chatbot, state_view, msg])
    demo.load(new_session, [scenario], [session, chatbot, state_view, msg])

if __name__ == "__main__":
    demo.launch(server_name=os.getenv("HOST", "0.0.0.0"), server_port=int(os.getenv("PORT", "7860")))
