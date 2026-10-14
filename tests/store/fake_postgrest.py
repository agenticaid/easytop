"""A small in-process stand-in for Supabase's Data API (PostgREST) over the Easytop schema: enough to run the
store contract tests offline. It mimics the behaviours the store relies on, as documented by PostgREST:
eq / in / not.in filters, select, order, limit, on_conflict upserts (Prefer: resolution=merge-duplicates),
return=representation vs minimal, unique and check constraints (409 / 400 with the Postgres code), foreign
keys with cascade, bytea as \\x-hex and time as HH:MM:SS. The real service is checked by the live test."""

from __future__ import annotations

import json
import re
import uuid

import httpx

UNIQUE = {
    "parents": [("id",)],
    "parent_channels": [("id",), ("channel", "channel_user_id")],
    "kids": [("id",), ("parent_id", "webtop_kid_id")],
    "webtop_sessions": [("parent_id",)],
    "digest_settings": [("parent_id",)],
    "allowed_users": [("channel", "channel_user_id")],
    "invites": [("code_hash",)],
}
HAS_ID = {"parents", "parent_channels", "kids"}
CHILDREN = ["parent_channels", "kids", "webtop_sessions", "digest_settings"]


class FakePostgrest:
    def __init__(self, service_key: str = "service-key"):
        self.key = service_key
        self.tables: dict[str, list[dict]] = {t: [] for t in UNIQUE}
        self.requests: list[httpx.Request] = []

    # ---- plumbing ----
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def _error(status: int, code: str, message: str) -> httpx.Response:
        return httpx.Response(status, json={"code": code, "message": message})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("apikey") != self.key:
            return self._error(401, "PGRST301", "Invalid API key")
        if (
            "authorization" in request.headers
        ):  # a non-JWT secret key there makes PostgREST reject the call
            return self._error(401, "PGRST301", "JWT invalid")
        table = request.url.path.removeprefix("/rest/v1/")
        if table not in self.tables:
            return self._error(404, "42P01", f"relation {table} does not exist")
        params = dict(request.url.params)
        prefer = request.headers.get("prefer", "")
        if request.method == "GET":
            return self._get(table, params)
        if request.method == "POST":
            return self._post(table, params, prefer, json.loads(request.content))
        if request.method == "PATCH":
            return self._patch(table, params, prefer, json.loads(request.content))
        if request.method == "DELETE":
            return self._delete(table, params)
        return self._error(405, "PGRST000", "method")

    # ---- filters ----
    @staticmethod
    def _matches(row: dict, params: dict) -> bool:
        for col, cond in params.items():
            if col in ("select", "order", "limit", "on_conflict"):
                continue
            if cond.startswith("eq."):
                value = row.get(col)
                if (str(value).lower() if isinstance(value, bool) else str(value)) != cond[3:]:
                    return False
            elif cond == "is.null":
                if row.get(col) is not None:
                    return False
            elif cond.startswith("gt."):
                from datetime import datetime

                if not datetime.fromisoformat(row[col]) > datetime.fromisoformat(cond[3:]):
                    return False
            elif cond.startswith("in.("):
                values = re.findall(r'"((?:[^"\\]|\\.)*)"', cond)
                if str(row.get(col)) not in values:
                    return False
            elif cond.startswith("not.in.("):
                values = re.findall(r'"((?:[^"\\\\]|\\\\.)*)"', cond)
                values = [v.replace('\\\\"', '"').replace("\\\\\\\\", "\\\\") for v in values]
                if str(row.get(col)) in values:
                    return False
            else:
                raise AssertionError(f"filter not supported by the fake: {col}={cond}")
        return True

    # ---- verbs ----
    def _get(self, table: str, params: dict) -> httpx.Response:
        rows = [r for r in self.tables[table] if self._matches(r, params)]
        for spec in reversed(params.get("order", "").split(",")) if params.get("order") else []:
            col, _, direction = spec.partition(".")
            rows.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=direction == "desc")
        if "limit" in params:
            rows = rows[: int(params["limit"])]
        cols = params.get("select", "*")
        out = rows if cols == "*" else [{c: r.get(c) for c in cols.split(",")} for r in rows]
        return httpx.Response(200, json=out)

    def _post(self, table: str, params: dict, prefer: str, body) -> httpx.Response:
        incoming = body if isinstance(body, list) else [body]
        stored = []
        for row in incoming:
            row = dict(row)
            if table in HAS_ID:
                row.setdefault("id", str(uuid.uuid4()))
            err = self._check(table, row)
            if err:
                return err
            conflict = self._conflict(table, row)
            if conflict is not None:
                if "resolution=ignore-duplicates" in prefer:
                    continue
                if "resolution=merge-duplicates" in prefer:
                    keys = params.get("on_conflict", "").split(",")
                    if not all(k in row for k in keys) or conflict is False:
                        return self._error(
                            400, "42P10", "no unique constraint matches the ON CONFLICT"
                        )
                    conflict.update(row)
                    stored.append(conflict)
                    continue
                return self._error(
                    409, "23505", f"duplicate key value violates unique constraint on {table}"
                )
            self.tables[table].append(row)
            stored.append(row)
        if "return=representation" in prefer:
            return httpx.Response(201, json=stored)
        return httpx.Response(201)

    def _patch(self, table: str, params: dict, prefer: str, body: dict) -> httpx.Response:
        hit = [r for r in self.tables[table] if self._matches(r, params)]
        for row in hit:
            row.update(body)
        if "return=representation" in prefer:
            cols = params.get("select", "*")
            out = hit if cols == "*" else [{c: r.get(c) for c in cols.split(",")} for r in hit]
            return httpx.Response(200, json=out)
        return httpx.Response(204)

    def _delete(self, table: str, params: dict) -> httpx.Response:
        doomed = [r for r in self.tables[table] if self._matches(r, params)]
        self.tables[table] = [r for r in self.tables[table] if r not in doomed]
        if table == "parents":  # on delete cascade
            ids = {r["id"] for r in doomed}
            for child in CHILDREN:
                self.tables[child] = [r for r in self.tables[child] if r["parent_id"] not in ids]
        return httpx.Response(204)

    # ---- constraints ----
    def _check(self, table: str, row: dict) -> httpx.Response | None:
        if table in CHILDREN and not any(
            p["id"] == row.get("parent_id") for p in self.tables["parents"]
        ):
            return self._error(409, "23503", "insert violates foreign key constraint")
        if table == "parent_channels" and row.get("channel") not in ("telegram", "whatsapp"):
            return self._error(400, "23514", "violates check constraint on channel")
        if table == "digest_settings" and row.get("enabled") and not row.get("local_time"):
            return self._error(400, "23514", "violates check constraint digest_settings")
        return None

    def _conflict(self, table: str, row: dict):
        for cols in UNIQUE[table]:
            if all(c in row for c in cols):
                for existing in self.tables[table]:
                    if all(existing.get(c) == row[c] for c in cols):
                        return existing
        return None
