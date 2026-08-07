from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select, text

from .config import get_settings
from .db import SessionLocal
from .models import Company, Resource, Ticket
from .ollama import chat
from .semantic_search import semantic_ticket_ids

settings = get_settings()

FILLER = {
    "show", "me", "find", "get", "list", "give", "all", "the", "a", "an",
    "ticket", "tickets", "please", "about", "for", "from", "in", "on", "with",
    "what", "did", "we", "do", "have", "has", "had", "pull", "display", "return",
    "only", "just", "matching", "match", "results", "result", "every", "any",
}

NUMBER_WORDS = {
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
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}


def parse_dates(question: str):
    lower = question.lower()
    today = date.today()

    if "today" in lower:
        return today, today + timedelta(days=1)
    if "yesterday" in lower:
        return today - timedelta(days=1), today
    if "this week" in lower:
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7)
    if "last week" in lower:
        end = today - timedelta(days=today.weekday())
        return end - timedelta(days=7), end
    if "this month" in lower:
        start = today.replace(day=1)
        end = (
            date(start.year + 1, 1, 1)
            if start.month == 12
            else date(start.year, start.month + 1, 1)
        )
        return start, end
    if "last month" in lower:
        end = today.replace(day=1)
        return (end - timedelta(days=1)).replace(day=1), end
    if "this year" in lower:
        return date(today.year, 1, 1), date(today.year + 1, 1, 1)
    if "last year" in lower:
        return date(today.year - 1, 1, 1), date(today.year, 1, 1)

    relative_days = re.search(
        r"\b(?:past|previous|last)\s+(\d{1,4})\s+days?\b",
        lower,
    )
    if relative_days:
        days = int(relative_days.group(1))
        return today - timedelta(days=days), today + timedelta(days=1)

    relative_weeks = re.search(
        r"\b(?:past|previous|last)\s+(\d{1,3})\s+weeks?\b",
        lower,
    )
    if relative_weeks:
        days = int(relative_weeks.group(1)) * 7
        return today - timedelta(days=days), today + timedelta(days=1)

    slash_dates = re.findall(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", question)
    parsed = [date(int(year), int(month), int(day)) for month, day, year in slash_dates]
    if len(parsed) >= 2:
        parsed.sort()
        return parsed[0], parsed[1] + timedelta(days=1)
    if len(parsed) == 1:
        return parsed[0], parsed[0] + timedelta(days=1)

    iso_dates = re.findall(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", question)
    parsed = [date(int(year), int(month), int(day)) for year, month, day in iso_dates]
    if len(parsed) >= 2:
        parsed.sort()
        return parsed[0], parsed[1] + timedelta(days=1)
    if len(parsed) == 1:
        return parsed[0], parsed[0] + timedelta(days=1)

    return None, None


def _ticket_number(question: str) -> str | None:
    match = re.search(r"\bT\d{8}\.\d+\b", question, re.I)
    return match.group(0) if match else None


def _known_name_match(question: str, names: Iterable[str]) -> str | None:
    lower = question.lower()
    exact = [name for name in names if name and name.lower() in lower]
    if exact:
        return max(exact, key=len)
    return None


def _resource_after_phrase(question: str, names: Iterable[str], phrase: str) -> str | None:
    match = re.search(
        rf"\b{phrase}\s+([a-z][a-z .'-]{{0,80}})",
        question.lower(),
    )
    if not match:
        return None

    fragment = match.group(1)
    candidates = []
    for name in names:
        if not name:
            continue
        parts = [part.lower() for part in re.findall(r"[a-z]+", name)]
        if name.lower() in fragment or any(
            len(part) >= 3 and re.search(rf"\b{re.escape(part)}\b", fragment)
            for part in parts
        ):
            candidates.append(name)
    return max(candidates, key=len, default=None)


def _resource_anywhere(
    question: str,
    names: Iterable[str],
) -> str | None:
    """
    Match a unique technician by full name, first name, or last name
    anywhere in the request.

    Examples:
        Alex -> Alex Rivera
        Morgan -> Morgan Lee

    Ambiguous names return None instead of guessing.
    """
    lower = question.lower()
    question_words = set(re.findall(r"[a-z]+", lower))

    full_matches = [
        name
        for name in names
        if name and name.lower() in lower
    ]
    if full_matches:
        return max(full_matches, key=len)

    candidates = []

    for name in names:
        if not name:
            continue

        name_parts = {
            part.lower()
            for part in re.findall(r"[a-z]+", name)
            if len(part) >= 3
        }

        overlap = question_words.intersection(name_parts)

        if overlap:
            candidates.append((
                len(overlap),
                sum(len(part) for part in overlap),
                name,
            ))

    if not candidates:
        return None

    candidates.sort(reverse=True)
    best_score = candidates[0][:2]

    best_names = list(dict.fromkeys(
        name
        for count, length, name in candidates
        if (count, length) == best_score
    ))

    return best_names[0] if len(best_names) == 1 else None


def _keyword_tokens(
    question: str,
    company: str | None,
    assigned: str | None,
    created_by: str | None,
) -> list[str]:
    tokens = [
        token
        for token in re.findall(r"[a-z0-9_.-]{2,}", question.lower())
        if token not in FILLER and not token.isdigit()
    ]

    excluded = set()
    for value in (company, assigned, created_by):
        if value:
            excluded.update(re.findall(r"[a-z0-9_.-]+", value.lower()))

    # Remove date-language that has already become a structured date filter.
    excluded.update({
        "today", "yesterday",
        "day", "days",
        "week", "weeks",
        "month", "months",
        "year", "years",
        "this", "last", "past", "previous",
        "assigned", "worked", "working",
        "technician", "created", "completed",
        "closed", "resolved", "done", "finished",
        "by",
    })

    return list(dict.fromkeys(token for token in tokens if token not in excluded))


def requested_result_count(question: str, supplied_limit: int | None = None) -> int | None:
    """Return an exact requested quantity; None means all genuine matches."""
    if supplied_limit is not None:
        return max(1, int(supplied_limit))

    lower = question.lower().strip()

    if _ticket_number(question):
        return 1

    numeric = re.search(
        r"\b(\d{1,3})\b(?=[^.!?\n]{0,60}\btickets?\b)",
        lower,
    )
    if numeric:
        return max(1, int(numeric.group(1)))

    word_pattern = "|".join(NUMBER_WORDS)
    word = re.search(
        rf"\b({word_pattern})\b(?=[^.!?\n]{{0,60}}\btickets?\b)",
        lower,
    )
    if word:
        return NUMBER_WORDS[word.group(1)]

    singular_phrases = (
        "a ticket",
        "one ticket",
        "single ticket",
        "the ticket",
        "only one",
        "just one",
        "first ticket",
        "latest ticket",
        "most recent ticket",
    )
    if any(phrase in lower for phrase in singular_phrases):
        return 1

    # Bare singular noun: "show me the VPN ticket" or "find VPN ticket".
    if re.search(r"\bticket\b", lower) and not re.search(r"\btickets\b", lower):
        return 1

    # Plural wording, "all", "every", or no quantity means all real matches.
    return None


def _apply_limit(sql: str, params: dict, count: int | None) -> str:
    if count is None:
        return sql
    params["requested_count"] = count
    return sql + "\nLIMIT :requested_count"


def search_tickets(question: str, limit: int | None = None):
    """
    Search synchronized Autotask tickets without involving Ollama.

    Quantity comes entirely from the request:
    - singular wording returns one best match;
    - an explicit number returns up to that exact number of genuine matches;
    - plural or "all" returns every genuine match;
    - no evidence returns no records, never unrelated filler.
    """
    requested_count = requested_result_count(question, limit)
    ticket_number = _ticket_number(question)
    start_date, end_date = parse_dates(question)

    with SessionLocal() as db:
        company_names = db.scalars(select(Company.name)).all()
        resource_names = db.scalars(select(Resource.name)).all()

        company = _known_name_match(question, company_names)
        assigned = _resource_after_phrase(
            question,
            resource_names,
            r"(?:assigned\s+to|worked\s+on\s+by|technician)",
        )

        # Natural historical requests:
        # "What tickets has Jerry done?"
        # "What tickets did Marcus complete?"
        # "Tickets Cameron worked."
        if not assigned and re.search(
            r"\b(?:has|have|did|tickets?|work)\b.*"
            r"\b(?:done|worked|completed|closed|resolved|finished)\b",
            question,
            re.I,
        ):
            assigned = _resource_anywhere(
                question,
                resource_names,
            )

        created_by = _resource_after_phrase(
            question,
            resource_names,
            r"created\s+by",
        )

        if ticket_number:
            return db.scalars(
                select(Ticket)
                .where(Ticket.ticket_number.ilike(ticket_number))
                .limit(1)
            ).all()

        tokens = _keyword_tokens(question, company, assigned, created_by)
        filters = ["1=1"]
        params: dict = {}

        if company:
            filters.append("LOWER(COALESCE(company_name, '')) = :company")
            params["company"] = company.lower()

        if assigned:
            filters.append("LOWER(COALESCE(assigned_to, '')) = :assigned")
            params["assigned"] = assigned.lower()

        if created_by:
            filters.append("LOWER(COALESCE(created_by, '')) = :created_by")
            params["created_by"] = created_by.lower()

        if start_date:
            filters.append("create_date >= :start_date")
            params["start_date"] = datetime.combine(
                start_date,
                datetime.min.time(),
                timezone.utc,
            )

        if end_date:
            filters.append("create_date < :end_date")
            params["end_date"] = datetime.combine(
                end_date,
                datetime.min.time(),
                timezone.utc,
            )

        hard_filter_present = any((company, assigned, created_by, start_date, end_date))

        # Preserve only hard filters for semantic fallback.
        structured_filters = list(filters)
        structured_params = dict(params)

        if not tokens:
            if not hard_filter_present:
                # A vague request such as "show me tickets" legitimately means all tickets.
                # A singular vague request means the newest one.
                pass

            sql = f"""
                SELECT id
                FROM tickets
                WHERE {' AND '.join(filters)}
                ORDER BY COALESCE(last_activity_date, create_date) DESC NULLS LAST
            """
            sql = _apply_limit(sql, params, requested_count)
            rows = db.execute(text(sql), params).all()
        else:
            # Full-text matching requires all meaningful concepts and uses the existing
            # PostgreSQL GIN index. Weighted field matches choose the best ticket when
            # the user asks for a singular result.
            params["fts_query"] = " ".join(tokens)

            score_parts = []
            for index, token in enumerate(tokens):
                key = f"token_{index}"
                params[key] = f"%{token}%"
                score_parts.extend([
                    f"CASE WHEN LOWER(COALESCE(title,'')) LIKE :{key} THEN 12 ELSE 0 END",
                    f"CASE WHEN LOWER(COALESCE(resolution,'')) LIKE :{key} THEN 8 ELSE 0 END",
                    f"CASE WHEN LOWER(COALESCE(description,'')) LIKE :{key} THEN 5 ELSE 0 END",
                    f"CASE WHEN LOWER(COALESCE(document_text,'')) LIKE :{key} THEN 2 ELSE 0 END",
                ])

            relevance = " + ".join(score_parts) or "0"
            sql = f"""
                SELECT id, ({relevance}) AS relevance
                FROM tickets
                WHERE {' AND '.join(filters)}
                  AND to_tsvector('english', COALESCE(document_text, ''))
                      @@ plainto_tsquery('english', :fts_query)
                ORDER BY relevance DESC,
                         COALESCE(last_activity_date, create_date) DESC NULLS LAST
            """
            sql = _apply_limit(sql, params, requested_count)
            rows = db.execute(text(sql), params).all()

        ids = [int(row.id) for row in rows]

        # Existing lexical/full-text search remains primary. Semantic search
        # runs only when lexical search finds nothing.
        if not ids and tokens:
            ids = semantic_ticket_ids(
                db=db,
                question=question,
                structured_filters=structured_filters,
                structured_params=structured_params,
                requested_count=requested_count,
            )

        if not ids:
            return []

        found = db.scalars(select(Ticket).where(Ticket.id.in_(ids))).all()
        order = {ticket_id: index for index, ticket_id in enumerate(ids)}
        return sorted(found, key=lambda ticket: order[ticket.id])


def answer_ticket_question(question: str, tickets, history):
    if not tickets:
        return (
            "## No matching Autotask tickets\n\n"
            "I searched the synchronized Autotask index but did not find a match."
        )

    lines = [
        f"## {len(tickets)} matching ticket"
        f"{'s' if len(tickets) != 1 else ''}",
        "",
    ]

    for ticket in tickets:
        created = (
            ticket.create_date.strftime("%B %d, %Y at %I:%M %p")
            if ticket.create_date
            else "Not recorded"
        )
        resolution = (ticket.resolution or "").strip() or "No resolution recorded"

        lines.extend([
            f"### {ticket.ticket_number or ticket.id} — "
            f"{ticket.title or 'Untitled ticket'}",
            f"- **Date created:** {created}",
            f"- **Assigned to:** {ticket.assigned_to or 'Not recorded'}",
            f"- **Created by:** {ticket.created_by or 'Not recorded'}",
            f"- **Resolution:** {resolution}",
            "",
        ])

    return "\n".join(lines)


def answer_tech_question(question: str, history):
    messages = [{
        "role": "system",
        "content": """ /no_think
You are MBC - Beepy, a senior MSP support technician.

Give technically accurate, practical troubleshooting guidance.

Rules:
- Do not invent product behavior, error meanings, commands, or settings.
- Clearly separate likely causes from confirmed facts.
- Begin with the safest and most likely checks.
- Give exact commands only when they are appropriate.
- Explain what each check proves.
- Do not recommend disabling security controls except briefly for a controlled test.
- Ask for the exact error, product, version, or topology when the question lacks enough information.
- Keep the answer organized as: likely causes, checks, and next action.
- Never pretend that you searched Autotask unless ticket evidence was supplied.
""",
    }]
    messages.extend(history[-8:])
    messages.append({"role": "user", "content": question})
    return chat(messages, temperature=0.15)
