from __future__ import annotations


def evaluate_evidence_flow(
    expected_document_keys: list[str],
    candidate_document_keys: list[str],
    usable_document_keys: list[str],
) -> dict:
    expected = set(expected_document_keys)
    candidates = set(candidate_document_keys)
    usable = set(usable_document_keys)
    return {
        "candidateHit": not expected or bool(expected & candidates),
        "usableHit": not expected or bool(expected & usable),
        "retrievalMisses": sorted(expected - candidates),
        "evidenceGradeMisses": sorted((expected & candidates) - usable),
    }
