"""Conservative intent routing for local document questions.

The router is deliberately not an autonomous planning agent.  It converts a
natural-language request into a small, auditable set of product intents before
the expensive retrieval or generation path starts.  Rules handle obvious
commands; the existing embedding provider is only used as a guarded fallback
for ambiguous wording.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class IntentKind(StrEnum):
    """Supported user intents at the question-entry boundary."""

    FILE_LOCATION = "file_location"
    KNOWLEDGE_QA = "knowledge_qa"
    TABLE_OPERATION = "table_operation"


@dataclass(frozen=True)
class IntentRoute:
    """A structured, explainable routing decision."""

    intent: IntentKind
    object_query: str
    confidence: float
    method: str


class EmbeddingProvider(Protocol):
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


_LOCATION_TERMS = (
    "在哪",
    "哪里",
    "哪儿",
    "路径",
    "文件夹",
    "目录",
    "位置",
    "地点",
    "方位",
    "地址",
    "存放地",
    "存储地址",
    "保存地址",
    "地方",
    "path",
    "location",
    "folder",
    "directory",
)
_STRONG_FILE_LOCATION_TERMS = (
    "路径",
    "文件夹",
    "目录",
    "位置",
    "地点",
    "方位",
    "地址",
    "存放地",
    "存储地址",
    "保存地址",
    "path",
    "location",
    "folder",
    "directory",
)
_DOCUMENT_TERMS = (
    "文件",
    "文档",
    "资料",
    "附件",
    "表格",
    "档案",
    "材料",
    "报告",
    "课件",
    "文献",
    "手册",
    "说明书",
    "清单",
    "包",
)
_SEARCH_ACTION_TERMS = (
    "查找",
    "查一下",
    "查下",
    "查询",
    "寻找",
    "找一下",
    "找到",
    "找",
    "搜索",
    "搜一下",
    "看下",
    "看看",
    "定位",
    "获取",
    "有没有",
    "有无",
    "是否有",
)
_TABLE_OPERATION_TERMS = (
    "行列转换",
    "行转列",
    "列转行",
    "转置",
    "透视",
    "透视表",
    "宽表转长表",
    "长表转宽表",
    "交叉表",
    "pivot",
    "unpivot",
    "求和",
    "汇总",
    "筛选",
    "分组",
    "排序",
    "去重",
)
_KNOWLEDGE_QUESTION_MARKERS = (
    "是什么",
    "为什么",
    "如何",
    "怎么",
    "怎样",
    "原理",
    "区别",
    "影响",
    "含义",
    "作用",
    "用途",
    "特点",
    "优点",
    "缺点",
    "优缺点",
    "包括",
    "包含",
    "几种",
    "多少",
)
_LEADING_SCAFFOLDING = tuple(
    sorted(
        (
            "麻烦帮我",
            "请帮我",
            "可不可以",
            "能不能",
            "麻烦你",
            "有没有",
            "是否有",
            "请问",
            "麻烦",
            "能否",
            "可以",
            "可否",
            "我想要",
            "我需要",
            "我想",
            "我要",
            "回答",
            "告诉我",
            "返回",
            "给出",
            "提供",
            "给我",
            "帮我",
            "帮忙",
            "查找",
            "查一下",
            "查下",
            "查询",
            "寻找",
            "找一下",
            "找到",
            "搜索",
            "搜一下",
            "看下",
            "看看",
            "定位",
            "获取",
            "有无",
            "请",
            "能",
            "找",
            "搜",
            "把",
        ),
        key=len,
        reverse=True,
    )
)
_TRAILING_DOCUMENT_WORDS = re.compile(
    r"(?:这个|该|此|那)?(?:相关|有关)?"
    r"(?:文件|文档|资料|附件|表格|档案|材料|报告|课件)"
    r"(?:的)?(?:存放|保存|放置|放在|存放在|位于|在|放)?(?:什么|哪个)?\s*$"
)
_TRAILING_DEMONSTRATIVE = re.compile(r"(?:这个|该|此|那)\s*$")
_TRAILING_POSSESSIVE = re.compile(r"的\s*$")
_TRAILING_EXTENDED_DOCUMENT_WORDS = re.compile(
    r"(?:\u8fd9\u4e2a|\u8be5|\u6b64|\u90a3)?(?:\u76f8\u5173|\u6709\u5173)?"
    r"(?:\u6587\u732e|\u624b\u518c|\u8bf4\u660e\u4e66|\u6e05\u5355|\u538b\u7f29\u5305)"
    r"(?:\u7684)?(?:\u5b58\u653e|\u4fdd\u5b58|\u5b58\u50a8|\u50a8\u5b58|\u653e\u7f6e|\u653e\u5728|"
    r"\u5b58\u653e\u5728|\u4f4d\u4e8e|\u5728|\u653e)?(?:\u4ec0\u4e48|\u54ea\u4e2a)?\s*$"
)
_LEADING_TOPIC_CONNECTOR = re.compile(r"^(?:关于|有关|和|与)\s*")


class IntentRouter:
    """Route requests with deterministic-first, semantic-fallback policy."""

    def __init__(self, *, embedding_provider: EmbeddingProvider | None = None) -> None:
        self.embedding_provider = embedding_provider

    def route(self, question: str) -> IntentRoute:
        normalized = _normalize_question(question)
        object_query = _extract_document_reference(normalized)
        has_location_phrase = any(term in normalized for term in _LOCATION_TERMS)
        has_strong_location_phrase = any(
            term in normalized for term in _STRONG_FILE_LOCATION_TERMS
        )
        has_document_word = any(term in normalized for term in _DOCUMENT_TERMS)
        has_search_action = any(term in normalized for term in _SEARCH_ACTION_TERMS)
        has_filename_suffix = bool(
            re.search(r"\.(?:[A-Za-z]{2,12}|7z)(?![A-Za-z0-9])", normalized)
        )
        looks_like_knowledge_question = any(
            marker in normalized for marker in _KNOWLEDGE_QUESTION_MARKERS
        )

        # Explicit document language plus a location/search request is safe to
        # route without a model.  This covers phrases such as “回答可解释性文档
        # 位置”, “帮我寻找 xx 文件”, and the usual “xx 在哪个文件夹”.
        if object_query and (has_location_phrase or has_search_action) and (
            has_document_word or has_filename_suffix
        ):
            return IntentRoute(
                intent=IntentKind.FILE_LOCATION,
                object_query=object_query,
                confidence=0.98,
                method="rule",
            )

        # “标题 + 位置/路径/目录” is a common terse file-lookup form.  In a
        # document application these spatial nouns are stronger evidence than
        # the generic “在哪/哪里”; keep a small guard for factual questions
        # such as “模型位置是什么”.
        if (
            object_query
            and has_strong_location_phrase
            and not looks_like_knowledge_question
        ):
            return IntentRoute(
                intent=IntentKind.FILE_LOCATION,
                object_query=object_query,
                confidence=0.92,
                method="rule",
            )

        # A bare title followed by “在哪” is inherently ambiguous (it can be
        # either a factual question or a document lookup).  Use embeddings only
        # for that narrow boundary, and require a high confidence margin.
        if object_query and has_location_phrase:
            semantic_route = self._semantic_file_location_route(
                normalized=normalized,
                object_query=object_query,
            )
            if semantic_route is not None:
                return semantic_route

        if any(term in normalized for term in _TABLE_OPERATION_TERMS):
            return IntentRoute(
                intent=IntentKind.TABLE_OPERATION,
                object_query=normalized,
                confidence=0.90,
                method="rule",
            )
        return IntentRoute(
            intent=IntentKind.KNOWLEDGE_QA,
            object_query=normalized,
            confidence=0.70,
            method="default",
        )

    def _semantic_file_location_route(
        self, *, normalized: str, object_query: str
    ) -> IntentRoute | None:
        if self.embedding_provider is None:
            return None
        prompts = [
            normalized,
            "查找文件、文档、资料的存放路径、目录和位置",
            "解释资料内容并回答知识问题",
            "查询表格的行列、计算、汇总或转换操作",
        ]
        try:
            vectors = self.embedding_provider.embed_texts(prompts)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return None
        if len(vectors) != len(prompts):
            return None
        scores = [_cosine_similarity(vectors[0], vector) for vector in vectors[1:]]
        file_score, qa_score, table_score = scores
        runner_up = max(qa_score, table_score)
        if file_score < 0.78 or file_score - runner_up < 0.08:
            return None
        return IntentRoute(
            intent=IntentKind.FILE_LOCATION,
            object_query=object_query,
            confidence=file_score,
            method="semantic",
        )


def _normalize_question(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).strip()


def _extract_document_reference(question: str) -> str:
    positions = [question.find(term) for term in _LOCATION_TERMS if question.find(term) >= 0]
    candidate = question[: min(positions)] if positions else question
    candidate = candidate.strip(" \t\r\n，,。；;：:'\"“”‘’（）()[]【】")
    candidate = _TRAILING_EXTENDED_DOCUMENT_WORDS.sub("", candidate).strip()
    candidate = _TRAILING_DOCUMENT_WORDS.sub("", candidate).strip()
    candidate = _TRAILING_DEMONSTRATIVE.sub("", candidate).strip()
    candidate = _TRAILING_POSSESSIVE.sub("", candidate).strip()
    candidate = _strip_leading_scaffolding(candidate)
    candidate = _LEADING_TOPIC_CONNECTOR.sub("", candidate).strip()
    return candidate.strip(" \t\r\n，,。；;：:'\"“”‘’（）()[]【】")


def _strip_leading_scaffolding(value: str) -> str:
    """Remove composable politeness, search and answer-command prefixes."""

    remaining = value.lstrip()
    while remaining:
        prefix = next(
            (item for item in _LEADING_SCAFFOLDING if remaining.startswith(item)),
            None,
        )
        if prefix is None:
            break
        remaining = remaining.removeprefix(prefix).lstrip()
    return remaining


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    if denominator == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator
