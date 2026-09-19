"""
The Q&A knowledge base and sensitive-question policy.

Sections 11 and 12 are both marked CRITICAL, and the failure they describe —
auto-answering an unknown question, especially a legal one — was live in the
merged code. These tests exist to make that failure impossible to reintroduce.
"""

from __future__ import annotations

import pytest

from app.models import AnswerSource, PendingQuestion, Question, QuestionCategory
from app.qa import KnowledgeBase, categorize, classify_sensitivity, is_sensitive
from app.qa.knowledge_base import normalize_question


# ======================================================== sensitivity

@pytest.mark.parametrize(
    "question",
    [
        "Are you legally authorized to work in the United States?",
        "Do you have the right to work in India?",
        "Are you eligible to work in this country without restriction?",
        "Will you now or in the future require visa sponsorship?",
        "Do you require sponsorship for employment?",
        "Are you on H1B?",
        "Do you have a disability?",
        "Do you require any reasonable accommodation?",
        "Are you a protected veteran?",
        "What is your race/ethnicity?",
        "Please select your gender",
        "What are your pronouns?",
        "Have you ever been convicted of a felony?",
        "Do you consent to a background check?",
        "What are your salary expectations?",
        "What is your expected CTC?",
        "Are you willing to relocate to Bangalore?",
        "I certify that the information provided is accurate",
        "Do you agree to the terms and conditions?",
        "What is your date of birth?",
        "What is your marital status?",
    ],
)
def test_sensitive_questions_are_caught(question):
    """
    Each of these is a legal declaration, a protected characteristic or a
    binding commitment. Every one must reach the user.
    """
    assert is_sensitive(question), f"not flagged: {question}"


@pytest.mark.parametrize(
    "question",
    [
        "How many years of experience do you have with Python?",
        "What is your notice period?",
        "Which cloud platforms have you worked with?",
        "Rate your proficiency in Kubernetes",
        "What is your current job title?",
        "How did you hear about us?",
        "What is your LinkedIn profile URL?",
    ],
)
def test_ordinary_questions_are_not_flagged(question):
    """Over-flagging everything would make the feature useless noise."""
    assert not is_sensitive(question), f"wrongly flagged: {question}"


def test_sensitivity_verdict_explains_itself():
    """The user is being interrupted — they should be told why."""
    verdict = classify_sensitivity("Will you require visa sponsorship?")
    assert verdict.topic == "visa_sponsorship"
    assert verdict.rationale


def test_categorisation():
    assert categorize("How many years of experience with Go?") is QuestionCategory.EXPERIENCE
    assert categorize("What is your notice period?") is QuestionCategory.AVAILABILITY
    assert categorize("What is your expected CTC?") is QuestionCategory.COMPENSATION
    assert categorize("Do you have a disability?") is QuestionCategory.SENSITIVE


# ====================================================== normalization

def test_differently_phrased_questions_normalize_alike():
    """
    Portals phrase the same question a dozen ways. Normalizing handles the
    common case without needing an embedding model running.
    """
    a = normalize_question("How many years of experience do you have with Python?")
    b = normalize_question("Years of experience with Python")
    c = normalize_question("Please tell us: years of experience with Python*")
    assert a == b == c


def test_normalization_keeps_meaningful_differences():
    assert normalize_question("Experience with Python") != normalize_question("Experience with Java")


def test_normalization_strips_parentheticals_and_markers():
    assert normalize_question("Notice period (in days)*") == normalize_question("notice period")


# ==================================================== resolution rules

def test_unknown_question_asks_the_user(session, settings):
    """
    The central guarantee of section 11. The old code answered "Yes" here.
    """
    kb = KnowledgeBase(session, settings=settings)
    resolution = kb.resolve("Do you know how to operate a forklift?")

    assert resolution.needs_user is True
    assert resolution.answer is None
    assert resolution.resolved is False


def test_there_is_no_default_answer_field(session, settings):
    """
    A caller with nothing to fall back to cannot invent an answer. This is
    structural, not a matter of discipline.
    """
    resolution = KnowledgeBase(session, settings=settings).resolve("Anything at all?")
    assert not hasattr(resolution, "default")
    assert resolution.answer is None


def test_stored_answer_is_reused(session, settings):
    kb = KnowledgeBase(session, settings=settings)
    kb.store_answer("What is your notice period?", "60 days")

    resolution = kb.resolve("What is your notice period?")
    assert resolution.resolved
    assert resolution.answer == "60 days"


def test_rephrased_question_reuses_the_stored_answer(session, settings):
    """The point of normalization: not asking the user twice."""
    kb = KnowledgeBase(session, settings=settings)
    kb.store_answer("How many years of experience do you have with Python?", "4")

    resolution = kb.resolve("Years of experience with Python*")
    assert resolution.resolved
    assert resolution.answer == "4"


def test_sensitive_question_asks_even_with_a_stored_answer(session, settings):
    """
    Section 12's explicit requirement: confirmation is required "even if a
    similar answer exists". This is the test that makes that real.
    """
    kb = KnowledgeBase(session, settings=settings)
    kb.store_answer("Are you willing to relocate?", "Yes")

    resolution = kb.resolve("Are you willing to relocate?")
    assert resolution.needs_user is True
    assert resolution.is_sensitive is True
    # The stored answer is still offered, so confirming is one click.
    assert resolution.suggestion == "Yes"
    assert resolution.answer is None


