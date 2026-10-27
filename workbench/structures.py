"""Reconstruction capabilities and dependencies, shared by HTTP and worker."""
LABELS = {
    "heart": "心脏", "aorta": "主动脉", "arteries": "肺动脉", "veins": "肺静脉", "airways": "气管与支气管",
    "nodules": "肺结节候选", "suspects": "低密度候选筛选", "lungs": "肺轮廓", "lobes": "肺叶", "ribs": "肋骨与脊柱", "scapulae": "肩胛骨",
    "margin": "几何外扩参考", "segments": "肺段", "fractures": "肋骨骨折自动定位",
}
SUPPORTED = set(LABELS) - {"segments", "fractures"}
PRESETS = {"nodule": ["airways", "arteries", "veins", "nodules"], "ribs": ["ribs"]}
PRESET_LABELS = {"nodule": "肺结节套餐", "ribs": "肋骨套餐"}
LEGACY_PRESETS = {"quick": ["heart", "lungs", "airways"], "full": ["heart", "lungs", "airways", "arteries", "veins", "nodules", "suspects"]}
LOBE_NAMES = ["lung_upper_lobe_left", "lung_lower_lobe_left", "lung_upper_lobe_right", "lung_middle_lobe_right", "lung_lower_lobe_right"]
RIB_NAMES = [f"rib_{side}_{i}" for side in ("left", "right") for i in range(1, 13)]
SPINE_NAMES = ["sacrum", "vertebrae_S1"] + [f"vertebrae_{region}{i}" for region, count in [("L", 5), ("T", 12), ("C", 7)] for i in range(1, count+1)]
SCAPULA_NAMES = ["scapula_left", "scapula_right"]


def resolve(request):
    if "preset" in request and (not isinstance(request["preset"], str) or request["preset"] not in PRESETS):
        raise ValueError("请选择肺结节或肋骨套餐；也可直接勾选重建结构")
    # Saved legacy jobs retain their original defaults. New UI always submits
    # the explicit checklist, independently of the inference precision mode.
    defaults = PRESETS[request["preset"]] if "preset" in request else LEGACY_PRESETS.get(request.get("mode", "quick"), [])
    selected = request.get("structures", defaults)
    if not isinstance(selected, list) or not selected or any(not isinstance(x, str) or x not in SUPPORTED for x in selected):
        raise ValueError("请选择可用的重建结构；肺段及骨折自动定位尚无可用模型")
    selected = list(dict.fromkeys(selected))
    margin = None
    if "margin" in selected:
        try:
            margin = float(request.get("margin_mm"))
        except (ValueError, TypeError):
            raise ValueError("请自行填写几何外扩距离（毫米），该值不是系统推荐的安全切缘")
        if not 0 < margin <= 50:
            raise ValueError("几何外扩距离必须大于 0 且不超过 50 mm（计算范围限制）")
        if "nodules" not in selected:
            selected.append("nodules")
    return selected, margin


def capabilities():
    reasons = {"aorta": "快速模式 3 mm，精细模式 1.5 mm；仅重建扫描覆盖范围内的主动脉", "ribs": "从原始 CT 提取肋骨与脊柱整体骨表面，AI 只用于定位；肩胛骨单独可选；不填充原片间隙", "scapulae": "左右肩胛骨独立图层；精细模式使用 1.5 mm 模型；不随肋骨套餐自动勾选", "airways": "气管与支气管；保留与主气管相连的分支，原始预测另行保存供核对"}
    return [{"id": key, "label": label, "available": key in SUPPORTED, "reason": "未配置专用模型，不生成替代性分区或骨折结论" if key not in SUPPORTED else reasons.get(key, "")} for key, label in LABELS.items()]


def reconstruction_presets():
    return [{"id": key, "label": PRESET_LABELS[key], "structures": list(value)} for key, value in PRESETS.items()]


def inference_plan(selected, mode):
    """Selected anatomy and hidden dependencies; precision never selects anatomy."""
    roi = []
    if any(key in selected for key in ["nodules", "margin", "suspects"]) or (mode == "quick" and any(key in selected for key in ["lungs", "lobes"])):
        roi += LOBE_NAMES
    if mode == "quick":
        for key, names in [("heart", ["heart"]), ("aorta", ["aorta"]), ("airways", ["trachea"]), ("ribs", RIB_NAMES+SPINE_NAMES), ("scapulae", SCAPULA_NAMES)]:
            if key in selected: roi += names
        if "ribs" in selected:roi += SCAPULA_NAMES
    tasks = []
    if roi: tasks.append(("total", "total", {"fast": True, "roi_subset": list(dict.fromkeys(roi))}, "所选结构与必要依赖分割", 15))
    if mode == "full":
        for keys, key, names, title, percent in [
            (["heart"], "heart_highres", ["heart"], "1.5 mm 心脏分割", 35),
            (["aorta"], "aorta_highres", ["aorta"], "1.5 mm 主动脉分割", 40),
            (["lungs", "lobes"], "lung_highres", LOBE_NAMES, "1.5 mm 肺轮廓与肺叶分割", 41),
            (["airways"], "trachea_highres", ["trachea"], "1.5 mm 气管分割", 42),
            (["ribs"], "ribs_highres", RIB_NAMES, "1.5 mm 肋骨分割", 43),
            (["ribs"], "spine_highres", SPINE_NAMES, "1.5 mm 脊柱分割", 44),
            (["scapulae", "ribs"], "scapulae_highres", SCAPULA_NAMES, "1.5 mm 肩胛骨定位（用于骨性胸廓分离）", 45),
        ]:
            if any(k in selected for k in keys):tasks.append(("total", key, {"roi_subset": list(names)}, title, percent))
    if any(key in selected for key in ["arteries", "veins", "airways", "suspects"]):
        tasks.append(("lung_vessels", "lung_vessels", {}, "肺血管与支气管分割", 50))
    if "nodules" in selected:tasks.append(("lung_nodules", "lung_nodules", {}, "肺结节候选分割", 65))
    return tasks


def validate_candidate_parameters(value):
    import math
    if not isinstance(value,dict) or set(value)-{"density_min_hu","density_max_hu","response_floor_hu","max_candidates"}:
        raise ValueError("疑似区域筛选参数无效")
    defaults={"density_min_hu":-850.,"density_max_hu":-250.,"response_floor_hu":10.,"max_candidates":40}
    for key,raw in value.items():
        if isinstance(raw,bool) or not isinstance(raw,(int,float)) or not math.isfinite(raw):
            raise ValueError("筛选参数必须为有限数值")
        defaults[key]=raw
    if not -1024<=defaults["density_min_hu"]<defaults["density_max_hu"]<=1000 or not 1<=defaults["response_floor_hu"]<=100 or not 1<=defaults["max_candidates"]<=40 or int(defaults["max_candidates"])!=defaults["max_candidates"]:
        raise ValueError("请核对密度上下限、密度响应（1–100）及候选数量（1–40）；参数不是诊断标准")
    defaults["max_candidates"]=int(defaults["max_candidates"])
    return defaults
