from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
import re
from pathlib import Path
from typing import Any

from skills import resolve_data_path
from skills.tool_decorator import tool


MIN_SEMANTIC_MATCH_THRESHOLD = 0.015
LEXICAL_WEIGHT = 0.58
SEMANTIC_WEIGHT = 0.42


@dataclass
class _Document:
    path: Path
    text: str
    filename: str


def _snippet(
    text: str,
    terms: list[str],
    radius: int = 60,
    center: int | None = None,
) -> str:
    lowered = text.casefold()
    positions = [lowered.find(term.casefold()) for term in terms] if terms else []
    positions = [position for position in positions if position >= 0]
    anchor = center if center is not None else (min(positions) if positions else 0)
    start = max(0, anchor - radius)
    end = min(len(text), start + radius * 2)
    prefix = "..." if start else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end].replace("\n", " ").strip() + suffix


def _score_document(
    text: str,
    filename: str,
    phrase: str,
    terms: list[str],
) -> tuple[float, list[str], bool, float]:
    lowered_text = text.casefold()
    lowered_filename = filename.casefold()

    score = 0.0
    filename_score = 0.0
    matched_terms: list[str] = []

    for term in terms:
        text_hits = lowered_text.count(term)
        filename_hits = lowered_filename.count(term)

        if text_hits > 0 or filename_hits > 0:
            matched_terms.append(term)

        score += min(text_hits, 5)
        if filename_hits > 0:
            score += 3
            filename_score += 3

    phrase_matched = False
    if len(terms) > 1:
        compact_phrase = re.sub(r"\s+", "", phrase.casefold())
        compact_text = re.sub(r"\s+", "", lowered_text)
        compact_filename = re.sub(r"\s+", "", lowered_filename)

        if compact_phrase and compact_phrase in compact_text:
            score += 2 * len(terms) * compact_text.count(compact_phrase)
            phrase_matched = True

        if compact_phrase and compact_phrase in compact_filename:
            score += 20
            filename_score += 20
            phrase_matched = True

    return round(score, 3), matched_terms, phrase_matched, round(filename_score, 3)


def _normalize_score(score: float) -> float:
    if score <= 0:
        return 0.0
    return score / (score + 8.0)


def _build_query(query: str) -> dict:
    normalized = query.casefold().strip()
    phrase = re.sub(r"\s+", "", normalized)
    raw_terms = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", normalized)

    terms = []
    seen = set()
    for term in raw_terms:
        if term not in seen:
            terms.append(term)
            seen.add(term)

    return {
        "phrase": phrase,
        "terms": terms,
    }


