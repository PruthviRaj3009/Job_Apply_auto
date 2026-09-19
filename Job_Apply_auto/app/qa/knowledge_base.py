"""
Application question knowledge base (sections 10, 11).

The rule this module exists to enforce:

    If a safe, high-confidence match exists, use the stored answer.
    Otherwise PAUSE, ASK THE USER, STORE, and RESUME.
    Never fall back to answering "Yes".

`resolve()` returns either an answer or a reason to stop. There is no third
outcome and no default — which is what makes the guarantee hold, because a
caller has nothing to fall through to.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import (
    Answer,
    AnswerSource,
    PendingQuestion,
    Question,
    QuestionCategory,
)
from .sensitive import categorize, classify_sensitivity

logger = logging.getLogger(__name__)

#: Filler that varies between portals asking the same thing.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "is", "are", "do", "does", "did", "you", "your",
        "please", "kindly", "tell", "us", "me", "what", "which", "how",
        "have", "has", "will", "would", "can", "could", "in", "of", "for",
        "to", "with", "and", "or", "this", "that", "it", "be", "been",
        "if", "any", "about", "at", "on", "as", "we", "our",
        # Quantifier filler: "how many years" and "years" must reduce to the
        # same key, otherwise the commonest rephrasing on earth misses.
        "many", "much", "long", "often", "there",
    }
)


def normalize_question(question: str) -> str:
    """
    Reduce a question to a comparable key.

    Portals phrase the same question a dozen ways. Normalizing lets exact
    matching handle the common case before embeddings are consulted at all,
    which matters because embeddings need a running Ollama and this must work
    without one.
    """
    if not question:
        return ""
    text = question.lower().strip()
    text = re.sub(r"\*+", " ", text)              # required-field markers
    text = re.sub(r"\(.*?\)", " ", text)          # parenthetical asides
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = [t for t in text.split() if t not in _STOPWORDS]
    return " ".join(tokens).strip()


@dataclass(slots=True)
class AnswerResolution:
    """
    The outcome of looking up one question.

    Exactly one of `answer` / `needs_user` is meaningful. There is deliberately
    no "default" field: a caller with nothing to fall back to cannot invent an
    answer.
    """

    answer: str | None = None
    confidence: float = 0.0
    source: AnswerSource | None = None
    needs_user: bool = False
    reason: str = ""
    is_sensitive: bool = False
    #: Best stored candidate, offered to the user as a suggestion even when it
    #: is not good enough to use automatically. Suggesting is safe;
    #: auto-filling is not.
    suggestion: str | None = None

    @property
    def resolved(self) -> bool:
        return self.answer is not None and not self.needs_user


class KnowledgeBase:
    """
    Stores and retrieves application answers.

    `semantic` is an optional embedding-backed matcher. Without it, lookup
    falls back to normalized exact match plus token overlap — degraded, but
    never wrong, because the confidence threshold still gates everything.
    """

    def __init__(
        self,
        session: Session,
        *,
        settings: Settings | None = None,
        semantic: object | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.semantic = semantic

    # --------------------------------------------------------- resolution

    def resolve(
        self,
        question_text: str,
        *,
        field_type: str = "text",
        options: list[str] | None = None,
        platform: str | None = None,
    ) -> AnswerResolution:
        """
        Find an answer, or say why the user must be asked.

        Sensitive questions always return needs_user, however confident the
        stored answer is (section 12). A stored answer is still surfaced as a
        suggestion so confirming is one click rather than retyping.
        """
        sensitivity = classify_sensitivity(question_text)
        question = self.record_question(
            question_text, field_type=field_type, options=options
        )
        best, score = self._best_answer(question, question_text, platform)

        if sensitivity.is_sensitive:
            return AnswerResolution(
                needs_user=True,
                is_sensitive=True,
                reason=sensitivity.rationale,
                suggestion=best.answer if best else None,
                confidence=score,
            )

        if best is not None and score >= self.settings.answer_confidence_threshold:
            best.times_used += 1
            best.last_used_at = datetime.now(timezone.utc)
            return AnswerResolution(
                answer=best.answer,
                confidence=score,
                source=AnswerSource(best.source),
            )

        return AnswerResolution(
            needs_user=True,
            reason=(
                "No stored answer for this question."
                if best is None
                else f"Closest stored answer scored {score:.2f}, below the "
                     f"{self.settings.answer_confidence_threshold:.2f} threshold."
            ),
            suggestion=best.answer if best else None,
            confidence=score,
        )

    def _best_answer(
        self, question: Question, raw_text: str, platform: str | None
    ) -> tuple[Answer | None, float]:
        """
        The best stored answer and how much we trust it for this question.

        Exact normalized match is checked first and scores highest: it needs
        no model, it is deterministic, and it covers most repeat questions.
        """
        # 1. An answer already attached to this exact question.
        answers = list(
            self.session.scalars(
                select(Answer).where(
                    Answer.question_id == question.id, Answer.is_active.is_(True)
                )
            )
        )
        if answers:
            # Prefer a platform-specific answer when one exists.
            preferred = [a for a in answers if platform and a.platform == platform]
            chosen = (preferred or answers)[0]
            return chosen, chosen.confidence

        # 2. A semantically similar stored question.
        if self.semantic is not None:
            try:
                match = self.semantic.find_similar(raw_text)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - RAG must never break the flow
                logger.debug("Semantic question lookup unavailable: %s", exc)
            else:
                if match is not None:
                    other_id, similarity = match
                    answer = self.session.scalar(
                        select(Answer).where(
                            Answer.question_id == other_id, Answer.is_active.is_(True)
                        )
                    )
                    if answer is not None:
                        return answer, similarity * answer.confidence

        # 3. Token-overlap fallback, so the system still works without Ollama.
        return self._lexical_match(question)

    def _lexical_match(self, question: Question) -> tuple[Answer | None, float]:
        """
        Closest stored question by token overlap.

        Scored below the exact-match path on purpose: overlap is a weak signal
        and the threshold should usually reject it, sending the question to the
        user rather than guessing.
        """
        target = set(question.normalized_question.split())
        if not target:
            return None, 0.0

        best: tuple[Answer | None, float] = (None, 0.0)
        candidates = self.session.scalars(
            select(Question).where(Question.id != question.id)
        )
        for other in candidates:
            tokens = set(other.normalized_question.split())
            if not tokens:
                continue
            overlap = len(target & tokens) / max(len(target | tokens), 1)
            if overlap <= best[1]:
                continue
            answer = self.session.scalar(
                select(Answer).where(
                    Answer.question_id == other.id, Answer.is_active.is_(True)
                )
            )
            if answer is not None:
                best = (answer, overlap * answer.confidence)
        return best

    # ------------------------------------------------------------ storage

    def record_question(
        self,
        question_text: str,
        *,
        field_type: str = "text",
        options: list[str] | None = None,
    ) -> Question:
        """Get or create the Question row, updating its sighting counters."""
        normalized = normalize_question(question_text)
        question = self.session.scalar(
            select(Question).where(Question.normalized_question == normalized)
        )
        now = datetime.now(timezone.utc)

        if question is None:
            question = Question(
                question=question_text,
                normalized_question=normalized,
                category=categorize(question_text),
                is_sensitive=classify_sensitivity(question_text).is_sensitive,
                field_type=field_type,
                options=options or [],
                times_seen=1,
                last_seen_at=now,
            )
            self.session.add(question)
            self.session.flush()
        else:
            question.times_seen += 1
            question.last_seen_at = now
            # Options can appear on one portal and not another.
            if options and not question.options:
                question.options = options
        return question

    def store_answer(
        self,
        question_text: str,
        answer_text: str,
        *,
        source: AnswerSource = AnswerSource.USER,
        confidence: float = 1.0,
        platform: str | None = None,
        field_type: str = "text",
        options: list[str] | None = None,
    ) -> Answer:
        """
        Store the user's answer so the same question is never asked twice.

        A new answer supersedes rather than overwrites: the old row is
        deactivated, not deleted, so the audit trail for an already-submitted
        application stays intact.
        """
        question = self.record_question(
            question_text, field_type=field_type, options=options
        )

        for existing in self.session.scalars(
            select(Answer).where(
                Answer.question_id == question.id, Answer.is_active.is_(True)
            )
        ):
            if existing.platform == platform:
                existing.is_active = False

        answer = Answer(
            question_id=question.id,
            answer=answer_text,
            source=source,
            confidence=confidence,
            platform=platform,
        )
        self.session.add(answer)
        self.session.flush()

        if self.semantic is not None:
            try:
                self.semantic.index(question)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not index question %s: %s", question.id, exc)
        return answer

    # ----------------------------------------------------- pending queue

    def raise_pending(
        self,
        question_text: str,
        resolution: AnswerResolution,
        *,
        job_id: int | None = None,
        application_id: int | None = None,
        field_type: str = "text",
        options: list[str] | None = None,
    ) -> PendingQuestion:
        """
        Put a question in front of the user (the "Action Required" queue).

        Idempotent per job: re-running a paused application must not pile up
        duplicate rows for the same question.
        """
        question = self.record_question(
            question_text, field_type=field_type, options=options
        )
        existing = self.session.scalar(
            select(PendingQuestion).where(
                PendingQuestion.job_id == job_id,
                PendingQuestion.question_id == question.id,
                PendingQuestion.resolved.is_(False),
            )
        )
        if existing is not None:
            return existing

        pending = PendingQuestion(
            job_id=job_id,
            application_id=application_id,
            question_id=question.id,
            question_text=question_text,
            field_type=field_type,
            options=options or [],
            is_sensitive=resolution.is_sensitive,
            suggested_answer=resolution.suggestion,
            suggested_confidence=resolution.confidence,
        )
        self.session.add(pending)
        self.session.flush()
        return pending

    def answer_pending(self, pending_id: int, answer_text: str) -> Answer:
        """
        Record the user's answer to a pending question and close it out.

        This is the "STORE Q&A -> CONTINUE" half of the section 11 loop; the
        job is released back into the pipeline by the caller.
        """
        pending = self.session.get(PendingQuestion, pending_id)
        if pending is None:
            raise KeyError(f"No pending question {pending_id}")

        answer = self.store_answer(
            pending.question_text,
            answer_text,
            source=AnswerSource.USER,
            confidence=1.0,
            field_type=pending.field_type,
            options=pending.options,
        )
        pending.resolved = True
        pending.resolved_at = datetime.now(timezone.utc)
        self.session.flush()
        return answer

    def pending_questions(self, *, job_id: int | None = None) -> list[PendingQuestion]:
        stmt = select(PendingQuestion).where(PendingQuestion.resolved.is_(False))
        if job_id is not None:
            stmt = stmt.where(PendingQuestion.job_id == job_id)
        return list(self.session.scalars(stmt.order_by(PendingQuestion.created_at)))

    # ------------------------------------------------------- bootstrapping

    def seed_from_profile(self, profile) -> int:
        """
        Pre-answer the questions every application asks, from profile data.

        These are facts the user already gave us, so asking again would be
        noise. Marked DERIVED with confidence below 1.0 so a correction from
        the user always wins.

        Deliberately excludes anything sensitive: salary expectations and
        notice period are *in* the profile, but stating them on an application
        is a decision, not a lookup.
        """
        facts: list[tuple[str, str]] = [
            ("What is your full name?", profile.full_name),
            ("What is your email address?", profile.email),
            ("What is your phone number?", profile.phone),
            ("What is your current location?", profile.location),
            ("How many years of total experience do you have?",
             f"{profile.total_experience_years:g}"),
        ]
        if profile.linkedin_url:
            facts.append(("What is your LinkedIn profile URL?", profile.linkedin_url))
        if profile.github_url:
            facts.append(("What is your GitHub profile URL?", profile.github_url))
        if profile.portfolio_url:
            facts.append(("What is your portfolio URL?", profile.portfolio_url))

        stored = 0
        for question, value in facts:
            if not value:
                continue
            if classify_sensitivity(question).is_sensitive:
                continue
            self.store_answer(
                question, str(value),
                source=AnswerSource.PROFILE, confidence=0.95,
            )
            stored += 1
        return stored
