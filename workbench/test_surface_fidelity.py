"""Engineering fidelity tests; these do not validate anatomical segmentation."""
import unittest
import numpy as np
import SimpleITK as sitk
import vtk
from scipy.spatial import cKDTree
from vtk.util.numpy_support import numpy_to_vtk, vtk_to_numpy
from surface_geometry import build_surface


class SurfaceFidelityTests(unittest.TestCase):
    def check_mask(self, mask, group, direction):
        image = sitk.GetImageFromArray(mask.astype(np.uint8))
        image.SetSpacing((.63, .81, 1.27))
        image.SetOrigin((17., -32., -709.))
        image.SetDirection(direction)
        original = mask.tobytes()
        result, metadata = build_surface(mask, image, group, source_resolution_mm=1.5)
        self.assertEqual(original, mask.tobytes())
        self.assertFalse(metadata['geometry_smoothing'])
        self.assertFalse(metadata['volume_compensation'])
        self.assertFalse(metadata['full_mesh_simplified'])
        # Independent extractor over the entire volume, not the production crop.
        field = np.pad(mask.astype(np.uint8), 1)
        grid = vtk.vtkImageData(); grid.SetDimensions(*field.shape[::-1])
        grid.GetPointData().SetScalars(numpy_to_vtk(field.ravel(), deep=True))
        iso = vtk.vtkMarchingCubes(); iso.SetInputData(grid); iso.SetValue(0, .5); iso.Update()
        indices = vtk_to_numpy(iso.GetOutput().GetPoints().GetData()) - 1
        matrix = np.array(direction).reshape(3, 3) @ np.diag(image.GetSpacing())
        expected = (indices @ matrix.T + image.GetOrigin()) * [-1, -1, 1]
        actual = vtk_to_numpy(result.GetPoints().GetData())
        self.assertLess(cKDTree(expected).query(actual)[0].max(), .001)
        self.assertLess(cKDTree(actual).query(expected)[0].max(), .001)
        self.assertEqual(result.GetNumberOfPolys(), iso.GetOutput().GetNumberOfPolys())
        self.assertTrue(np.isfinite(actual).all())

    def test_rotated_anisotropic_thin_branch_and_small_island(self):
        mask = np.zeros((15, 18, 21), bool)
        mask[3:12, 8, 4:16] = True
        mask[8, 8:14, 11] = True
        mask[4, 3, 3] = True
        for group in ['ribs', 'airways', 'arteries', 'nodules']:
            with self.subTest(group=group):
                self.check_mask(mask, group, (0., -1., 0., 1., 0., 0., 0., 0., 1.))

    def test_organ_with_cavity_and_boundary_touching(self):
        mask = np.zeros((16, 19, 22), bool)
        mask[:12, 2:17, 3:18] = True
        mask[4:9, 5:12, 7:13] = False
        for group in ['lungs', 'lobes', 'heart', 'aorta', 'veins', 'margin', 'reviewed_regions']:
            with self.subTest(group=group):
                self.check_mask(mask, group, (1., 0., 0., 0., 1., 0., 0., 0., 1.))


if __name__ == '__main__':
    unittest.main()
