"""Mechanical evidence checks; these do not replace semantic review."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import re


_capture: ContextVar[list | None] = ContextVar("evidence_diagnostic_capture", default=None)
_CJK_WRAP = re.compile(
    r"(?<=[一-鿿])[ \t\r\n]+(?=[一-鿿])"
    r"|(?<=[一-鿿，。；：！？、（）【】])[ \t]*[\r\n]+[ \t]*(?=[一-鿿，。；：！？、（）【】0-9])"
    r"|(?<=[0-9])[ \t]*[\r\n]+[ \t]*(?=[一-鿿，。；：！？、（）【】])"
)
_CAMPUS = re.compile(r"[一-鿿A-Za-z0-9]{1,16}校区")
_PHONE = re.compile(r"(?<!\d)\d{7,12}(?!\d)")


@contextmanager
def capture_evidence_diagnostics():
    """Explicit isolated-evaluation capture, never enabled by production logging."""
    records = []
    token = _capture.set(records)
    try:
        yield records
    finally:
        _capture.reset(token)


def record_evidence_diagnostic(payload):
    records = _capture.get()
    if records is not None:
        records.append(deepcopy(payload))


def normalize_quote(text: str) -> str:
    # 仅合并中文排版换行；不合并数字之间或英文单词之间的空白，不改标点和单位。
    return _CJK_WRAP.sub("", text.strip())


def locate_quote(quote: str, original: str) -> str | None:
    if not quote.strip():
        return None
    if quote in original:
        return quote
    removed = {index for match in _CJK_WRAP.finditer(original) for index in range(*match.span())}
    offsets = [index for index in range(len(original)) if index not in removed]
    normalized = "".join(original[index] for index in offsets)
    needle = normalize_quote(quote)
    start = normalized.find(needle) if needle else -1
    if start < 0:
        return None
    return original[offsets[start]:offsets[start + len(needle) - 1] + 1]


def scoped_contacts(evidence: list[dict]) -> dict[str, set[str]]:
    contacts: dict[str, set[str]] = {}
    for item in evidence:
        text = str(item.get("content") or item.get("text") or item.get("snippet") or "")
        lines = text.splitlines()
        campuses = set(_CAMPUS.findall(text))
        for index, line in enumerate(lines):
            if not re.search(r"电话|热线|联系电话|联系方式", line):
                continue
            local = set(_CAMPUS.findall(line))
            if not local:
                local = set(_CAMPUS.findall("\n".join(lines[max(0, index - 2):index])))
            if not local and len(campuses) == 1:
                local = campuses
            if len(local) == 1:
                for phone in _PHONE.findall(line):
                    contacts.setdefault(phone, set()).update(local)
    return contacts


def contact_scope_violations(answer: str, evidence: list[dict]) -> list[str]:
    """Reject dropping a known campus qualifier next to a telephone number."""
    contacts = scoped_contacts(evidence)
    violations = []
    # A campus in an unrelated paragraph must not authorize an unqualified number.
    for paragraph in re.split(r"\n\s*\n|[。；;]", answer):
        for phone in _PHONE.findall(paragraph):
            scopes = contacts.get(phone, set())
            if scopes and (not any(scope in paragraph for scope in scopes)
                           or any(word in paragraph for word in ("全校通用", "所有校区", "各校区通用"))):
                violations.append(phone)
    return list(dict.fromkeys(violations))
