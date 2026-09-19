"""
Job-description analysis (section 6).

Pulls structure out of free-text JDs: skills, qualifications,
responsibilities, and the mandatory/optional split.

Deliberately deterministic. An LLM is better at nuance, but this runs on every
discovered job and its output feeds the match score — a parser that
hallucinates a requirement would reject jobs the candidate could do. The LLM
layer sits on top (`app.matching.llm`) and is advisory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Section detection
# --------------------------------------------------------------------------

#: Heading text -> the bucket its bullets belong to. Matched case-insensitively
#: against a line that looks like a heading.
_SECTION_HEADINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "responsibilities",
        (
            "responsibilities", "what you will do", "what you'll do", "the role",
            "your role", "key responsibilities", "job description", "duties",
            "day to day", "what you will be doing", "about the role",
        ),
    ),
    (
        "requirements",
        (
            "requirements", "qualifications", "what we are looking for",
            "what we're looking for", "who you are", "skills", "must have",
            "must haves", "required skills", "essential", "you have",
            "minimum qualifications", "basic qualifications", "eligibility",
        ),
    ),
    (
        "optional",
        (
            "nice to have", "nice to haves", "good to have", "bonus", "preferred",
            "preferred qualifications", "plus", "desirable", "advantageous",
            "would be a plus", "icing on the cake",
        ),
    ),
    (
        "benefits",
        ("benefits", "perks", "what we offer", "compensation", "why join", "we offer"),
    ),
)

#: Phrases marking a line as a hard requirement.
_MANDATORY_MARKERS = (
    "must have", "must-have", "required", "requirement", "mandatory", "essential",
    "minimum", "at least", "should have", "need to have", "non-negotiable",
    "you will need", "proven", "demonstrated",
)

#: Phrases marking a line as optional, checked first — "preferred but not
#: required" is optional, and a naive scan for "required" would misread it.
_OPTIONAL_MARKERS = (
    "nice to have", "nice-to-have", "good to have", "preferred", "plus",
    "bonus", "desirable", "advantageous", "ideally", "a plus", "not required",
    "would be great", "familiarity with",
)

_BULLET_RE = re.compile(r"^\s*(?:[-*•‣◦⁃∙·●▪➢➜]|\d+[.)])\s+")
_HEADING_RE = re.compile(r"^\s*(?:#+\s*)?([A-Za-z][A-Za-z '/&-]{2,60})\s*:?\s*$")


@dataclass(slots=True)
class ParsedJD:
    """Structured view of a job description."""

    skills: list[str] = field(default_factory=list)
    qualifications: list[str] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    mandatory_requirements: list[str] = field(default_factory=list)
    optional_requirements: list[str] = field(default_factory=list)

    def as_job_fields(self) -> dict:
        """Column values to merge onto a `Job` row."""
        return {
            "skills": self.skills,
            "qualifications": self.qualifications,
            "responsibilities": self.responsibilities,
            "mandatory_requirements": self.mandatory_requirements,
            "optional_requirements": self.optional_requirements,
        }


# --------------------------------------------------------------------------
# Skill vocabulary
# --------------------------------------------------------------------------

#: Canonical skill -> spellings that mean it. Matching is done on word
#: boundaries so "Go" does not match "Google" and "R" does not match every
#: capital R in the document.
SKILL_VOCABULARY: dict[str, tuple[str, ...]] = {
    # Cloud
    "AWS": ("aws", "amazon web services"),
    "Azure": ("azure", "microsoft azure"),
    "GCP": ("gcp", "google cloud", "google cloud platform"),
    # Containers & orchestration
    "Kubernetes": ("kubernetes", "k8s", "eks", "aks", "gke"),
    "Docker": ("docker", "containerization", "containerisation"),
    "Helm": ("helm",),
    "OpenShift": ("openshift",),
    # IaC & config
    "Terraform": ("terraform", "hcl"),
    "Ansible": ("ansible",),
    "Pulumi": ("pulumi",),
    "CloudFormation": ("cloudformation", "cloud formation"),
    # CI/CD
    "Jenkins": ("jenkins",),
    "GitHub Actions": ("github actions",),
    "GitLab CI": ("gitlab ci", "gitlab-ci", "gitlab ci/cd"),
    "Azure DevOps": ("azure devops", "azure pipelines", "vsts"),
    "ArgoCD": ("argocd", "argo cd"),
    "CircleCI": ("circleci", "circle ci"),
    # Observability
    "Prometheus": ("prometheus",),
    "Grafana": ("grafana",),
    "Datadog": ("datadog", "data dog"),
    "ELK": ("elk", "elasticsearch", "logstash", "kibana"),
    "Splunk": ("splunk",),
    "OpenTelemetry": ("opentelemetry", "open telemetry", "otel"),
    # Languages
    "Python": ("python",),
    "Go": ("golang", "go lang"),
    "Java": ("java",),
    "JavaScript": ("javascript", "js"),
    "TypeScript": ("typescript",),
    "Bash": ("bash", "shell scripting", "shell script"),
    "PowerShell": ("powershell", "power shell"),
    "SQL": ("sql",),
    # Data & storage
    "PostgreSQL": ("postgresql", "postgres"),
    "MySQL": ("mysql",),
    "MongoDB": ("mongodb", "mongo"),
    "Redis": ("redis",),
    "Kafka": ("kafka",),
    "BigQuery": ("bigquery", "big query"),
    "Snowflake": ("snowflake",),
    # AI / ML
    "LLM": ("llm", "llms", "large language model", "large language models"),
    "RAG": ("rag", "retrieval augmented generation", "retrieval-augmented generation"),
    "LangChain": ("langchain", "lang chain"),
    "MLOps": ("mlops", "ml ops"),
    "LLMOps": ("llmops", "llm ops"),
    "PyTorch": ("pytorch", "torch"),
    "TensorFlow": ("tensorflow", "tensor flow"),
    "Vector Database": ("vector database", "vector db", "chromadb", "pinecone", "faiss", "weaviate", "qdrant"),
    "Generative AI": ("generative ai", "genai", "gen ai"),
    "Ollama": ("ollama",),
    "Hugging Face": ("hugging face", "huggingface"),
    # Web frameworks
    "FastAPI": ("fastapi", "fast api"),
    "Django": ("django",),
    "Flask": ("flask",),
    "React": ("react", "reactjs", "react.js"),
    "Node.js": ("node.js", "nodejs", "node js"),
    "Spring Boot": ("spring boot", "springboot"),
    # Practices & security
    "CI/CD": ("ci/cd", "ci cd", "continuous integration", "continuous delivery", "continuous deployment"),
    "Microservices": ("microservices", "micro services"),
    "REST API": ("rest api", "restful", "rest apis"),
    "GraphQL": ("graphql",),
    "SRE": ("sre", "site reliability"),
    "Linux": ("linux", "unix"),
    "Git": ("git",),
    "Agile": ("agile", "scrum", "kanban"),
    "Security": ("devsecops", "sast", "dast", "sonarqube", "snyk", "trivy", "sbom"),
    "Networking": ("tcp/ip", "networking", "load balancer", "vpc", "dns"),
    "IAM": ("iam", "identity and access management", "rbac"),
    "Serverless": ("serverless", "lambda", "cloud functions", "cloud run"),
}


def _build_skill_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """
    Compile one word-boundary pattern per canonical skill.

    Built once at import: this runs over every JD, and recompiling ~70
    alternations per call would dominate analysis time.
    """
    patterns = []
    for canonical, spellings in SKILL_VOCABULARY.items():
        # Longest first so "google cloud platform" is tried before "gcp"-style
        # shorter spellings that could match inside it.
        alternation = "|".join(re.escape(s) for s in sorted(spellings, key=len, reverse=True))
        # Lookarounds rather than :  would not stop "Go" matching inside
        # "Google", and would break spellings containing "/" like "ci/cd".
        pattern = re.compile(
            r"(?<![\w/])(?:" + alternation + r")(?![\w/])", re.IGNORECASE
        )
        patterns.append((canonical, pattern))
    return patterns


_SKILL_PATTERNS = _build_skill_patterns()


def extract_skills(text: str) -> list[str]:
    """
    Canonical skills mentioned in `text`, in vocabulary order.

    Only known skills are returned. Harvesting arbitrary capitalized tokens
    would fill the list with company names and section headings, and those
    then read as unmet requirements in the gap analysis.
    """
    if not text:
        return []
    return [canonical for canonical, pattern in _SKILL_PATTERNS if pattern.search(text)]


# --------------------------------------------------------------------------
# Structure extraction
# --------------------------------------------------------------------------


def _classify_heading(line: str) -> str | None:
    """Return the bucket a heading line introduces, or None."""
    match = _HEADING_RE.match(line)
    candidate = (match.group(1) if match else line).strip().lower().rstrip(":")
    if len(candidate) > 60:
        return None
    for bucket, headings in _SECTION_HEADINGS:
        if any(candidate == h or candidate.startswith(h) for h in headings):
            return bucket
    return None


def _split_lines(text: str) -> list[str]:
    """
    Normalize a JD into lines.

    Portals often deliver a JD as one long paragraph with bullets inline, so
    bullet glyphs are promoted to line breaks first.
    """
    text = re.sub(r"\s*[•●▪➢]\s*", "\n• ", text or "")
    return [ln.rstrip() for ln in text.splitlines()]


def is_mandatory(line: str) -> bool:
    """
    Whether a requirement line is hard.

    Optional markers are checked first: "preferred but not required" is
    optional, and scanning for "required" first would misclassify it.
    """
    lowered = line.lower()
    if any(marker in lowered for marker in _OPTIONAL_MARKERS):
        return False
    return any(marker in lowered for marker in _MANDATORY_MARKERS)


def parse_jd(text: str) -> ParsedJD:
    """
    Extract structure from a job description.

    Falls back gracefully: a JD with no recognizable headings still yields
    skills, and its bullets are classified individually by marker words.
    """
    parsed = ParsedJD()
    if not text or not text.strip():
        return parsed

    parsed.skills = extract_skills(text)

    current = ""
    for line in _split_lines(text):
        stripped = line.strip()
        if not stripped:
            continue

        if (bucket := _classify_heading(stripped)) is not None:
            current = bucket
            continue

        is_bullet = bool(_BULLET_RE.match(line))
        content = _BULLET_RE.sub("", line).strip()
        if not content or len(content) < 8:
            continue

        # Only bullets and clearly-marked sentences become items; prose
        # paragraphs are left out rather than chopped into fake requirements.
        if not is_bullet and current not in {"requirements", "optional", "responsibilities"}:
            continue

        if current == "responsibilities":
            parsed.responsibilities.append(content)
        elif current == "optional":
            parsed.optional_requirements.append(content)
            parsed.qualifications.append(content)
        elif current == "requirements":
            parsed.qualifications.append(content)
            if is_mandatory(content):
                parsed.mandatory_requirements.append(content)
            else:
                parsed.optional_requirements.append(content)
        elif current == "benefits":
            continue
        elif is_bullet:
            # No heading seen yet — classify the bullet on its own wording.
            if is_mandatory(content):
                parsed.mandatory_requirements.append(content)
                parsed.qualifications.append(content)
            elif any(m in content.lower() for m in _OPTIONAL_MARKERS):
                parsed.optional_requirements.append(content)
                parsed.qualifications.append(content)
            else:
                parsed.responsibilities.append(content)

    return parsed
