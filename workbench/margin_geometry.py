"""Physical-distance expansion; no clinical safety or resectability decision."""
def geometric_shell(component, spacing_zyx, distance_mm):
    import numpy as np
    from scipy.ndimage import distance_transform_edt
    if not np.isfinite(distance_mm) or distance_mm <= 0 or not component.any():
        raise ValueError('A nonempty target and positive finite distance are required')
    distance = distance_transform_edt(~component, sampling=spacing_zyx)
    return (distance <= distance_mm) & ~component