def _tokenize_for_vector(text: str) -> list[str]:
    normalized = text.casefold()
    tokens: list[str] = []

    try:
        import jieba

        tokens.extend(token.strip() for token in jieba.cut(normalized) if token.strip())
    except Exception:
        tokens.extend(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", normalized))

    tokens.extend(re.findall(r"[a-z0-9_]+", normalized))
    chinese = re.sub(r"[^\u4e00-\u9fff]", "", normalized)
    tokens.extend(chinese[index : index + 2] for index in range(max(0, len(chinese) - 1)))

    deduped = []
    seen = set()
    for token in tokens:
        if token and token not in seen:
            deduped.append(token)
            seen.add(token)
    return deduped


def _fallback_vector_scores(query: str, documents: list[_Document]) -> list[float]:
    query_tokens = _tokenize_for_vector(query)
    doc_tokens = [
        _tokenize_for_vector(f"{document.filename} {document.text}")
        for document in documents
    ]
    all_tokens = doc_tokens + [query_tokens]

    document_frequency: dict[str, int] = {}
    for tokens in all_tokens:
        for token in set(tokens):
            document_frequency[token] = document_frequency.get(token, 0) + 1

    doc_count = len(all_tokens)

    def vectorize(tokens: list[str]) -> dict[str, float]:
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1

        vector = {}
        for token, count in counts.items():
            idf = 1.0 + doc_count / (1 + document_frequency.get(token, 0))
            vector[token] = count * idf
        return vector

    def cosine(left: dict[str, float], right: dict[str, float]) -> float:
        if not left or not right:
            return 0.0
        dot = sum(value * right.get(token, 0.0) for token, value in left.items())
        left_norm = sqrt(sum(value * value for value in left.values()))
        right_norm = sqrt(sum(value * value for value in right.values()))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return dot / (left_norm * right_norm)

    query_vector = vectorize(query_tokens)
    return [cosine(vectorize(tokens), query_vector) for tokens in doc_tokens]


def _semantic_scores(query: str, documents: list[_Document]) -> list[float]:
    if not documents:
        return []

    corpus = [f"{document.filename}\n{document.text}" for document in documents]
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        vectorizer = TfidfVectorizer(
            analyzer="word",
            tokenizer=_tokenize_for_vector,
            token_pattern=None,
            ngram_range=(1, 2),
            lowercase=False,
        )
        matrix = vectorizer.fit_transform(corpus + [query])
        similarities = cosine_similarity(matrix[:-1], matrix[-1]).reshape(-1)
        return [max(0.0, float(score)) for score in similarities]
    except Exception:
        return _fallback_vector_scores(query, documents)


@tool(
    description="Search local txt and md files for query terms.",
    parameters={
        "query": "Search query.",
        "root_dir": "Directory relative to data root.",
        "file_types": "File extensions to search.",
        "top_k": "Maximum number of matches.",
    },
    returns={
        "results": {"type": "array", "description": "Ranked paths, snippets, and scores."},
    },
)
def local_file_search(
    query: str,
    root_dir: str = "docs",
    file_types: list[str] | None = None,
    top_k: int = 5,
    *,
    data_root: str | None = None,
) -> dict:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValueError("top_k must be a positive integer")

    search_root, data_root_path = resolve_data_path(root_dir, data_root)
    if not search_root.is_dir():
        raise FileNotFoundError(f"search directory not found: {root_dir}")

    extensions = file_types or ["txt", "md"]
    normalized_extensions = {f".{item.lower().lstrip('.')}" for item in extensions}
    if not normalized_extensions.issubset({".txt", ".md"}):
        raise ValueError("local_file_search only supports txt and md")

    documents = []
    for path in sorted(search_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in normalized_extensions:
            continue
        documents.append(_Document(path=path, text=path.read_text(encoding="utf-8"), filename=path.stem))

    query_info = _build_query(query)
    phrase = query_info["phrase"]
    terms = query_info["terms"]
    semantic_scores = _semantic_scores(query, documents)
    max_semantic_score = max(semantic_scores, default=0.0)
    semantic_threshold = max(
        MIN_SEMANTIC_MATCH_THRESHOLD,
        max_semantic_score * 0.6,
    )

    results = []
    for document, semantic_score in zip(documents, semantic_scores):
        raw_lexical_score, matched_terms, phrase_matched, filename_score = _score_document(
            text=document.text,
            filename=document.filename,
            phrase=phrase,
            terms=terms,
        )
        semantic_matched = semantic_score > 0 and semantic_score >= semantic_threshold
        if not matched_terms and not semantic_matched:
            continue

        lexical_score = _normalize_score(raw_lexical_score)
        final_score = (LEXICAL_WEIGHT * lexical_score) + (SEMANTIC_WEIGHT * semantic_score)
        if phrase_matched:
            final_score += 0.08
        if filename_score:
            final_score += min(0.07, filename_score / 100)

        results.append(
            {
                "path": document.path.relative_to(data_root_path).as_posix(),
                "score": round(final_score, 4),
                "lexical_score": round(lexical_score, 4),
                "semantic_score": round(semantic_score, 4),
                "matched_terms": matched_terms,
                "phrase_matched": phrase_matched,
                "semantic_matched": semantic_matched,
                "snippet": _snippet(document.text, matched_terms),
            }
        )

    results.sort(
        key=lambda item: (
            not item["phrase_matched"],
            -item["score"],
            not item["semantic_matched"],
            item["path"],
        )
    )
    return {"results": results[:top_k]}
