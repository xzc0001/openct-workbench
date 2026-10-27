"""Conservative, auditable airway component selection. No erosion or branch pruning."""
import numpy as np
from scipy import ndimage as ndi

VERSION = 1


def connected_airways(trachea, bronchi):
    trachea = np.asarray(trachea, dtype=bool)
    bronchi = np.asarray(bronchi, dtype=bool)
    if trachea.shape != bronchi.shape or trachea.ndim != 3:
        raise ValueError("气管与支气管标签几何不一致")
    if not trachea.any():
        raise ValueError("未获得主气管标签，不能自动判断支气管树的连通范围")
    raw = trachea | bronchi
    labels, count = ndi.label(raw, structure=np.ones((3, 3, 3), dtype=bool))
    counts = np.bincount(labels.ravel())
    anchored = np.unique(labels[trachea])
    anchored = anchored[anchored != 0]
    lookup = np.zeros(count+1, dtype=bool)
    lookup[anchored] = True
    clean = lookup[labels]
    removed = counts[1:][~lookup[1:]]
    report = {"version": VERSION, "method": "26_connected_components_intersecting_trachea", "source_voxels": int(raw.sum()), "retained_voxels": int(clean.sum()), "excluded_voxels": int(removed.sum()), "source_components": int(count), "retained_components": int(len(anchored)), "excluded_component_voxels": sorted(map(int, removed), reverse=True), "erosion": False, "branch_pruning": False, "source_predictions_modified": False, "anatomical_validation": False, "note": "排除与气管不连通的成分用于默认显示；游离成分可能包含真实分支，保留原始联合标签供人工核对。"}
    return clean, raw, report
