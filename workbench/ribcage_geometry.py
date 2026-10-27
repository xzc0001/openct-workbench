"""One rib/spine model from native CT isosurfaces, with AI used only as seeds."""
import numpy as np
from scipy import ndimage as ndi

BONE_HU = 150.0


def select_ct_bone(hu, positive, negative, threshold=BONE_HU):
    from skimage.segmentation import watershed
    if hu.shape != positive.shape or hu.shape != negative.shape:
        raise ValueError('骨性胸廓定位与原始 CT 几何不一致')
    candidate = hu >= threshold
    markers = np.zeros(hu.shape, np.uint8)
    markers[negative & candidate] = 2
    markers[positive & candidate] = 1
    if not np.any(markers == 1):raise ValueError('原始 CT 中没有可用的肋骨/脊柱骨密度种子')
    # Rib and spine share ONE marker. There is no separating plane between them.
    # Scapular markers prevent a CT contact/partial-volume bridge from selecting
    # the shoulder girdle as part of the rib cage. No new positive voxels are made.
    # Face adjacency keeps corner-touching high-density noise from leaking into
    # the skeleton. Every AI-localized CT-positive island remains independently
    # seeded, including thin/disconnected bone within the original locator.
    labels = watershed(-hu.astype(np.float32), markers, mask=candidate, connectivity=ndi.generate_binary_structure(3,1))
    selected = labels == 1
    return selected, {'threshold_hu':float(threshold),'method':'native_ct_density_marker_watershed','rib_and_spine_share_marker':True,'density_connectivity':6,'candidate_voxels':int(candidate.sum()),'selected_voxels':int(selected.sum()),'selected_outside_ai_localizer':int((selected & ~positive).sum()),'positive_seed_voxels':int((markers==1).sum()),'negative_seed_voxels':int((markers==2).sum()),'overlapping_localizer_voxels':int((positive & negative & candidate).sum()),'synthetic_bone_voxels':0,'anatomical_validation':False}


def audit_ct_surface(vertices_ras, native, threshold=BONE_HU):
    import SimpleITK as sitk
    hu = sitk.GetArrayFromImage(native).astype(np.float32,copy=False)
    matrix = np.array(native.GetDirection()).reshape(3,3) @ np.diag(native.GetSpacing())
    inverse = np.linalg.inv(matrix);size = np.array(native.GetSize());maximum=0.
    for start in range(0,len(vertices_ras),65536):
        q = (vertices_ras[start:start+65536]*[-1,-1,1]-native.GetOrigin()) @ inverse.T
        if np.any(q < -.002) or np.any(q > size-1+.002):raise ValueError('CT 骨表面超出源体积')
        q = np.clip(q,0,size-1)
        values=ndi.map_coordinates(hu,q[:,::-1].T,order=1,mode='nearest',prefilter=False)
        maximum=max(maximum,float(np.max(np.abs(values-threshold))))
    if maximum > 1.:raise ValueError(f'骨表面离开原始 CT 等值面：最大数值偏差 {maximum:g} HU')
    return {'checked_vertices':len(vertices_ras),'max_interpolated_hu_error':maximum,'numeric_tolerance_hu':1.,'scope':'native_ct_isovalue_geometry_only','anatomical_validation':False}


