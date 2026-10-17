"""Cloud graphics choices shared by the terminal and browser controller."""
QUALITY_OPTIONS = (
    ('high', 0, '高画质'),
    ('traffic_saving', 1, '省流画质'),
    ('extreme_saving', 2, '极致省流'),
)
_LEGACY_VALUES = {'0': 'high', '1': 'high', '2': 'traffic_saving', '3': 'extreme_saving'}


def normalize_quality(value):
    key = str(value).strip().lower()
    key = _LEGACY_VALUES.get(key, key)
    if key not in {option[0] for option in QUALITY_OPTIONS}:
        raise ValueError('云游戏画质无效，请选择高画质、省流画质或极致省流')
    return key


def graphics_mode(value):
    key = normalize_quality(value)
    return next(mode for name, mode, label in QUALITY_OPTIONS if name == key)


def quality_label(value):
    key = normalize_quality(value)
    return next(label for name, mode, label in QUALITY_OPTIONS if name == key)
