import json
from flask import Blueprint, current_app, jsonify, render_template, request
from flask_login import current_user, login_required

from bgcc.content import AI_DISCLAIMER
from bgcc.extensions import db, limiter
from bgcc.services import assistant_service, page_context_service

bp = Blueprint("assistant", __name__, url_prefix="/assistant")


@bp.route("/", methods=["GET"])
@login_required
def index():
    thread = assistant_service.thread_for(current_user)
    thread = list(reversed(thread))
    context_url = request.args.get("context_url") or request.args.get("source_url")
    return render_template(
        "assistant/index.html",
        thread=thread,
        disclaimer=AI_DISCLAIMER,
        initial_context_url=context_url,
        active_nav="assistant",
    )


@bp.route("/ask", methods=["POST"])
@login_required
@limiter.limit("60 per hour", methods=["POST"], key_func=lambda: str(getattr(__import__("flask_login", fromlist=["current_user"]).current_user, "id", "anon")))
def ask():
    is_json = request.is_json
    payload = request.get_json(silent=True) if is_json else {}

    question = (payload.get("question") if is_json else request.form.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Please enter a question."}), 400

    page_url = payload.get("page_url") if is_json else request.form.get("page_url")
    if not page_url and request.referrer:
        # Avoid self-referencing loop if referrer was /assistant itself
        ref_path = request.referrer.split(request.host_url)[-1] if request.host_url in request.referrer else request.referrer
        if not ref_path.startswith("/assistant"):
            page_url = "/" + ref_path.lstrip("/")

    page_title = payload.get("page_title") if is_json else request.form.get("page_title")

    client_context = payload.get("client_context") if is_json else None
    if not client_context and request.form.get("client_context"):
        try:
            client_context = json.loads(request.form.get("client_context"))
        except (ValueError, TypeError):
            client_context = {}

    # Persist the user message
    assistant_service.save_message(current_user, "user", question)
    db.session.commit()

    try:
        answer, sources, link = assistant_service.answer_question(
            user=current_user,
            question=question,
            page_url=page_url,
            page_title=page_title,
            client_context=client_context,
        )
    except Exception as exc:
        current_app.logger.exception("Assistant answer generation failed: %s", exc)
        fallback_msg = "I couldn't process your request right now. Please try again in a moment."
        assistant_service.save_message(
            current_user, "assistant",
            fallback_msg,
            sources=[], link=None,
        )
        db.session.commit()
        return jsonify({
            "answer": fallback_msg,
            "sources": [],
            "link": None,
        })

    assistant_service.save_message(current_user, "assistant", answer, sources=sources, link=link)
    db.session.commit()
    return jsonify({"answer": answer, "sources": sources, "link": link})


@bp.route("/history", methods=["GET"])
@login_required
def history():
    thread = list(reversed(assistant_service.thread_for(current_user)))
    return jsonify({
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "sources": m.cited_sources or [],
                "link": m.related_link_url,
                "created_at": m.created_at.isoformat(),
            }
            for m in thread
        ]
    })


@bp.route("/context", methods=["GET", "POST"])
@login_required
def get_page_context():
    target_url = request.args.get("url") or (request.get_json(silent=True) or {}).get("url")
    if not target_url and request.referrer:
        target_url = request.referrer

    ctx = page_context_service.build_page_context(current_user, page_url=target_url)
    details = ctx.get("page_details", {})
    return jsonify({
        "route": ctx.get("detected_route"),
        "title": details.get("page_title") or ctx.get("page_title") or "General Platform Context",
        "type": details.get("context_type", "general"),
        "summary": details.get("summary") or details.get("metrics") or {},
    })


@bp.route("/clear", methods=["POST"])
@login_required
def clear_history():
    assistant_service.clear_thread(current_user)
    return jsonify({"success": True, "message": "Conversation history cleared."})
