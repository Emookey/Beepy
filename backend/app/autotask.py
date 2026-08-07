from __future__ import annotations
from typing import Any
import httpx
from .config import get_settings

settings = get_settings()

class AutotaskError(RuntimeError):
    pass

class AutotaskClient:
    def __init__(self) -> None:
        self.base_url = settings.autotask_base_url.rstrip("/")
        self.headers = {
            "UserName": settings.autotask_username,
            "Secret": settings.autotask_secret,
            "ApiIntegrationCode": settings.autotask_integration_code,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def query_page(
        self,
        entity: str,
        filters: list[dict[str, Any]],
        include_fields: list[str] | None = None,
        max_records: int = 500,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"MaxRecords": max_records, "filter": filters}
        if include_fields:
            if "id" not in [field.lower() for field in include_fields]:
                include_fields = ["id", *include_fields]
            body["IncludeFields"] = include_fields
        with httpx.Client(timeout=60) as client:
            response = client.post(
                f"{self.base_url}/{entity}/query",
                headers=self.headers,
                json=body,
            )
        if response.status_code >= 400:
            raise AutotaskError(
                f"{entity} query failed with HTTP {response.status_code}: {response.text[:1000]}"
            )
        return response.json().get("items", [])

    def query_all_by_id(
        self,
        entity: str,
        filters: list[dict[str, Any]] | None = None,
        include_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        # Autotask documents ID-based looping as an alternative to paging URLs.
        # It avoids /query/next method/body inconsistencies.
        items: list[dict[str, Any]] = []
        last_id = -1
        for _ in range(settings.max_autotask_pages):
            page_filters = list(filters or [])
            page_filters.append({"op": "gt", "field": "id", "value": last_id})
            page = self.query_page(entity, page_filters, include_fields, 500)
            if not page:
                break
            items.extend(page)
            new_last_id = max(int(item["id"]) for item in page)
            if new_last_id <= last_id:
                raise AutotaskError(f"{entity} ID pagination stopped advancing.")
            last_id = new_last_id
            if len(page) < 500:
                break
        return items

    def query_since(
        self,
        entity: str,
        field: str,
        iso_value: str,
        include_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.query_all_by_id(
            entity,
            filters=[{"op": "gte", "field": field, "value": iso_value}],
            include_fields=include_fields,
        )

    def ticket_web_url(self, ticket_id: int) -> str:
        # Autotask deep-link command for a ticket entity.
        return (
            f"{settings.autotask_web_base_url}"
            f"?Command=OpenTicket&TicketID={int(ticket_id)}"
        )
