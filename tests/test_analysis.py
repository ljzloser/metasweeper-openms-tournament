from dataclasses import replace

import pytest

from openms_tournament.analysis import ScoreVideo, analyze_personal
from openms_tournament.client import OpenmsError, Tournament


def tournament(kind='w'):
    return Tournament(30, '个人成绩测试', 'ongoing', None, None, kind)


def video(identifier=1, **changes):
    values = dict(id=identifier, level='i', mode='00', state='c', timems=20000, bv=30, clicks=30)
    values.update(changes)
    return ScoreVideo(**values)


@pytest.mark.parametrize('kind,expected,count', [('w', 780, 7), ('e', 780, 7), ('g', 2120, 37)])
def test_empty_personal_scores_use_rule_defaults_and_do_not_invent_videos(kind, expected, count):
    result = analyze_personal(tournament(kind), 7, ())
    assert result.total == expected
    assert sum(len(group.slots) for group in result.groups) == count
    assert all(slot.video is None for group in result.groups for slot in group.slots)


def test_weekly_selects_fastest_two_expert_and_five_intermediate_and_caps_defaults():
    videos = tuple(video(index, timems=index * 1000) for index in range(1, 7)) + (
        video(7, level='e', timems=50000), video(8, level='e', timems=60000), video(9, level='e', timems=70000),
    )
    result = analyze_personal(tournament(), 7, videos)
    assert result.total == 125
    assert [slot.video.id for slot in result.groups[0].slots] == [1, 2, 3, 4, 5]
    assert [slot.video.id for slot in result.groups[1].slots] == [7, 8]
    assert not result.videos[5].counted and '名额' in result.videos[5].reason


@pytest.mark.parametrize('changes,reason', [
    ({'state': 'a'}, '待审核'), ({'state': 'b'}, '冻结'), ({'state': 'd'}, '标识不匹配'),
    ({'state': 'e'}, '外站审核'), ({'mode': '05'}, '非标准'), ({'level': 'b'}, '难度'),
    ({'timems': 60000}, '上限'), ({'timems': 0}, '无效'),
])
def test_weekly_explains_every_excluded_video_instead_of_counting_upload_success(changes, reason):
    result = analyze_personal(tournament(), 7, (video(**changes),))
    assert result.total == 780
    assert not result.videos[0].counted and reason in result.videos[0].reason


def test_efficiency_uses_clicks_and_bv_instead_of_time_or_effective_clicks():
    fast = video(1, timems=20000, clicks=60, bv=30)  # IOE=.5, score=80: excluded at intermediate.
    slow_efficient = video(2, timems=40000, clicks=15, bv=30)  # IOE=2, score=10.
    result = analyze_personal(tournament('e'), 7, (fast, slow_efficient))
    assert result.total == 730
    assert result.groups[0].slots[0].video.id == 2
    assert result.groups[0].slots[0].value == 10
    assert '上限' in result.videos[0].reason


@pytest.mark.parametrize('changes', [{'clicks': None}, {'clicks': 0}, {'bv': 0}])
def test_missing_efficiency_stats_are_excluded_without_fabricating_zero_score(changes):
    result = analyze_personal(tournament('e'), 7, (video(**changes),))
    assert result.total == 780
    assert '无法计算' in result.videos[0].reason


def test_efficiency_selects_using_full_precision_before_rounding_for_display():
    # The displayed score is equal to 3 decimals, but the lower raw score must win the fifth slot.
    videos = tuple(video(index, timems=index * 1000) for index in range(1, 5)) + (
        video(5, timems=10000, clicks=1000002, bv=1000000),
        video(6, timems=10000, clicks=1000001, bv=1000000),
    )
    result = analyze_personal(tournament('e'), 7, videos)
    selected = [slot.video.id for slot in result.groups[0].slots]
    assert selected == [1, 2, 3, 4, 6]


def test_gsc_requires_bv_thresholds_and_has_independent_reference_video_selection():
    videos = (
        video(1, level='b', timems=1000, bv=9),
        video(2, level='b', timems=2000, bv=10),
        video(3, level='i', timems=50000, bv=30),
        video(4, level='e', timems=120000, bv=100),
        video(5, level='e', timems=250000, bv=500),  # No time credit, but can appear in reference stats.
    )
    result = analyze_personal(tournament('g'), 7, videos)
    assert result.total == 2120 - 8 - 10 - 120
    assert '3BV' in result.videos[0].reason
    assert '上限' in result.videos[4].reason
    expert_bvs = next(group for group in result.references if group.level == 'e' and group.metric == '3BV/s')
    assert expert_bvs.values[0] == (5, 2)
    assert expert_bvs.missing == 3
    expert_stnb = next(group for group in result.references if group.level == 'e' and group.metric == 'STNB')
    assert expert_stnb.total == pytest.approx(435 * 100 / 120 ** 1.7 + 435 * 500 / 250 ** 1.7)


def test_analysis_does_not_mutate_videos_and_rejects_duplicate_ids():
    videos = (video(2, timems=30000), video(1, timems=20000))
    original = tuple(replace(item) for item in videos)
    analyze_personal(tournament(), 7, videos)
    assert videos == original
    with pytest.raises(OpenmsError, match='重复'):
        analyze_personal(tournament(), 7, (video(), video()))


def test_unknown_tournament_type_is_not_guessed():
    with pytest.raises(OpenmsError, match='不支持'):
        analyze_personal(tournament('custom'), 7, ())
    with pytest.raises(OpenmsError, match='不支持'):
        analyze_personal(replace(tournament(), tournament_format='unknown'), 7, ())
