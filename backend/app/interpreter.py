from __future__ import annotations

import calendar
import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Iterable

from sqlalchemy import select

from .db import SessionLocal
from .models import Company, Resource


@dataclass
class Interpretation:
    intent: str
    technician: str | None
    company: str | None
    date_from: str | None
    date_to: str | None
    quantity: int | None
    quantity_mode: str
    keywords: list[str]
    confidence: float
    reasons: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


FILLER_WORDS = {
    "show", "find", "get", "give", "pull", "list", "me", "my",
    "ticket", "tickets", "the", "a", "an", "all", "any", "every",
    "assigned", "to", "for", "from", "by", "worked", "work",
    "created", "closed", "resolved", "resolution", "today",
    "yesterday", "tomorrow", "this", "last", "week", "month",
    "year", "please", "who", "what", "when", "where", "how",
    "many", "number", "count", "total", "of", "in", "on",
}


def normalize(value: str) -> str:
    value = value.lower().replace("’", "'")
    value = re.sub(r"\b([a-z0-9-]+)'s\b", r"\1", value)
    return value


def words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9_.-]+", normalize(value))


def match_known_name(question: str, names: Iterable[str]) -> tuple[str | None, bool]:
    """
    Returns:
        matched_name
        ambiguous
    """
    lower = normalize(question)
    valid_names = [name for name in names if name]

    full_matches = [
        name
        for name in valid_names
        if re.search(
            rf"(?<![a-z0-9]){re.escape(normalize(name))}(?![a-z0-9])",
            lower,
        )
    ]

    if full_matches:
        return max(full_matches, key=len), False

    question_words = set(words(question))
    candidates: list[tuple[int, int, str]] = []

    for name in valid_names:
        name_words = set(words(name))
        overlap = question_words.intersection(name_words)

        if not overlap:
            continue

        candidates.append((
            len(overlap),
            sum(len(word) for word in overlap),
            name,
        ))

    if not candidates:
        return None, False

    candidates.sort(reverse=True)
    best_score = candidates[0][:2]

    best_names = list(dict.fromkeys(
        name
        for count, length, name in candidates
        if (count, length) == best_score
    ))

    if len(best_names) == 1:
        return best_names[0], False

    return None, True


def parse_dates(question: str) -> tuple[date | None, date | None]:
    lower = normalize(question)
    today = date.today()

    if "today" in lower:
        return today, today + timedelta(days=1)

    if "yesterday" in lower:
        return today - timedelta(days=1), today

    if "tomorrow" in lower:
        return today + timedelta(days=1), today + timedelta(days=2)

    if "this week" in lower:
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7)

    if "last week" in lower:
        end = today - timedelta(days=today.weekday())
        return end - timedelta(days=7), end

    if "this month" in lower:
        start = today.replace(day=1)
        if start.month == 12:
            end = date(start.year + 1, 1, 1)
        else:
            end = date(start.year, start.month + 1, 1)
        return start, end

    if "last month" in lower:
        end = today.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
        return start, end

    if "this year" in lower:
        return date(today.year, 1, 1), date(today.year + 1, 1, 1)

    if "last year" in lower:
        return date(today.year - 1, 1, 1), date(today.year, 1, 1)

    days_match = re.search(
        r"\b(?:past|last|previous)\s+(\d+)\s+days?\b",
        lower,
    )
    if days_match:
        days = int(days_match.group(1))
        return today - timedelta(days=days), today + timedelta(days=1)

    weeks_match = re.search(
        r"\b(?:past|last|previous)\s+(\d+)\s+weeks?\b",
        lower,
    )
    if weeks_match:
        days = int(weeks_match.group(1)) * 7
        return today - timedelta(days=days), today + timedelta(days=1)

    weekdays = {
        name.lower(): index
        for index, name in enumerate(calendar.day_name)
    }

    weekday_match = re.search(
        r"\b(" + "|".join(weekdays) + r")\b",
        lower,
    )
    if weekday_match:
        target = weekdays[weekday_match.group(1)]
        offset = (today.weekday() - target) % 7
        selected = today - timedelta(days=offset)
        return selected, selected + timedelta(days=1)

    year_match = re.search(r"\b(?:from|during|in)\s+(20\d{2})\b", lower)
    if year_match:
        year = int(year_match.group(1))
        return date(year, 1, 1), date(year + 1, 1, 1)

    return None, None


def parse_quantity(question: str) -> tuple[int | None, str]:
    lower = normalize(question)

    if re.search(r"\bT\d{8}\.\d+\b", question, re.I):
        return 1, "exact-ticket"

    numeric = re.search(
        r"\b(\d+)\b(?:\s+\S+){0,8}\s+tickets?\b",
        lower,
    )
    if numeric:
        return int(numeric.group(1)), "explicit"

    number_words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }

    word_match = re.search(
        r"\b(" + "|".join(number_words) + r")\b"
        r"(?:\s+\S+){0,8}\s+tickets?\b",
        lower,
    )

    if word_match:
        return number_words[word_match.group(1)], "explicit"

    if re.search(
        r"\b(?:a|one|single|the)\b(?:\s+\S+){0,8}\s+ticket\b",
        lower,
    ):
        return 1, "singular"

    return None, "all-matches"


