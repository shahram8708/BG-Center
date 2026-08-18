"""BG Intelligent Assistant & Policy Q&A Service.

Provides real-time, context-aware answers powered by Google Gemini. Automatically
incorporates the user's active page/workflow state (Bank Guarantees, approval stages,
deviations, lifecycle windows, configurations), session conversation history, and
TF-IDF retrieved platform policy references.
"""
import json
import logging

from bgcc.extensions import db
from bgcc.models.assistant_messages import AssistantMessage
from bgcc.models.settings import ApplicationSetting
from bgcc.services import gemini_service, page_context_service

logger = logging.getLogger("bgcc.assistant")

RELEVANCE_THRESHOLD = 0.10
MAX_CHUNKS = 5

_matcher = {"vectorizer": None, "matrix": None, "chunks": None}


def _config_chunks():
    """Live structured configuration chunks read from application_settings."""
    chunks = []
    matrix = ApplicationSetting.query.filter_by(setting_key="doa_matrix").first()
    if matrix and matrix.setting_value:
        chunks.append({
            "title": "Delegation of authority matrix",
            "body": json.dumps(matrix.setting_value, default=str),
            "link": "/bg-multi-stage-approval",
        })
    template = ApplicationSetting.query.filter_by(setting_key="active_clause_template").first()
    if template and template.setting_value:
        value = template.setting_value
        chunks.append({
            "title": "Active clause template",
            "body": json.dumps({
                "name": value.get("name"),
                "mandatory_clauses": value.get("mandatory_clauses"),
                "body": value.get("body"),
            }, default=str),
            "link": "/bg-multi-stage-approval",
        })
    checklist = ApplicationSetting.query.filter_by(setting_key="checklist_definitions").first()
    if checklist and checklist.setting_value:
        chunks.append({
            "title": "Format & physical checklist",
            "body": json.dumps(checklist.setting_value, default=str),
            "link": "/bg-multi-stage-approval",
        })
    return chunks


def _policy_chunks():
    setting = ApplicationSetting.query.filter_by(setting_key="policy_reference_content").first()
    value = (setting.setting_value if setting else {}) or {}
    sections = value.get("sections", []) if isinstance(value, dict) else value or []
    return [{"title": s.get("title", ""), "body": s.get("body", ""),
             "link": s.get("link")} for s in sections]


def _all_chunks():
    return _policy_chunks() + _config_chunks()


def _ensure_matcher():
    chunks = _all_chunks()
    if _matcher["chunks"] == [c["title"] for c in chunks] and _matcher["vectorizer"] is not None:
        return _matcher
    from sklearn.feature_extraction.text import TfidfVectorizer

    corpus = [f"{c['title']} {c['body']}" for c in chunks]
    if not corpus:
        return _matcher
    vectorizer = TfidfVectorizer(stop_words="english")
    matrix = vectorizer.fit_transform(corpus)
    _matcher.update({"vectorizer": vectorizer, "matrix": matrix, "chunks": chunks})
    return _matcher


def retrieve(query, top_k=MAX_CHUNKS):
    """Return top-k relevant policy chunks, empty if none clear threshold."""
    if not query or not query.strip():
        return []
    matcher = _ensure_matcher()
    if matcher["matrix"] is None or matcher["vectorizer"] is None:
        return []
    try:
        q_vec = matcher["vectorizer"].transform([query])
        from sklearn.metrics.pairwise import cosine_similarity

        scores = cosine_similarity(q_vec, matcher["matrix"])[0]
        scored = [
            {"title": matcher["chunks"][i]["title"],
             "body": matcher["chunks"][i]["body"],
             "link": matcher["chunks"][i].get("link"),
             "score": float(scores[i])}
            for i in range(len(scores))
        ]
        scored.sort(key=lambda c: c["score"], reverse=True)
        relevant = [c for c in scored if c["score"] >= RELEVANCE_THRESHOLD]
        return relevant[:top_k]
    except Exception as exc:
        logger.warning("Policy retrieval failed: %s", exc)
        return []


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "cited_sources": {"type": "array", "items": {"type": "string"}},
        "related_link": {"type": "string"},
    },
    "required": ["answer", "cited_sources"],
}