def build_ribcage(native, canonical, positive, negative, threshold=BONE_HU):
    import SimpleITK as sitk
    import vtk
    from vtk.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy
    def to_native(mask):
        image=sitk.GetImageFromArray(mask.astype(np.uint8));image.CopyInformation(canonical)
        return sitk.GetArrayFromImage(sitk.Resample(image,native,sitk.Transform(),sitk.sitkNearestNeighbor,0,sitk.sitkUInt8))>0
    hu=sitk.GetArrayFromImage(native)
    selected,receipt=select_ct_bone(hu,to_native(positive),to_native(negative),threshold)
    axes=[np.flatnonzero(selected.any(axis=pair)) for pair in [(1,2),(0,2),(0,1)]]
    lower=np.maximum(0,np.array([a[0] for a in axes])-1);upper=np.minimum(selected.shape,np.array([a[-1]+2 for a in axes]))
    region=tuple(slice(int(a),int(b)) for a,b in zip(lower,upper))
    field=np.ascontiguousarray(hu[region]);keep=selected[region]
    data=vtk.vtkImageData();data.SetDimensions(*field.shape[::-1]);data.SetSpacing(*native.GetSpacing());data.GetPointData().SetScalars(numpy_to_vtk(field.ravel(),deep=False))
    iso=vtk.vtkFlyingEdges3D();iso.SetInputData(data);iso.SetValue(0,threshold);iso.ComputeNormalsOff();iso.Update();source=iso.GetOutput()
    points=vtk_to_numpy(source.GetPoints().GetData());q=points/np.array(native.GetSpacing())
    rounded=np.rint(q);q=np.where(np.abs(q-rounded)<.001,rounded,q)
    lo=np.clip(np.floor(q).astype(int),0,np.array(field.shape[::-1])-1);hi=np.clip(np.ceil(q).astype(int),0,np.array(field.shape[::-1])-1)
    vertex_selected=keep[lo[:,2],lo[:,1],lo[:,0]] | keep[hi[:,2],hi[:,1],hi[:,0]]
    ix=vtk_to_numpy(source.GetPolys().GetConnectivityArray()).reshape(-1,3)
    ix=np.ascontiguousarray(ix[np.all(vertex_selected[ix],axis=1)],dtype=np.int64)
    if not len(ix):raise ValueError('原始 CT 没有提取到骨性胸廓表面')
    # Select existing CT triangles only. No bridges, caps, deformation or new
    # joint surfaces are generated when removing unselected anatomical parts.
    cells=vtk.vtkCellArray();cells.SetData(numpy_to_vtkIdTypeArray(np.arange(0,(len(ix)+1)*3,3,dtype=np.int64),deep=True),numpy_to_vtkIdTypeArray(ix.ravel(),deep=True))
    poly=vtk.vtkPolyData();poly.SetPoints(source.GetPoints());poly.SetPolys(cells)
    clean=vtk.vtkCleanPolyData();clean.SetInputData(poly);clean.SetTolerance(0.)
    triangles=vtk.vtkTriangleFilter();triangles.SetInputConnection(clean.GetOutputPort());triangles.PassLinesOff();triangles.PassVertsOff()
    origin=native.TransformIndexToPhysicalPoint(tuple(map(int,lower[::-1])));direction=np.array(native.GetDirection()).reshape(3,3)
    matrix=vtk.vtkMatrix4x4();matrix.Identity()
    for i in range(3):
        sign=-1 if i<2 else 1
        for j in range(3):matrix.SetElement(i,j,sign*direction[i,j])
        matrix.SetElement(i,3,sign*origin[i])
    transform=vtk.vtkTransform();transform.SetMatrix(matrix)
    convert=vtk.vtkTransformPolyDataFilter();convert.SetInputConnection(triangles.GetOutputPort());convert.SetTransform(transform)
    normals=vtk.vtkPolyDataNormals();normals.SetInputConnection(convert.GetOutputPort());normals.SplittingOff();normals.ConsistencyOn();normals.AutoOrientNormalsOn();normals.Update()
    result=vtk.vtkPolyData();result.DeepCopy(normals.GetOutput())
    audit=audit_ct_surface(vtk_to_numpy(result.GetPoints().GetData()),native,threshold)
    image=sitk.GetImageFromArray(selected.astype(np.uint8));image.CopyInformation(native)
    canonical_mask=sitk.GetArrayFromImage(sitk.Resample(image,canonical,sitk.Transform(),sitk.sitkNearestNeighbor,0,sitk.sitkUInt8))>0
    receipt.update(version=6,method='native_ct_hu_isosurface',source_resolution_mm=list(native.GetSpacing()),geometry_smoothing=False,full_mesh_simplified=False,source_predictions_modified=False,source_ct_modified=False,artificial_joint_bridges=False,native_ct_surface_audit=audit,open_scan_or_part_boundaries_possible=True)
    return canonical_mask,result,receipt