def test_weak_match_is_not_used(session, settings):
    """
    Below the confidence threshold the answer is a suggestion, not an answer.
    """
    kb = KnowledgeBase(session, settings=settings)
    kb.store_answer("What is your notice period?", "60 days")

    resolution = kb.resolve("What is your favourite programming language?")
    assert resolution.needs_user is True
    assert resolution.answer is None


def test_threshold_is_respected(session, settings):
    kb = KnowledgeBase(session, settings=settings)
    kb.store_answer("Do you know Kubernetes?", "Yes", confidence=0.5)

    settings.answer_confidence_threshold = 0.9
    assert KnowledgeBase(session, settings=settings).resolve("Do you know Kubernetes?").needs_user

    settings.answer_confidence_threshold = 0.3
    assert KnowledgeBase(session, settings=settings).resolve("Do you know Kubernetes?").resolved


# ======================================================== storage rules

def test_question_is_recorded_once_and_counted(session, settings):
    kb = KnowledgeBase(session, settings=settings)
    kb.resolve("What is your notice period?")
    kb.resolve("Notice period (in days)")

    questions = session.query(Question).all()
    assert len(questions) == 1
    assert questions[0].times_seen == 2


def test_new_answer_supersedes_without_deleting(session, settings):
    """
    The audit trail for an already-submitted application must stay intact, so
    a correction deactivates rather than overwrites.
    """
    from app.models import Answer

    kb = KnowledgeBase(session, settings=settings)
    kb.store_answer("What is your notice period?", "90 days")
    kb.store_answer("What is your notice period?", "60 days")

    answers = session.query(Answer).all()
    assert len(answers) == 2
    assert [a.answer for a in answers if a.is_active] == ["60 days"]
    assert kb.resolve("What is your notice period?").answer == "60 days"


def test_platform_specific_answers_coexist(session, settings):
    kb = KnowledgeBase(session, settings=settings)
    kb.store_answer("Preferred location?", "Pune", platform="naukri")
    kb.store_answer("Preferred location?", "Remote", platform="linkedin")

    assert kb.resolve("Preferred location?", platform="linkedin").answer == "Remote"
    assert kb.resolve("Preferred location?", platform="naukri").answer == "Pune"


def test_usage_is_counted(session, settings):
    from app.models import Answer

    kb = KnowledgeBase(session, settings=settings)
    kb.store_answer("What is your notice period?", "60 days")
    kb.resolve("What is your notice period?")
    kb.resolve("What is your notice period?")

    assert session.query(Answer).filter_by(is_active=True).one().times_used == 2


# ======================================================== pending queue

def test_pending_question_is_raised_and_resolved(session, settings):
    kb = KnowledgeBase(session, settings=settings)
    resolution = kb.resolve("Do you hold a forklift licence?")
    pending = kb.raise_pending("Do you hold a forklift licence?", resolution)

    assert pending.resolved is False
    assert kb.pending_questions() == [pending]

    kb.answer_pending(pending.id, "No")
    assert pending.resolved is True
    assert kb.pending_questions() == []
    # And the answer is now known, so it is never asked again.
    assert kb.resolve("Do you hold a forklift licence?").answer == "No"


def test_raising_the_same_question_twice_does_not_duplicate(session, settings):
    """Re-running a paused application must not pile up duplicate rows."""
    from .conftest import make_job

    job = make_job(session, fingerprint="pending-dedupe")
    kb = KnowledgeBase(session, settings=settings)
    resolution = kb.resolve("Some question?")
    first = kb.raise_pending("Some question?", resolution, job_id=job.id)
    second = kb.raise_pending("Some question?", resolution, job_id=job.id)

    assert first.id == second.id
    assert session.query(PendingQuestion).count() == 1


def test_pending_carries_the_suggestion_for_sensitive_questions(session, settings):
    kb = KnowledgeBase(session, settings=settings)
    kb.store_answer("Are you willing to relocate?", "Yes")
    resolution = kb.resolve("Are you willing to relocate?")
    pending = kb.raise_pending("Are you willing to relocate?", resolution)

    assert pending.is_sensitive is True
    assert pending.suggested_answer == "Yes"


# ========================================================== seeding

def test_profile_seeding_answers_the_obvious_questions(session, settings, profile):
    kb = KnowledgeBase(session, settings=settings)
    stored = kb.seed_from_profile(profile)

    assert stored >= 5
    assert kb.resolve("What is your email address?").answer == profile.email
    assert kb.resolve("What is your full name?").answer == profile.full_name


def test_seeded_answers_are_marked_derived(session, settings, profile):
    """
    A correction from the user must always beat a derived answer, so derived
    ones are stored below full confidence.
    """
    kb = KnowledgeBase(session, settings=settings)
    kb.seed_from_profile(profile)

    resolution = kb.resolve("What is your full name?")
    assert resolution.source is AnswerSource.PROFILE
    assert resolution.confidence < 1.0


def test_seeding_never_pre_answers_a_sensitive_question(session, settings, profile):
    """
    Salary and notice period are in the profile, but stating them on an
    application is a decision, not a lookup.
    """
    kb = KnowledgeBase(session, settings=settings)
    kb.seed_from_profile(profile)

    for question in (
        "What are your salary expectations?",
        "Are you willing to relocate?",
        "Are you legally authorized to work here?",
    ):
        assert kb.resolve(question).needs_user is True