SYSTEM_INSTRUCTION = (
    "You are the intelligent BG Command Centre AI Assistant. "
    "You assist users with bank guarantees, review and approval workflows, lifecycle management "
    "(extensions, closures, returns, invocations), compliance deviations, and platform policies.\n\n"
    "Guidelines:\n"
    "1. **Greetings & Casual Inquiries**: When greeted (e.g. 'Hey', 'Hi', 'Hello', 'Good morning') or asked general questions, respond warmly and professionally, briefly stating how you can assist with their bank guarantees, active screen data, and platform workflows.\n"
    "2. **Current Screen & Live Workflow Data**: Use the provided CURRENT APPLICATION CONTEXT to answer questions about currently visible BGs, dashboard metrics, workflow stages, deviations, claim-window warnings, closure statuses, or extension deadlines. Cite the screen/BG in cited_sources.\n"
    "3. **Follow-ups & Conversation Continuity**: Use RECENT CONVERSATION HISTORY along with current context to smoothly handle follow-up questions, summaries (e.g. 'Now summarize in 1 sentence'), status inquiries, and explanations.\n"
    "4. **Policy & Reference Rules**: When asked about DoA matrix levels, clause template mandatory requirements, checklist criteria, or platform guidelines, ground your answer in the RELEVANT POLICY REFERENCE CHUNKS and cite their titles.\n"
    "5. **Graceful Decline**: Only state that no relevant context was found if the user asks a specific technical or factual question that is completely absent from both the application context, recent conversation, and policy documents. Never give a decline message for greetings or questions regarding the visible screen.\n"
    "6. **Precision & Safety**: Keep answers concise, factual, and actionable. Never invent fictional BG numbers or expose internal passwords/secrets."
)


def answer_question(user, question, page_url=None, page_title=None, client_context=None):
    """Answer question with dynamic page context, conversation history, and policy grounding."""
    # 1. Build live application context
    ctx = page_context_service.build_page_context(
        user,
        page_url=page_url,
        page_title=page_title,
        client_context=client_context,
    )
    formatted_page_context = page_context_service.format_context_for_prompt(ctx)

    # 2. Retrieve policy references
    retrieved_policy = retrieve(question)

    # 3. Load recent conversation history (last 8 messages)
    recent_history = thread_for(user, limit=8)
    history_lines = []
    for msg in reversed(recent_history):
        role_label = "User" if msg.role == "user" else "Assistant"
        history_lines.append(f"{role_label}: {msg.content}")
    history_block = "\n".join(history_lines) if history_lines else "No previous conversation in this session."

    # 4. Policy chunks text
    if retrieved_policy:
        policy_blocks = [f"[citation: {c['title']}]\n{c['body']}" for c in retrieved_policy]
        policy_text = "\n\n".join(policy_blocks)
    else:
        policy_text = "No specific policy document matched this keyword query."

    # 5. Build prompt parts
    prompt = (
        f"{gemini_service.DELIM_OPEN}\n"
        f"{formatted_page_context}\n"
        f"{gemini_service.DELIM_CLOSE}\n\n"
        f"### RELEVANT POLICY REFERENCE CHUNKS\n"
        f"{policy_text}\n\n"
        f"### RECENT CONVERSATION HISTORY\n"
        f"{history_block}\n\n"
        f"### CURRENT USER QUESTION\n"
        f"{question}\n\n"
        "Please provide a helpful, context-grounded response matching the JSON schema."
    )

    logger.info(
        "Invoking AI assistant for user_id=%s on page=%s (retrieved_policy_count=%d)",
        user.id,
        ctx.get("detected_route"),
        len(retrieved_policy),
    )

    result = gemini_service.generate_structured(
        feature="policy_assistant",
        bg_id=None,
        user_id=user.id,
        parts=[prompt],
        response_schema=RESPONSE_SCHEMA,
        system_instruction=SYSTEM_INSTRUCTION,
    )

    answer = result.get("answer", "")
    sources = result.get("cited_sources", []) or []
    link = result.get("related_link") or (retrieved_policy[0].get("link") if retrieved_policy else None)

    # If no source was cited by the AI model, provide sensible context-based source
    if not sources:
        if retrieved_policy:
            sources = [c["title"] for c in retrieved_policy]
        else:
            page_screen_title = ctx.get("page_details", {}).get("page_title") or ctx.get("page_title") or "Live Screen Context"
            sources = [page_screen_title]

    if not link:
        if ctx.get("page_url") and ctx.get("page_url") not in ("/assistant", "/assistant/"):
            link = ctx.get("page_url")

    return answer, sources, link


def save_message(user, role, content, sources=None, link=None):
    msg = AssistantMessage(
        user_id=user.id,
        role=role,
        content=content,
        cited_sources=sources,
        related_link_url=link,
    )
    db.session.add(msg)
    db.session.commit()
    return msg


def thread_for(user, limit=50):
    return (
        AssistantMessage.query.filter_by(user_id=user.id)
        .order_by(AssistantMessage.created_at.desc())
        .limit(limit)
        .all()
    )


def clear_thread(user):
    AssistantMessage.query.filter_by(user_id=user.id).delete()
    db.session.commit()
