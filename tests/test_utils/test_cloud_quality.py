import pytest

from utils.cloud_quality import normalize_quality, graphics_mode, quality_label


@pytest.mark.parametrize('key,mode,label', [
    ('high',0,'高画质'),('traffic_saving',1,'省流画质'),('extreme_saving',2,'极致省流')])
def test_actual_three_graphics_modes(key,mode,label):
    assert normalize_quality(key)==key
    assert graphics_mode(key)==mode
    assert quality_label(key)==label


@pytest.mark.parametrize('legacy,key,mode', [('0','high',0),('1','high',0),('2','traffic_saving',1),('3','extreme_saving',2)])
def test_old_saved_choices_are_mapped_without_reinterpreting_numeric_ids(legacy,key,mode):
    assert normalize_quality(legacy)==key
    assert graphics_mode(legacy)==mode


@pytest.mark.parametrize('bad', ['unknown',None,True,-1,4])
def test_invalid_choice_does_not_silently_change_quality(bad):
    with pytest.raises(ValueError):
        normalize_quality(bad)
