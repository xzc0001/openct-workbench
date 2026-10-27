"""Derived display surfaces; source CT and binary segmentation are never edited."""
import base64

SURFACE_VERSION = 5
SMOOTH_GROUPS = {"lungs", "lobes", "ribs", "heart", "aorta", "arteries", "veins", "airways", "nodules", "reviewed_regions", "margin", "rll"}


def audit_label_boundary(vertices_ras, mask, volume):
    """Check every reference vertex against a raw binary-label edge, not anatomy."""
    import numpy as np
    matrix = np.array(volume.GetDirection()).reshape(3, 3) @ np.diag(volume.GetSpacing())
    inverse = np.linalg.inv(matrix)
    size = np.array(volume.GetSize())
    maximum = 0.
    for start in range(0, len(vertices_ras), 65536):
        lps = vertices_ras[start:start+65536] * [-1, -1, 1]
        q = (lps - volume.GetOrigin()) @ inverse.T
        twice = np.rint(q * 2).astype(np.int64)
        error = np.max(np.abs(q - twice / 2))
        maximum = max(maximum, float(error))
        if error > .002 or not np.all(np.sum(twice % 2, axis=1) == 1):
            raise ValueError("Reference surface moved away from raw label edges")
        lo = np.floor(twice / 2).astype(np.int64)
        hi = np.ceil(twice / 2).astype(np.int64)
        def values(indices):
            valid = np.all((indices >= 0) & (indices < size), axis=1)
            result = np.zeros(len(indices), bool)
            ix = indices[valid]
            result[valid] = mask[ix[:, 2], ix[:, 1], ix[:, 0]]
            return result
        if not np.all(values(lo) != values(hi)):
            raise ValueError("Reference vertex is not between foreground/background labels")
    return {"checked_vertices": len(vertices_ras), "max_grid_numeric_error_voxels": maximum,
            "numeric_tolerance_voxels": .002, "scope": "label_geometry_only", "anatomical_validation": False}


def build_surface(mask, volume, group, source_resolution_mm=3.):
    import numpy as np
    import vtk
    from vtk.util.numpy_support import numpy_to_vtk, vtk_to_numpy

    axes = [np.flatnonzero(mask.any(axis=pair)) for pair in ((1, 2), (0, 2), (0, 1))]
    if any(not len(axis) for axis in axes):
        raise ValueError("Empty surface mask")
    start = np.array([axis[0] for axis in axes], dtype=int)
    stop = np.array([axis[-1] + 1 for axis in axes], dtype=int)
    spacing = np.array(volume.GetSpacing())
    # Accuracy reference: raw binary-label .5 isosurface, with no geometry smoothing.
    # A voxelized boundary must not be made prettier by moving anatomy.
    pad = np.ones(3, dtype=int)
    crop = mask[tuple(slice(int(a), int(b)) for a, b in zip(start, stop))]
    field = np.pad(crop.astype(np.uint8), 1)
    threshold = .5
    data = vtk.vtkImageData()
    data.SetDimensions(*field.shape[::-1]); data.SetSpacing(*spacing)
    data.GetPointData().SetScalars(numpy_to_vtk(field.ravel(), deep=True))
    # Smooth scalar interpolation removes voxel terraces before mesh simplification.
    iso = vtk.vtkFlyingEdges3D(); iso.SetInputData(data); iso.SetValue(0, threshold)
    iso.Update()
    poly = iso.GetOutput()
    # Include image direction as well as spacing/origin, then convert LPS to RAS.
    direction = np.array(volume.GetDirection()).reshape(3, 3)
    origin = volume.TransformIndexToPhysicalPoint(tuple(int(v) for v in (start - pad)[::-1]))
    matrix = vtk.vtkMatrix4x4(); matrix.Identity()
    for i in range(3):
        sign = -1 if i < 2 else 1
        for j in range(3): matrix.SetElement(i, j, sign * direction[i, j])
        matrix.SetElement(i, 3, sign * origin[i])
    clean = vtk.vtkCleanPolyData(); clean.SetInputData(poly); clean.SetTolerance(0.)
    triangles = vtk.vtkTriangleFilter(); triangles.SetInputConnection(clean.GetOutputPort())
    triangles.PassLinesOff(); triangles.PassVertsOff(); triangles.Update(); poly = triangles.GetOutput()
    # FlyingEdges gradients describe the voxel isosurface before smoothing.
    # Remove inherited normals so vtkPolyDataNormals must recompute on final geometry.
    poly.GetPointData().SetNormals(None); poly.GetCellData().SetNormals(None)
    transform = vtk.vtkTransform(); transform.SetMatrix(matrix)
    convert = vtk.vtkTransformPolyDataFilter(); convert.SetInputData(poly); convert.SetTransform(transform)
    normals = vtk.vtkPolyDataNormals(); normals.SetInputConnection(convert.GetOutputPort())
    normals.ConsistencyOn(); normals.AutoOrientNormalsOn(); normals.SplittingOff(); normals.Update()
    result = vtk.vtkPolyData(); result.DeepCopy(normals.GetOutput())
    if not result.GetNumberOfCells(): raise ValueError("Smoothed surface is empty")
    audit = audit_label_boundary(vtk_to_numpy(result.GetPoints().GetData()), mask, volume)
    return result, {"version": SURFACE_VERSION, "method": "binary_label_isosurface_0.5",
                    "gaussian_sigma_mm": 0., "isovalue": threshold,
                    "source_resolution_mm": source_resolution_mm,
                    "volume_compensation": False, "geometry_smoothing": False,
                    "full_mesh_simplified": False, "source_mask_preserved": True,
                    "label_boundary_vertex_audit": audit,
                    "accuracy_reference": "automatic_segmentation_not_anatomical_ground_truth"}



def surface_arrays(poly):
    import numpy as np
    from vtk.util.numpy_support import vtk_to_numpy
    vertices = vtk_to_numpy(poly.GetPoints().GetData()).astype("<f4")
    indices = vtk_to_numpy(poly.GetPolys().GetConnectivityArray()).astype("<u4")
    normals = vtk_to_numpy(poly.GetPointData().GetNormals()).astype("<f4")
    if not np.isfinite(vertices).all() or not np.isfinite(normals).all() or indices.size % 3 or indices.max() >= len(vertices):
        raise ValueError("Invalid surface geometry")
    encode = lambda a: base64.b64encode(a.tobytes()).decode("ascii")
    return {"positions": encode(vertices), "indices": encode(indices), "normals": encode(normals),
            "vertices": len(vertices), "triangles": indices.size // 3, "bounds_ras": list(poly.GetBounds())}
