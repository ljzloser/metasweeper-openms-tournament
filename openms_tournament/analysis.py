"""Personal tournament scoring from website-associated videos, without local replay guesses."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import fsum, isfinite

from .client import OpenmsError, Tournament

LEVEL_NAMES = {'b': '初级', 'i': '中级', 'e': '高级'}
STATE_NAMES = {'a': '待审核', 'b': '已冻结', 'c': '审核通过', 'd': '玩家标识不匹配', 'e': '外站审核'}
COUNTS = {'w': {'i': 5, 'e': 2}, 'e': {'i': 5, 'e': 2}, 'g': {'b': 20, 'i': 12, 'e': 5}}
TIME_LIMITS = {'b': 10, 'i': 60, 'e': 240}
GSC_BV_MIN = {'b': 10, 'i': 30, 'e': 100}
STNB_COEFFICIENTS = {'b': 36, 'i': 162, 'e': 435}


@dataclass(frozen=True)
class ScoreVideo:
    id: int
    level: str
    mode: str
    state: str
    timems: int
    bv: int
    clicks: int | None

    @property
    def seconds(self) -> float:
        return self.timems / 1000

    @property
    def ioe(self) -> float | None:
        return self.bv / self.clicks if self.clicks and self.bv > 0 else None

    @property
    def efficiency_score(self) -> float | None:
        if self.clicks is None or self.clicks <= 0 or self.bv <= 0 or self.timems <= 0:
            return None
        value = self.timems * self.clicks ** 2 / (1000 * self.bv ** 2)
        return value if isfinite(value) else None


@dataclass(frozen=True)
class ScoreSlot:
    video: ScoreVideo | None
    value: float


@dataclass(frozen=True)
class ScoreGroup:
    level: str
    slots: tuple[ScoreSlot, ...]

    @property
    def total(self) -> float:
        return fsum(slot.value for slot in self.slots)

    @property
    def selected_count(self) -> int:
        return sum(slot.video is not None for slot in self.slots)


@dataclass(frozen=True)
class VideoAssessment:
    video: ScoreVideo
    value: float | None
    counted: bool
    reason: str


@dataclass(frozen=True)
class ReferenceGroup:
    level: str
    metric: str
    values: tuple[tuple[int, float], ...]
    missing: int

    @property
    def total(self) -> float:
        return fsum(value for _, value in self.values)


@dataclass(frozen=True)
class PersonalAnalysis:
    tournament: Tournament
    user_id: int
    fetched_at: datetime
    groups: tuple[ScoreGroup, ...]
    videos: tuple[VideoAssessment, ...]
    references: tuple[ReferenceGroup, ...] = ()

    @property
    def total(self) -> float:
        return fsum(group.total for group in self.groups)


def analyze_personal(tournament: Tournament, user_id: int, videos: tuple[ScoreVideo, ...]) -> PersonalAnalysis:
    if tournament.subclass not in COUNTS:
        raise OpenmsError('暂不支持该比赛类型的成绩分析。')
    if tournament.subclass == 'w' and tournament.tournament_format != 'c':
        raise OpenmsError('暂不支持该周赛赛制的成绩分析。')
    if len({video.id for video in videos}) != len(videos):
        raise OpenmsError('网站返回了重复录像 ID，无法可靠计算成绩。')
    counts = COUNTS[tournament.subclass]
    eligible: dict[str, list[tuple[ScoreVideo, float]]] = {level: [] for level in counts}
    results: dict[int, VideoAssessment] = {}
    reference_candidates: dict[str, list[ScoreVideo]] = {level: [] for level in counts}
    for video in videos:
        value = video.efficiency_score if tournament.subclass == 'e' else video.seconds
        reason = ''
        if video.state != 'c':
            reason = STATE_NAMES.get(video.state, '未知审核状态') + '，不计分'
        elif video.mode != '00':
            reason = '非标准模式，不计分'
        elif video.level not in counts:
            reason = '该难度不属于本场计分范围'
        elif video.timems <= 0:
            reason = '录像用时无效'
        elif tournament.subclass == 'g' and video.bv < GSC_BV_MIN[video.level]:
            reason = f'3BV 未达到 {GSC_BV_MIN[video.level]} 的计分下限'
        else:
            if tournament.subclass == 'g':
                reference_candidates[video.level].append(video)
            if value is None:
                reason = '缺少有效点击数或 3BV，无法计算效率分'
            elif value >= TIME_LIMITS[video.level]:
                reason = f'{"效率分" if tournament.subclass == "e" else "用时"}未低于 {TIME_LIMITS[video.level]} 的计分上限'
            else:
                eligible[video.level].append((video, value))
                reason = '有效录像，未进入本难度取用名额'
        results[video.id] = VideoAssessment(video, value, False, reason)
    groups = []
    for level, count in counts.items():
        selected = sorted(eligible[level], key=lambda pair: (pair[1], pair[0].id))[:count]
        slots = []
        for video, value in selected:
            slots.append(ScoreSlot(video, value))
            results[video.id] = VideoAssessment(video, value, True, '已计入当前成绩')
        slots.extend(ScoreSlot(None, TIME_LIMITS[level]) for _ in range(count - len(slots)))
        groups.append(ScoreGroup(level, tuple(slots)))
    references = []
    if tournament.subclass == 'g':
        for level, count in counts.items():
            for metric in ('3BV/s', 'STNB'):
                pairs = [(video.id, video.bv / video.seconds if metric == '3BV/s'
                          else STNB_COEFFICIENTS[level] * video.bv / video.seconds ** 1.7)
                         for video in reference_candidates[level]]
                selected = sorted(pairs, key=lambda pair: (-pair[1], pair[0]))[:count]
                references.append(ReferenceGroup(level, metric, tuple(selected), count - len(selected)))
    return PersonalAnalysis(tournament, user_id, datetime.now(timezone.utc), tuple(groups),
                            tuple(results[video.id] for video in videos), tuple(references))