def detect_intent(question: str) -> tuple[str, list[str], float]:
    lower = normalize(question)
    reasons: list[str] = []

    if re.search(r"\bT\d{8}\.\d+\b", question, re.I):
        return "ticket_search", ["Exact Autotask ticket number detected."], 1.0

    history_patterns = (
        r"\bwhat\s+did\s+we\b",
        r"\bhow\s+did\s+we\s+fix\b",
        r"\bwhat\s+was\s+done\b",
        r"\bwho\s+(?:worked|resolved|created|closed)\b",
    )

    if any(re.search(pattern, lower) for pattern in history_patterns):
        reasons.append("Historical service-work wording detected.")
        return "ticket_search", reasons, 0.98

    ticket_terms = (
        "ticket",
        "tickets",
        "autotask",
        "assigned to",
        "created by",
        "worked on by",
        "client history",
        "service history",
    )

    if any(term in lower for term in ticket_terms):
        reasons.append("Explicit ticket wording detected.")
        return "ticket_search", reasons, 0.95

    tech_patterns = (
        "how do i",
        "how can i",
        "how to",
        "why does",
        "why is",
        "what causes",
        "troubleshoot",
        "not working",
        "cannot connect",
        "can't connect",
        "error",
        "offline",
        "crashing",
        "freezing",
    )

    if any(term in lower for term in tech_patterns):
        reasons.append("Technical troubleshooting wording detected.")
        return "tech_question", reasons, 0.90

    reasons.append("No explicit ticket-history wording detected.")
    return "tech_question", reasons, 0.60


def extract_keywords(
    question: str,
    technician: str | None,
    company: str | None,
) -> list[str]:
    excluded = set(FILLER_WORDS)

    for value in (technician, company):
        if value:
            excluded.update(words(value))

    return list(dict.fromkeys(
        word
        for word in words(question)
        if len(word) >= 2
        and word not in excluded
        and not word.isdigit()
    ))


def interpret_question(
    question: str,
    user_email: str | None = None,
) -> Interpretation:
    intent, reasons, confidence = detect_intent(question)

    with SessionLocal() as db:
        resources = db.scalars(select(Resource)).all()
        companies = db.scalars(select(Company.name)).all()

    resource_names = [
        resource.name
        for resource in resources
        if resource.name
    ]

    technician, technician_ambiguous = match_known_name(
        question,
        resource_names,
    )

    company, company_ambiguous = match_known_name(
        question,
        companies,
    )

    if technician:
        reasons.append(f"Matched technician: {technician}.")
        confidence = min(1.0, confidence + 0.03)

    if technician_ambiguous:
        reasons.append("Technician name is ambiguous.")

    if company:
        reasons.append(f"Matched company: {company}.")
        confidence = min(1.0, confidence + 0.03)

    if company_ambiguous:
        reasons.append("Company name is ambiguous.")

    if re.search(
        r"\b(?:my tickets|mine|assigned to me)\b",
        normalize(question),
    ):
        email = (user_email or "").lower()
        local_part = email.split("@", 1)[0] if "@" in email else email

        user_matches = [
            resource.name
            for resource in resources
            if resource.name
            and (
                (resource.username or "").lower() == email
                or (resource.username or "").lower() == local_part
                or local_part in words(resource.name)
            )
        ]

        user_matches = list(dict.fromkeys(user_matches))

        if len(user_matches) == 1:
            technician = user_matches[0]
            reasons.append(
                f"Mapped signed-in user to technician: {technician}."
            )
        elif len(user_matches) > 1:
            reasons.append(
                "The signed-in account matches multiple Autotask resources."
            )
        else:
            reasons.append(
                "The signed-in account could not be mapped to an Autotask resource."
            )

    date_from, date_to = parse_dates(question)
    quantity, quantity_mode = parse_quantity(question)

    if date_from:
        reasons.append(
            f"Date range: {date_from.isoformat()} through "
            f"{(date_to - timedelta(days=1)).isoformat()}."
        )

    reasons.append(
        "Quantity: all genuine matches."
        if quantity is None
        else f"Quantity: {quantity}."
    )

    keywords = extract_keywords(
        question,
        technician,
        company,
    )

    if keywords:
        reasons.append(
            "Keywords: " + ", ".join(keywords) + "."
        )

    return Interpretation(
        intent=intent,
        technician=technician,
        company=company,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
        quantity=quantity,
        quantity_mode=quantity_mode,
        keywords=keywords,
        confidence=round(confidence, 2),
        reasons=reasons,
    )
