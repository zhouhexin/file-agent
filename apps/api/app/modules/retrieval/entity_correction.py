"""事实检索短实体的一字差异候选。

该服务只读取可重建检索投影，返回有限候选提示。候选不能成为证据、不能自动
改写用户问题，也不能直接授权 ``evidence-answer``。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.db.models import (
    DocumentChunk,
    DocumentIndexRun,
    ManagedFile,
    ManagedFileRevision,
    ManagedFileSearchProfile,
    ManagedRoot,
    WorkingCopy,
)


_COMMON_SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华"
    "金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方"
    "俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮"
    "卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计"
    "伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江"
    "童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经"
    "房裘缪干解应宗丁宣邓郁单杭洪包诸左石崔吉龚程邢裴陆荣翁荀"
    "羊甄曲家封芮储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬"
    "全郗班仰秋仲伊宫宁仇栾暴甘厉戎祖武符刘景詹束龙叶幸司韶黎"
    "乔苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍郤璩桑桂濮牛寿通边"
    "燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖"
    "庾终暨居衡步都耿满弘匡国文寇广禄阙东欧利蔚越夔隆师巩厍聂"
    "晁勾敖融冷辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游"
    "竺权逯盖益桓公"
)
_CHINESE_RUN_PATTERN = re.compile(r"[\u3400-\u9fff]+")


@dataclass(frozen=True)
class EntityCorrection:
    """一个未经用户确认的一字差异候选。"""

    original: str
    candidate: str
    edit_distance: int
    source_count: int


class FactEntityCorrectionService:
    """在当前活动源侧检索投影中寻找有限的人名纠错候选。"""

    def __init__(self, *, db: Session, workspace_id: str | None = None) -> None:
        self.db = db
        self.workspace_id = str(workspace_id or "")

    def suggest(self, *, entity_phrases: list[str]) -> list[EntityCorrection]:
        """每个短人名最多返回一个唯一最佳候选，平分时不擅自选择。"""

        suggestions: list[EntityCorrection] = []
        for original in list(dict.fromkeys(entity_phrases))[:4]:
            if not self._eligible_person_name(original):
                continue
            counts = self._candidate_counts(original)
            if not counts:
                continue
            ranked = counts.most_common(2)
            if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
                # 多个候选同强度时必须让用户补充条件，不能按数据库返回顺序猜人。
                continue
            candidate, source_count = ranked[0]
            suggestions.append(
                EntityCorrection(
                    original=original,
                    candidate=candidate,
                    edit_distance=1,
                    source_count=source_count,
                )
            )
        return suggestions

    def _candidate_counts(self, original: str) -> Counter[str]:
        """先以共享字符收窄投影，再在有限文本中执行确定性编辑距离。"""

        text_column = ManagedFileSearchProfile.search_text
        remaining_chars = list(dict.fromkeys(original[1:]))
        if not remaining_chars:
            return Counter()
        shared_character_filter = and_(
            text_column.contains(original[0]),
            or_(*(text_column.contains(char) for char in remaining_chars)),
        )
        rows = (
            self.db.query(
                ManagedFileSearchProfile.search_text,
                ManagedFileSearchProfile.entities_json,
            )
            .join(
                ManagedFileRevision,
                ManagedFileRevision.id
                == ManagedFileSearchProfile.managed_file_revision_id,
            )
            .join(
                ManagedFile,
                ManagedFile.id == ManagedFileRevision.managed_file_id,
            )
            .join(ManagedRoot, ManagedRoot.id == ManagedFile.root_id)
            .filter(
                ManagedFileSearchProfile.status == "ACTIVE",
                ManagedFileRevision.is_current.is_(True),
                ManagedFileRevision.status == "READY",
                ManagedFile.status == "ACTIVE",
                ManagedRoot.enabled.is_(True),
                shared_character_filter,
            )
            .limit(300)
            .all()
        )
        counts: Counter[str] = Counter()
        for search_text, entities_json in rows:
            candidates: set[str] = set()
            for value in entities_json if isinstance(entities_json, list) else []:
                candidates.update(self._windows(str(value or ""), len(original)))
            candidates.update(self._windows(str(search_text or ""), len(original)))
            for candidate in candidates:
                if (
                    candidate != original
                    and candidate.startswith(original[0])
                    and _edit_distance_at_most_one(original, candidate) == 1
                ):
                    counts[candidate] += 1
        counts.update(self._working_copy_candidate_counts(original))
        return counts

    def _working_copy_candidate_counts(self, original: str) -> Counter[str]:
        """补查已完成 Chunk，兼容历史工作副本尚未重建瘦投影的情况。"""

        if not self.workspace_id:
            return Counter()
        text_column = DocumentChunk.search_text
        remaining_chars = list(dict.fromkeys(original[1:]))
        shared_character_filter = and_(
            text_column.contains(original[0]),
            or_(*(text_column.contains(char) for char in remaining_chars)),
        )
        rows = (
            self.db.query(DocumentChunk.text_content)
            .join(
                DocumentIndexRun,
                DocumentIndexRun.id == DocumentChunk.index_run_id,
            )
            .join(
                WorkingCopy,
                WorkingCopy.current_version_id == DocumentChunk.document_version_id,
            )
            .filter(
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.status == "ACTIVE",
                DocumentIndexRun.status == "COMPLETED",
                shared_character_filter,
            )
            .limit(100)
            .all()
        )
        counts: Counter[str] = Counter()
        for (text_content,) in rows:
            for candidate in self._windows(str(text_content or ""), len(original)):
                if (
                    candidate != original
                    and candidate.startswith(original[0])
                    and _edit_distance_at_most_one(original, candidate) == 1
                ):
                    counts[candidate] += 1
        return counts

    @staticmethod
    def _windows(value: str, width: int) -> set[str]:
        """从分词投影的中文片段提取等长窗口，不返回英文或标点碎片。"""

        result: set[str] = set()
        for run in _CHINESE_RUN_PATTERN.findall(value):
            if len(run) < width:
                continue
            for index in range(len(run) - width + 1):
                result.add(run[index : index + width])
        return result

    @staticmethod
    def _eligible_person_name(value: str) -> bool:
        """纠错仅覆盖常见中文姓名外形，机构和业务短语保持精确。"""

        return bool(
            re.fullmatch(r"[\u3400-\u9fff]{3,4}", str(value or ""))
            and value[0] in _COMMON_SURNAMES
        )


def _edit_distance_at_most_one(left: str, right: str) -> int:
    """等长短实体只允许一个字符替换；其它差异统一视为不可纠错。"""

    if len(left) != len(right):
        return 2
    distance = sum(1 for first, second in zip(left, right) if first != second)
    return distance if distance <= 1 else 2


def attach_entity_corrections(
    *, result: dict[str, Any], corrections: list[EntityCorrection]
) -> dict[str, Any]:
    """把纠错提示附加到空/候选结果，绝不提升文件相关性等级。"""

    if not corrections:
        return result
    public = [
        {
            "original": item.original,
            "candidate": item.candidate,
            "reason": "当前索引中存在仅一字不同的人名，需确认后再读取事实。",
        }
        for item in corrections
    ]
    return {
        **result,
        "query_corrections": public,
        "user_message": "发现可能的人名输入差异，请先确认姓名后再继续回答。",
    }
