from __future__ import annotations
import os
from typing import Any
from sqlalchemy import text
from sqlalchemy.orm import Session
from .ollama import embed

def semantic_threshold() -> float:
    try:
        value=float(os.getenv("SEMANTIC_TICKET_THRESHOLD","0.40"))
    except ValueError:
        value=0.40
    return min(max(value,0.0),1.0)

def semantic_ticket_ids(*,db:Session,question:str,structured_filters:list[str],structured_params:dict[str,Any],requested_count:int|None)->list[int]:
    question=question.strip()
    if not question:
        return []
    try:
        vector=embed([question])[0]
    except Exception as exc:
        print(f"Semantic embedding failed: {exc}",flush=True)
        return []
    params=dict(structured_params)
    params["semantic_vector"]=str(vector)
    params["semantic_threshold"]=semantic_threshold()
    limit_sql=""
    if requested_count is not None:
        params["semantic_limit"]=requested_count
        limit_sql="LIMIT :semantic_limit"
    sql=f"""
        SELECT id,
               1-(embedding <=> CAST(:semantic_vector AS vector)) AS similarity
        FROM tickets
        WHERE {' AND '.join(structured_filters)}
          AND embedding IS NOT NULL
          AND 1-(embedding <=> CAST(:semantic_vector AS vector)) >= :semantic_threshold
        ORDER BY embedding <=> CAST(:semantic_vector AS vector),
                 COALESCE(last_activity_date,create_date) DESC NULLS LAST
        {limit_sql}
    """
    try:
        rows=db.execute(text(sql),params).all()
    except Exception as exc:
        print(f"Semantic query failed: {exc}",flush=True)
        return []
    return [int(row.id) for row in rows]
