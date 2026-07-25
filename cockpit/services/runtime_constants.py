from dataclasses import dataclass

_DEFAULT_SMT_PLACEMENT_TIME_MIN: float = 0.012
_DEFAULT_THT_PLACEMENT_TIME_MIN: float = 0.15
_DEFAULT_AOI_INSPECTION_TIME_MIN: float = 0.0004
_DEFAULT_CLASS_3_MULTIPLIER_AOI: float = 1.2
_DEFAULT_CLASS_3_MULTIPLIER_THT: float = 1.2
_DEFAULT_CLEAN_PROCESS_MULTIPLIER_THT: float = 1.1
_DEFAULT_CLEAN_PROCESS_MULTIPLIER_OPS: float = 1.0
_DEFAULT_SHIPPING_FLAT_HOURS: float = 2.0
_DEFAULT_SHIPPING_BOARDS_PER_HOUR: float = 100.0

@dataclass(frozen=True)
class RuntimeConstants:
    smt_placement_time_min: float = _DEFAULT_SMT_PLACEMENT_TIME_MIN
    tht_placement_time_min: float = _DEFAULT_THT_PLACEMENT_TIME_MIN
    aoi_inspection_time_min: float = _DEFAULT_AOI_INSPECTION_TIME_MIN
    class_3_multiplier_aoi: float = _DEFAULT_CLASS_3_MULTIPLIER_AOI
    class_3_multiplier_tht: float = _DEFAULT_CLASS_3_MULTIPLIER_THT
    clean_process_multiplier_tht: float = _DEFAULT_CLEAN_PROCESS_MULTIPLIER_THT
    clean_process_multiplier_ops: float = _DEFAULT_CLEAN_PROCESS_MULTIPLIER_OPS
    shipping_flat_hours: float = _DEFAULT_SHIPPING_FLAT_HOURS
    shipping_boards_per_hour: float = _DEFAULT_SHIPPING_BOARDS_PER_HOUR

    @classmethod
    def defaults(cls) -> 'RuntimeConstants':
        return cls()
